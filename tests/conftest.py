"""测试环境准备：注入一个**最小 stub 的 `astrbot`**。

为什么需要它
------------
`modules/*/module.py` 是**框架胶水层**，会 `import astrbot`；而注册表的自动发现
（`core.registry.discover_modules`）必须 import 它才能找到 `Module` 子类。
本机与 CI 都没有 AstrBot 运行时，所以这里用最小 stub 顶上。

它**只补被 import 到的符号**（logger / MessageChain / 数据目录函数 / Web API 助手），
不模拟框架行为——被测的纯逻辑层依旧零框架依赖，注册表与基类也照旧不 import 框架。

副作用仅限 `sys.modules`，不落任何文件。
"""

import base64
import sys
import tempfile
import types
from typing import Any


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    web = types.ModuleType("astrbot.api.web")
    core = types.ModuleType("astrbot.core")
    utils = types.ModuleType("astrbot.core.utils")
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")

    class _StubLogger:
        """够用的假 logger：任何级别都吞掉，不写文件。"""

        def _noop(self, *args, **kwargs) -> None:
            return None

        debug = info = warning = error = exception = critical = _noop

    class MessageChain:
        """最小消息链：记住各段（文本或组件），够装配层调用与断言。

        可以无参构造（`MessageChain().message(text)`），也可以直接给一段组件列表
        （`MessageChain([Plain(...), Image.fromBytes(...)])`）——后者是森空岛模块
        发二维码用的形态。
        """

        def __init__(self, parts: Any = None) -> None:
            self.parts: list[Any] = list(parts) if parts else []

        def message(self, text: str) -> "MessageChain":
            self.parts.append(text)
            return self

    class Plain:
        """够用的文本组件：只记住文本。"""

        def __init__(self, text: str = "") -> None:
            self.text = text

    class Image:
        """够用的图片组件。

        形状**照真实实现**（已核实 `astrbot/core/message/components.py:501-533`）：
        `fromBytes` 走 base64，最终 `file` 是 `base64://...`。测试据此断言，
        不需要真的解码图片。
        """

        def __init__(self, file: str = "") -> None:
            self.file = file

        @staticmethod
        def fromBase64(data: str) -> "Image":
            return Image(f"base64://{data}")

        @staticmethod
        def fromBytes(data: bytes) -> "Image":
            return Image.fromBase64(base64.b64encode(data).decode())

    class AstrMessageEvent:
        """够用的假事件：宿主与模块只用到这几个属性/方法。"""

        def __init__(self, message_str: str = "", umo: str = "test:FriendMessage:1") -> None:
            self.message_str = message_str
            self.unified_msg_origin = umo
            self.stopped = False

        def plain_result(self, text: str) -> str:
            return text

        def stop_event(self) -> None:
            self.stopped = True

        def is_private_chat(self) -> bool:
            return True

        def is_admin(self) -> bool:
            return False

    class MessageEventResult:
        """占位：`main.py` 只把它用作返回类型注解。"""

    class _StubFilter:
        """够用的 filter：`command` 装饰器原样返回函数（不注册任何东西）。"""

        @staticmethod
        def command(*_args: Any, **_kwargs: Any):
            def decorator(func: Any) -> Any:
                return func

            return decorator

    class Star:
        """够用的 Star 基类：只保留 `__init__` 的签名与 context。"""

        def __init__(self, context: Any = None, config: Any = None) -> None:
            self.context = context
            self.config = config

    class Context:
        """占位类型（真实的 Context 由 AstrBot 提供）。"""

    class _StubQuery:
        """够用的 query 代理：装配层只用 `get`。"""

        def get(self, key: str, default: Any = None) -> Any:
            return default

    class _StubRequest:
        """够用的 request 代理：只补被 import 到的 `query` / `json`。

        `json()` 默认返回 `default`（等同空请求体）；需要给请求体的测试用
        `monkeypatch.setattr(web.request, "json", ...)` 覆盖即可——上传正是走这条。

        这里曾经还有 `files()` / `form()`：那是 multipart 上传时代的符号。而
        multipart 在真实 bridge 上**根本走不通**（`FormData` 不能被结构化克隆，
        浏览器直接抛错），上传已整体改为 base64 + JSON POST，这两个符号随之删除，
        不留下"以后可能要用"的死代码。
        """

        query = _StubQuery()

        async def json(self, default: Any = None) -> Any:
            return default

    def get_astrbot_plugin_data_path() -> str:
        """默认给一个**仓库之外**的位置；需要时由测试 monkeypatch 覆盖。

        这里原先返回 `Path.cwd() / ".pytest-plugin-data"`，而它的注释写着"不会污染
        仓库"——**注释与代码说的是两件事**（那个目录就在仓库里，且不在 `.gitignore`
        里）。2026-09-29 仓库里真的留下了未跟踪的 `.pytest-plugin-data/`（含模块
        状态）。现在退到系统临时目录：即使某条路径绕过了下面的隔离夹具，也只会写在
        仓库外面。
        """
        return tempfile.mkdtemp(prefix="ak-toolbox-test-data-")

    def json_response(payload: Any) -> Any:
        """原样返回，便于测试直接断言 handler 组装出来的数据。"""
        return payload

    def error_response(message: str, status_code: int = 400) -> Any:
        """与 json_response 同理：只保留可断言的信息，不模拟框架响应对象。"""
        return {"error": message, "status_code": status_code}

    api.logger = _StubLogger()
    event.MessageChain = MessageChain
    event.AstrMessageEvent = AstrMessageEvent
    event.MessageEventResult = MessageEventResult
    event.filter = _StubFilter()
    web.json_response = json_response
    web.error_response = error_response
    web.request = _StubRequest()
    astrbot_path.get_astrbot_plugin_data_path = get_astrbot_plugin_data_path

    star = types.ModuleType("astrbot.api.star")
    star.Star = Star
    star.Context = Context

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = Plain
    components.Image = Image

    astrbot.api = api
    astrbot.core = core
    api.event = event
    api.web = web
    api.star = star
    api.message_components = components
    core.utils = utils
    utils.astrbot_path = astrbot_path

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.web": web,
            "astrbot.api.star": star,
            "astrbot.api.message_components": components,
            "astrbot.core": core,
            "astrbot.core.utils": utils,
            "astrbot.core.utils.astrbot_path": astrbot_path,
        }
    )


