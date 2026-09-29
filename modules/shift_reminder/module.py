"""基建换班提醒模块的装配层。

这是本模块**唯一接触 AstrBot 的文件**：三班模型、判定策略与消息渲染都是纯逻辑
（`schedule` / `strategy` / `notify`，不 import astrbot），存储细节在 `core.storage`，
调度辅助在 `scheduler`。这里只负责把它们接起来。
"""

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.web import error_response, json_response, request
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 两种运行场景的导入差异：
#   * AstrBot 加载时插件是一个包（`<plugin>.modules.shift_reminder.module`），
#     插件根目录**不在** sys.path 上 → 只能用父级相对导入；
#   * 从仓库根直接跑 pytest 时（pyproject 配了 `pythonpath = ["."]`），顶层包是
#     `modules`，此时 `...` 会越界 → 必须用 `core.*` 绝对导入。
# 先绝对、失败再回退相对，两种场景都能工作；注册表的自动发现也才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.module import Module
    from core.permission import session_allowed
    from core.storage import JsonlSendLog, JsonStateStore, SendRecord
except ImportError:  # pragma: no cover
    from ...core.module import Module
    from ...core.permission import session_allowed
    from ...core.storage import JsonlSendLog, JsonStateStore, SendRecord
from . import avatars, notify, roster, scheduler, webapi
from .schedule import ConfigError, Shift, ShiftTable, parse_hhmm, validate
from .schedule_file import ScheduleFileError, parse_schedule_file
from .strategy import PeriodStrategy

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"
JOB_PREFIX = "ak_toolbox:shift_reminder:"
UMO_KEY = "bound_umo"
# 排班表导入状态（V1.5 才会写入）。页面据此显示「是否已导入」；尚未实现该功能时
# 读到的永远是 None，因此必须是 `is not None` 判断，不能假设键一定存在。
ROSTER_KEY = "imported_roster"
# 页面后端路由前缀：按规范必须带插件名（docs/architecture/rules.md §5）。
WEB_ROUTE_PREFIX = f"/{PLUGIN_NAME}/shift-reminder"
# 页面上传的排班表一律落成这个**固定名**：上传者给的文件名是伪造输入，绝不参与
# 路径拼接（既定文件名的做法会让 `../../x` 之类的名字有可乘之机）。
UPLOAD_FILENAME = "uploaded_schedule.json"
# 上传体积上限。真实排班表 15~30 KB，2 MB 是给足了余量的防御性上限——
# 目的不是"够用就行"，而是别让人拿一个几百 MB 的文件把内存撑爆。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEZONE = "Asia/Shanghai"
TIMEZONE_KEY = "timezone"
SHIFT_SLOTS = (1, 2, 3)
SEND_LOG_KEEP = 50
STATUS_RECENT = 5
DEFAULT_LEAD_MINUTES = 10

# 指令权限模型：私聊一律放行；群聊要求 AstrBot 管理员。**规则实现在 `core/permission.py`**，
# 这里只保留本模块自己的子命令清单（用于分发与提示文案）。
COMMAND_NAMES = ("bind", "status", "test", "import")


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


