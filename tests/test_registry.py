"""模块注册表测试。

用**假模块**（stub）测试注册表本身，**不 import astrbot**——
注册表与基类不依赖框架，因此这些用例在没有 AstrBot 的环境里也能跑
（见 docs/architecture/rules.md §6：框架胶水层不做单测）。
"""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

import core.registry as registry_module
from core.module import Module
from core.registry import (
    ModuleDiscoveryError,
    ModuleRegistry,
    UnknownModuleError,
    build_registry,
    discover_modules,
    known_module_names,
    read_module_switches,
    register_module,
    unregister_module,
)

Trace = list[tuple[str, str]]
SeenConfigs = dict[str, dict]

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"


class FakeModule(Module):
    """记录调用痕迹的假模块；给了 ``configs`` 就把收到的配置段也记下来。"""

    name = "fake"
    config_key = "fake"

    def __init__(
        self,
        name: str,
        trace: Trace,
        configs: SeenConfigs | None = None,
    ) -> None:
        self.name = name
        self.config_key = name
        self._trace = trace
        self._configs = configs

    async def initialize(self, ctx, config) -> None:
        self._trace.append(("initialize", self.name))
        if self._configs is not None:
            self._configs[self.name] = dict(config)

    async def terminate(self) -> None:
        self._trace.append(("terminate", self.name))


class FailInitModule(FakeModule):
    """``initialize`` 会抛错的假模块，用来验证启动失败时的回滚边界。"""

    async def initialize(self, ctx, config) -> None:
        await super().initialize(ctx, config)
        raise RuntimeError(f"{self.name} boom")


class BoomModule(FakeModule):
    """``terminate`` 会抛错的假模块，用来验证"不阻断其余模块"。"""

    async def terminate(self) -> None:
        await super().terminate()
        raise RuntimeError("boom")


class DegradedModule(FakeModule):
    """``initialize`` 成功、但自报干不了活的假模块（例如数据文件缺失）。

    它**不是**失败：模块活着、资源已分配、指令也答得上，只是关键能力没有。
    这正是 2026-09-29 线上缺陷里 `recruit` 的真实处境。
    """

    def __init__(self, name: str, trace: Trace, reason: str = "数据文件不见了") -> None:
        super().__init__(name, trace)
        self._reason = reason

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason


class BlankReasonModule(FakeModule):
    """自报不可用、但原因写成空白——不许因此被当成可用。"""

    @property
    def unavailable_reason(self) -> str | None:
        return "   "


class RaisingStatusModule(FakeModule):
    """读状态本身抛异常——按不可用处理，不许掀翻宿主。"""

    @property
    def unavailable_reason(self) -> str | None:
        raise RuntimeError("状态读不出来")


@pytest.fixture(autouse=True)
def _isolate_registry():
    """隔离全局注册表：用例结束后撤销它新登记的一切。"""
    before = set(known_module_names())
    yield
    for name in set(known_module_names()) - before:
        unregister_module(name)


def run(coro):
    """跑一个协程（本仓库不引入 pytest-asyncio）。"""
    return asyncio.run(coro)


# --- 装载 -------------------------------------------------------------------


def test_enabled_module_is_loaded_and_initialized():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))

    registry = build_registry({"alpha": True})

    assert registry.enabled_names == ("alpha",)
    assert len(registry) == 1
    run(registry.start_all(ctx=object(), config={}))
    assert trace == [("initialize", "alpha")]
    assert registry.started_names == ("alpha",)


def test_disabled_module_is_not_loaded():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))

    registry = build_registry({"alpha": False})

    assert registry.enabled_names == ()
    assert len(registry) == 0
    assert trace == []


def test_mixed_switches_load_only_the_enabled_ones():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))
    register_module("beta", lambda: FakeModule("beta", trace))

    registry = build_registry({"alpha": True, "beta": False})
    run(registry.start_all(ctx=object(), config={}))

    assert registry.enabled_names == ("alpha",)
    assert trace == [("initialize", "alpha")]


def test_empty_switches_give_empty_registry():
    registry = build_registry({})
    assert len(registry) == 0
    assert registry.enabled_names == ()


# --- 配置按段注入（C3 裁决） -------------------------------------------------


