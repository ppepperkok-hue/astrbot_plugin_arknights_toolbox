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

import sys
import types
from pathlib import Path
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
        """最小消息链：只记住文本，够装配层调用与断言。"""

        def __init__(self) -> None:
            self.parts: list[str] = []

        def message(self, text: str) -> "MessageChain":
            self.parts.append(text)
            return self

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

    class PluginUploadFile:
        """够用的上传文件占位：只保留 `read`、`filename` 与体积信息。

        真实的 `PluginUploadFile` 由 AstrBot 包装 Starlette 的上传对象；这里只实现
        装配层用到的部分（`read()` 读字节 + `read(size)` 截断，供体积上限判断）。
        """

        def __init__(self, data: bytes = b"", filename: str = "uploaded.json") -> None:
            self._data = data
            self.filename = filename
            self.content_type = "application/json"
            self.content_length = len(data)

        async def read(self, size: int = -1) -> bytes:
            return self._data if size < 0 else self._data[:size]

    class _StubFiles(dict):
        """够用的上传容器：`files.get("file")` 是装配层唯一的用法。"""

    class _StubRequest:
        """够用的 request 代理：只补被 import 到的 `query` / `json` / `files`。

        `json()` 默认返回 `default`（等同空请求体）；需要给请求体的测试用
        `monkeypatch.setattr(_StubRequest, "json", ...)` 覆盖即可。
        `files()` 同理，默认是**空的上传**——测试想模拟上传时 monkeypatch 它。
        """

        query = _StubQuery()

        async def json(self, default: Any = None) -> Any:
            return default

        async def files(self) -> Any:
            return _StubFiles()

        async def form(self) -> Any:
            return {}

    def get_astrbot_plugin_data_path() -> str:
        """默认给一个不会污染仓库的位置；需要时由测试 monkeypatch 覆盖。"""
        return str(Path.cwd() / ".pytest-plugin-data")

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
    web.PluginUploadFile = PluginUploadFile
    astrbot_path.get_astrbot_plugin_data_path = get_astrbot_plugin_data_path

    star = types.ModuleType("astrbot.api.star")
    star.Star = Star
    star.Context = Context

    astrbot.api = api
    astrbot.core = core
    api.event = event
    api.web = web
    api.star = star
    core.utils = utils
    utils.astrbot_path = astrbot_path

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.web": web,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.utils": utils,
            "astrbot.core.utils.astrbot_path": astrbot_path,
        }
    )


_install_astrbot_stub()
