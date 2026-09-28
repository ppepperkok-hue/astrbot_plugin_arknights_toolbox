"""三班模型与时刻计算的边界测试。

这些测试直接跑纯逻辑，不需要 AstrBot —— 这正是架构里"纯逻辑与框架解耦"的收益。
"""

from datetime import datetime

import pytest

from modules.shift_reminder.schedule import (
    ConfigError,
    Shift,
    boundaries_between,
    current_shift,
    format_hhmm,
    parse_hhmm,
    reminders_between,
    validate,
)

# 默认三班：早班 08:00–20:00（12h）/ 晚班 20:00–02:00（6h）/ 夜班 02:00–08:00（6h）
DEFAULT = [
    Shift("早班", 8 * 60, 12 * 60),
    Shift("晚班", 20 * 60, 6 * 60),
    Shift("夜班", 2 * 60, 6 * 60),
]


def table():
    return validate(DEFAULT)


# --- parse_hhmm ------------------------------------------------------------


def test_parse_hhmm_ok():
    assert parse_hhmm("08:00") == 480
    assert parse_hhmm("20:00") == 1200
    assert parse_hhmm("02:00") == 120
    assert parse_hhmm("00:00") == 0
    assert parse_hhmm("23:59") == 1439


@pytest.mark.parametrize("bad", ["08-00", "0800", "", "aa:bb", "24:00", "08:60", "08:00:00", 800])
def test_parse_hhmm_rejects(bad):
    with pytest.raises(ConfigError):
        parse_hhmm(bad)


@pytest.mark.parametrize(("text", "expected"), [("8:00", 480), ("8:5", 485)])
def test_parse_hhmm_is_lenient_about_padding(text, expected):
    """配置是手填的，`8:00` 这种写法应当被接受。"""
    assert parse_hhmm(text) == expected


def test_format_hhmm_roundtrip():
    for text in ("00:00", "02:00", "08:00", "20:00", "23:59"):
        assert format_hhmm(parse_hhmm(text)) == text


# --- validate --------------------------------------------------------------


def test_validate_default_ok():
    result = validate(DEFAULT)
    assert [s.name for s in result.shifts] == ["夜班", "早班", "晚班"]  # 按起始时刻排序
    assert result.start_minutes == (120, 480, 1200)


def test_validate_rejects_empty():
    with pytest.raises(ConfigError, match="至少要配置一个班次"):
        validate([])


def test_validate_rejects_wrong_total():
    with pytest.raises(ConfigError, match="24 小时"):
        validate(
            [Shift("早班", 480, 11 * 60), Shift("晚班", 1200, 6 * 60), Shift("夜班", 120, 6 * 60)]
        )


def test_validate_rejects_gap():
    # 三段总时长仍是 24 小时，但「晚班」从 19:00 开始会与早班重叠 1 小时，
    # 于是 07:00–08:00 之间没人值班 —— 总和对了，首尾仍然不相接。
    with pytest.raises(ConfigError, match="班次不连续"):
        validate(
            [
                Shift("早班", 8 * 60, 12 * 60),  # 08:00–20:00
                Shift("晚班", 19 * 60, 6 * 60),  # 19:00–01:00（与早班重叠）
                Shift("夜班", 1 * 60, 6 * 60),  # 01:00–07:00
            ]
        )


def test_validate_rejects_duplicate_names():
    with pytest.raises(ConfigError, match="不能重复"):
        validate(
            [Shift("早班", 480, 12 * 60), Shift("早班", 1200, 6 * 60), Shift("夜班", 120, 6 * 60)]
        )


def test_validate_rejects_non_positive_duration():
    with pytest.raises(ConfigError, match="必须大于 0"):
        validate([Shift("早班", 480, 0), Shift("晚班", 480, 24 * 60)])


# --- current_shift（跨天） -------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (8, 0, "早班"),
        (19, 59, "早班"),
        (20, 0, "晚班"),
        (23, 30, "晚班"),
        (0, 30, "晚班"),  # 跨天：仍处于 20:00 开始的晚班
        (1, 59, "晚班"),
        (2, 0, "夜班"),
        (7, 59, "夜班"),
    ],
)
def test_current_shift(hour, minute, expected):
    now = datetime(2026, 9, 29, hour, minute)
    assert current_shift(table(), now).name == expected


# --- boundaries_between ----------------------------------------------------


def test_boundaries_are_sorted_and_start_after_now():
    now = datetime(2026, 9, 29, 19, 30)
    found = boundaries_between(table(), now, days=3)
    assert found == sorted(found)
    assert all(moment > now for moment in found)
    assert found[0] == datetime(2026, 9, 29, 20, 0)
    assert found[1] == datetime(2026, 9, 30, 2, 0)
    assert found[2] == datetime(2026, 9, 30, 8, 0)
    assert len(found) == 9  # 3 班 × 3 天


def test_boundaries_cross_new_year():
    now = datetime(2026, 12, 31, 21, 0)
    found = boundaries_between(table(), now, days=1)
    assert found[0] == datetime(2027, 1, 1, 2, 0)


def test_boundaries_cross_month_end():
    now = datetime(2026, 9, 30, 21, 0)
    found = boundaries_between(table(), now, days=1)
    assert found[0] == datetime(2026, 10, 1, 2, 0)


# --- reminders_between（提前量、跨天） -------------------------------------


def test_reminders_apply_lead_time():
    now = datetime(2026, 9, 29, 19, 0)
    pairs = reminders_between(table(), now, lead_minutes=10, days=1)
    first_at, first_shift = pairs[0]
    assert first_at == datetime(2026, 9, 29, 19, 50)
    assert first_shift.name == "晚班"


def test_reminders_lead_crosses_midnight():
    # 夜班 02:00 换班、提前 10 分钟 → 01:50，跨天仍要算出来
    now = datetime(2026, 9, 29, 23, 0)
    pairs = reminders_between(table(), now, lead_minutes=10, days=1)
    crossing = [p for p in pairs if p[0] == datetime(2026, 9, 30, 1, 50)]
    assert crossing, "跨天的 01:50 提醒没有被算出来"
    assert crossing[0][1].name == "夜班"


def test_reminders_skip_already_passed():
    now = datetime(2026, 9, 29, 19, 55)  # 19:50 那一班已经过了
    pairs = reminders_between(table(), now, lead_minutes=10, days=1)
    assert all(moment > now for moment, _ in pairs)


def test_reminders_reject_negative_lead():
    with pytest.raises(ConfigError):
        reminders_between(table(), datetime(2026, 9, 29, 12, 0), lead_minutes=-1)
