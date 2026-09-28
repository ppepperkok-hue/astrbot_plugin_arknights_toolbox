"""模块注册表与配置开关解析。

只做两件事：把「配置里的开关」翻译成「一批模块实例」，以及统一启停它们。
与 `core/module.py` 一样，这里**不 import astrbot**——宿主骨架不需要框架，
保持零依赖才能被 pytest 直接覆盖（见 docs/architecture/rules.md §2）。
"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .module import Module

ModuleFactory = Callable[[], Module]

# 已知模块表：模块在装配时登记（docs/architecture/extension.md §1 第 6 步）。
# S1 阶段**刻意留空**——此时任何模块开关都会因为"未登记"而报错，
# 这正是我们要的：宁可显式失败，也不静默少装一个模块。
_KNOWN_MODULES: dict[str, ModuleFactory] = {}

# 配置里承载模块开关的键：{"modules": {"<模块名>": true|false}}
#
# 注意：`modules` 段在 S1 的 `_conf_schema.json` 中**刻意没有暴露**（裁决 D2），
# 留到 S3 装配真实模块时再加。AstrBot 会**删除** schema 未声明的配置键
# （core/config/astrbot_config.py:172 `check_config_integrity`，:245 打印
# "Config key removed"），所以现在手写它也会在 initialize() 之前被抹掉。
MODULE_SWITCHES_KEY = "modules"


class UnknownModuleError(ValueError):
    """配置里出现了注册表不认识的模块名。"""


def register_module(name: str, factory: ModuleFactory) -> None:
    """登记一个模块工厂。

    Args:
        name: 模块名，必须与 `Module.name` 一致。
        factory: 无参工厂，返回模块实例。

    Raises:
        ValueError: 模块名为空。
    """
    if not name:
        raise ValueError("模块名不能为空")
    _KNOWN_MODULES[name] = factory


def unregister_module(name: str) -> None:
    """撤销登记（测试清理用）。"""
    _KNOWN_MODULES.pop(name, None)


def known_module_names() -> tuple[str, ...]:
    """已登记的模块名，升序。"""
    return tuple(sorted(_KNOWN_MODULES))


def read_module_switches(config: Mapping[str, Any] | None) -> dict[str, bool]:
    """从插件配置里取出模块开关。

    约定：配置的 ``modules`` 段形如 ``{"<模块名>": true|false}``。
    该段缺失或类型不对时返回**空字典**（等价于"一个模块都不开"）——
    配置没写不等于配置写错，这里不抛错；名字写错由 `build_registry` 负责报。

    Args:
        config: AstrBot 传给插件的配置；可以是 None 或任意类型。

    Returns:
        模块名到开关的映射。
    """
    if not isinstance(config, Mapping):
        return {}
    section = config.get(MODULE_SWITCHES_KEY)
    if not isinstance(section, Mapping):
        return {}
    return {str(name): bool(value) for name, value in section.items()}


class ModuleRegistry:
    """一批已装载模块的持有者。

    区分「在册」与「已启动」两件事：``add`` 只把模块放进名单，
    真正调用过 ``initialize`` 且成功的才会进入 ``_started``；
    ``stop_all`` 只回收 ``_started``。这个区分是失败回滚的依据——
    启动中途失败时，没启动的模块不该被 ``terminate``。
    """

    def __init__(self) -> None:
        self._modules: list[Module] = []
        self._started: list[Module] = []

    def add(self, module: Module) -> None:
        """把模块放进注册表（装载由 `build_registry` 统一负责）。"""
        self._modules.append(module)

    @property
    def enabled_names(self) -> tuple[str, ...]:
        """当前注册表里（即已开启的）模块名。"""
        return tuple(module.name for module in self._modules)

    @property
    def started_names(self) -> tuple[str, ...]:
        """已经成功 ``initialize`` 过的模块名（回滚与诊断用）。"""
        return tuple(module.name for module in self._started)

    def __iter__(self) -> Iterator[Module]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    async def start_all(self, ctx: Any, config: Mapping[str, Any] | None) -> None:
        """依次调用各模块的 ``initialize``，并按段注入配置。

        每个模块只拿到**自己那一段**（按 ``config_key`` 从整份配置里取）；
        该段缺失或类型不对时传空字典——模块之间因此互不知情
        （见 docs/implementation/implementation.md「模块如何取得自己的配置」）。

        任一模块失败即抛出，不吞错——加载失败必须让人当场看见。
        已经成功启动的模块记在 ``started_names`` 里，供调用方回滚。

        Args:
            ctx: AstrBot 的 ``Context``，原样转交给每个模块。
            config: 整份插件配置；非 Mapping 时按缺省处理。
        """
        sections: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        self._started = []
        for module in self._modules:
            section = sections.get(module.config_key)
            await module.initialize(
                ctx,
                section if isinstance(section, Mapping) else {},
            )
            self._started.append(module)

    async def stop_all(self) -> None:
        """回收**已成功启动**的模块（调用其 ``terminate``）。

        只处理 ``_started`` 里的模块：启动中途失败时，没初始化过的模块不会被
        ``terminate`` 误伤。跑完即清空 ``_started``，因此**重复调用是幂等的**。

        单个模块清理失败**不阻断**其余模块的清理；全部跑完后把失败汇总抛出，
        既不静默吞错，也不留下没清理干净的模块。
        """
        modules, self._started = self._started, []
        failures: list[str] = []
        for module in modules:
            try:
                await module.terminate()
            except Exception as exc:  # noqa: BLE001 - 清理阶段要收集全部失败
                failures.append(f"{module.name}: {exc!r}")
        if failures:
            raise RuntimeError("以下模块的 terminate 失败：" + "；".join(failures))


def build_registry(enabled: Mapping[str, bool]) -> ModuleRegistry:
    """按开关实例化**已登记**的模块。

    Args:
        enabled: 模块名到开关的映射，通常来自 ``read_module_switches``。

    Returns:
        只含已开启模块的注册表。

    Raises:
        UnknownModuleError: 出现未登记的模块名——**无论开关真假都报**。
            名字写错就是配置错误，必须当场暴露，不许静默跳过
            （项目宪法 §2 第 2 条）。
    """
    unknown = sorted(name for name in enabled if name not in _KNOWN_MODULES)
    if unknown:
        known = "、".join(known_module_names()) or "（暂无）"
        raise UnknownModuleError(
            f"配置里有未登记的模块：{'、'.join(unknown)}；已登记的模块：{known}"
        )

    registry = ModuleRegistry()
    for name, is_enabled in enabled.items():
        if is_enabled:
            registry.add(_KNOWN_MODULES[name]())
    return registry
