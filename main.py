"""AstrBot 明日方舟工具箱 —— 插件入口。

入口只做三件事：读配置、装载模块、把指令转发出去。
**宿主不认识任何具体功能**——它不知道「三班」是什么，只知道「模块」。
这一条守住了，以后加模块才不用回来改入口（见 docs/architecture/scope.md §2）。

这是框架胶水层：允许 import astrbot，但不做单测（见 docs/architecture/rules.md §6）。
"""

from collections.abc import AsyncGenerator, Mapping
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star

from .core.registry import ModuleRegistry, build_registry, discover_modules, read_module_switches


class ArknightsToolbox(Star):
    """明日方舟工具箱：宿主插件。

    配置结构约定：``modules`` 段声明各模块开关，形如
    ``{"modules": {"<模块名>": true}}``；缺省表示一个模块都不开。

    **注意**：``modules`` 段在 S1 的 `_conf_schema.json` 里**尚未暴露**
    （裁决 D2），由 S3 装配真实模块时再加。AstrBot 会删除 schema 未声明的配置键
    （``core/config/astrbot_config.py`` 的 ``check_config_integrity``，会打印
    ``Config key removed``），所以 S1 期间手写它也会在 ``initialize()`` 之前被抹掉。
    """

    def __init__(self, context: Context, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(context, config)
        self._config: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        self._registry: ModuleRegistry | None = None

    async def initialize(self) -> None:
        """插件被激活时调用：解析开关 → 装载模块 → 启动。

        启动中途失败时**必须自己回滚**已经起来的模块：框架在 ``initialize``
        抛异常时只做状态清理，**不会调用** ``terminate()``（已核实 AstrBot 4.28.1
        ``core/star/star_manager.py:1421/1436/1452``；``terminate()`` 仅见于
        ``:1963`` 的停用/重载路径）。不回滚就会留下已注册定时任务的"半个模块"。
        """
        self._registry = None
        # 自动发现 modules/ 下的模块包。宿主**不认识任何具体功能**（架构红线，
        # 见 docs/architecture/scope.md §2）：新增模块只需往 modules/ 加一个子包。
        # 发现阶段出问题要当场抛，绝不静默少装一个模块。
        discover_modules()
        # 未知模块名在这一步就抛错，此时还没有任何模块被启动，无需回滚
        registry = build_registry(read_module_switches(self._config))
        try:
            await registry.start_all(self.context, self._config)
        except BaseException:
            try:
                await registry.stop_all()
            except Exception:  # noqa: BLE001 - 回滚失败要留痕，但不能盖掉原始异常
                logger.exception("[ak_toolbox] 启动失败后回滚模块时又出错")
            raise
        self._registry = registry
        loaded = "、".join(registry.enabled_names) or "（无）"
        logger.info(f"[ak_toolbox] 已装载模块：{loaded}")

    async def terminate(self) -> None:
        """插件被停用或重载时调用：先停模块，再释放自身引用。

        框架只在插件定义了 ``terminate`` 时才会 await 它（见实测记录），
        所以这里是唯一的清理时机。引用先摘掉再 await，避免清理抛错时留下
        半释放状态。
        """
        registry, self._registry = self._registry, None
        if registry is None:
            return
        await registry.stop_all()
        logger.info("[ak_toolbox] 模块已全部停止")

    @filter.command("ak")
    async def ak(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
        """明日方舟工具箱：解析子命令并转发给各模块。

        无参数时默认 ``status``。宿主**不认识任何子命令**，只负责分发；
        全部模块都返回 ``False`` 时明确回「未知子命令」并列出已装载模块，
        不静默吞掉（项目宪法 §2 第 2 条）。
        """
        subcommand = self._parse_subcommand(event.message_str)
        registry = self._registry
        if registry is not None:
            for module in registry:
                if await module.handle_command(subcommand, event):
                    return
        loaded = "、".join(registry.enabled_names) if registry is not None else "（无）"
        yield event.plain_result(
            f"未知子命令：{subcommand}\n当前已装载的模块：{loaded}\n"
            "可用子命令由各模块提供，见各模块文档。"
        )

    @staticmethod
    def _parse_subcommand(message_str: str) -> str:
        """从消息文本里取出 ``/ak`` 之后的第一个词；缺省为 ``status``。

        不依赖框架的参数解析：无论 ``message_str`` 是 ``"/ak status"`` 还是
        ``"status"``，结果都一样，少一处依赖就少一处能踩空的地方。
        """
        tokens = message_str.strip().split()
        if tokens and tokens[0].lstrip("/").lower() == "ak":
            tokens = tokens[1:]
        return tokens[0].lower() if tokens else "status"
