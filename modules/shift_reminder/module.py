"""基建换班提醒模块的装配层。

这是本模块**唯一接触 AstrBot 的文件**：三班模型、判定策略与消息渲染都是纯逻辑
（`schedule` / `strategy` / `notify`，不 import astrbot），存储细节在 `core.storage`，
调度辅助在 `scheduler`。这里只负责把它们接起来。
"""

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 两种运行场景的导入差异：
#   * AstrBot 加载时插件是一个包（`<plugin>.modules.shift_reminder.module`），
#     插件根目录**不在** sys.path 上 → 只能用父级相对导入；
#   * 从仓库根直接跑 pytest 时（pyproject 配了 `pythonpath = ["."]`），顶层包是
#     `modules`，此时 `...` 会越界 → 必须用 `core.*` 绝对导入。
# 先绝对、失败再回退相对，两种场景都能工作；注册表的自动发现也才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.module import Module
    from core.storage import JsonlSendLog, JsonStateStore, SendRecord
except ImportError:  # pragma: no cover
    from ...core.module import Module
    from ...core.storage import JsonlSendLog, JsonStateStore, SendRecord
from . import notify, scheduler
from .schedule import ConfigError, Shift, ShiftTable, parse_hhmm, validate
from .strategy import PeriodStrategy

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"
JOB_PREFIX = "ak_toolbox:shift_reminder:"
UMO_KEY = "bound_umo"
TIMEZONE = "Asia/Shanghai"
SHIFT_SLOTS = (1, 2, 3)
SEND_LOG_KEEP = 50
STATUS_RECENT = 5
DEFAULT_LEAD_MINUTES = 10


def parse_shift_table(config: Mapping[str, Any]) -> ShiftTable:
    """把扁平配置键组装成**已校验**的班次表。

    配置非法一律抛 `ConfigError`——加载失败要让人当场看见，不静默降级
    （项目宪法 §2 第 2 条）。
    """
    shifts: list[Shift] = []
    for slot in SHIFT_SLOTS:
        prefix = f"shift_{slot}"
        name = config.get(f"{prefix}_name")
        start = config.get(f"{prefix}_start")
        hours = config.get(f"{prefix}_hours")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{prefix}_name 必须是非空字符串，收到 {name!r}")
        if not isinstance(start, str):
            raise ConfigError(f"{prefix}_start 必须是 HH:MM 字符串，收到 {start!r}")
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise ConfigError(f"{prefix}_hours 必须是整数小时数，收到 {hours!r}")
        shifts.append(
            Shift(
                name=name.strip(),
                start_minute=parse_hhmm(start),
                duration_minutes=hours * 60,
            )
        )
    return validate(shifts)


def parse_lead_minutes(config: Mapping[str, Any]) -> int:
    """读取提前量；必须是 0 或正整数。"""
    raw = config.get("lead_minutes", DEFAULT_LEAD_MINUTES)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ConfigError(f"lead_minutes 必须是非负整数，收到 {raw!r}")
    return raw


