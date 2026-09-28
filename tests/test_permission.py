"""`command_allowed` 的权限判定测试（纯函数，不 import astrbot）。

权限模型与理由见 `docs/implementation/implementation.md` 的「S5 硬性完成判据」：
私聊一律放行（否则改绑会死锁），群聊仅限 AstrBot 管理员（否则会把提醒推到整个群）。

规则只要一个纯函数 + 一张真值表就能钉住，所以这里不做任何框架模拟。
"""

import pytest

from modules.shift_reminder.module import COMMAND_NAMES, command_allowed

PRIVATE = {"is_group": False, "is_admin": False}
GROUP_MEMBER = {"is_group": True, "is_admin": False}
GROUP_ADMIN = {"is_group": True, "is_admin": True}


# --- 私聊：无条件放行（这条是改绑死锁的解药） ------------------------------


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_private_chat_allows_every_command(command: str) -> None:
    allowed, reason = command_allowed(command, **PRIVATE)
    assert allowed is True
    assert reason == ""


def test_private_chat_allows_bind_from_a_second_session() -> None:
    """回归：旧模型下绑了 A 就再也换不到 B，此处必须放行。

    旧 `_is_allowed` 只认「已绑定的那个 umo」，于是「改绑」与「门禁」互相矛盾，
    绑了 A 之后从 B 发 `bind` 一律被拒——A 那个号一旦掉线，功能永久锁死。
    """
    for command in COMMAND_NAMES:
        allowed, _ = command_allowed(command, is_group=False, is_admin=False)
        assert allowed is True, f"私聊里 {command} 不应被门禁挡住"


# --- 群聊：仅限管理员 -------------------------------------------------------


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_group_chat_allows_admin(command: str) -> None:
    allowed, reason = command_allowed(command, **GROUP_ADMIN)
    assert allowed is True
    assert reason == ""


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_group_chat_rejects_non_admin(command: str) -> None:
    allowed, reason = command_allowed(command, **GROUP_MEMBER)
    assert allowed is False
    assert reason


def test_group_rejection_explains_why_and_how() -> None:
    """拒绝文案要说清「为什么被拒」+「怎样才能做到」，且不承诺做不到的事。"""
    _, reason = command_allowed("bind", **GROUP_MEMBER)

    assert "管理员" in reason
    assert "群" in reason
    # 必须给出可行的替代路径：私聊
    assert "/ak bind" in reason
    assert "私聊" in reason
    # 不许再出现旧文案里那句在群聊语境下做不到的指引
    assert "回到原会话" not in reason


def test_group_admin_is_the_only_difference() -> None:
    """同一命令、同为群聊，管理员与非管理员结论必须相反。"""
    for command in COMMAND_NAMES:
        assert command_allowed(command, **GROUP_ADMIN)[0] is True
        assert command_allowed(command, **GROUP_MEMBER)[0] is False


# --- 未知子命令 -------------------------------------------------------------


@pytest.mark.parametrize("command", ["", "help", "BIND", "bind ", "unknown"])
def test_unknown_command_is_rejected(command: str) -> None:
    allowed, reason = command_allowed(command, **PRIVATE)
    assert allowed is False
    assert "未知子命令" in reason


def test_unknown_command_rejection_lists_available_ones() -> None:
    _, reason = command_allowed("nope", **PRIVATE)
    for name in COMMAND_NAMES:
        assert name in reason


# --- 真值表整体 -------------------------------------------------------------


def test_permission_matrix() -> None:
    """把整张真值表钉住，防止有人顺手改松一格。"""
    matrix = {
        (False, False): True,  # 私聊、非管理员 → 放行
        (False, True): True,  # 私聊、管理员   → 放行
        (True, False): False,  # 群聊、非管理员 → 拒绝
        (True, True): True,  # 群聊、管理员   → 放行
    }
    for command in COMMAND_NAMES:
        for (is_group, is_admin), expected in matrix.items():
            allowed, _ = command_allowed(command, is_group=is_group, is_admin=is_admin)
            assert allowed is expected, f"{command} group={is_group} admin={is_admin}"
