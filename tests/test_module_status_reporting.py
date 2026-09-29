"""宿主对「模块能不能用」的交代必须诚实。

2026-09-29 服务器实测抓到的缺陷（`docs/implementation/plan.md` 待办 8）：故意删掉
`recruit` 的数据文件后重启，`recruit` 自己记了 ERROR「本模块将不可用」，而宿主那行
汇总仍打「已装载模块：shift_reminder、recruit」，`/ak` 兜底也把不可用的模块列成
可用。用户据此判断「哪个功能能用」就会被误导——正是宪法 §2 第 2 条点名的失败。

这里测**宿主的两个出口**：启动日志与 `/ak` 兜底回复。模块与注册表的行为在
`tests/test_registry.py`、`tests/test_recruit_module.py` 里分别覆盖。

用假模块而不是真模块：本文件要钉的是**宿主的措辞**（什么时候说「已装载」、
什么时候说「可用」），拿真模块反而把无关的初始化依赖（cron_manager、数据目录）
拖进来。真实的 `recruit` 接缝在 `test_recruit_module.py` 里测。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import main as plugin_main
from core.module import Module
from core.registry import known_module_names, register_module, unregister_module
from modules.recruit.dataset import RecruitDataError


class FakeModule(Module):
    """够用的假模块：可选自报不可用，可选在 initialize 里抛错。"""

    name = "fake"
    config_key = "fake"

    def __init__(self, name: str, *, reason: str | None = None, boom: bool = False) -> None:
        self.name = name
        self.config_key = name
        self._reason = reason
        self._boom = boom

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    async def initialize(self, ctx: Any, config: Any) -> None:
        if self._boom:
            raise RuntimeError(f"{self.name} 起不来")

    async def terminate(self) -> None:
        return None


class RecordingLogger:
    """把宿主打的日志记下来，好断言它到底说了什么。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.infos: list[str] = []

    @staticmethod
    def _render(args: tuple[Any, ...]) -> str:
        if not args:
            return ""
        template, rest = args[0], args[1:]
        return template % rest if rest else str(template)

    def error(self, *args: Any, **_kwargs: Any) -> None:
        self.errors.append(self._render(args))

    def info(self, *args: Any, **_kwargs: Any) -> None:
        self.infos.append(self._render(args))

    # 宿主与模块可能用到的其余级别：记下来也不需要，吞掉即可
    debug = warning = exception = critical = error


class FakeEvent:
    def __init__(self, message_str: str) -> None:
        self.message_str = message_str
        self.unified_msg_origin = "test:FriendMessage:1"
        self.stopped = False

    def plain_result(self, text: str) -> str:
        return text

    def stop_event(self) -> None:
        self.stopped = True

    def is_private_chat(self) -> bool:
        return True

    def is_admin(self) -> bool:
        return False


class _FakeJob:
    def __init__(self, name: str, job_id: str) -> None:
        self.name = name
        self.job_id = job_id


class _FakeCronManager:
    """换班提醒模块要用它注册任务；只实现真正被调用到的那几个方法。"""

    def __init__(self) -> None:
        self.jobs: list[_FakeJob] = []

    async def list_jobs(self) -> list[_FakeJob]:
        return list(self.jobs)

    async def delete_job(self, job_id: str) -> None:
        self.jobs = [j for j in self.jobs if j.job_id != job_id]

    async def add_basic_job(self, **kwargs: Any) -> _FakeJob:
        job = _FakeJob(kwargs["name"], f"job-{len(self.jobs) + 1}")
        self.jobs.append(job)
        return job


@pytest.fixture(autouse=True)
def _isolate_registry():
    """隔离全局注册表：用完把本文件新登记的名字撤掉。"""
    before = set(known_module_names())
    yield
    for name in set(known_module_names()) - before:
        unregister_module(name)


def _toolbox(switches: dict[str, bool], **sections: Any) -> Any:
    ctx = SimpleNamespace(
        register_web_api=lambda *a, **k: None,
        cron_manager=_FakeCronManager(),
    )
    return plugin_main.ArknightsToolbox(ctx, {"modules": switches, **sections})


