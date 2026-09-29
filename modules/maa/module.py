"""MAA 远程控制模块 —— **确认制派任务**。

分工（所有者 2026-09-29 的原话）：

    定时启动 maa 让它跑完全套流程；MAA 自己会根据排班更换干员。

⇒ **我们只负责「什么时候让它跑」，不负责「跑哪一班」**——协议里根本没有班次字段
（`docs/project-plan/10-maa-shift-switching.md` §4.1 已实测确认）。跑完之后 MAA 会把
它内部的计划索引前进一格，那是它自己的规则。

本模块做四件事：

1. 用户确认后（`/ak maa run`）把**一个**任务放进队列；
2. `getTask` 把队列里的任务交给 MAA（可重入，同 id 不会被重复执行）；
3. `reportStatus` 结清任务，并把结果**如实**转达给用户；
4. 超时（电脑没开／取走了没回话）**主动告知**，不静默。

## ⚠️ 没有用户确认，队列永远是空的

`getTask` 是**匿名可达**的（MAA 只能填两个普通 URL，带不了自定义请求头），所以它的
响应**只来自我们自己的队列状态**，**绝不读请求体里的任何字段**——请求里自报的
`user` / `device` 是谁都能编的。这条有测试钉住（不同请求体必须得到同一响应）。

## 为什么去重在这里是「正确性」而不是「优化」

MAA 每跑完一次基建任务就把内部计划索引**永久前进一格**（写回配置、不会自动纠正）。
⇒ **触发次数必须严格等于换班次数**：多下发一次，用户的排班就跳一班。
两手不变量见 `queue.py` 的模块 docstring（**同一换班 ⇒ 同一 id**、**至多一个未结任务**）。

## ⚠️ 已知缺口：「到点询问」尚未实现，卡的是**架构**，不是工时

任务书要求在换班时刻主动问用户。那需要知道**班次时刻**，而它只存在于
`shift_reminder` 的配置段里，本模块拿不到——四堵墙都已核实：

1. 宿主只按 `config_key` 注入**自己那一段**（`core/registry.py` 与 `main.py`），
   C3 裁决原文：模块「**不知道别的模块段存在**」；
2. `shift_reminder` **不持久化**班次表（`state.json` 里只有 `bound_umo` /
   `imported_roster` / 幂等键）；
3. 契约（`docs/architecture/extension.md` §2 结尾）写着模块「**不许 import 别的模块**」
   ——这与任务包里那句「可以只读 import 它的纯逻辑」直接冲突；
4. 「共享逻辑抽到 `core/`」虽是 §4 的既有规定，但要动 `core/**`——本包禁区。

**所以本模块不去绕。** 复制一份时刻、或偷读别人的配置段，都会让「班次时刻」变成
**两份**；用户改了提醒的时刻而忘了改另一份，我们就会在**错误的时刻**询问，用户一
确认，**MAA 多跑一次 ⇒ 班次多跳一班**——正是 `10` 号文档里我们必须避免的那个 bug。
本项目刚为「同一个事实两份实现」栽过两次（权限判定、`Content-Type` 只设在签名分支）。

⇒ 需要一条**单一事实来源**的通道（宿主注入共用视图 / 抽到 `core/` / 由
`shift_reminder` 触发），那属于动地基，**已回报待裁决**。在此之前派任务由
`/ak maa run` 手动触发——而「确认一次只跑一趟」这个最该先验的不变量，
手动路径已经能完整验到。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.web import error_response, json_response, request

# 两种运行场景的导入差异（与 modules/skland/module.py 同一取舍）：
#   * AstrBot 加载时插件是一个包，插件根目录不在 sys.path 上 → 只能用父级相对导入；
#   * 从仓库根跑 pytest 时顶层包是 `modules`，`...` 会越界 → 必须用绝对导入。
# 先绝对、失败再回退相对，注册表的自动发现才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.config import is_unset, setting
    from core.module import Module
    from core.permission import session_allowed
except ImportError:  # pragma: no cover
    from ...core.config import is_unset, setting
    from ...core.module import Module
    from ...core.permission import session_allowed

from . import protocol, relay, tasks
from .queue import AckOutcome, EnqueueOutcome, PendingTask, Slot, TaskQueue

COMMAND_NAMES = ("maa",)

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"

#: 两个端点在本插件路由前缀下的路径。**必须带插件名前缀**：Dashboard 注册的路由
#: 本身就是 `/<插件名>/<子路径>`，所以最终外部 URL 是
#: `/api/v1/plugins/extensions/<插件名>/maa/getTask` —— **插件名只出现一次**。
#: 写成两遍（`.../extensions/<插件名>/<插件名>/maa/getTask`）**匹配不上**，
#: 已在本机用 AstrBot 的路由匹配函数实测确认。
WEB_ROUTE_PREFIX = f"/{PLUGIN_NAME}/maa"

#: 面板 API Key 的查询参数名。已核实 AstrBot 4.28.1 的
#: `dashboard/api/auth.py:_extract_raw_api_key` 接受 `?api_key=` 与 `?key=` 两种写法
#: ——**这一点很关键**：MAA 只能填一个普通 URL，没法带自定义请求头，所以查询串是
#: 它唯一能携带凭据的方式。凭据由运维侧创建与保管，本模块不碰它。
API_KEY_QUERY = "api_key"

#: 本模块自己的定时任务名前缀。契约要求 `terminate()` 按
#: `ak_toolbox:<模块名>:` 前缀清理自己注册的任务（`extension.md` §3 接入检查单）。
JOB_PREFIX = "ak_toolbox:maa:"
SWEEP_JOB_NAME = f"{JOB_PREFIX}sweep"

#: 过期巡检：每 5 分钟一次。任务 TTL 以分钟计，这个粒度足够及时，又不会把日志刷满。
#:
#: 时区**故意用 UTC**：`*/5` 这种写法与本地时钟无关（任何真实时区偏移都是 5 分钟的
#: 整数倍，触发时刻完全一致），用 UTC 是为了不假装我们跟着用户的本地钟走。
SWEEP_CRON = "*/5 * * * *"
SWEEP_TIMEZONE = "UTC"

DEFAULT_TASK_TTL_MINUTES = 30
DEFAULT_FETCHED_TTL_MINUTES = 240

#: TTL 的上限。给一个上限而不是任意整数，是为了让「毫秒写成 30000」这类手滑
#: 当场被拒，而不是变成一个 20 天都不会过期的任务。
MAX_TTL_MINUTES = 7 * 24 * 60

#: 逐次轮询不写 INFO，改为每一段时间最多一条摘要。
#:
#: **为什么必须节流**：MAA 按协议**每秒**来一次，一次一条 INFO 就是一天 86400 条。
#: 后果不止是吵——「首次收到请求」「任务被取走」「回报到达」「超时作废」这些**真正
#: 需要看见的事**会被埋在同一秒一条的心跳里，而服务器上的容器日志没有轮转保证。
#:
#: **为什么不是直接删掉**：这个模块的价值之一就是「看得出 MAA 还连着没有」。
#: 所以逐次降为 DEBUG（默认不输出，排查时可开），而 INFO 留给三件事：
#: 首次到达、周期性摘要、以及所有真正的事件（那些在别处的调用点，**不受本限流影响**）。
#:
#: 一小时是权衡后的取值：一天最多 24 条摘要，既撑不起噪音，又能让「昨晚它还在连吗」
#: 这类问题在日志里直接看得到。
HEARTBEAT_INTERVAL_SECONDS = 3600


def _parse_minutes(config: Mapping[str, Any], key: str, default: int, *, what: str) -> int:
    """从配置里读一个「分钟数」。

    **空值 ⇒ 回退默认值**（记一条 WARN）：宿主按 ``_conf_schema.json`` 的 ``items``
    逐项生成配置，漏写 ``default`` 的那一项在全新安装后就是空串；空串不是"填错了"，
    而是"没设置"，不该让整个模块起不来（2026-09-29 的线上复现：``task_type`` 空串
    导致模块启动失败，提示还在怪用户填错）。

    **非法值一律抛错，不回落默认值**：用户改了个错值却以为生效了，比直接报错糟得多。
    （``bool`` 是 ``int`` 的子类，所以单独挡一下——``True`` 不该被当成 1 分钟。）
    """
    if is_unset(config.get(key)):
        logger.warning(
            "[ak_toolbox][maa] 配置 %s 未设置（或为空），采用默认值 %d 分钟", key, default
        )
    raw = setting(config, key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{what}必须是整数（分钟），收到 {raw!r}")
    if not 1 <= raw <= MAX_TTL_MINUTES:
        raise ValueError(f"{what}必须在 1..{MAX_TTL_MINUTES} 分钟之间，收到 {raw}")
    return raw


def _parse_task_type(config: Mapping[str, Any]) -> str:
    """读任务类型。

    **空值 ⇒ 回退默认值**（同上，记 WARN）；**非法值**由 `tasks.coerce_task_type`
    抛 `TaskTypeError`——``"NotATask"`` 这种是真的填错了，必须当场说出来。
    """
    if is_unset(config.get("task_type")):
        logger.warning(
            "[ak_toolbox][maa] 配置 task_type 未设置（或为空），采用默认值 %s",
            tasks.DEFAULT_TASK_TYPE,
        )
    return tasks.coerce_task_type(setting(config, "task_type", tasks.DEFAULT_TASK_TYPE))


def _header(name: str) -> str:
    """取一个请求头。**只用于 User-Agent**——整张头表不能进日志（见 `_record`）。"""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    try:
        return str(headers.get(name, "") or "")
    except Exception:  # noqa: BLE001 - 取不到头不该让请求失败
        return ""


def _attr(name: str) -> str:
    return str(getattr(request, name, "") or "")


class MaaModule(Module):
    """两个端点 + 一个至多一条任务的队列 + 一份到达记录。"""

    name = "maa"
    config_key = "maa"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._routes_registered = False
        self._queue = TaskQueue()
        self._task_type: str = tasks.DEFAULT_TASK_TYPE
        self._job_ids: list[str] = []
        #: 通知目标：用户最后一次用 `/ak maa` 的那个会话。
        #:
        #: **只在内存**（与到达统计同一取舍）。插件重启会丢掉它，丢掉时**必须记
        #: WARNING 并把内容写进日志**——通知发不出去不能是静默的（本项目铁律）。
        self._umo = ""
        #: 用户最近一次选择「我自己换」。用户手动换班与 MAA 的计划索引是**两套东西**，
        #: 记下时间点，排查「班次对不上」时才知道该往哪儿看。
        self._last_manual_at: datetime | None = None

        #: 到达统计。**故意只放内存**：这个模块的用途是「刚刚那次重启之后，
        #: MAA 到底连上没有」，而重启本来就该把计数清零；MAA 一秒轮询一次，
        #: 真连着的话几秒内就会重新有数。
        self._arrivals = 0
        self._get_task_hits = 0
        self._report_hits = 0
        self._failures = 0
        self._last_seen: datetime | None = None
        self._last_summary = ""
        self._last_body_shape = ""
        self._last_status = ""

        #: 日志节流状态。逐次轮询走 DEBUG，INFO 只留首次与周期性摘要。
        #: 上一次摘要的时刻与「那之后又来了多少请求」，两个一起才能说出
        #: 「过去 N 分钟收到 M 次」这种可用的话，而不是干巴巴一句「还在连」。
        self._heartbeat_at: datetime | None = None
        self._heartbeat_arrivals = 0

        #: 时钟**可注入**，只为让节流的测试不必 sleep。
        #: 生产路径下它恒等于 `datetime.now`，没有任何行为差异。
        self._now: Callable[[], datetime] = datetime.now

    # --- 生命周期 -----------------------------------------------------------

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """读配置、建队列、注册两个端点与过期巡检。

        **填错**的值当场抛：宿主会逐模块隔离失败、把原因记进清单并单列出来，
        用户看得到「maa 没起来，因为任务类型写错了」。

        **空值不算填错**，它等于"没设置"——宿主的默认配置里这一项就可能是个空串
        （`_conf_schema.json` 的 `items` 漏写 `default` 时会这样），此时回退默认值
        并记一条 WARN。判据与边界见 `core/config.py`。
        """
        self._task_type = _parse_task_type(config)
        task_ttl = _parse_minutes(
            config, "task_ttl_minutes", DEFAULT_TASK_TTL_MINUTES, what="任务保留时间"
        )
        fetched_ttl = _parse_minutes(
            config,
            "fetched_ttl_minutes",
            DEFAULT_FETCHED_TTL_MINUTES,
            what="取走后等待回报的时间",
        )
        self._queue = TaskQueue(task_ttl_minutes=task_ttl, fetched_ttl_minutes=fetched_ttl)

        self._ctx = ctx
        self._register_web_api(ctx)
        await self._register_sweep_job()

        if self._routes_registered:
            logger.info(
                "[ak_toolbox][maa] 模块已装载：任务类型「%s」；等待 MAA 轮询"
                "（还没收到过请求）。**没有用户确认之前，队列是空的。**"
                "换班提醒与其它模块不受本模块影响。",
                tasks.task_type_label(self._task_type),
            )
        else:
            # 这里**不抛**：端点没注册上只说明这个功能不可用，但它不该连累整个插件
            # 加载（换班提醒是核心功能）。不可用这件事通过契约给的出口
            # `unavailable_reason` 如实报告给宿主。
            logger.error(
                "[ak_toolbox][maa] 端点注册失败，MAA 连不上我们。本模块不可用；其它模块不受影响。"
            )

    async def terminate(self) -> None:
        """清掉自己注册的定时任务与引用；可安全重复调用。

        端点本身**没有反注册接口**（`register_web_api` 只在「同路由 + 同方法」时
        替换），所以插件卸载后这个对象上仍可能挂着已注册的绑定方法。那没有危害：
        两个处理函数只读本对象的队列状态，而队列在确认之前恒为空。
        """
        removed = await self._purge_jobs()
        if removed:
            logger.info("[ak_toolbox][maa] 已清理 %d 个定时任务", removed)
        self._job_ids = []
        self._ctx = None
        self._routes_registered = False

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """配置改了之后更新任务类型与两个窗口，**不动在途任务**。

        `save_config` 只保证内存与磁盘更新，不会让运行中的状态跟着变（已核源码，见
        `implementation.md` §2.6）。如果只保存不更新，用户会看到面板显示新值、行为
        却还是旧的——每个环节单看都正常，是最难查的一类错。

        为什么不重建队列：见 `TaskQueue.retune` 的说明（会丢掉 MAA 可能已经取走的任务）。
        """
        task_type = _parse_task_type(config)
        task_ttl = _parse_minutes(
            config, "task_ttl_minutes", DEFAULT_TASK_TTL_MINUTES, what="任务保留时间"
        )
        fetched_ttl = _parse_minutes(
            config,
            "fetched_ttl_minutes",
            DEFAULT_FETCHED_TTL_MINUTES,
            what="取走后等待回报的时间",
        )
        self._task_type = task_type
        self._queue.retune(task_ttl_minutes=task_ttl, fetched_ttl_minutes=fetched_ttl)

    @property
    def unavailable_reason(self) -> str | None:
        """端点没注册上 = 干不了这个模块的活。

        注意与「还没收到过 MAA 请求」区分：**那不算不可用**——等待对方来连正是
        本模块的正常状态，把它报成不可用会让用户以为坏了。
        """
        if not self._routes_registered:
            return "两个端点没注册上（当前 Context 没有 register_web_api），MAA 连不上"
        return None

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        """处理 `/ak maa [run|skip|cancel]`。

        宿主只把 `/ak` 之后的**第一个词**转给模块，所以子命令由本模块从
        `event.message_str` 自己解析——与 `skland` 的做法一致。
        """
        if command not in COMMAND_NAMES:
            return False

        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="操作 MAA 远程控制",
        )
        if not allowed:
            await self._reply(event, reason)
            return True

        # 记住这个会话：任务回报与超时告知都要发回这里。**在解析子命令之前记**，
        # 这样即使子命令写错了，我们也不会丢掉通知目标。
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo:
            self._umo = umo

        sub = self._parse_subcommand(str(getattr(event, "message_str", "") or ""))
        try:
            if sub in ("", "status"):
                await self._reply(event, self._status_text())
            elif sub == "run":
                await self._cmd_run(event)
            elif sub == "skip":
                await self._cmd_skip(event)
            elif sub == "cancel":
                await self._cmd_cancel(event)
            else:
                await self._reply(event, f"未知的 maa 子命令：{sub}\n{relay.HELP}")
        except Exception as exc:  # noqa: BLE001 - 见下方说明
            # **不让异常冒到宿主**：宿主接住后只会打一段栈 + 一句「出现异常」，
            # 用户看不懂、我们也丢掉了「是这条指令出的错」这个上下文。
            # 但也**绝不吞掉**（失败要显式）：① 记 ERROR 日志（含栈）② 回一句带原因的话。
            # （与 `skland` 同一处置；抽到 `core/` 做共享包装属于动地基，须单独裁决。）
            logger.exception("[ak_toolbox][maa] 处理子命令 %r 时出错", sub)
            await self._reply(
                event,
                f"这条指令出错了：{type(exc).__name__}: {exc}\n"
                "（已记进日志；换班提醒不受本模块影响，它照常工作。）",
            )
        return True

    @staticmethod
    def _parse_subcommand(message_str: str) -> str:
        """取 `/ak maa` 之后的第一个词；没有则返回空串（表示看状态）。"""
        tokens = message_str.strip().split()
        if tokens and tokens[0].lstrip("/").lower() == "ak":
            tokens = tokens[1:]
        if tokens and tokens[0].lower() == "maa":
            tokens = tokens[1:]
        return tokens[0].lower() if tokens else ""

    async def _cmd_run(self, event: Any) -> None:
        """`/ak maa run`：排一个任务。**这是唯一会让 MAA 跑起来的入口。**"""
        slot = self._manual_slot()
        result = self._queue.enqueue(slot, task_type=self._task_type)
        if result.outcome is EnqueueOutcome.CREATED:
            logger.info(
                "[ak_toolbox][maa] 已排队任务 %s（%s / %s），等 MAA 取走",
                result.task_id[:8],
                slot.label,
                tasks.task_type_label(self._task_type),
            )
            await self._reply(event, relay.queued_text(result))
        elif result.outcome is EnqueueOutcome.ALREADY_PENDING:
            await self._reply(event, relay.already_pending_text(result))
        else:
            await self._reply(event, relay.already_done_text(result))

    def _withdraw(self) -> tuple[PendingTask | None, bool]:
        """撤掉待结任务；**若它已被 MAA 取走，补发一条停止指令**。

        两处调用（`skip` 与 `cancel`）共用这一段：它们对用户是两件事，对 MAA 是
        同一件事——「别跑那一个」。分开写迟早会有一边忘了补发，而**忘了补发不会报错**，
        只会让用户以为拦住了（`skip` 的旧文案就是这么错的：它无条件保证「索引不会前进」）。

        Returns:
            (被撤掉的任务, 是否发了停止指令)
        """
        cancelled = self._queue.cancel()
        if cancelled is None or cancelled.first_fetch_at is None:
            return cancelled, False
        control = self._queue.enqueue_control(tasks.STOP_TASK)
        logger.info(
            "[ak_toolbox][maa] 任务 %s 已被取走，已下发停止指令（id %s）",
            cancelled.task_id[:8],
            control["id"][:8],
        )
        return cancelled, True

    async def _cmd_skip(self, event: Any) -> None:
        """`/ak maa skip`：「这一班我自己换」。

        做两件事：**撤掉待取任务**（用户改主意了，不让 MAA 跑），以及**记下这个时间点**。
        为什么记：MAA 的计划索引与用户的手动操作是两套东西——用户手动换过班之后又让
        MAA 跑，它会在「它以为的下一班」上再换一次，与手动结果错位。记下时间点，
        排查「班次对不上」时才知道该往哪儿看。
        """
        cancelled, stop_sent = self._withdraw()
        self._last_manual_at = datetime.now()
        logger.info(
            "[ak_toolbox][maa] 用户选择自己换班（%s）；待取任务：%s%s",
            self._last_manual_at.strftime("%m-%d %H:%M:%S"),
            "已撤掉" if cancelled else "无",
            "（并发出了停止指令）" if stop_sent else "",
        )
        await self._reply(
            event,
            relay.skipped_text(cancelled.slot_label if cancelled else "", stop_sent=stop_sent),
        )

    async def _cmd_cancel(self, event: Any) -> None:
        """`/ak maa cancel`：撤掉任务；**已经被取走的话还必须下发停止指令**。

        为什么必须补上那一步：任务一进队列，MAA 通常**在 1 秒内**就把它领走了
        （现场实测：排队 14:06:55.665、被取走 14:06:55.668，**只隔 3 毫秒**）。
        所以「只撤队列」删掉的只是我们手里的记录，**MAA 那边照跑不误**——
        这正是用户报的「id dc34e29b 的任务撤销了依旧执行」。

        判据用 `first_fetch_at`：它记着这条任务**有没有被取走过**，不需要另加状态。
        """
        cancelled, stop_sent = self._withdraw()
        if not stop_sent:
            logger.info("[ak_toolbox][maa] 用户撤掉了待取任务：%s", "有" if cancelled else "无")
        await self._reply(event, relay.cancelled_text(cancelled, stop_sent=stop_sent))

    def _manual_slot(self) -> Slot:
        """手动触发用的 slot，**按分钟粒度**。

        为什么不是秒级：手抖连发两条消息会让 MAA 连跑两趟，而**每多跑一趟，班次就多
        前进一格**。同一分钟内重复触发会被判成「已经派过」，一分钟之后再派则是新任务
        ——用户明确要求过，就该真的跑。
        """
        moment = datetime.now()
        return Slot(
            key=moment.strftime("manual-%Y%m%d-%H%M"),
            label=moment.strftime("手动触发 %m-%d %H:%M"),
        )

    # --- Web API ------------------------------------------------------------

    def _register_web_api(self, ctx: Any) -> None:
        """注册两个端点。

        由**模块自己**注册，宿主不参与（宿主眼里只有「模块」，它不认识 MAA）。
        `register_web_api` 对「同路由 + 同方法」是替换语义，所以插件重载不会堆出
        重复路由。
        """
        register = getattr(ctx, "register_web_api", None)
        if register is None:
            self._routes_registered = False
            return

        register(
            f"{WEB_ROUTE_PREFIX}{protocol.GET_TASK_SUBPATH}",
            self._web_get_task,
            ["POST"],
            "MAA 远程控制：领取任务",
        )
        register(
            f"{WEB_ROUTE_PREFIX}{protocol.REPORT_SUBPATH}",
            self._web_report_status,
            ["POST"],
            "MAA 远程控制：汇报任务状态",
        )
        self._routes_registered = True

    async def _web_get_task(self) -> Any:
        """领任务端点。**响应只来自我们自己的队列状态。**

        ⚠️ **这是本项目最危险的一处代码。** 改它之前请读完这段：

        这个端点**必须是匿名可达的**——MAA 只能填两个普通 URL，没法带自定义请求头，
        所以任何人都能 POST 它。当前的安全性质有两条：

        1. **响应不读请求体里的任何字段**：任务取自 `self._queue`，而队列只由用户在
           本机发的 `/ak maa run` 填充。请求里自报的 `user` / `device` **概不采信**
           ——那是谁都能编的字段（官方范例工作流也提醒过这件事）。
        2. **没有用户确认，队列永远是空的。**

        任何「按请求内容决定下发什么任务」的改动，都会把它变成一条**可被远程触发的
        执行通道**。真到那一步，必须先有**真正的身份校验**（例如按请求体里的标识去查
        **我们预先登记过的**设备）。

        「不读请求」这件事是可测的：`tests/test_maa_module.py` 有一条用例断言
        **不同的请求体得到同一个响应**。
        """
        try:
            payload = await request.json(default=None)
            self._record(protocol.GET_TASK_KIND, payload)
        except Exception as exc:  # noqa: BLE001 - 统一转成明确的响应，见下
            # 给 MAA 一个**明确**的响应而不是让它超时：超时会被误判成证书/网络问题，
            # 而真相是我们自己出错了（2026-09-29 那类「错误码指不到真因」的教训）。
            self._note_failure(protocol.GET_TASK_KIND, exc)
            return error_response("内部错误：登记请求失败，详见插件日志", status_code=500)

        delivered = self._queue.take_for_delivery()
        pending = self._queue.pending
        if delivered and pending is not None and pending.fetch_count == 1:
            # 只在**第一次**被取走时记一行：这个端点一秒被调一次，每次都记会把日志刷满。
            logger.info(
                "[ak_toolbox][maa] 任务 %s（%s）已被 MAA 取走，开始执行",
                pending.task_id[:8],
                pending.slot_label,
            )
        return json_response(protocol.tasks_response(delivered))

    async def _web_report_status(self) -> Any:
        """汇报端点：结清任务，并把结果**如实**转达给用户。

        MAA 不读回执内容、也不校验状态码（官方原文），所以回什么都可以；我们回一个
        合法 JSON，便于人工用 curl 试。

        **结清与「跑成没跑成」无关**：协议明说它通常不论成败都报 `SUCCESS`，
        所以这里只做「这条任务处理完了」，措辞由 `relay.report_text` 负责。
        """
        try:
            payload = await request.json(default=None)
            self._record(protocol.REPORT_KIND, payload)
        except Exception as exc:  # noqa: BLE001 - 同上
            self._note_failure(protocol.REPORT_KIND, exc)
            return error_response("内部错误：登记汇报失败，详见插件日志", status_code=500)

        task_id = protocol.extract_task_id(payload)
        status = protocol.extract_status(payload)
        # 先取 label 再 ack：ack 会把未结任务清掉，之后就取不到了。
        pending = self._queue.pending
        slot_label = (
            pending.slot_label if pending is not None and pending.task_id == task_id else ""
        )
        outcome = self._queue.ack(task_id)

        if outcome is AckOutcome.ALREADY:
            # 重复汇报：不回执第二次，免得给用户刷两条一样的消息。
            logger.info("[ak_toolbox][maa] 收到重复汇报（id %s），已忽略", task_id[:8])
            return json_response(protocol.report_ack_response())

        if outcome is AckOutcome.UNKNOWN:
            logger.warning("[ak_toolbox][maa] 收到对不上号的任务回报（id %r）", task_id[:8])
            await self._notify(relay.unknown_report_text(task_id))
            return json_response(protocol.report_ack_response())

        logger.info(
            "[ak_toolbox][maa] 任务 %s 已结清：status=%s（协议通常不论成败都报 SUCCESS）",
            task_id[:8],
            status or "（未给）",
        )
        await self._notify(
            relay.report_text(
                status=status,
                slot_label=slot_label,
                payload_shape=self._last_body_shape,
            )
        )
        return json_response(protocol.report_ack_response())

    # --- 定时任务 -----------------------------------------------------------

    async def _register_sweep_job(self) -> None:
        """注册过期巡检。

        取不到 `cron_manager` 只意味着「超时不会主动告知」，**派任务本身照常**，
        所以这里明确降级并记 WARNING，而不是让模块起不来。
        """
        cron_manager = getattr(self._ctx, "cron_manager", None)
        if cron_manager is None:
            logger.warning(
                "[ak_toolbox][maa] 取不到 cron_manager：过期巡检未注册，"
                "任务超时（电脑没开／取走没回报）不会主动告知。派任务本身不受影响。"
            )
            return

        # 先清掉自己遗留的，再注册——可重入，与 shift_reminder 同一做法。
        purged = await self._purge_jobs(cron_manager)
        if purged:
            logger.info("[ak_toolbox][maa] 已清理 %d 个历史定时任务", purged)
        try:
            job = await cron_manager.add_basic_job(
                name=SWEEP_JOB_NAME,
                cron_expression=SWEEP_CRON,
                handler=self._sweep,
                description="MAA 任务过期巡检（每 5 分钟；超时会主动告知用户）",
                timezone=SWEEP_TIMEZONE,
                payload={},
            )
        except Exception:
            logger.exception(
                "[ak_toolbox][maa] 注册过期巡检失败：任务超时不会主动告知，派任务本身不受影响"
            )
            return
        self._job_ids = [job.job_id]

    async def _purge_jobs(self, cron_manager: Any | None = None) -> int:
        """按前缀删掉自己注册的 job。**可重入**：`initialize` 与 `terminate` 都走它。"""
        manager = (
            cron_manager if cron_manager is not None else getattr(self._ctx, "cron_manager", None)
        )
        if manager is None:
            return 0
        try:
            jobs = list(await manager.list_jobs())
        except Exception:
            logger.exception("[ak_toolbox][maa] 列出定时任务失败，跳过清理")
            return 0
        removed = 0
        for job in jobs:
            name = str(getattr(job, "name", "") or "")
            job_id = str(getattr(job, "job_id", "") or "")
            if not name.startswith(JOB_PREFIX):
                continue
            try:
                await manager.delete_job(job_id)
                removed += 1
            except Exception:
                logger.exception("[ak_toolbox][maa] 删除定时任务 %s 失败", job_id)
        return removed

    async def _sweep(self, **_: Any) -> None:
        """过期巡检：把「没跑成」的两类情况**主动告诉用户**（不许静默）。"""
        for notice in self._queue.sweep():
            logger.warning(
                "[ak_toolbox][maa] 任务作废（%s）：%s，等待 %d 分钟",
                notice.reason.value,
                notice.slot_label,
                notice.waited_minutes,
            )
            await self._notify(relay.timeout_text(notice))

    # --- 记录 ---------------------------------------------------------------

    def _record(self, kind: str, payload: object) -> None:
        """记下这次到达。**只记形状，不记值**（渲染全在 `protocol` 那一层）。

        **日志分档（节流）**：MAA 每秒来一次，逐次写 INFO 会变成一天 86400 条，
        把「首次连接」「任务被取走」「回报到达」「超时作废」这些**真正要看见的事**
        全埋掉。所以这里：

        * **首次到达** → INFO（它是「公网可达 + 证书被接受 + 轮询已开始」的
          唯一证据，也是这个模块最初存在的理由）；
        * **每满一段时间** → INFO 一条摘要，回答「它还连着吗」；
        * **其余每一次** → DEBUG，排查时可开，平时不出声。

        ⚠️ **限流只覆盖这一处**。任务派发、取走、回报、作废、停止指令、任何
        ERROR 都在各自的调用点记日志，**一条都不会被这里吃掉**——那是本模块的
        可观测性底线。
        """
        body_shape = protocol.describe_payload(payload)
        summary = protocol.describe_request(
            kind=kind,
            method=_attr("method"),
            path=_attr("path"),
            client_host=_attr("client_host"),
            user_agent=_header("user-agent"),
        )

        now = self._now()
        self._arrivals += 1
        self._heartbeat_arrivals += 1
        self._last_seen = now
        self._last_summary = summary
        self._last_body_shape = body_shape
        if kind == protocol.GET_TASK_KIND:
            self._get_task_hits += 1
        else:
            self._report_hits += 1
            self._last_status = protocol.extract_status(payload)

        if self._arrivals == 1:
            # 第一条到达就是「证书这关过了」的证据——**刻意用 INFO 并且措辞醒目**，
            # 因为它正是这个模块最初存在的理由。
            logger.info(
                "[ak_toolbox][maa] ✅ 首次收到 MAA 请求，连接是通的"
                "（公网可达 + 证书被接受 + 轮询已开始）。"
            )
            # 从这一刻开始计周期；紧接着的那一次不进摘要（首次那条已经报告了它）。
            self._heartbeat_at = now
            self._heartbeat_arrivals = 0
        elif self._heartbeat_at is None:
            # 理论上到不了（首次必设基准）。真到得了也不能沉默——用当下当基准，
            # 一个周期之后就会开始出摘要，而不是永远不出。
            self._heartbeat_at = now
        elif (now - self._heartbeat_at).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS:
            logger.info(
                "[ak_toolbox][maa] 持续连接中：过去约 %d 分钟收到 %d 次请求"
                "（累计 %d 次；最近一次 %s）。",
                int((now - self._heartbeat_at).total_seconds() // 60),
                self._heartbeat_arrivals,
                self._arrivals,
                now.strftime("%m-%d %H:%M:%S"),
            )
            self._heartbeat_at = now
            self._heartbeat_arrivals = 0

        logger.debug("[ak_toolbox][maa] 收到请求：%s；请求体 %s", summary, body_shape)

    def _note_failure(self, kind: str, exc: BaseException) -> None:
        self._failures += 1
        label = "领任务" if kind == protocol.GET_TASK_KIND else "汇报状态"
        logger.error(
            "[ak_toolbox][maa] 处理%s请求时出错（第 %d 次）：%s: %s",
            label,
            self._failures,
            type(exc).__name__,
            exc,
        )

    # --- 文本 ---------------------------------------------------------------

    def _status_text(self) -> str:
        lines = ["【MAA】" + self._arrival_line()]
        if self._arrivals:
            lines.append(f"领任务 {self._get_task_hits} 次 / 汇报 {self._report_hits} 次")
            if self._last_status:
                lines.append(
                    f"最近汇报 status={self._last_status}"
                    "（协议通常不论成败都报 SUCCESS，不能当「班换对了」的凭证）"
                )
        lines.append(f"任务类型：{tasks.task_type_label(self._task_type)}")

        snap = self._queue.snapshot()
        if snap.has_pending:
            state = f"已被取走 {snap.fetch_count} 次" if snap.first_fetch_at else "还没被取走"
            lines.append(f"队列：1 个待结任务（{snap.pending_slot_label}，{state}）")
        elif snap.created_total:
            lines.append(
                f"队列：空（累计派过 {snap.created_total} 个，结清 {snap.acked_total}，"
                f"作废 {snap.expired_total}）"
            )
        else:
            lines.append("队列：空（还没派过任务——要派就发 /ak maa run）")
        if self._last_manual_at:
            lines.append(f"最近一次「我自己换」：{self._last_manual_at.strftime('%m-%d %H:%M:%S')}")
        if self._failures:
            lines.append(f"处理出错 {self._failures} 次，详见插件日志")

        lines.append("")
        lines.append("只在**你确认之后**才派任务；没确认，队列永远是空的。")
        lines.append("它每跑完一次，班次就按 MAA 自己的规则前进一格——一次换班只派一次。")
        lines.append(
            "「到点自动问你」**还没做**：那要读换班提醒的时刻，而模块拿不到"
            "（架构限制，已记录待裁决）。现在只能手动触发。"
        )
        return "\n".join(lines)

    def _arrival_line(self) -> str:
        if self._last_seen is None:
            return "还没收到过请求（MAA 侧还没配好，或电脑没开）"
        stamp = self._last_seen.strftime("%m-%d %H:%M:%S")
        return f"已收到 {self._arrivals} 次请求，最近一次 {stamp}"

    async def _reply(self, event: Any, text: str) -> None:
        await self._send(event.unified_msg_origin, text)

    async def _notify(self, text: str) -> bool:
        """把一条**用户必须看到**的话发给最近一次用过的会话。

        发不出去时**必须留痕**：记 WARNING 并把内容写进日志。通知丢失不能是静默的
        ——这个模块存在的意义之一就是「跑没跑成要让人知道」。
        """
        if not self._umo:
            logger.warning(
                "[ak_toolbox][maa] 没有通知目标（还没人发过 /ak maa），这条消息只留在日志里：%s",
                text,
            )
            return False
        return await self._send(self._umo, text, notify=True)

    async def _send(self, umo: str, text: str, *, notify: bool = False) -> bool:
        if self._ctx is None:
            logger.warning("[ak_toolbox][maa] 模块尚未初始化，无法发送：%s", text)
            return False
        try:
            sent = await self._ctx.send_message(umo, MessageChain().message(text))
        except Exception:  # noqa: BLE001 - 发送失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][maa] 发送异常（%s）", "通知" if notify else "回执")
            return False
        if not sent:
            logger.warning(
                "[ak_toolbox][maa] 发送失败（%s，会话可能已失效）：%s",
                "通知" if notify else "回执",
                text,
            )
            return False
        return True
