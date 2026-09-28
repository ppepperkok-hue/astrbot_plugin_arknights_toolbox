"""模块注册表测试。

用**假模块**（stub）测试注册表本身，**不 import astrbot**——
注册表与基类不依赖框架，因此这些用例在没有 AstrBot 的环境里也能跑
（见 docs/architecture/rules.md §6：框架胶水层不做单测）。
"""

import asyncio

import pytest

from core.module import Module
from core.registry import (
    ModuleRegistry,
    UnknownModuleError,
    build_registry,
    known_module_names,
    read_module_switches,
    register_module,
    unregister_module,
)

Trace = list[tuple[str, str]]


class FakeModule(Module):
    """记录调用痕迹的假模块。"""

    name = "fake"
    config_key = "fake"

    def __init__(self, name: str, trace: Trace) -> None:
        self.name = name
        self.config_key = name
        self._trace = trace

    async def initialize(self, ctx) -> None:
        self._trace.append(("initialize", self.name))

    async def terminate(self) -> None:
        self._trace.append(("terminate", self.name))


class BoomModule(FakeModule):
    """terminate 会抛错的假模块，用来验证"不阻断其余模块"。"""

    async def terminate(self) -> None:
        self._trace.append(("terminate", self.name))
        raise RuntimeError("boom")


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
    run(registry.start_all(ctx=object()))
    assert trace == [("initialize", "alpha")]


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
    run(registry.start_all(ctx=object()))

    assert registry.enabled_names == ("alpha",)
    assert trace == [("initialize", "alpha")]


def test_empty_switches_give_empty_registry():
    registry = build_registry({})
    assert len(registry) == 0
    assert registry.enabled_names == ()


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

    run(registry.stop_all())

    assert trace == [("terminate", "alpha")]


def test_stop_all_finishes_others_then_reports_failures():
    trace: Trace = []
    register_module("boom", lambda: BoomModule("boom", trace))
    register_module("calm", lambda: FakeModule("calm", trace))
    registry = build_registry({"boom": True, "calm": True})

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


# --- 基类契约 ---------------------------------------------------------------


def test_module_base_is_abstract():
    with pytest.raises(TypeError):
        Module()  # type: ignore[abstract]


def test_module_default_hooks_are_empty():
    class Bare(Module):
        name = "bare"
        config_key = "bare"

        async def initialize(self, ctx) -> None:
            return None

        async def terminate(self) -> None:
            return None

    bare = Bare()
    assert bare.commands() == []
    assert bare.jobs() == []


def test_registry_iteration_exposes_modules():
    trace: Trace = []
    register_module("alpha", lambda: FakeModule("alpha", trace))
    registry = build_registry({"alpha": True})
    assert [module.name for module in registry] == ["alpha"]
    assert isinstance(registry, ModuleRegistry)
