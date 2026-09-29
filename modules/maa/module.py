"""MAA 远程控制模块 —— **最小可验证端点**。

这个模块当前只做一件事：**让 MAA 连上我们，并让「它连上了」成为一件可见的事实。**

它**不执行任何任务**：领任务端点无论收到什么，都回一个**常量**空任务列表
（`protocol.empty_tasks_response`）。这是刻意的，安全理由写在
:meth:`MaaModule._web_get_task` 的注释里——**将来加真实任务时，那里是本项目最
危险的一处代码**。

为什么先做这一步（而不是直接做「到点让 MAA 跑一次」）：

1. 需求本身还没定论——用户机器上的 MAA **已经在定时自动换班**（04:00 / 23:00，
   用的正是他从 riic.autos 导出的三班表），「到点自动跑」这件事早就成立了。
2. 而这条链路有三个**只能实测**的未知：公网能否到达、**MAA（.NET）认不认我们的
   自签证书**、以及它实际的轮询行为。三个都不需要任何换班逻辑就能验掉。

所以本模块的产出是**证据**，不是功能。

协议依据：https://docs.maa.plus/zh-tw/protocol/remote-control-schema.html
（要点逐条抄在 `protocol.py` 的模块 docstring 里，含原文措辞。）
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.web import error_response, json_response, request

# 两种运行场景的导入差异（与 modules/recruit/module.py 同一取舍）：
#   * AstrBot 加载时插件是一个包，插件根目录不在 sys.path 上 → 只能用父级相对导入；
#   * 从仓库根跑 pytest 时顶层包是 `modules`，`...` 会越界 → 必须用绝对导入。
# 先绝对、失败再回退相对，注册表的自动发现才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.module import Module
    from core.permission import session_allowed
except ImportError:  # pragma: no cover
    from ...core.module import Module
    from ...core.permission import session_allowed

from . import protocol

COMMAND_NAMES = ("maa",)

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"

#: 两个端点在本插件路由前缀下的路径。**必须带插件名前缀**，否则 Dashboard 的
#: 转发匹配不到（`/api/v1/plugins/extensions/<plugin>/<这里>`）。
WEB_ROUTE_PREFIX = f"/{PLUGIN_NAME}/maa"

#: 面板 API Key 的查询参数名。已核实 AstrBot 4.28.1 的
#: `dashboard/api/auth.py:_extract_raw_api_key` 接受 `?api_key=` 与 `?key=` 两种写法
#: ——**这一点很关键**：MAA 只能填一个普通 URL，没法带自定义请求头，所以查询串是
#: 它唯一能携带凭据的方式。凭据由运维侧创建与保管，本模块不碰它。
API_KEY_QUERY = "api_key"


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
    """两个只回固定内容的端点 + 一份到达记录。"""

    name = "maa"
    config_key = "maa"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._routes_registered = False
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

    # --- 生命周期 -----------------------------------------------------------

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """注册两个端点。

        本模块**没有配置项**（除了总开关），所以 `config` 只用来占位——保留这个
        参数是为了满足契约；刻意不读它，避免造出一个没人用的配置面。
        """
        self._ctx = ctx
        self._register_web_api(ctx)
        if self._routes_registered:
            logger.info(
                "[ak_toolbox][maa] 模块已装载，等待 MAA 轮询（还没收到过请求）。"
                "换班提醒与其它模块不受本模块影响。"
            )
        else:
            # 这里**不抛**：端点没注册上只说明这个功能不可用，但它不该连累整个
            # 插件加载（换班提醒是核心功能）。不可用这件事通过
            # `unavailable_reason` 如实报告给宿主——那是契约给的出口。
            logger.error(
                "[ak_toolbox][maa] 端点注册失败，MAA 连不上我们。本模块不可用；其它模块不受影响。"
            )

    async def terminate(self) -> None:
        """清掉引用与统计；可安全重复调用。

        端点本身**没有反注册接口**（`register_web_api` 只在「同路由 + 同方法」时
        替换），所以插件卸载后这个对象上仍可能挂着已注册的绑定方法。那没有危害：
        两个处理函数返回的都是常量，且不读任何外部状态。
        """
        self._ctx = None
        self._routes_registered = False

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
        """`/ak maa` —— 看 MAA 连上来没有。"""
        if command not in COMMAND_NAMES:
            return False

        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="查看 MAA 连接状态",
        )
        if not allowed:
            await self._reply(event, reason)
            return True

        try:
            await self._reply(event, self._status_text())
        except Exception as exc:  # noqa: BLE001 - 见下方说明
            # **不让异常冒到宿主**：宿主接住后只会打一段栈 + 一句「出现异常」，
            # 用户看不懂、我们也丢掉了上下文。但也**绝不吞掉**（失败要显式）：
            # ① 记 ERROR 日志（含栈）② 回一句带原因的话。
            logger.exception("[ak_toolbox][maa] 处理 /ak maa 时出错")
            await self._reply(
                event,
                f"查看 MAA 状态失败：{type(exc).__name__}: {exc}\n"
                "把这句话原样发回来，日志里有完整调用栈。",
            )
        return True

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
            "MAA 远程控制：领取任务（当前恒为空）",
        )
        register(
            f"{WEB_ROUTE_PREFIX}{protocol.REPORT_SUBPATH}",
            self._web_report_status,
            ["POST"],
            "MAA 远程控制：汇报任务状态（当前只记日志）",
        )
        self._routes_registered = True

    async def _web_get_task(self) -> Any:
        """领任务端点。**永远回空任务列表。**

        ⚠️ **这是本项目最危险的一处代码。** 往这里加真实任务之前，请先读完这段：

        这个端点**必须是匿名可达的**——MAA 只能填两个普通 URL，没法带自定义请求头，
        所以任何人都能 POST 它。当前它**结构上不可能执行任何东西**，因为响应是
        `protocol.empty_tasks_response()` 返回的**常量**，**不读请求里的任何字段**。

        任何「根据请求内容决定下发什么任务」的改动，都会把它变成一条**可被远程
        触发的执行通道**。真到那一步，必须先有**真正的身份校验**（例如按请求体里的
        标识去查我们**预先登记过的**设备），而**绝不能**靠请求体里自报的 `user` /
        `device` 判断归属——那是攻击者可以随便填的字段。

        「不读请求」这件事本身是可测的：`tests/test_maa_module.py` 里有一条用例
        断言响应与请求内容无关。
        """
        try:
            payload = await request.json(default=None)
            self._record(protocol.GET_TASK_KIND, payload)
        except Exception as exc:  # noqa: BLE001 - 统一转成明确的响应，见下
            # 给 MAA 一个**明确**的响应而不是让它超时：超时会被误判成证书/网络问题，
            # 而真相是我们自己出错了（2026-09-29 那类「错误码指不到真因」的教训）。
            self._note_failure(protocol.GET_TASK_KIND, exc)
            return error_response("内部错误：登记请求失败，详见插件日志", status_code=500)
        return json_response(protocol.empty_tasks_response())

    async def _web_report_status(self) -> Any:
        """汇报端点。**只记一行日志**，不解析业务含义。

        MAA 不读回执内容、也不校验状态码（官方文档原文），所以回什么都可以；
        我们回一个合法 JSON，便于人工用 curl 试。
        """
        try:
            payload = await request.json(default=None)
            self._record(protocol.REPORT_KIND, payload)
        except Exception as exc:  # noqa: BLE001 - 同上
            self._note_failure(protocol.REPORT_KIND, exc)
            return error_response("内部错误：登记汇报失败，详见插件日志", status_code=500)
        return json_response(protocol.report_ack_response())

    # --- 记录 ---------------------------------------------------------------

    def _record(self, kind: str, payload: object) -> None:
        """记下这次到达。**只记形状，不记值**（渲染全在 `protocol` 那一层）。"""
        body_shape = protocol.describe_payload(payload)
        summary = protocol.describe_request(
            kind=kind,
            method=_attr("method"),
            path=_attr("path"),
            client_host=_attr("client_host"),
            user_agent=_header("user-agent"),
        )

        self._arrivals += 1
        self._last_seen = datetime.now()
        self._last_summary = summary
        self._last_body_shape = body_shape
        if kind == protocol.GET_TASK_KIND:
            self._get_task_hits += 1
        else:
            self._report_hits += 1
            self._last_status = protocol.extract_status(payload)

        if self._arrivals == 1:
            # 第一条到达就是「证书这关过了」的证据——**刻意用 INFO 并且措辞醒目**，
            # 因为它正是这个模块存在的理由。
            logger.info(
                "[ak_toolbox][maa] ✅ 首次收到 MAA 请求，连接是通的"
                "（公网可达 + 证书被接受 + 轮询已开始）。"
            )
        logger.info("[ak_toolbox][maa] 收到请求：%s；请求体 %s", summary, body_shape)

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
            if self._last_body_shape:
                lines.append(f"最近请求体形状：{self._last_body_shape}")
            if self._last_status:
                # 文档原文：通常不论成败皆汇报 SUCCESS —— 不解释这一点，
                # 用户会把 SUCCESS 当成「换班成功了」。
                lines.append(
                    f"最近汇报 status={self._last_status}"
                    "（协议通常不论成败都报 SUCCESS，别当成功凭证）"
                )
        if self._failures:
            lines.append(f"处理出错 {self._failures} 次，详见插件日志")
        lines.append("本模块目前只回空任务表：它证明连得上，**不会执行任何操作**。")
        lines.append(
            "要让它连上，得在 MAA 的「获取任务端点 / 汇报任务端点」里填两个 URL "
            f"（都要带 ?{API_KEY_QUERY}=<面板 API Key>）；具体地址见插件文档。"
        )
        return "\n".join(lines)

    def _arrival_line(self) -> str:
        if self._last_seen is None:
            return "还没收到过请求（MAA 侧还没配好，或电脑没开）"
        stamp = self._last_seen.strftime("%m-%d %H:%M:%S")
        return f"已收到 {self._arrivals} 次请求，最近一次 {stamp}"

    async def _reply(self, event: Any, text: str) -> None:
        await self._send(event.unified_msg_origin, text)

    async def _send(self, umo: str, text: str) -> None:
        if self._ctx is None:
            logger.warning("[ak_toolbox][maa] 模块尚未初始化，无法回执")
            return
        try:
            sent = await self._ctx.send_message(umo, MessageChain().message(text))
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][maa] 回执发送异常")
            return
        if not sent:
            logger.warning("[ak_toolbox][maa] 回执发送失败（会话可能已失效）")