class ShiftReminderModule(Module):
    """三班制换班提醒。"""

    name = "shift_reminder"
    config_key = "shift_reminder"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._strategy: PeriodStrategy | None = None
        self._store: JsonStateStore | None = None
        self._send_log: JsonlSendLog | None = None
        self._breaker = scheduler.FailureBreaker()
        self._job_ids: list[str] = []
        self._lead_minutes = DEFAULT_LEAD_MINUTES

    # --- 生命周期 -----------------------------------------------------------

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        table = parse_shift_table(config)
        lead_minutes = parse_lead_minutes(config)

        data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)

        cron_manager = getattr(ctx, "cron_manager", None)
        if cron_manager is None:
            raise RuntimeError(
                "取不到 AstrBot 的 cron_manager，无法注册换班提醒。"
                "本插件要求 AstrBot >= 4.17（见 metadata.yaml 的 astrbot_version）。"
            )

        self._ctx = ctx
        self._lead_minutes = lead_minutes
        self._strategy = PeriodStrategy(table, lead_minutes)
        self._store = JsonStateStore(data_dir / "state.json")
        self._send_log = JsonlSendLog(data_dir / "sends.jsonl", keep=SEND_LOG_KEEP)

        for shift in table.shifts:
            job = await cron_manager.add_basic_job(
                name=f"{JOB_PREFIX}{shift.name}",
                cron_expression=scheduler.reminder_cron_expression(shift, lead_minutes),
                handler=self._make_handler(shift.name),
                description=f"{shift.name} 换班提醒（提前 {lead_minutes} 分钟）",
                timezone=TIMEZONE,
                payload={"shift": shift.name},
            )
            self._job_ids.append(job.job_id)

        logger.info(
            "[ak_toolbox][shift_reminder] 已注册 %d 个换班提醒任务（提前 %d 分钟）",
            len(self._job_ids),
            lead_minutes,
        )

    async def terminate(self) -> None:
        """按前缀清掉自己注册的定时任务；可安全重复调用。"""
        job_ids, self._job_ids = self._job_ids, []
        cron_manager = getattr(self._ctx, "cron_manager", None) if self._ctx else None
        if cron_manager is None or not job_ids:
            return

        wanted = set(job_ids)
        removed = 0
        for job in await cron_manager.list_jobs():
            job_name = str(getattr(job, "name", ""))
            if job.job_id in wanted or job_name.startswith(JOB_PREFIX):
                await cron_manager.delete_job(job.job_id)
                removed += 1
        logger.info("[ak_toolbox][shift_reminder] 已清理 %d 个定时任务", removed)

    # --- 推送 ---------------------------------------------------------------

    def _make_handler(self, shift_name: str):
        async def handler(**_: Any) -> None:
            await self._push(shift_name)

        return handler

    async def _push(self, shift_name: str) -> None:
        """一次提醒推送。每条失败路径都留痕，不静默。"""
        if self._breaker.is_open:
            logger.warning(
                "[ak_toolbox][shift_reminder] 熔断已打开（连续失败 %d 次），跳过本次推送",
                self._breaker.consecutive_failures,
            )
            return

        strategy, store, send_log = self._strategy, self._store, self._send_log
        if strategy is None or store is None or send_log is None:
            logger.warning("[ak_toolbox][shift_reminder] 模块尚未初始化，忽略本次触发")
            return

        now = datetime.now()
        snapshot = strategy.snapshot(now)

        key = scheduler.idempotency_key(shift_name, snapshot.change_at)
        if store.get(key):
            logger.info("[ak_toolbox][shift_reminder] %s 的这次提醒已发过，跳过", shift_name)
            return

        umo = store.get(UMO_KEY)
        if not isinstance(umo, str) or not umo:
            logger.warning(
                "[ak_toolbox][shift_reminder] 尚未绑定提醒目标，跳过推送；请先发 /ak bind"
            )
            return

        text = notify.render_reminder(
            ending=snapshot.current,
            starting=snapshot.upcoming,
            change_at=snapshot.change_at,
            lead_minutes=self._lead_minutes,
        )
        sent = await self._ctx.send_message(umo, MessageChain().message(text))

        if sent:
            store.set(key, True)
            send_log.append(SendRecord(at=now, shift=shift_name, ok=True))
            self._breaker.record_success()
            logger.info("[ak_toolbox][shift_reminder] 已推送 %s 的换班提醒", shift_name)
            return

        detail = "send_message 返回 False（没找到匹配的平台或会话）"
        send_log.append(SendRecord(at=now, shift=shift_name, ok=False, detail=detail))
        self._breaker.record_failure()
        logger.warning("[ak_toolbox][shift_reminder] 推送失败：%s", detail)

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        handlers = {
            "bind": self._cmd_bind,
            "test": self._cmd_test,
            "status": self._cmd_status,
        }
        handler = handlers.get(command)
        if handler is None:
            return False
        if not self._is_allowed(event):
            await self._reply(
                event,
                "这个会话没有权限操作换班提醒：提醒只属于已绑定的那个会话。"
                "要改绑请回到原会话执行 /ak bind。",
            )
            return True
        await handler(event)
        return True

    def _is_allowed(self, event: Any) -> bool:
        """只允许已绑定的会话；尚未绑定任何人时放行（用于首次绑定）。

        门禁不能等 S5 再补——`status` 会泄露排班与绑定目标，`bind` 会改掉推送目标。
        """
        store = self._store
        if store is None:
            return False
        bound = store.get(UMO_KEY)
        if not isinstance(bound, str) or not bound:
            return True
        return event.unified_msg_origin == bound

    async def _reply(self, event: Any, text: str) -> None:
        sent = await self._ctx.send_message(event.unified_msg_origin, MessageChain().message(text))
        if not sent:
            logger.warning("[ak_toolbox][shift_reminder] 回执发送失败：%s", text)

    async def _cmd_bind(self, event: Any) -> None:
        previous = self._store.get(UMO_KEY)
        self._store.set(UMO_KEY, event.unified_msg_origin)
        if isinstance(previous, str) and previous and previous != event.unified_msg_origin:
            await self._reply(event, f"已把提醒目标改到本会话（原来是 {previous}）。")
            return
        await self._reply(event, "已绑定：往后换班提醒会发到这个会话。")

    async def _cmd_test(self, event: Any) -> None:
        """立刻发一条测试提醒：不写幂等键、不改绑定。"""
        now = datetime.now()
        snapshot = self._strategy.snapshot(now)
        text = notify.render_reminder(
            ending=snapshot.current,
            starting=snapshot.upcoming,
            change_at=snapshot.change_at,
            lead_minutes=self._lead_minutes,
        )
        sent = await self._ctx.send_message(
            event.unified_msg_origin, MessageChain().message(f"[测试]\n{text}")
        )
        if not sent:
            logger.warning("[ak_toolbox][shift_reminder] /ak test 发送失败")

    async def _cmd_status(self, event: Any) -> None:
        now = datetime.now()
        snapshot = self._strategy.snapshot(now)

        recent = []
        for record in self._send_log.recent(STATUS_RECENT):
            mark = "成功" if record.ok else "失败"
            suffix = f"（{record.detail}）" if record.detail else ""
            recent.append(f"{record.at:%m-%d %H:%M} {record.shift} {mark}{suffix}")

        body = notify.render_status(
            now=now,
            current=snapshot.current,
            upcoming=snapshot.upcoming,
            change_at=snapshot.change_at,
            module_states={"shift_reminder": True},
            recent_sends=recent,
        )

        bound = self._store.get(UMO_KEY)
        binding = bound if isinstance(bound, str) and bound else "（未绑定）"
        breaker = (
            f"熔断已打开（连续失败 {self._breaker.consecutive_failures} 次）"
            if self._breaker.is_open
            else "正常"
        )
        await self._reply(event, f"{body}\n绑定目标：{binding}\n推送状态：{breaker}")
