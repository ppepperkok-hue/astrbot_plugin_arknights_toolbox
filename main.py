"""AstrBot 明日方舟工具箱 —— 插件入口。

入口只做三件事：读配置、装载模块、把指令转发出去。
**宿主不认识任何具体功能**——它不知道「三班」是什么，只知道「模块」。
这一条守住了，以后加模块才不用回来改入口（见 docs/architecture/scope.md §2）。

这是框架胶水层：允许 import astrbot，但不做单测（见 docs/architecture/rules.md §6）。
"""

import inspect
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request

from .core.registry import ModuleRegistry, build_registry, discover_modules, read_module_switches

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"


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
        self._register_config_api(context)

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

        模块返回 ``True`` 表示它已经处理并自己把回复发出去了，此时必须调用
        ``event.stop_event()`` 终止事件继续传播——否则事件会一路流进 LLM 管线，
        用户每条指令收到两份回复，还会白烧一次 LLM 调用
        （AstrBot 4.28.1 ``core/platform/astr_message_event.py:348``）。
        """
        subcommand = self._parse_subcommand(event.message_str)
        registry = self._registry
        if registry is not None:
            for module in registry:
                if await module.handle_command(subcommand, event):
                    event.stop_event()
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

    # --- 配置写回（宿主级基础设施，不是业务功能） ---------------------------

    def _register_config_api(self, context: Context) -> None:
        """注册一条通用的「保存插件配置」路由给页面用。

        **为什么由宿主提供**：模块只拿到自己那一段配置（C3 裁决），是一个普通
        dict，**没有** ``save_config``；只有宿主手里那个 ``AstrBotConfig`` 能写回。
        与其为此改模块契约，不如把「配置写回」当作宿主级基础设施——宿主只搬配置，
        它依然**不知道「三班」是什么**（业务校验在模块侧）。

        取不到 ``register_web_api`` 时降级（老版本 AstrBot）：页面不可用，但提醒
        与指令不受影响，且降级要留痕。
        """
        register = getattr(context, "register_web_api", None)
        if register is None:
            logger.warning(
                "[ak_toolbox] 当前 Context 没有 register_web_api，"
                "插件页面与配置写回将不可用；提醒功能不受影响。"
            )
            return
        register(
            f"/{PLUGIN_NAME}/config",
            self._web_save_config,
            ["POST"],
            "保存插件配置",
        )

    async def _web_save_config(self) -> Any:
        """把页面提交的配置片段合并保存，并让模块跟上新配置。

        安全边界（宿主只做形状校验，不碰业务）：
        - 只允许合并**已存在的顶层键**，拒绝凭空新增段（防止页面往配置里塞垃圾）；
        - 每个值必须是对象；
        - 业务校验（三班是否合法）在模块侧——宿主不认识三班。
        """
        saver = getattr(self._config, "save_config_async", None) or getattr(
            self._config, "save_config", None
        )
        if saver is None:
            return error_response("当前 AstrBot 版本不支持写回插件配置", status_code=501)

        payload = await request.json(default={})
        if not isinstance(payload, Mapping) or not payload:
            return error_response("请求体必须是非空对象", status_code=400)

        merged: dict[str, Any] = {}
        previous_values: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in self._config:
                return error_response(f"不允许新增配置段：{key}", status_code=400)
            if not isinstance(value, Mapping):
                return error_response(f"配置段 {key} 必须是对象", status_code=400)
            previous_values[str(key)] = self._config.get(key)
            merged[str(key)] = dict(value)

        outcome = saver(merged)
        if inspect.isawaitable(outcome):
            await outcome

        # 配置对象更新了，但**运行中的定时任务不会自动跟着变**——必须让模块重建。
        # 这里有个顺序陷阱：业务校验（三班是否合法）在模块侧，而配置此时已经写盘。
        # 所以 apply 失败时必须**把旧值写回去**，否则用户的配置文件会被一个非法值
        # 永久污染，下次启动直接加载失败。宁可多写一次盘，也不要留下坏配置。
        try:
            await self._apply_config_to_modules()
        except Exception as exc:  # noqa: BLE001 - 要把失败原因回给页面
            logger.exception("[ak_toolbox] 新配置未能生效，正在回滚")
            rollback: dict[str, Any] = {}
            for key in merged:
                previous = previous_values.get(key)
                if isinstance(previous, Mapping):
                    rollback[key] = dict(previous)
            if rollback:
                restored = saver(rollback)
                if inspect.isawaitable(restored):
                    await restored
                try:
                    await self._apply_config_to_modules()
                except Exception:  # noqa: BLE001 - 回滚也失败只能留痕，但必须报出去
                    logger.exception("[ak_toolbox] 回滚配置后仍未能让模块恢复")
            return error_response(f"配置不合法，已拒绝保存：{exc}", status_code=400)

        logger.info("[ak_toolbox] 页面提交的配置已保存并生效")
        return json_response({"saved": True, "applied": True})

    async def _apply_config_to_modules(self) -> None:
        """把新配置按段发给各模块，让它重建自己的运行状态。

        单个模块失败**不阻断**其余模块（与 ``stop_all`` 同一取舍），但会留痕并
        向上抛，由调用方决定怎么回报——不静默。
        """
        registry = self._registry
        if registry is None:
            return
        failures: list[str] = []
        for module in registry:
            section = self._config.get(module.config_key)
            if not isinstance(section, Mapping):
                section = {}
            try:
                await module.apply_config(section)
            except Exception as exc:  # noqa: BLE001 - 收集全部失败再抛
                logger.exception("[ak_toolbox] 模块 %s 应用新配置失败", module.name)
                failures.append(f"{module.name}: {exc}")
        if failures:
            raise RuntimeError("；".join(failures))
