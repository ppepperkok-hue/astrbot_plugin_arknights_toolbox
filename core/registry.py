"""模块注册表与配置开关解析。

只做两件事：把「配置里的开关」翻译成「一批模块实例」，以及统一启停它们。
与 `core/module.py` 一样，这里**不 import astrbot**——宿主骨架不需要框架，
保持零依赖才能被 pytest 直接覆盖（见 docs/architecture/rules.md §2）。
"""

import importlib
import pkgutil
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from .module import Module

ModuleFactory = Callable[[], Module]

# 插件自己的包名：AstrBot 加载时是 `<插件包>`，在仓库根直接跑 pytest 时是空串。
# 用它拼出 modules 包的绝对名，同一份代码在两种场景下都能定位模块目录。
_PLUGIN_PACKAGE = __package__.rsplit(".", 1)[0] if __package__ and "." in __package__ else ""
MODULES_PACKAGE = f"{_PLUGIN_PACKAGE}.modules" if _PLUGIN_PACKAGE else "modules"

# 模块入口文件名约定：`<模块包>/module.py`
MODULE_ENTRY_NAME = "module"

# 已知模块表：由 `discover_modules()` 在插件加载时扫描填充——
# 新增模块只需在 `modules/` 下加一个子包，宿主不必认识任何具体功能
# （见 docs/architecture/scope.md §2）。测试里也可显式 `register_module()`。
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


class ModuleDiscoveryError(ValueError):
    """自动发现模块时发现问题。"""


def discover_modules() -> dict[str, ModuleFactory]:
    """扫描 `modules/` 下的子包，把其中的 `Module` 子类登记进注册表。

    约定（见 docs/architecture/extension.md §1）：模块目录名 = 模块 id =
    `Module.name`，模块类定义在 `<模块包>/module.py` 里。

    这样一来宿主**不必 import 任何具体模块**（架构红线：宿主不认识具体功能），
    新增模块只要往 `modules/` 下加一个子包。

    Returns:
        本次发现的「模块名 → 工厂」映射。

    Raises:
        ModuleDiscoveryError: 模块目录无法导入、入口文件里没有 `Module` 子类、
            有多个子类、或类属性 `name` 与目录名不一致。**一律显式失败**，
            绝不静默跳过——少装了一个模块却显示加载成功是最难查的那种 bug
            （项目宪法 §2 第 2 条）。

    Note:
        幂等：重复调用结果一致（同名覆盖登记）。
    """
    package = _import_modules_package()
    discovered: dict[str, ModuleFactory] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg or info.name.startswith("_"):
            continue
        entry_name = f"{MODULES_PACKAGE}.{info.name}.{MODULE_ENTRY_NAME}"
        module_cls = _load_module_class(entry_name, info.name)
        if module_cls.name != info.name:
            raise ModuleDiscoveryError(
                f"模块「{info.name}」的类属性 name={module_cls.name!r} 与目录名不一致；"
                "两者必须相同（见 docs/architecture/extension.md §1）"
            )
        register_module(info.name, module_cls)
        discovered[info.name] = module_cls
    return discovered


def _import_modules_package() -> Any:
    """导入 `modules` 包本身；失败时转成可读的领域错误。"""
    try:
        return importlib.import_module(MODULES_PACKAGE)
    except Exception as exc:  # noqa: BLE001 - 统一转成领域错误并保留原始原因
        raise ModuleDiscoveryError(f"无法导入模块目录 {MODULES_PACKAGE}：{exc!r}") from exc


def _load_module_class(entry_name: str, module_name: str) -> type[Module]:
    """从 `<模块包>.module` 里取出**唯一**的 `Module` 子类。"""
    try:
        entry = importlib.import_module(entry_name)
    except Exception as exc:  # noqa: BLE001 - 同上
        raise ModuleDiscoveryError(
            f"模块「{module_name}」的入口 {entry_name} 导入失败：{exc!r}"
        ) from exc

    candidates = [
        obj
        for obj in vars(entry).values()
        if isinstance(obj, type)
        and issubclass(obj, Module)
        and obj is not Module
        and obj.__module__ == entry_name
    ]
    if not candidates:
        raise ModuleDiscoveryError(f"模块「{module_name}」的 {entry_name} 里没有 Module 子类")
    if len(candidates) > 1:
        names = "、".join(sorted(cls.__name__ for cls in candidates))
        raise ModuleDiscoveryError(
            f"模块「{module_name}」的 {entry_name} 里有多个 Module 子类（{names}），无法确定用哪个"
        )
    return candidates[0]