def test_each_module_receives_only_its_own_config_section():
    """模块只看到自己那一段，看不到别人的段。"""
    seen: SeenConfigs = {}
    register_module("alpha", lambda: FakeModule("alpha", [], seen))
    register_module("beta", lambda: FakeModule("beta", [], seen))

    registry = build_registry({"alpha": True, "beta": True})
    run(
        registry.start_all(
            ctx=object(),
            config={
                "alpha": {"x": 1},
                "beta": {"y": 2},
                "unrelated": {"z": 3},
            },
        )
    )

    assert seen == {"alpha": {"x": 1}, "beta": {"y": 2}}


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"other": {"x": 1}},
        {"alpha": "not-a-mapping"},
        {"alpha": None},
        "not-a-mapping",
    ],
)
def test_module_without_usable_section_receives_empty_dict(config):
    """段缺失或类型不对时传空字典，而不是整份配置。"""
    seen: SeenConfigs = {}
    register_module("alpha", lambda: FakeModule("alpha", [], seen))

    registry = build_registry({"alpha": True})
    run(registry.start_all(ctx=object(), config=config))

    assert seen == {"alpha": {}}


# --- 启动失败：只回滚真正启动过的模块（C1） ----------------------------------


def test_failed_start_tracks_only_successfully_started_modules():
    """一个模块起不来不再抛出：失败被记下，其余模块照常。

    旧语义是「任一模块失败即抛出，宿主回滚全部」——那等于让一个数据源挂掉
    拖垮整个插件。现在改成逐模块隔离（见 `ModuleRegistry.start_all`）。
    """
    trace: Trace = []
    register_module("calm", lambda: FakeModule("calm", trace))
    register_module("boom", lambda: FailInitModule("boom", trace))
    registry = build_registry({"calm": True, "boom": True})

    run(registry.start_all(ctx=object(), config={}))

    # 只有成功启动的 calm 进入可回滚名单；boom 自己的清理是它的责任
    assert registry.started_names == ("calm",)
    assert ("initialize", "calm") in trace
    assert ("initialize", "boom") in trace


def test_failed_module_is_recorded_with_a_reason():
    """失败必须带着原因留下来——用户问「为什么这个功能没了」时要有答案。"""
    register_module("calm", lambda: FakeModule("calm", []))
    register_module("boom", lambda: FailInitModule("boom", []))
    registry = build_registry({"calm": True, "boom": True})

    run(registry.start_all(ctx=object(), config={}))

    failed = registry.failed_modules
    assert len(failed) == 1
    name, reason = failed[0]
    assert name == "boom"
    assert reason  # 非空，且调用方不需要再去翻日志才知道原因


