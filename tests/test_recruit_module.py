"""装配层测试：权限、指令分发、配置读取、以及装载失败时的行为。

异步部分用 `asyncio.run` 直接驱动，**不引 pytest-asyncio**：技术栈规定运行时与
开发依赖都尽量为零（`docs/tech-stack.md` §7），而同仓库的其它模块测试早就是这个
写法。这里会 import `modules/recruit/module.py`（它 import astrbot），由 `conftest.py`
注入的最小 stub 顶着。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core.registry import build_registry, discover_modules, known_module_names
from modules.recruit.dataset import RecruitDataError
from modules.recruit.module import (
    DEFAULT_MAX_COMBINATIONS,
    DEFAULT_MAX_OPERATORS,
    LIMIT_CEILING,
    RecruitModule,
    read_limit,
)
from modules.recruit.parsing import parse_tags


class _FakeCtx:
    """够用的假 ctx：只记**真正送出去**的文本，并可选择返回 False 或抛异常。"""

    def __init__(self, *, ok: bool = True, raises: bool = False) -> None:
        self.sent: list[str] = []
        self.attempts = 0
        self._ok = ok
        self._raises = raises

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.attempts += 1
        if self._raises:
            raise RuntimeError("平台已离线")
        if self._ok:
            self.sent.append("".join(chain.parts))
        return self._ok


class _Event:
    """够用的假事件：只提供装配层实际用到的四个成员。"""

    def __init__(self, text: str = "", *, group: bool = False, admin: bool = False) -> None:
        self.message_str = text
        self.unified_msg_origin = "test:FriendMessage:1"
        self._group = group
        self._admin = admin

    def is_private_chat(self) -> bool:
        return not self._group

    def is_admin(self) -> bool:
        return self._admin


def _ready_module(ctx: _FakeCtx, config: dict | None = None) -> RecruitModule:
    module = RecruitModule()
    asyncio.run(module.initialize(ctx, config or {}))
    return module


def _say(module: RecruitModule, text: str) -> None:
    asyncio.run(module.handle_command("recruit", _Event(text)))


def _say_group(module: RecruitModule, event: _Event) -> None:
    asyncio.run(module.handle_command("recruit", event))


# --- read_limit -------------------------------------------------------------


def test_read_limit_returns_the_configured_value() -> None:
    assert read_limit({"max_operators": 5}, "max_operators", 20) == 5


def test_read_limit_falls_back_when_the_key_is_missing() -> None:
    assert read_limit({}, "max_operators", DEFAULT_MAX_OPERATORS) == DEFAULT_MAX_OPERATORS


@pytest.mark.parametrize("bad", ["20", 1.5, None, True, [1]])
def test_read_limit_falls_back_on_non_integer(bad: Any) -> None:
    """`True` 是 int 的子类，但它显然不是「20 条」那种意思，必须挡住。"""
    assert read_limit({"max_operators": bad}, "max_operators", 20) == 20


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_read_limit_falls_back_on_below_one(bad: int) -> None:
    assert read_limit({"max_operators": bad}, "max_operators", 20) == 20


def test_read_limit_clamps_to_the_ceiling() -> None:
    """配成 1000 会让一条 QQ 消息长到没法看，压到上限而不是照单全收。"""
    assert read_limit({"max_operators": 1000}, "max_operators", 20) == LIMIT_CEILING


# --- parse_tags -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ak recruit 输出 近战位", ("输出", "近战位")),
        ("/ak recruit 输出", ("输出",)),
        ("recruit 输出", ("输出",)),
        ("输出 近战位", ("输出", "近战位")),
        ("ak recruit", ()),
        ("", ()),
        ("   ", ()),
    ],
)
def test_parse_tags_strips_command_words_only(text: str, expected: tuple[str, ...]) -> None:
    """AstrBot 剥掉唤醒前缀但留下指令名，所以这几种形态都要认。"""
    assert parse_tags(text) == expected


def test_parse_tags_keeps_the_users_order() -> None:
    assert parse_tags("ak recruit 近战位 输出") == ("近战位", "输出")


# --- 指令分发 ---------------------------------------------------------------


def test_recruit_without_arguments_lists_tags() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx)

    handled = asyncio.run(module.handle_command("recruit", _Event("ak recruit")))

    assert handled is True
    assert len(ctx.sent) == 1
    assert "可填标签" in ctx.sent[0]


def test_recruit_with_tags_returns_a_result() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx)

    handled = asyncio.run(module.handle_command("recruit", _Event("ak recruit 输出 近战位")))

    assert handled is True
    assert "候选" in ctx.sent[0]


def test_unknown_subcommand_is_left_to_other_modules() -> None:
    """返回 False 才能让宿主继续问下一个模块。"""
    ctx = _FakeCtx()
    module = _ready_module(ctx)

    handled = asyncio.run(module.handle_command("status", _Event("ak status")))

    assert handled is False
    assert ctx.sent == []


def test_group_command_is_allowed_for_a_plain_member() -> None:
    """公开招募**刻意不做会话门禁**：它只读、无状态、不碰绑定。

    原先它沿用了「绑定」那条群聊限制（群里只有管理员能用），属于**限制错了
    对象**——2026-09-29 发布前审查的 S7。实测场景是：用户把机器人拉进群，发一条
    `/ak recruit 输出 近战位`，收到「只有管理员能查公开招募」，于是以为功能坏了。
    这条用例钉住「群聊里普通成员也能用」，防止有人把门禁加回来。
    """
    ctx = _FakeCtx()
    module = _ready_module(ctx)

    _say_group(module, _Event("ak recruit 输出", group=True))

    assert len(ctx.sent) == 1
    assert "管理员" not in ctx.sent[0]
    assert "候选" in ctx.sent[0]


def test_group_command_is_allowed_for_admin() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx)

    _say_group(module, _Event("ak recruit 输出", group=True, admin=True))

    assert "候选" in ctx.sent[0]


# --- 配置驱动 ---------------------------------------------------------------


def test_max_operators_from_config_truncates_the_output() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx, {"max_operators": 1})

    _say(module, "ak recruit 输出")

    assert "还有" in ctx.sent[0]


def test_apply_config_updates_the_limits() -> None:
    """配置改动后要跟着变，否则页面改了值、行为还是旧的。"""
    ctx = _FakeCtx()
    module = _ready_module(ctx, {"max_operators": LIMIT_CEILING})

    asyncio.run(module.apply_config({"max_operators": 1}))
    _say(module, "ak recruit 输出")

    assert "还有" in ctx.sent[0]


def test_default_limits_are_used_when_config_is_empty() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx)
    assert module._max_operators == DEFAULT_MAX_OPERATORS
    assert module._max_combinations == DEFAULT_MAX_COMBINATIONS


# --- 装载失败：必须显式，但**不许**拖垮别的模块 -----------------------------


def _boom_data_error() -> None:
    """模拟数据文件缺失（服务器实测时正是删掉文件触发的）。"""
    raise RecruitDataError("数据文件不见了")


def test_data_failure_does_not_raise_from_initialize(monkeypatch) -> None:
    """`initialize` 抛错会让宿主回滚全部模块、插件整体起不来。

    招募数据坏了只说明这个模块没用，不该连累换班提醒——与 §2.7 铁律同一条原则。
    """

    monkeypatch.setattr("modules.recruit.module.load_data", _boom_data_error)

    ctx = _FakeCtx()
    module = RecruitModule()
    asyncio.run(module.initialize(ctx, {}))  # 不抛

    _say(module, "ak recruit")

    assert "数据文件不见了" in ctx.sent[0]
    assert "换班提醒不受影响" in ctx.sent[0]


def test_data_failure_is_reported_to_the_host_not_hidden(monkeypatch) -> None:
    """光记 ERROR 不够：宿主还得能问出「你到底能不能用」。

    2026-09-29 的缺陷就是日志说不可用、宿主汇总却说已装载——同一件事的两个说法。
    """
    monkeypatch.setattr("modules.recruit.module.load_data", _boom_data_error)

    module = RecruitModule()
    asyncio.run(module.initialize(_FakeCtx(), {}))

    reason = module.unavailable_reason
    assert reason is not None
    assert "数据文件不见了" in reason


def test_a_healthy_module_reports_no_unavailable_reason() -> None:
    module = _ready_module(_FakeCtx())
    assert module.unavailable_reason is None


def test_registry_sees_a_data_less_recruit_as_loaded_but_unusable(monkeypatch) -> None:
    """真实接缝：真模块 + 真注册表——宿主必须把它算成「已装载但不可用」。

    这是 plan.md 待办 8 的原始复现场景。
    """
    monkeypatch.setattr("modules.recruit.module.load_data", _boom_data_error)

    discover_modules()
    registry = build_registry({"recruit": True})
    asyncio.run(registry.start_all(ctx=_FakeCtx(), config={}))

    assert registry.started_names == ("recruit",)  # 活着
    assert registry.available_names == ()  # 但干不了活
    ((name, reason),) = registry.degraded_modules
    assert name == "recruit"
    assert "数据文件不见了" in reason


# --- 回执路径：平台离线不许冒泡 ---------------------------------------------


def test_reply_survives_send_message_returning_false() -> None:
    ctx = _FakeCtx(ok=False)
    module = _ready_module(ctx)

    _say(module, "ak recruit")

    assert ctx.sent == []  # 没送出去，但没崩


def test_reply_survives_send_message_raising() -> None:
    """异常冒泡会让用户发指令毫无反应——这正是 2026-09-29 事故的同类路径。"""
    ctx = _FakeCtx(raises=True)
    module = _ready_module(ctx)

    _say(module, "ak recruit")  # 不抛


def test_reply_before_initialize_is_reported_not_raised() -> None:
    module = RecruitModule()
    _say(module, "ak recruit")  # 不抛


# --- terminate --------------------------------------------------------------


def test_terminate_is_safe_to_call_twice() -> None:
    ctx = _FakeCtx()
    module = _ready_module(ctx)
    asyncio.run(module.terminate())
    asyncio.run(module.terminate())  # 不得抛


# --- 自动发现：这是「加模块不改宿主」的验证 ---------------------------------


def test_discover_modules_finds_recruit() -> None:
    """模块名 = 目录名 = `Module.name`，注册表校验通过才说明契约挂对了。"""
    found = discover_modules()

    assert "recruit" in found
    assert "recruit" in known_module_names()
    assert found["recruit"]().name == "recruit"
    assert found["recruit"]().config_key == "recruit"


def test_both_modules_are_discovered_together() -> None:
    """工具箱第一次真正装了两个模块——老模块不该因此消失。"""
    found = discover_modules()
    assert {"shift_reminder", "recruit"} <= set(found)


def test_module_instances_come_from_the_registry_factory() -> None:
    """注册表用无参工厂实例化，模块构造函数必须不收参数。"""
    instance = discover_modules()["recruit"]()
    assert isinstance(instance, RecruitModule)