def _ak_reply(plugin: Any, message: str) -> str:
    """驱动 `/ak` 的异步生成器，把它回给用户的文本拼起来。"""

    async def drive() -> list[str]:
        return [chunk async for chunk in plugin.ak(FakeEvent(message))]

    return "\n".join(asyncio.run(drive()))


# --- 启动日志：不许把用不了的模块说成「已装载」 -------------------------------


def test_startup_log_is_unchanged_when_every_module_is_healthy(monkeypatch):
    """全绿时措辞一字不改——改造不许动既有行为（宪法 §2 第 5 条）。"""
    recorder = RecordingLogger()
    monkeypatch.setattr(plugin_main, "logger", recorder)
    register_module("healthy", lambda: FakeModule("healthy"))

    asyncio.run(_toolbox({"healthy": True}).initialize())

    assert "[ak_toolbox] 已装载模块：healthy" in recorder.infos
    assert recorder.errors == []


def test_startup_log_never_calls_a_degraded_module_loaded(monkeypatch):
    """缺陷本体：`recruit` 那种「装载成功但干不了活」的模块不许混进「已装载」。"""
    recorder = RecordingLogger()
    monkeypatch.setattr(plugin_main, "logger", recorder)
    register_module("healthy", lambda: FakeModule("healthy"))
    register_module("degraded", lambda: FakeModule("degraded", reason="数据文件不见了"))

    asyncio.run(_toolbox({"healthy": True, "degraded": True}).initialize())

    assert not any("已装载模块" in line and "degraded" in line for line in recorder.infos)
    assert any(
        "已装载但当前不可用" in line and "数据文件不见了" in line for line in recorder.errors
    )
    assert any("可用模块 1 个（healthy）" in line for line in recorder.errors)


def test_startup_log_distinguishes_failed_from_degraded(monkeypatch):
    """两种状态要分得清：一个压根没起来，一个是活着但干不了活。"""
    recorder = RecordingLogger()
    monkeypatch.setattr(plugin_main, "logger", recorder)
    register_module("degraded", lambda: FakeModule("degraded", reason="数据缺了"))
    register_module("dead", lambda: FakeModule("dead", boom=True))

    asyncio.run(_toolbox({"degraded": True, "dead": True}).initialize())

    assert any("已装载但当前不可用" in line and "degraded" in line for line in recorder.errors)
    assert any("启动失败" in line and "dead" in line for line in recorder.errors)
    assert any("可用模块 0 个" in line for line in recorder.errors)


# --- `/ak` 兜底回复：同理 ------------------------------------------------------


def test_ak_fallback_keeps_the_original_wording_when_all_modules_are_healthy():
    register_module("healthy", lambda: FakeModule("healthy"))
    plugin = _toolbox({"healthy": True})
    asyncio.run(plugin.initialize())

    reply = _ak_reply(plugin, "/ak 不存在的子命令")

    assert "已装载的模块：healthy" in reply.splitlines()
    assert "可用模块" not in reply


def test_ak_fallback_never_lists_a_degraded_module_as_usable():
    register_module("healthy", lambda: FakeModule("healthy"))
    register_module("degraded", lambda: FakeModule("degraded", reason="数据文件不见了"))
    plugin = _toolbox({"healthy": True, "degraded": True})
    asyncio.run(plugin.initialize())

    reply = _ak_reply(plugin, "/ak 不存在的子命令")
    # 逐行**全等**比对（去掉缩进），不用 `in`：写成子串断言时 `可用模块：healthy`
    # 会在 `可用模块：healthy、degraded` 上照样通过——正是这个缺陷要防的假绿灯
    lines = [line.strip() for line in reply.splitlines()]

    assert "可用模块：healthy" in lines
    assert "已装载但当前不可用的模块：" in lines
    assert "degraded：数据文件不见了" in lines
    # 这一条就是缺陷本身
    assert "已装载的模块：" not in reply