def parse_shift_order(config: Mapping[str, Any]) -> tuple[str, ...]:
    """配置里三班的**原始顺序**（第一班、第二班、第三班）。

    为什么需要它：`validate()` 会按开始时刻把班次重新排序——夜班 02:00 会排到最前。
    而排班表的 ``plans[0..2]`` 是按作者编排的「第 1/2/3 班」来的。拿**排序后**的
    下标去索引 ``plans`` 会在跨天班次上张冠李戴：早班的提醒里显示晚班的干员。
    所以与排班表对应一律用这个顺序，不用 `ShiftTable.shifts` 的顺序。
    """
    names: list[str] = []
    for slot in SHIFT_SLOTS:
        name = config.get(f"shift_{slot}_name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"shift_{slot}_name 必须是非空字符串，收到 {name!r}")
        names.append(name.strip())
    return tuple(names)


def parse_lead_minutes(config: Mapping[str, Any]) -> int:
    """读取提前量；必须是 0 或正整数。"""
    raw = config.get("lead_minutes", DEFAULT_LEAD_MINUTES)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ConfigError(f"lead_minutes 必须是非负整数，收到 {raw!r}")
    return raw


def _load_timezone(key: str) -> ZoneInfo | None:
    """解析时区名；本机没有时区数据时返回 `None`。

    刻意用 `zoneinfo` 而不是别的库：AstrBot 的调度器
    （`core/cron/manager.py:235`）注册任务时正是用 `ZoneInfo(job.timezone)` 解析，
    **取不到就只打一条 WARNING、然后回落到系统时区**。用同一个机制校验，才不会
    出现「我们说它合法、它却悄悄换了个时区」的错位。
    """
    try:
        return ZoneInfo(key)
    except Exception:  # noqa: BLE001 - 失败原因不止一种：无数据 / 名字非法 / 路径非法
        return None


def parse_timezone(config: Mapping[str, Any]) -> str:
    """读取时区名，并校验它**在本机可解析**。三条路径都不静默。

    1. 缺失、`None` 或空白 → 用默认值（老用户的配置文件里没有这个键，必须照常工作）。
    2. 有值且解析得了 → 原样返回。
    3. 有值但解析不了 → 若连默认值都解析不了，说明**本机根本没有时区数据**
       （Windows、精简镜像上常见），此时无法判定名字对错：如实打一条 WARNING
       说明「AstrBot 会回落到服务器本地时区」并放行——不能因为查不到就拒掉一个
       可能合法的配置，那会让整个插件加载失败。否则就是名字确实写错了，抛错。

    Args:
        config: 本模块自己的那一段配置。

    Returns:
        已校验的 IANA 时区名。

    Raises:
        ConfigError: 名字可判定为非法，或类型不对。
    """
    raw = config.get(TIMEZONE_KEY, DEFAULT_TIMEZONE)
    if raw is None:
        return DEFAULT_TIMEZONE
    if not isinstance(raw, str):
        raise ConfigError(f"{TIMEZONE_KEY} 必须是字符串，收到 {raw!r}")

    key = raw.strip()
    if not key:
        return DEFAULT_TIMEZONE

    if _load_timezone(key) is not None:
        return key

    if _load_timezone(DEFAULT_TIMEZONE) is None:
        logger.warning(
            "[ak_toolbox][shift_reminder] 本机没有可用的时区数据，无法校验 %s=%r；"
            "将原样交给 AstrBot，届时它可能回落到服务器本地时区（按系统时区跑）。",
            TIMEZONE_KEY,
            key,
        )
        return key

    raise ConfigError(
        f"{TIMEZONE_KEY} 不是可识别的 IANA 时区名：{key!r}。例如 Asia/Shanghai、Asia/Tokyo、UTC。"
    )


def stale_job_ids(jobs: Iterable[Any], prefix: str = JOB_PREFIX) -> list[str]:
    """挑出**属于本模块**、（可能）是上一条进程遗留的 job id。

    只认名字前缀，别的一律不碰：AstrBot 的 cron 表是所有插件共用的，
    过滤条件放宽一点就可能删掉用户其它功能的定时任务。

    Args:
        jobs: `cron_manager.list_jobs()` 的返回值（任何带 `name` / `job_id` 的对象）。
        prefix: 本模块的 job 名前缀。

    Returns:
        待清理的 job id 列表；没有匹配项时返回空列表。
    """
    stale: list[str] = []
    for job in jobs:
        name = getattr(job, "name", "")
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        job_id = getattr(job, "job_id", None)
        if job_id:
            stale.append(str(job_id))
    return stale


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
        self._tz: ZoneInfo | None = None
        # 三班的**原始顺序**（第1/2/3班）：排班表的 plans 按这个顺序对应，
        # 不能用 ShiftTable.shifts 的顺序（那个按开始时刻排过）。
        self._shift_order: tuple[str, ...] = ()
        # `/ak import` 只认这个目录里的文件；由 initialize 填好（见 resolve_import_path）。
        self._data_dir: Path | None = None
        # 干员头像映射（中文名 → 外链 URL）。装载失败时为 None，页面退回首字色块——
        # 头像只是锦上添花，**绝不能因为它让插件加载失败**。
        self._avatars: avatars.AvatarIndex | None = None

    # --- 生命周期 -----------------------------------------------------------

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        table = parse_shift_table(config)
        shift_order = parse_shift_order(config)
        lead_minutes = parse_lead_minutes(config)
        timezone = parse_timezone(config)

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
        self._tz = _load_timezone(timezone)
        self._strategy = PeriodStrategy(table, lead_minutes)
        self._shift_order = shift_order
        self._data_dir = data_dir
        self._store = JsonStateStore(data_dir / "state.json")
        self._send_log = JsonlSendLog(data_dir / "sends.jsonl", keep=SEND_LOG_KEEP)
        self._job_ids = []
        self._avatars = self._load_avatars()

        await self._register_jobs(cron_manager, table, lead_minutes, timezone)

        # 放在最后注册：前面任何一步失败都会让 initialize 抛出并被宿主回滚，
        # 此时页面路由不该已经指向一个没初始化完的实例。
        self._register_web_api(ctx)

    def _load_avatars(self) -> avatars.AvatarIndex | None:
        """装载干员头像映射（数据文件在插件包内，**只读**）。

        失败时**记 WARNING 并返回 None**，不抛异常：头像没有映射只意味着页面退回
        中文首字色块，功能依旧完整——为它让整个模块起不来是本末倒置。但降级必须
        留痕，否则用户看到一片色块也不知道是坏了还是本来就这样（宪法 §2 第 2 条）。

        数据由 `scripts/build_avatar_map.py` 生成，来源与许可见数据文件的 ``_comment``。
        """
        path = Path(__file__).resolve().parent / "data" / "avatar_map.json"
        index = avatars.load_avatar_index(path)
        if index is None:
            logger.warning(
                "[ak_toolbox][shift_reminder] 头像映射不可用（%s 缺失或格式不对），"
                "页面将显示中文首字色块；换班提醒与排班表不受影响。"
                "可用 scripts/build_avatar_map.py 重新生成。",
                path.name,
            )
            return None
        logger.info(
            "[ak_toolbox][shift_reminder] 已装载头像映射：%d 位干员（外链引用，不打包图片）",
            index.size,
        )
        return index

    async def _register_jobs(
        self,
        cron_manager: Any,
        table: ShiftTable,
        lead_minutes: int,
        timezone: str,
    ) -> None:
        """先清掉自己遗留的任务，再按当前班次表注册。**可重入**。

        ``initialize`` 与 ``apply_config`` 都走这里：前者是首次装载，后者是页面
        改了班次之后让改动**立刻生效**。两处共用同一段逻辑，就不会出现「启动时
        会清理、热更新时不清理」这种只在某一条路径上炸的缺陷。
        """
        purged = await self._purge_stale_jobs(cron_manager)
        logger.info(
            "[ak_toolbox][shift_reminder] 已清理 %d 个历史任务，开始注册 %d 个换班提醒任务",
            purged,
            len(table.shifts),
        )

        self._job_ids = []
        for shift in table.shifts:
            job = await cron_manager.add_basic_job(
                name=f"{JOB_PREFIX}{shift.name}",
                cron_expression=scheduler.reminder_cron_expression(shift, lead_minutes),
                handler=self._make_handler(shift.name),
                description=f"{shift.name} 换班提醒（提前 {lead_minutes} 分钟）",
                timezone=timezone,
                payload={"shift": shift.name},
            )
            self._job_ids.append(job.job_id)

        logger.info(
            "[ak_toolbox][shift_reminder] 已注册 %d 个换班提醒任务（提前 %d 分钟，时区 %s）",
            len(self._job_ids),
            lead_minutes,
            timezone,
        )

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """配置被改后重建定时任务，让改动**立刻生效**。

        `save_config` 只保证内存与磁盘更新，**不会**让已经注册的 cron 任务跟着变
        （已核源码，见 implementation.md §2.6 的前置结论）。如果只保存不重建，
        用户会看到面板显示新时刻、提醒却仍按旧时刻跑——每个环节单看都正常，
        是最难查的一类错。

        配置非法一律抛错：宿主会把失败原因回给页面，用户看得到「没保存成功」，
        而不是以为成功了。
        """
        cron_manager = getattr(self._ctx, "cron_manager", None)
        if cron_manager is None:
            raise RuntimeError("取不到 AstrBot 的 cron_manager，无法重建换班提醒任务。")

        table = parse_shift_table(config)
        shift_order = parse_shift_order(config)
        lead_minutes = parse_lead_minutes(config)
        timezone = parse_timezone(config)

        self._lead_minutes = lead_minutes
        self._tz = _load_timezone(timezone)
        self._strategy = PeriodStrategy(table, lead_minutes)
        self._shift_order = shift_order
        await self._register_jobs(cron_manager, table, lead_minutes, timezone)

    async def _purge_stale_jobs(self, cron_manager: Any) -> int:
        """删掉上一条进程遗留的定时任务，返回清掉的条数。

        AstrBot 被 kill（容器重启、OOM）时 ``terminate()`` 来不及跑，上一轮注册的
        job 会留在 cron 表里；不先清掉，每重启一次就多三条，到点触发三次。
        幂等：没有历史任务时安静返回 0。

        清理失败一律向上抛——宁可不启动，也不要在明知有重复任务的情况下继续注册
        更多（项目宪法 §2 第 2、4 条）。宿主会回滚已启动的模块，不会留下半个插件。
        """
        stale: list[str] = []
        try:
            stale = stale_job_ids(await cron_manager.list_jobs())
            for job_id in stale:
                await cron_manager.delete_job(job_id)
        except Exception:
            logger.exception(
                "[ak_toolbox][shift_reminder] 清理历史定时任务失败，中止启动以免重复注册"
            )
            raise
        return len(stale)

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

    # --- 时间 ---------------------------------------------------------------

    def _now(self) -> datetime:
        """当前时刻，**带上配置的时区**。

        必须带：cron 是按配置时区的墙上时刻触发的，若这里仍用服务器本地时间算
        「现在第几班」，换个时区之后提醒会在正确时刻触发、却描述错误的班次。
        本机没有时区数据时（`_tz is None`）退回系统本地时间，与 AstrBot 调度器的
        回落行为保持一致。
        """
        if self._tz is None:
            return datetime.now()
        return datetime.now(self._tz)

    # --- 绑定目标（读写封装，为将来「多人各收各的」留门） -------------------

    def _get_target(self) -> str | None:
        """当前绑定的提醒目标（umo）；未绑定时返回 `None`。

        读写收在一处是为了**把变化点封在一点**：将来若改成「一个实例里多人各收
        各的」，只需改这两个函数，调用方一行不用动。现在就是单目标、覆盖式改绑。
        """
        value = self._store.get(UMO_KEY)
        return value if isinstance(value, str) and value else None

    def _set_target(self, umo: str) -> None:
        """把提醒目标改到 `umo`（单目标，覆盖旧值）。"""
        self._store.set(UMO_KEY, umo)

    # --- 推送 ---------------------------------------------------------------

    def _make_handler(self, shift_name: str):
        async def handler(**_: Any) -> None:
            await self._push(shift_name)

        return handler

    def _roster_extra(self, current: Any) -> list[str] | None:
        """当前班次该显示的房间与干员；**没导入过排班表时返回 None**。

        返回 None 时调用方照旧渲染，于是「从未导入过」的用户看到的提醒与从前
        一字不差——那是绝大多数人的第一天状态，不能被这个功能改变。

        班次与排班表靠**下标**对应（`build_roster` 用 `plan_index` 记录），不靠名字：
        排班表里的 `plans[].name` 写法五花八门，而下标是唯一稳定的对应关系。
        """
        strategy, store = self._strategy, self._store
        if strategy is None or store is None:
            return None

        data = store.get(ROSTER_KEY)
        if data is None:
            return None

        try:
            # 用**配置的原始顺序**，不是 ShiftTable.shifts 的顺序：后者按开始时刻
            # 排过（夜班 02:00 会跑到最前），而 plans 是按第 1/2/3 班编的。
            plan_index = self._shift_order.index(current.name) + 1
        except ValueError:
            logger.warning(
                "[ak_toolbox][shift_reminder] 配置里找不到当前班次 %s，本次不附干员名单",
                getattr(current, "name", current),
            )
            return None

        lines = roster.render_roster_lines(data, plan_index)
        return lines or None

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

        now = self._now()
        snapshot = strategy.snapshot(now)

        key = scheduler.idempotency_key(shift_name, snapshot.change_at)
        if store.get(key):
            logger.info("[ak_toolbox][shift_reminder] %s 的这次提醒已发过，跳过", shift_name)
            return

        umo = self._get_target()
        if umo is None:
            logger.warning(
                "[ak_toolbox][shift_reminder] 尚未绑定提醒目标，跳过推送；请先发 /ak bind"
            )
            return

        text = notify.render_reminder(
            ending=snapshot.current,
            starting=snapshot.upcoming,
            change_at=snapshot.change_at,
            lead_minutes=self._lead_minutes,
            extra=self._roster_extra(snapshot.current),
        )

        try:
            sent = await self._ctx.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001 - 见下方注释：异常同样是失败，不是崩溃
            # 平台离线时 `send_message` 会**抛异常**，而不是返回 `False`。
            # 只处理返回值的版本会漏掉这种失败：熔断计数永不增长（熔断形同虚设）、
            # `sends.jsonl` 里看不到这次失败、用户查 `/ak status` 完全不知情，
            # 而异常冒泡到 cron 层只会让 AstrBot 记一条 error——插件自己的状态
            # 依然干净（2026-09-29 线上事故）。所以这里走**同一条失败路径**。
            #
            # `detail` 只带异常类型与消息：本地异常字符串不含凭据，且排查时最有价值。
            self._record_push_failure(
                send_log,
                now,
                shift_name,
                f"send_message 抛异常：{type(exc).__name__}: {exc}",
            )
            return

        if sent:
            store.set(key, True)
            send_log.append(SendRecord(at=now, shift=shift_name, ok=True))
            self._breaker.record_success()
            logger.info("[ak_toolbox][shift_reminder] 已推送 %s 的换班提醒", shift_name)
            return

        self._record_push_failure(
            send_log,
            now,
            shift_name,
            "send_message 返回 False（没找到匹配的平台或会话）",
        )

    def _record_push_failure(
        self, send_log: Any, now: datetime, shift_name: str, detail: str
    ) -> None:
        """记一次推送失败：发送记录、熔断计数、日志——三条都不能少。

        抽出来是为了让「返回 False」与「抛异常」两种失败形态**走同一条路径**，
        不会因为将来只改其中一处而再次漏掉计数。
        """
        send_log.append(SendRecord(at=now, shift=shift_name, ok=False, detail=detail))
        self._breaker.record_failure()
        logger.warning("[ak_toolbox][shift_reminder] 推送失败：%s", detail)

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        handlers = {
            "bind": self._cmd_bind,
            "test": self._cmd_test,
            "status": self._cmd_status,
            "import": self._cmd_import,
        }
        handler = handlers.get(command)
        if handler is None:
            return False
        # 官方 API：`is_private_chat()` 在 core/platform/astr_message_event.py:260，
        # `is_admin()` 在 :268（其 `role` 由 waking_check/stage.py:105 依据配置的
        # `admins_id` 置为 "admin"）。直接调用，拿不到就抛——不让权限判定的失败
        # 静默降级成「放行」。
        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="操作换班提醒",
        )
        if not allowed:
            await self._reply(event, reason)
            return True
        await handler(event)
        return True

    async def _reply(self, event: Any, text: str) -> None:
        """给指令发起者回一条消息。

        平台离线时 `send_message` 会抛异常。这里**必须包住**：回执发不出去只是遗憾，
        异常冒泡上去会让整个指令 handler 失败——用户发 `/ak status` 结果什么反应
        都没有，比「收到一句失败提示」更糟（2026-09-29 事故的同类路径）。
        """
        try:
            sent = await self._ctx.send_message(
                event.unified_msg_origin, MessageChain().message(text)
            )
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][shift_reminder] 回执发送异常（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][shift_reminder] 回执发送失败：%s", text)

    async def _cmd_bind(self, event: Any) -> None:
        previous = self._get_target()
        self._set_target(event.unified_msg_origin)
        if previous is not None and previous != event.unified_msg_origin:
            await self._reply(event, f"已把提醒目标改到本会话（原来是 {previous}）。")
            return
        await self._reply(event, "已绑定：往后换班提醒会发到这个会话。")

    async def _cmd_test(self, event: Any) -> None:
        """立刻发一条测试提醒：不写幂等键、不改绑定。"""
        now = self._now()
        snapshot = self._strategy.snapshot(now)
        text = notify.render_reminder(
            ending=snapshot.current,
            starting=snapshot.upcoming,
            change_at=snapshot.change_at,
            lead_minutes=self._lead_minutes,
            extra=self._roster_extra(snapshot.current),
        )
        try:
            sent = await self._ctx.send_message(
                event.unified_msg_origin, MessageChain().message(f"[测试]\n{text}")
            )
        except Exception:  # noqa: BLE001 - 见 _reply：指令路径的发送失败不该冒泡
            # 注意：`/ak test` 是**发给发起者**的即时消息，不是计划推送，
            # 因此不计入熔断（熔断的语义是「这个平台的提醒一直送不到」）。
            logger.exception("[ak_toolbox][shift_reminder] /ak test 发送异常（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][shift_reminder] /ak test 发送失败")

    async def _cmd_status(self, event: Any) -> None:
        now = self._now()
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

        binding = self._get_target() or "（未绑定）"
        breaker = (
            f"熔断已打开（连续失败 {self._breaker.consecutive_failures} 次）"
            if self._breaker.is_open
            else "正常"
        )
        await self._reply(event, f"{body}\n绑定目标：{binding}\n推送状态：{breaker}")

    async def _cmd_import(self, event: Any) -> None:
        """``/ak import <文件名>``：把数据目录下的排班表读进来并落盘。

        文件名来自用户消息，所以**先过 `roster.resolve_import_path` 再做任何读取**：
        校验不通过时连 `is_file()` 都不调用（见该函数的说明）。

        成功与失败的每条路径都要有回执——用户看不到结果就无从自查
        （项目宪法 §2 第 2 条）。
        """
        data_dir, store = self._data_dir, self._store
        if data_dir is None or store is None:
            await self._reply(event, "换班提醒模块尚未初始化完成，请稍后重试。")
            return

        raw_name = roster.parse_import_argument(str(event.message_str or ""))
        try:
            path = roster.resolve_import_path(data_dir, raw_name)
        except roster.RosterImportError as exc:
            await self._reply(event, f"{exc}\n数据目录：{data_dir}")
            return

        if not path.is_file():
            await self._reply(
                event,
                f"找不到文件：{path.name}\n把排班表 JSON 放进这个目录再试：{data_dir}",
            )
            return

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[ak_toolbox][shift_reminder] 读取排班表失败：%s", exc)
            await self._reply(event, f"读取文件失败：{exc}")
            return

        try:
            imported = roster.build_roster(
                parse_schedule_file(text),
                source=path.name,
                imported_at=self._now(),
            )
        except (ScheduleFileError, roster.RosterImportError) as exc:
            logger.warning("[ak_toolbox][shift_reminder] 导入排班表失败：%s", exc)
            await self._reply(event, f"导入失败：{exc}")
            return

        store.set(ROSTER_KEY, imported)
        logger.info(
            "[ak_toolbox][shift_reminder] 已导入排班表 %s（%d 个班次）",
            path.name,
            imported["shift_count"],
        )
        await self._reply(
            event,
            f"{roster.describe_roster(imported)}\n"
            f"{roster.describe_duration_hint(imported)}\n"
            f"来源：{path.name}",
        )

    # --- WebUI（页面只是另一种入口；数据一律复用上面的既有实现） -----------

    def _register_web_api(self, ctx: Any) -> None:
        """注册页面用的只读 Web API。

        由**模块自己**注册，宿主不参与——宿主眼里只有「模块」，它不认识三班
        （docs/architecture/scope.md §2）。`register_web_api` 对「同路由 + 同方法」
        是替换语义，所以插件重载不会堆出重复路由。

        取不到 `register_web_api` 时**只降级页面、不拖垮提醒**：换班提醒是核心功能，
        不该因为一个可选页面让整个插件加载失败。但降级必须留痕——WARNING 写明后果，
        不做成静默跳过（项目宪法 §2 第 2 条）。
        """
        register = getattr(ctx, "register_web_api", None)
        if register is None:
            logger.warning(
                "[ak_toolbox][shift_reminder] 当前 Context 没有 register_web_api，"
                "WebUI 页面将不可用；换班提醒本身不受影响。"
            )
            return
        register(
            f"{WEB_ROUTE_PREFIX}/status",
            self._web_status,
            ["GET"],
            "基建换班提醒状态",
        )
        register(
            f"{WEB_ROUTE_PREFIX}/roster",
            self._web_roster,
            ["GET"],
            "已导入的排班表全貌",
        )
        register(
            f"{WEB_ROUTE_PREFIX}/upload",
            self._web_upload,
            ["POST"],
            "上传排班表 JSON",
        )

    async def _web_status(self) -> Any:
        """只读状态：当前班次、下一班倒计时、绑定目标、最近发送记录。

        组装全在 `webapi.build_status`（纯逻辑、有单测）；这里只负责取数据，
        绝不重算班次——两处逻辑必然漂移（docs/implementation/implementation.md §2.6）。
        """
        if self._strategy is None or self._store is None or self._send_log is None:
            return error_response("换班提醒模块尚未初始化完成，请稍后重试", status_code=503)

        raw_limit = request.query.get("limit", STATUS_RECENT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            logger.warning(
                "[ak_toolbox][shift_reminder] 页面传入的 limit 不是整数：%r，改用默认值 %d",
                raw_limit,
                STATUS_RECENT,
            )
            limit = STATUS_RECENT
        limit = max(1, min(limit, SEND_LOG_KEEP))

        payload = webapi.build_status(
            self._strategy.snapshot(self._now()),
            lead_minutes=self._lead_minutes,
            bound=self._get_target() is not None,
            recent=self._send_log.recent(limit),
            roster_imported=self._store.get(ROSTER_KEY) is not None,
            breaker_open=self._breaker.is_open,
            consecutive_failures=self._breaker.consecutive_failures,
            # 必须传**配置里的原始顺序**，不是 `table.shifts`：后者按开始时刻
            # 升序排过（夜班 02:00 会跑到最前），而页面表单是按位置认
            # `shift_1/2/3` 的——用排序后的顺序回填，用户会看到「第一班 = 夜班」，
            # 一保存就把夜班时刻写进 `shift_1`，配置被改坏。
            shifts=self._strategy.table.shifts,
            shift_order=self._shift_order,
        )
        return json_response(payload)

    async def _web_roster(self) -> Any:
        """已导入排班表的**全貌**（含被标为「不动」的房间）。

        与提醒消息的取舍刻意不同：提醒里不渲染「不动」的房间（那是给用户的**指令**，
        让用户去改一间标明不要动的房就是错误信息）；页面上必须显示（那是给用户看的
        **全貌**，藏起来用户会以为我们读漏了）。同一个数据，两种用途。

        组装全在 `webapi.roster_view`（纯逻辑、有单测）。
        """
        if self._store is None:
            return error_response("换班提醒模块尚未初始化完成，请稍后重试", status_code=503)

        # 默认选中「当前正在进行的班次」：名字来自配置（早班），而排班表里的班次名
        # 来自文件（Shift 1 · 12h），两者对不上——靠 _shift_order 那层既有映射搭桥，
        # 对不上时 webapi 那边会老实退回第一班（见 current_shift_slot）。
        current_shift = None
        if self._strategy is not None:
            current_shift = self._strategy.snapshot(self._now()).current.name

        return json_response(
            webapi.roster_view(
                self._store.get(ROSTER_KEY),
                current_shift=current_shift,
                shift_order=self._shift_order,
                avatars=self._avatars,
            )
        )

    async def _web_upload(self) -> Any:
        """接收页面提交的排班表（base64 + JSON POST）并导入。

        为什么不用 multipart：AstrBot 的插件页面 bridge 用 `postMessage` 与父页面通信，
        而 `FormData` **不能被结构化克隆**——真实浏览器里直接抛
        「FormData object could not be cloned.」，文件递不到这里。所以前端把文件读成
        base64 走普通 JSON POST；解码与体积判断在
        `webapi.decode_schedule_upload`（纯逻辑、可单测）。

        安全要点（三条都是硬性的）：

        1. **绝不使用上传者提供的文件名落盘**——那是伪造输入。一律落成固定名
           :data:`UPLOAD_FILENAME`；请求里的 `filename` 只用于日志与回执展示。
        2. **先解码校验、合法了才写盘**——避免把脏文件留在数据目录里。体积按**解码后**
           的字节数卡（解码前先按长度做一次廉价拒绝，见 `decode_schedule_upload`）。
        3. **路径不来自用户**——目标路径由 `self._data_dir` 与固定名拼成，
           没有任何用户输入参与拼接。

        解析失败返回 400 并带上解析器的原因（不是 500）——用户需要知道哪里不对。
        """
        data_dir, store = self._data_dir, self._store
        if data_dir is None or store is None:
            return error_response("换班提醒模块尚未初始化完成，请稍后重试", status_code=503)

        try:
            payload = await request.json(default={})
        except Exception as exc:  # noqa: BLE001 - 请求体读不出来要给用户明确原因
            logger.warning("[ak_toolbox][shift_reminder] 读取上传请求失败：%s", exc)
            return error_response(f"读取上传内容失败：{exc}", status_code=400)

        if not isinstance(payload, Mapping):
            return error_response("请求体必须是一个 JSON 对象。", status_code=400)

        try:
            raw = webapi.decode_schedule_upload(
                payload.get("content_b64"), max_bytes=MAX_UPLOAD_BYTES
            )
        except webapi.UploadPayloadError as exc:
            logger.warning("[ak_toolbox][shift_reminder] 页面上传内容被拒绝：%s", exc)
            return error_response(str(exc), status_code=exc.status_code)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return error_response("文件不是 UTF-8 编码的文本，无法解析。", status_code=400)

        # 去掉 BOM：Windows 上用记事本另存 JSON 很容易带上它，而 json.loads 见到
        # 开头那个 \ufeff 会直接报错——用户看到的就是「文件格式不对」，很难自查。
        text = text.lstrip("\ufeff")

        try:
            imported = roster.build_roster(
                parse_schedule_file(text),
                source=UPLOAD_FILENAME,
                imported_at=self._now(),
            )
        except (ScheduleFileError, roster.RosterImportError) as exc:
            logger.warning("[ak_toolbox][shift_reminder] 页面上传的排班表无法导入：%s", exc)
            return error_response(f"导入失败：{exc}", status_code=400)

        target = data_dir / UPLOAD_FILENAME
        try:
            target.write_bytes(raw)
        except OSError as exc:
            logger.warning("[ak_toolbox][shift_reminder] 保存上传的排班表失败：%s", exc)
            return error_response(f"保存文件失败：{exc}", status_code=500)

        store.set(ROSTER_KEY, imported)
        # 请求里的文件名是**用户完全可控**的字符串，进日志前先削掉空白（换行能伪造出
        # 一整条假日志行），并限长。它到此为止——不参与任何路径拼接。
        original = payload.get("filename")
        shown = " ".join(original.split())[:80] if isinstance(original, str) else ""
        logger.info(
            "[ak_toolbox][shift_reminder] 页面已上传排班表（%d 字节，%d 个班次，原文件名 %s）",
            len(raw),
            imported.get("shift_count", 0),
            shown or "(未提供)",
        )
        return json_response(
            {
                "saved": True,
                "filename": UPLOAD_FILENAME,
                # 与 /ak import 的回执同口径，用户两处看到的数字一致。
                "summary": roster.describe_roster(imported),
                # 能不能从名字里读出班次时长——页面据此显示「按排班表填入」按钮。
                "duration_hint": roster.describe_duration_hint(imported),
                "duration_hints_minutes": (
                    list(hinted) if (hinted := roster.suggested_durations(imported)) else None
                ),
                "duration_suggestion_hours": (
                    list(hours) if (hours := roster.suggested_hours(imported)) else None
                ),
                # 回显用户自己的文件名，只为让他确认「传的是哪个文件」。
                "original_filename": shown,
            }
        )
