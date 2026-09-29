"""`core.permission.session_allowed` 的权限判定测试（纯函数，不 import astrbot）。

权限模型与理由见 `docs/implementation/implementation.md` 的「S5 硬性完成判据」：
私聊一律放行（否则改绑会死锁），群聊仅限 AstrBot 管理员（否则会把结果推给整个群）。

**这个文件测的是共享实现本身**。规则只有一份（`core/permission.py`），各模块
只是传入自己的 `action` 文案——所以这里既不该出现模块特有的子命令清单，
也不该测「未知子命令」：那件事属于各模块 `handle_command` 的分发职责，
在各自的模块测试里覆盖。
"""

import pytest

from core.permission import session_allowed

PRIVATE = {"is_group": False, "is_admin": False}
GROUP_MEMBER = {"is_group": True, "is_admin": False}
GROUP_ADMIN = {"is_group": True, "is_admin": True}
ACTION = "操作换班提醒"


# --- 私聊：无条件放行（这条是改绑死锁的解药） ------------------------------


def test_private_chat_is_always_allowed() -> None:
    """回归：旧模型下绑了 A 就再也换不到 B，此处必须放行。

    旧实现只认「已绑定的那个 umo」，于是「改绑」与「门禁」互相矛盾，
    绑了 A 之后从 B 发 `bind` 一律被拒——A 那个号一旦掉线，功能永久锁死。
    """
    for is_admin in (False, True):
        allowed, reason = session_allowed(is_group=False, is_admin=is_admin, action=ACTION)
        assert allowed is True
        assert reason == ""


# --- 群聊：仅限管理员 -------------------------------------------------------


def test_group_chat_allows_admin() -> None:
    allowed, reason = session_allowed(is_group=True, is_admin=True, action=ACTION)
    assert allowed is True
    assert reason == ""


def test_group_chat_rejects_non_admin() -> None:
    allowed, reason = session_allowed(is_group=True, is_admin=False, action=ACTION)
    assert allowed is False
    assert reason


def test_group_rejection_explains_why_and_how() -> None:
    """拒绝文案要说清「为什么被拒」+「怎样才能做到」，且不承诺做不到的事。"""
    _, reason = session_allowed(**GROUP_MEMBER, action=ACTION)

    assert "管理员" in reason
    assert "群" in reason
    # 必须给出可行的替代路径：私聊
    assert "私聊" in reason
    # 不许再出现旧文案里那句在群聊语境下做不到的指引
    assert "回到原会话" not in reason


def test_rejection_mentions_the_module_action() -> None:
    """文案必须带上调用方给的说法——否则用户不知道是哪件事被拒了。

    这正是把 `action` 做成参数（而不是写死一句「权限不足」）的意义：
    同一个会话里可能有好几个模块的指令，笼统的拒绝理由帮不上忙。

    例子用的是**仍然受限**的动作。`recruit`（公开招募）曾经拿它举过例，但那个
    模块现在刻意不做门禁（见 `docs/implementation/pre-release-audit.md` S7），
    再拿它举例会让人以为公招在群里也受限。
    """
    _, reason = session_allowed(**GROUP_MEMBER, action="操作换班提醒")
    assert "操作换班提醒" in reason


def test_group_admin_is_the_only_difference() -> None:
    """同为群聊，管理员与非管理员结论必须相反。"""
    assert session_allowed(**GROUP_ADMIN, action=ACTION)[0] is True
    assert session_allowed(**GROUP_MEMBER, action=ACTION)[0] is False


# --- 真值表整体 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_group", "is_admin", "expected"),
    [
        (False, False, True),  # 私聊、非管理员 → 放行
        (False, True, True),  # 私聊、管理员   → 放行
        (True, False, False),  # 群聊、非管理员 → 拒绝
        (True, True, True),  # 群聊、管理员   → 放行
    ],
)
def test_permission_matrix(is_group: bool, is_admin: bool, expected: bool) -> None:
    """把整张真值表钉住，防止有人顺手改松一格。"""
    allowed, _ = session_allowed(is_group=is_group, is_admin=is_admin, action=ACTION)
    assert allowed is expected, f"group={is_group} admin={is_admin}"
