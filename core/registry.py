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

    区分「在册」「已启动」「启动失败」三件事：``add`` 只把模块放进名单，
    真正调用过 ``initialize`` 且成功的才会进入 ``_started``；失败的进 ``_failed``。
    ``stop_all`` 只回收 ``_started``。这个区分是失败回滚的依据——
    启动失败的模块不该被 ``terminate``。

    **逐模块隔离失败**：一个模块起不来**不阻断其余模块**（见 ``start_all``）。
    这条是「森空岛失效绝不许拖垮核心功能」那条铁律的宿主层形态——与其让每个
    模块自己想办法「不抛异常」，不如让宿主本来就不怕某个模块抛。
    """

    def __init__(self) -> None:
        self._modules: list[Module] = []
        self._started: list[Module] = []
        self._failed: list[tuple[str, str]] = []
        self._degraded: list[tuple[str, str]] = []

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

    @property
    def failed_modules(self) -> tuple[tuple[str, str], ...]:
        """启动失败的模块：``(模块名, 失败原因)``，供宿主报给用户与日志。

        保留原因而不是只留名字——用户看到「recruit 起不来」时，
        下一句一定是「为什么」，而那时日志可能已经滚掉了。
        """
        return tuple(self._failed)

    @property
    def degraded_modules(self) -> tuple[tuple[str, str], ...]:
        """**已装载但干不了活**的模块：``(模块名, 原因)``。

        与 ``failed_modules`` 的区别在「起没起来」：这里的模块 ``initialize`` 成功、
        资源已分配、指令也答得上，只是关键能力缺失（例如数据文件没找到）。它们同时
        也在 ``started_names`` 里，所以 ``stop_all`` 会照常回收它们。

        只在 ``initialize`` 成功返回后**查询一次** ``Module.unavailable_reason`` 并
        记录：查询点固定（「已装载」这句话要覆盖的正是装载那一刻），也避免模块自己的
        代码在展示路径上抛异常。
        """
        return tuple(self._degraded)

    @property
    def available_names(self) -> tuple[str, ...]:
        """名副其实**能用**的模块名：已启动、且没有自报不可用。

        汇总日志与用户可见的状态一律用这个，**不要**用 ``started_names``——
        「已装载」里混着用不了的模块就是在对用户说谎（项目宪法 §2 第 2 条）。
        """
        degraded = {name for name, _ in self._degraded}
        return tuple(module.name for module in self._started if module.name not in degraded)

    def __iter__(self) -> Iterator[Module]:
        return iter(self._modules)

    def __len__(self) -> int:
        return len(self._modules)

    async def start_all(self, ctx: Any, config: Mapping[str, Any] | None) -> None:
        """依次调用各模块的 ``initialize``，并按段注入配置。

        每个模块只拿到**自己那一段**（按 ``config_key`` 从整份配置里取）；
        该段缺失或类型不对时传空字典——模块之间因此互不知情
        （见 docs/implementation/implementation.md「模块如何取得自己的配置」）。

        **单个模块失败不阻断其余模块**：失败的记进 ``failed_modules`` 并继续，
        一个数据源挂掉不该让整个插件起不来。失败**必须**由调用方显式报告
        （宿主会写 ERROR 日志并在指令里告诉用户），不允许静默——本方法自己
        不打印，因为它是纯逻辑层，日志属于装配层的职责。

        注意「失败」只捕获 ``Exception``：``BaseException``（如取消、退出信号）
        照旧向上传播，那些不是模块的错误，不该被当作「这条路不行」吞掉。

        ``initialize`` 成功但模块**自报不可用**（见 ``Module.unavailable_reason``）的，
        既进 ``started_names``（它确实活着、要回收）也进 ``degraded_modules``——
        「已装载」与「能用」是两件事，混为一谈就会对用户说假话。

        Args:
            ctx: AstrBot 的 ``Context``，原样转交给每个模块。
            config: 整份插件配置；非 Mapping 时按缺省处理。
        """
        sections: Mapping[str, Any] = config if isinstance(config, Mapping) else {}
        self._started = []
        self._failed = []
        self._degraded = []
        for module in self._modules:
            section = sections.get(module.config_key)
            try:
                await module.initialize(
                    ctx,
                    section if isinstance(section, Mapping) else {},
                )
            except Exception as exc:  # noqa: BLE001 - 逐模块隔离，失败要记录而非上抛
                self._failed.append((module.name, f"{type(exc).__name__}: {exc}"))
                continue
            self._started.append(module)
            reason = _read_unavailable_reason(module)
            if reason is not None:
                self._degraded.append((module.name, reason))

    async def stop_all(self) -> None:
        """回收**已成功启动**的模块（调用其 ``terminate``）。

        只处理 ``_started`` 里的模块：启动中途失败时，没初始化过的模块不会被
        ``terminate`` 误伤。跑完即清空 ``_started``，因此**重复调用是幂等的**。

        ``_degraded`` 随 ``_started`` 一起清空——它描述的是「当前已启动的模块」，
        全部停掉之后没有谁还能是「已装载但不可用」。``_failed`` 不清：它记的是
        **上一次启动尝试**的事实，而那些模块从未启动过，谈不上被停止。

        单个模块清理失败**不阻断**其余模块的清理；全部跑完后把失败汇总抛出，
        既不静默吞错，也不留下没清理干净的模块。
        """
        modules, self._started = self._started, []
        self._degraded = []
        failures: list[str] = []
        for module in modules:
            try:
                await module.terminate()
            except Exception as exc:  # noqa: BLE001 - 清理阶段要收集全部失败
                failures.append(f"{module.name}: {exc!r}")
        if failures:
            raise RuntimeError("以下模块的 terminate 失败：" + "；".join(failures))


def _read_unavailable_reason(module: Module) -> str | None:
    """问模块「你现在能用吗」，返回 ``None`` 表示可用，否则返回给用户看的原因。

    为什么把模块的代码包起来：这是「所有模块都正常」这条主路径上新加的一步，而它
    调用的是各模块自己的实现。模块写坏了不该在装载阶段掀翻宿主——那正是本机制要
    防的「一个模块拖垮全部」。**读不出来时不敢说它可用**，所以归入不可用并留下
    异常原文；用户看到的是「读取状态失败」，而不是一片沉默。

    空字符串也按不可用处理（换成占位文案），理由同上：漏写原因不该退化成
    「看起来可用」，那还是假话。判定规则因此只有一条：``None`` 即可用。
    """
    try:
        reason = module.unavailable_reason
    except Exception as exc:  # noqa: BLE001 - 见上：宁可报不可用，也不冒泡
        return f"读取模块状态失败：{type(exc).__name__}: {exc}"
    if reason is None:
        return None
    text = str(reason).strip()
    return text or "模块自报不可用，但未给出原因"


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