_install_astrbot_stub()


try:  # pragma: no cover - 没有 pytest 时（例如只跑加载形态脚本）跳过夹具定义
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


if pytest is not None:

    @pytest.fixture(autouse=True)
    def _isolate_plugin_data(tmp_path, monkeypatch):
        """把**插件数据目录**指到本次测试的临时目录：任何测试都不许写进仓库。

        为什么需要它：`modules/*/module.py` 会在运行时往 `plugin_data/` 写东西
        （绑定目标、去重记录、上传的排班表、共享班次表）。而上面 stub 给的默认值是
        **仓库内**的 `.pytest-plugin-data`，那个路径**不在** `.gitignore` 里。
        于是"顺手写一点状态"就会在仓库里留下未跟踪文件——`AGENTS.md` §5 第 6 条
        要求的测试隔离并没有被真正保证（2026-09-29 发现的隐患）。

        两层都要换：stub 里的那份（供**之后**才 import 的模块取用），以及**已经**
        import 过的每个 `modules.*` 模块里的同名属性——`from ... import x` 在 import
        那一刻就把名字抄进了模块命名空间，改 stub 影响不到它。

        显式把数据目录指到 `tmp_path` 的用例不受影响：它们本来指的就是同一个目录。
        """
        target = str(tmp_path)
        stub = sys.modules.get("astrbot.core.utils.astrbot_path")
        if stub is not None:
            monkeypatch.setattr(stub, "get_astrbot_plugin_data_path", lambda: target, raising=False)
        for name, module in list(sys.modules.items()):
            if name.startswith("modules.") and hasattr(module, "get_astrbot_plugin_data_path"):
                monkeypatch.setattr(module, "get_astrbot_plugin_data_path", lambda: target)
        yield