def test_ak_fallback_reports_failed_modules_separately():
    register_module("healthy", lambda: FakeModule("healthy"))
    register_module("dead", lambda: FakeModule("dead", boom=True))
    plugin = _toolbox({"healthy": True, "dead": True})
    asyncio.run(plugin.initialize())

    reply = _ak_reply(plugin, "/ak 不存在的子命令")

    assert "可用模块：healthy" in reply.splitlines()
    assert "启动失败的模块（对应功能不可用）：" in reply
    assert "dead：" in reply
    assert "已装载但当前不可用的模块：" not in reply


# --- 真实接缝：真模块 + 真宿主（待办 8 的原始复现场景） -----------------------


SHIFT_CONFIG = {
    "shift_1_name": "早班",
    "shift_1_start": "08:00",
    "shift_1_hours": 12,
    "shift_2_name": "晚班",
    "shift_2_start": "20:00",
    "shift_2_hours": 6,
    "shift_3_name": "夜班",
    "shift_3_start": "02:00",
    "shift_3_hours": 6,
    "lead_minutes": 10,
}


def test_the_original_server_scenario_end_to_end(monkeypatch, tmp_path):
    """招募数据没了，换班提醒照常可用，而日志**不再说谎**。

    这是 2026-09-29 在服务器上删掉数据文件后重启的原始场景，也是本包的存在理由。
    用真模块（`shift_reminder` + `recruit`）跑，只有框架与宿主依赖是替代品。
    """
    import modules.recruit.module as recruit_module
    import modules.shift_reminder.module as shift_module

    def _missing_data():
        raise RecruitDataError("找不到招募数据文件：/…/recruit_pool.json")

    monkeypatch.setattr(recruit_module, "load_data", _missing_data)
    # 别把测试数据写进仓库：让换班提醒把 state.json 落在 pytest 的临时目录
    monkeypatch.setattr(shift_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    recorder = RecordingLogger()
    monkeypatch.setattr(plugin_main, "logger", recorder)

    plugin = _toolbox(
        {"shift_reminder": True, "recruit": True},
        shift_reminder=SHIFT_CONFIG,
    )
    asyncio.run(plugin.initialize())

    registry = plugin._registry
    assert registry is not None
    assert registry.available_names == ("shift_reminder",)  # 核心功能活着
    assert dict(registry.degraded_modules)["recruit"]  # 公招不可用，且带原因

    # 汇总日志的措辞必须与事实一致
    summary = "\n".join(recorder.errors)
    assert "可用模块 1 个（shift_reminder）" in summary
    assert "已装载但不可用 1 个（recruit）" in summary
    assert "recruit_pool.json" in summary  # 原因带上，用户能自己修
    assert not any("已装载模块" in line for line in recorder.infos)

    # 用户问起来时，`/ak` 的兜底也要给出同样的答案
    reply = _ak_reply(plugin, "/ak 不存在的子命令")
    lines = [line.strip() for line in reply.splitlines()]
    assert "可用模块：shift_reminder" in lines
    assert "已装载但当前不可用的模块：" in lines
    assert any(line.startswith("recruit：") and "recruit_pool.json" in line for line in lines)


def test_the_real_host_still_works_when_no_module_is_broken(monkeypatch, tmp_path):
    """全绿路径的真实接缝：日志回到原来那一句，别把正常情况也改了口径。"""
    import modules.shift_reminder.module as shift_module

    monkeypatch.setattr(shift_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    recorder = RecordingLogger()
    monkeypatch.setattr(plugin_main, "logger", recorder)

    plugin = _toolbox(
        {"shift_reminder": True, "recruit": True},
        shift_reminder=SHIFT_CONFIG,
    )
    asyncio.run(plugin.initialize())

    assert any(
        line == "[ak_toolbox] 已装载模块：shift_reminder、recruit" for line in recorder.infos
    )
    assert recorder.errors == []
