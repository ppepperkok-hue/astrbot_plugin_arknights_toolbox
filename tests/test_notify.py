"""消息渲染测试（纯逻辑，不需要 AstrBot）。"""

from datetime import datetime

from modules.shift_reminder.notify import render_reminder, render_status
from modules.shift_reminder.schedule import Shift, validate

DEFAULT = [
    Shift("早班", 8 * 60, 12 * 60),
    Shift("晚班", 20 * 60, 6 * 60),
    Shift("夜班", 2 * 60, 6 * 60),
]


def test_render_reminder_contains_key_facts():
    text = render_reminder(
        ending=Shift("早班", 480, 720),
        starting=Shift("晚班", 1200, 360),
        change_at=datetime(2026, 9, 29, 20, 0),
        lead_minutes=10,
    )
    assert "10 分钟后换班" in text
    assert "20:00" in text
    assert "早班" in text
    assert "晚班" in text


def test_render_reminder_extra_slot_is_absent_by_default():
    text = render_reminder(
        ending=Shift("早班", 480, 720),
        starting=Shift("晚班", 1200, 360),
        change_at=datetime(2026, 9, 29, 20, 0),
        lead_minutes=10,
    )
    assert "干员" not in text


def test_render_reminder_accepts_extra_lines():
    """S3 插槽：V2 的干员名单会走 extra。"""
    text = render_reminder(
        ending=Shift("早班", 480, 720),
        starting=Shift("晚班", 1200, 360),
        change_at=datetime(2026, 9, 29, 20, 0),
        lead_minutes=10,
        extra=["贸易站：但书 / 推进之王", "制造站：迷迭香"],
    )
    assert "贸易站：但书 / 推进之王" in text
    assert "制造站：迷迭香" in text


def test_render_status_counts_down():
    text = render_status(
        now=datetime(2026, 9, 29, 19, 0),
        current=Shift("早班", 480, 720),
        upcoming=Shift("晚班", 1200, 360),
        change_at=datetime(2026, 9, 29, 20, 0),
        module_states={"shift_reminder": True},
        recent_sends=["09-29 07:50 早班"],
    )
    assert "还有 1 小时 0 分" in text
    assert "shift_reminder=开" in text
    assert "09-29 07:50 早班" in text


def test_render_status_without_history():
    text = render_status(
        now=datetime(2026, 9, 29, 19, 0),
        current=Shift("早班", 480, 720),
        upcoming=Shift("晚班", 1200, 360),
        change_at=datetime(2026, 9, 29, 20, 0),
        module_states={"shift_reminder": True},
        recent_sends=[],
    )
    assert "无记录" in text


def test_render_status_handles_past_change_point():
    """换班时刻已过时不该出现负数倒计时。"""
    text = render_status(
        now=datetime(2026, 9, 29, 20, 5),
        current=Shift("晚班", 1200, 360),
        upcoming=Shift("夜班", 120, 360),
        change_at=datetime(2026, 9, 30, 2, 0),
        module_states={"shift_reminder": True},
        recent_sends=[],
    )
    assert "还有 5 小时 55 分" in text


def test_validate_and_render_agree_on_names():
    """渲染用到的班次名必须来自同一张校验过的表。"""
    table = validate(DEFAULT)
    text = render_reminder(
        ending=table.shifts[1],
        starting=table.shifts[2],
        change_at=datetime(2026, 9, 29, 20, 0),
        lead_minutes=10,
    )
    assert table.shifts[1].name in text
    assert table.shifts[2].name in text
