"""AstrBot 明日方舟工具箱 —— 插件入口。

入口只做三件事：读配置、装载模块、把指令转发出去。
**宿主不认识任何具体功能**——它不知道「三班」是什么，只知道「模块」。
这一条守住了，以后加模块才不用回来改入口（见 docs/architecture/scope.md §2）。

这是框架胶水层：允许 import astrbot，但不做单测（见 docs/architecture/rules.md §6）。
"""

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.registry import ModuleRegistry, build_registry, read_module_switches


class ArknightsToolbox(Star):
    """明日方舟工具箱：宿主插件。

    配置结构约定：``modules`` 段声明各模块开关，形如
    ``{"modules": {"<模块名>": true}}``；缺省表示一个模块都不开。
    """

    def __init__(self, context: Context, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self._config: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        self._registry: ModuleRegistry | None = None

    async def initialize(self) -> None:
        """插件被激活时调用：解析开关 → 装载模块 → 启动。"""
        switches = read_module_switches(self._config)
        self._registry = build_registry(switches)
        await self._registry.start_all(self.context)
        loaded = "、".join(self._registry.enabled_names) or "（无）"
        logger.info(f"[ak_toolbox] 已装载模块：{loaded}")

    async def terminate(self) -> None:
        """插件被停用或重载时调用：先停模块，再释放自身引用。

        框架只在插件定义了 ``terminate`` 时才会 await 它（见实测记录），
        所以这里是唯一的清理时机。
        """
        if self._registry is not None:
            await self._registry.stop_all()
            logger.info("[ak_toolbox] 模块已全部停止")
            self._registry = None

    @filter.command("ak")
    async def ak(self, event: AstrMessageEvent):
        """明日方舟工具箱：查看状态与执行子命令。"""
        yield event.plain_result("工具箱已加载，功能尚未实现。")