def test_a_failing_module_does_not_block_the_ones_after_it():
    """顺序很重要：失败的那个排在前面时，后面的仍要起来。"""
    trace: Trace = []
    register_module("boom", lambda: FailInitModule("boom", trace))
    register_module("calm", lambda: FakeModule("calm", trace))
    registry = build_registry({"boom": True, "calm": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.started_names == ("calm",)
    assert ("initialize", "calm") in trace


def test_stop_after_failed_start_only_terminates_started_modules():
    trace: Trace = []
    register_module("calm", lambda: FakeModule("calm", trace))
    register_module("boom", lambda: FailInitModule("boom", trace))
    registry = build_registry({"calm": True, "boom": True})

    run(registry.start_all(ctx=object(), config={}))
    run(registry.stop_all())

    assert trace == [
        ("initialize", "calm"),
        ("initialize", "boom"),
        ("terminate", "calm"),
    ]


# --- 已装载但不可用：宿主必须知道（2026-09-29 线上缺陷，plan.md 待办 8） --------
#
# 缺陷原样：`recruit` 的 `initialize` 把数据装载失败吞在内部，于是宿主那层「逐模块
# 隔离」根本没被触发、也无从知晓，汇总日志仍然打「已装载模块：shift_reminder、recruit」。
# 用户据此判断「哪个功能能用」会被误导——宪法 §2 第 2 条点名的「看起来成功但什么都
# 没发生」。
#
# 修法：模块可以自报不可用（`Module.unavailable_reason`），宿主把「已装载」与
# 「能用」分成两件事。**注意它与「启动失败」是两种状态**，处置也不同：失败的是
# 压根没起来，降级的是活着但干不了活（还能告诉用户怎么修）。


def test_a_module_reporting_itself_unusable_is_recorded_with_a_reason():
    register_module("degraded", lambda: DegradedModule("degraded", [], "数据文件不见了"))
    registry = build_registry({"degraded": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.degraded_modules == (("degraded", "数据文件不见了"),)


def test_a_degraded_module_is_not_listed_as_available():
    """这就是那个缺陷本身：日志说「已装载」，而它干不了活。"""
    register_module("calm", lambda: FakeModule("calm", []))
    register_module("degraded", lambda: DegradedModule("degraded", []))
    registry = build_registry({"calm": True, "degraded": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ("calm",)
    # 它确实活着（initialize 成功、资源在手），所以仍在可回滚名单里
    assert registry.started_names == ("calm", "degraded")


def test_a_degraded_module_is_still_terminated():
    """降级不等于失败：它分配过资源，回收时不能漏掉它。"""
    trace: Trace = []
    register_module("degraded", lambda: DegradedModule("degraded", trace))
    registry = build_registry({"degraded": True})

    run(registry.start_all(ctx=object(), config={}))
    run(registry.stop_all())

    assert trace == [("initialize", "degraded"), ("terminate", "degraded")]


def test_stopping_clears_the_degraded_list():
    """全部停掉之后，没有谁还能是「已装载但不可用」。"""
    register_module("degraded", lambda: DegradedModule("degraded", []))
    registry = build_registry({"degraded": True})

    run(registry.start_all(ctx=object(), config={}))
    run(registry.stop_all())

    assert registry.degraded_modules == ()
    assert registry.available_names == ()


def test_nothing_degraded_keeps_the_old_behaviour():
    """所有模块正常时，「可用」与「已启动」必须完全一致——既有输出不许被改动。"""
    register_module("calm", lambda: FakeModule("calm", []))
    registry = build_registry({"calm": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.degraded_modules == ()
    assert registry.available_names == registry.started_names == ("calm",)


def test_available_names_excludes_failed_modules_too():
    register_module("calm", lambda: FakeModule("calm", []))
    register_module("boom", lambda: FailInitModule("boom", []))
    registry = build_registry({"calm": True, "boom": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ("calm",)
    assert registry.started_names == ("calm",)


def test_a_degraded_module_does_not_block_the_ones_after_it():
    trace: Trace = []
    register_module("degraded", lambda: DegradedModule("degraded", trace))
    register_module("calm", lambda: FakeModule("calm", trace))
    registry = build_registry({"degraded": True, "calm": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ("calm",)
    assert ("initialize", "calm") in trace


def test_a_module_whose_status_read_raises_is_reported_as_unusable():
    """读状态是主路径上新加的一步，而它调的是模块自己的代码——写坏了不许掀翻宿主。

    读不出来时也就不知道它能不能用，所以**不敢说它可用**。
    """
    register_module("broken", lambda: RaisingStatusModule("broken", []))
    registry = build_registry({"broken": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ()
    reason = dict(registry.degraded_modules)["broken"]
    assert "读取模块状态失败" in reason
    assert "状态读不出来" in reason


def test_an_empty_reason_still_counts_as_unusable():
    """漏写原因不许退化成「看起来可用」——那还是假话。"""
    register_module("blank", lambda: BlankReasonModule("blank", []))
    registry = build_registry({"blank": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ()
    assert dict(registry.degraded_modules)["blank"]


def test_commands_still_reach_a_degraded_module():
    """它活着，所以要能应答指令、把原因告诉用户——这是「降级」区别于「失败」的地方。"""
    register_module("degraded", lambda: DegradedModule("degraded", []))
    registry = build_registry({"degraded": True})

    run(registry.start_all(ctx=object(), config={}))

    assert [module.name for module in registry] == ["degraded"]


def test_modules_that_do_not_override_the_status_are_available_by_default():
    """不改 `unavailable_reason` 的模块一律可用——既有模块因此不受影响。"""
    register_module("plain", lambda: FakeModule("plain", []))
    registry = build_registry({"plain": True})

    run(registry.start_all(ctx=object(), config={}))

    assert registry.available_names == ("plain",)
    assert registry.degraded_modules == ()


# --- 未知模块名：必须显式失败 -------------------------------------------------


@pytest.mark.parametrize("switch", [True, False])
def test_unknown_module_name_raises_even_when_disabled(switch):
    """名字写错就是配置错误，开关真假都要报，不许静默跳过。"""
    with pytest.raises(UnknownModuleError, match="未登记"):
        build_registry({"does_not_exist": switch})


def test_unknown_module_error_lists_known_names():
    register_module("alpha", lambda: FakeModule("alpha", []))
    with pytest.raises(UnknownModuleError) as excinfo:
        build_registry({"beta": True})
    assert "beta" in str(excinfo.value)
    assert "alpha" in str(excinfo.value)


# --- 停止 -------------------------------------------------------------------


def test_stop_all_calls_terminate():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))
    registry = build_registry({"alpha": True})

    run(registry.start_all(ctx=object(), config={}))
    run(registry.stop_all())

    assert trace == [("initialize", "alpha"), ("terminate", "alpha")]
    assert registry.started_names == ()


def test_stop_all_is_idempotent():
    """重复 stop 不得重复 terminate（宪法 §2 第 4 条）。"""
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))
    registry = build_registry({"alpha": True})

    run(registry.start_all(ctx=object(), config={}))
    run(registry.stop_all())
    run(registry.stop_all())

    assert trace == [("initialize", "alpha"), ("terminate", "alpha")]


def test_stop_all_finishes_others_then_reports_failures():
    trace: Trace = []
    register_module("boom", lambda: BoomModule("boom", trace))
    register_module("calm", lambda: FakeModule("calm", trace))
    registry = build_registry({"boom": True, "calm": True})

    run(registry.start_all(ctx=object(), config={}))
    with pytest.raises(RuntimeError, match="boom"):
        run(registry.stop_all())

    # 单个模块炸了，其余模块仍要清理完
    assert ("terminate", "calm") in trace


# --- 配置开关解析 ------------------------------------------------------------


def test_read_module_switches_reads_modules_section():
    config = {"modules": {"alpha": True, "beta": 0}, "other": {"x": 1}}
    assert read_module_switches(config) == {"alpha": True, "beta": False}


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"modules": None},
        {"modules": []},
        "not-a-mapping",
        42,
    ],
)
def test_read_module_switches_is_tolerant_about_missing_section(config):
    """配置没写不等于配置写错：缺失一律当"一个都不开"，不抛错。"""
    assert read_module_switches(config) == {}


# --- 接缝：schema 与注册表之间的真实集成 --------------------------------------


def _default_config_from_schema(schema: dict) -> dict:
    """按 AstrBot 的规则从 schema 递归生成默认配置。

    已核实（``core/config/astrbot_config.py:145`` ``_config_schema_to_default_config``）：
    ``object`` 类型**忽略顶层 ``default``**，只用 ``items`` 逐项递归生成，
    因此空 ``items`` 只会生成 ``{}``，不会凭空造出模块名。
    """

    def build(node: dict):
        if node.get("type") == "object":
            return {key: build(child) for key, child in node.get("items", {}).items()}
        return node.get("default")

    return {key: build(node) for key, node in schema.items()}


def test_schema_seam_actually_loads_the_shift_module():
    """schema 与注册表之间的接缝必须**真的通**。

    这条测的是真实接缝：从 `_conf_schema.json` 递归生成默认配置 → 解析开关 →
    自动发现模块 → 建注册表。人手构造的 dict 测不到它。

    S3 起 `modules` 段存在且 `shift_reminder` 默认开启，所以这里必须真的装载出
    `shift_reminder` —— 只断言「空注册表也不报错」等于什么都没测。

    **不写死模块名单**：每加一个模块都得回来改测试的话，这条护栏迟早被人图省事
    改成 `assert True`。改为「schema 里声明了几个开关，打开几个就应当装载出几个」，
    这样它对新模块自动生效，且仍然钉住「声明与装载必须一致」。

    2026-09-29 修订：原先断言「模块开关应当**默认全开**」。森空岛模块（`skland`）
    需要用户主动扫码授权，按 SSOT §2.7 的要求**必须默认关**——装上就用不了的模块
    默认打开，用户会以为插件坏了。所以这条改成按**默认值策略**判：
    核心模块默认可用；需要授权的模块默认关。
    """
    if not SCHEMA_PATH.is_file():
        pytest.fail(f"缺少配置文件：{SCHEMA_PATH}")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    declared = set(schema["modules"]["items"])
    switches = read_module_switches(_default_config_from_schema(schema))
    assert set(switches) == declared, "schema 声明的开关与解析出来的开关必须一致"
    assert all(isinstance(value, bool) for value in switches.values()), (
        f"每个开关的默认值必须是布尔，实际 {switches}"
    )

    enabled = {name for name, on in switches.items() if on}
    assert enabled, "至少得有一个模块默认开着，否则用户装上什么也得不到"
    assert "shift_reminder" in enabled, "换班提醒是本插件的核心，必须默认可用"
    assert switches.get("skland") is False, (
        "森空岛需要用户扫码授权，默认必须是关的——默认开着等于装上就报「不可用」"
    )

    discover_modules()
    registry = build_registry(switches)
    assert set(registry.enabled_names) == enabled, "装载出来的必须正好是打开的那些"
    assert "shift_reminder" in registry.enabled_names


# --- 模块自动发现 -----------------------------------------------------------


def test_discover_modules_finds_shift_reminder_and_is_idempotent():
    """自动发现必须找得到真实模块，且重复调用结果一致。"""
    first = discover_modules()
    second = discover_modules()

    assert "shift_reminder" in first
    assert set(first) == set(second)
    assert "shift_reminder" in known_module_names()


def test_discover_modules_rejects_name_mismatch(monkeypatch):
    """目录名与 `Module.name` 不一致时必须抛错——不许静默登记成别的名字。"""
    package = types.ModuleType("modules.mismatch")
    package.__path__ = []
    entry = types.ModuleType("modules.mismatch.module")

    class WrongName(Module):
        name = "not_mismatch"
        config_key = "not_mismatch"

        async def initialize(self, ctx, config):  # pragma: no cover - 不会被调用
            return None

        async def terminate(self):  # pragma: no cover - 不会被调用
            return None

    WrongName.__module__ = "modules.mismatch.module"
    entry.WrongName = WrongName
    monkeypatch.setitem(sys.modules, "modules.mismatch", package)
    monkeypatch.setitem(sys.modules, "modules.mismatch.module", entry)
    monkeypatch.setattr(registry_module, "_import_modules_package", lambda: package)
    monkeypatch.setattr(
        registry_module.pkgutil,
        "iter_modules",
        lambda path: [types.SimpleNamespace(name="mismatch", ispkg=True)],
    )

    with pytest.raises(ModuleDiscoveryError, match="与目录名不一致"):
        discover_modules()


def test_discover_modules_reports_unimportable_entry():
    """入口文件导入不进来时要抛明确错误，而不是静默跳过这个模块。"""
    with pytest.raises(ModuleDiscoveryError, match="导入失败"):
        registry_module._load_module_class("modules.__no_such__.module", "__no_such__")


# --- 基类契约 ---------------------------------------------------------------


def test_module_base_is_abstract():
    with pytest.raises(TypeError):
        Module()  # type: ignore[abstract]


def test_module_wants_config_argument():
    """基类签名必须是 initialize(ctx, config)——契约冻结在 extension.md §2。"""
    import inspect

    params = list(inspect.signature(Module.initialize).parameters)
    assert params == ["self", "ctx", "config"]


def test_module_default_handle_command_returns_false():
    """模块不认识的子命令返回 False，由宿主统一回「未知子命令」。"""

    class Bare(Module):
        name = "bare"
        config_key = "bare"

        async def initialize(self, ctx, config) -> None:
            return None

        async def terminate(self) -> None:
            return None

    bare = Bare()
    assert run(bare.handle_command("whatever", object())) is False


def test_registry_iteration_exposes_modules():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))
    registry = build_registry({"alpha": True})
    assert [module.name for module in registry] == ["alpha"]
    assert isinstance(registry, ModuleRegistry)
