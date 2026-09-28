"""测试环境准备：注入一个**最小 stub 的 `astrbot`**。

为什么需要它
------------
`modules/*/module.py` 是**框架胶水层**，会 `import astrbot`；而注册表的自动发现
（`core.registry.discover_modules`）必须 import 它才能找到 `Module` 子类。
本机与 CI 都没有 AstrBot 运行时，所以这里用最小 stub 顶上。

它**只补被 import 到的符号**（logger / MessageChain / 数据目录函数），
不模拟框架行为——被测的纯逻辑层依旧零框架依赖，注册表与基类也照旧不 import 框架。

副作用仅限 `sys.modules`，不落任何文件。
"""

import sys
import types
from pathlib import Path


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
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

    def get_astrbot_plugin_data_path() -> str:
        """默认给一个不会污染仓库的位置；需要时由测试 monkeypatch 覆盖。"""
        return str(Path.cwd() / ".pytest-plugin-data")

    api.logger = _StubLogger()
    event.MessageChain = MessageChain
    astrbot_path.get_astrbot_plugin_data_path = get_astrbot_plugin_data_path

    astrbot.api = api
    astrbot.core = core
    api.event = event
    core.utils = utils
    utils.astrbot_path = astrbot_path

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.core": core,
            "astrbot.core.utils": utils,
            "astrbot.core.utils.astrbot_path": astrbot_path,
        }
    )


_install_astrbot_stub()
