"""`core/shifts.py`——共享班次模型的单测。

为什么单独一个文件（而不是并进 `test_schedule.py`）：那个文件测的是
**`shift_reminder` 的用法**；这里测的是**模型本身**，包括"它只此一份"这件事。
两者失败时的含义不同，混在一起会让"是模型错了还是模块用错了"变得难判断。
"""

from datetime import datetime, timedelta

import pytest

from core.shifts import (
    MINUTES_PER_DAY,
    SHIFT_SLOTS,
    ConfigError,
    Shift,
    ShiftTable,
    boundaries_between,
    current_shift,
    format_hhmm,
    parse_hhmm,
    parse_shift_slots,
    parse_shift_table,
    reminders_between,
    validate,
)

MORNING = Shift("早班", 8 * 60, 12 * 60)
EVENING = Shift("晚班", 20 * 60, 6 * 60)
NIGHT = Shift("夜班", 2 * 60, 6 * 60)
TABLE = validate([MORNING, EVENING, NIGHT])


# --- 单一事实来源（本文件最重要的一条） --------------------------------------


def test_module_schedule_is_the_same_objects_as_core() -> None:
    """`modules/shift_reminder/schedule.py` 必须是 core 的**同一批对象**。

    这是"没有第二份实现"的对象级证明：壳里若哪天又写了一个 `class Shift`，
    两边就不再是同一个类型，`isinstance` 与异常捕获会开始出诡异问题，而
    `git grep` 未必看得出来。所以用身份断言把它钉死。
    """
    from modules.shift_reminder import schedule as shim

    assert shim.Shift is Shift
    assert shim.ShiftTable is ShiftTable
    assert shim.ConfigError is ConfigError
    assert shim.parse_shift_table is parse_shift_table
    assert shim.validate is validate
    assert shim.boundaries_between is boundaries_between
    assert shim.current_shift is current_shift
    assert shim.reminders_between is reminders_between
    assert shim.format_hhmm is format_hhmm
    assert shim.parse_hhmm is parse_hhmm
    assert shim.MINUTES_PER_DAY == MINUTES_PER_DAY


def test_core_has_no_framework_or_host_dependency() -> None:
    """`core/shifts.py` 必须是纯逻辑：不 import astrbot，也不 import 其它 core 模块。

    用源码扫描而不是靠自觉——本项目已经因为"纯逻辑偷偷依赖"栽过一次
    （`credentials.py` 的绝对导入掀翻了整个插件的加载，见
    `scripts/check_astrbot_load_form.py` 的 docstring）。
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "core" / "shifts.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert imported, "至少应该 import 一些标准库——空的说明扫描写错了"
    for name in imported:
        assert not name.startswith("astrbot"), f"纯逻辑层不该依赖 astrbot：{name}"
        assert name != "core" and not name.startswith("core."), (
            f"纯逻辑层不该依赖 core 自身：{name}"
        )
        assert name != "logging", f"不许用标准库 logging：{name}"


# --- 配置解析与校验（非法配置是必测项） ---------------------------------------


def test_parse_shift_table_reads_the_three_slots() -> None:
    """三班按配置顺序读出来，并按开始时刻排好序。"""
    table = parse_shift_table(
        {
            "shift_1_name": "早班",
            "shift_1_start": "08:00",
            "shift_1_hours": 12,
            "shift_2_name": "晚班",
            "shift_2_start": "20:00",
            "shift_2_hours": 6,
            "shift_3_name": "夜班",
            "shift_3_start": "02:00",
            "shift_3_hours": 6,
        }
    )
    assert [s.name for s in table.shifts] == ["夜班", "早班", "晚班"]
    assert table.start_minutes == (120, 480, 1200)
    assert SHIFT_SLOTS == (1, 2, 3)


#: 一份**槽位顺序与时刻顺序刻意相反**的配置：`shift_1` 是 20:00 那班。
#:
#: 用它测 `parse_shift_slots` 才有意义——若两者顺序相同，函数有没有保序就看不出差别。
OUT_OF_TIME_ORDER = {
    "shift_1_name": "第 1 班",
    "shift_1_start": "20:00",
    "shift_1_hours": 6,
    "shift_2_name": "第 2 班",
    "shift_2_start": "08:00",
    "shift_2_hours": 12,
    "shift_3_name": "第 3 班",
    "shift_3_start": "02:00",
    "shift_3_hours": 6,
}


def test_parse_shift_slots_keeps_the_config_order() -> None:
    """槽位顺序要**原样保留**：这是按 `plans` 下标对齐排班表的唯一依据。

    `parse_shift_table` 会按时刻排序（那份顺序对"现在几点该换班"是对的，
    对"第 i 班配了多长"是错的），所以两者必须都存在、且分工写清楚。
    """
    slots = parse_shift_slots(OUT_OF_TIME_ORDER)

    assert [s.name for s in slots] == ["第 1 班", "第 2 班", "第 3 班"]
    assert [s.start_minute for s in slots] == [1200, 480, 120]
    # 对比：同一份配置进 `parse_shift_table` 出来是按时刻排的，顺序确实不同。
    assert [s.start_minute for s in parse_shift_table(OUT_OF_TIME_ORDER).shifts] == [
        120,
        480,
        1200,
    ]


def test_parse_shift_slots_leaves_whole_table_validation_to_parse_shift_table() -> None:
    """`parse_shift_slots` 只做逐项校验：**总和不是 24 小时**时它不拦，整表校验才拦。

    这条钉的是分工边界。若哪天有人把 `validate()` 塞进 `parse_shift_slots`，
    装配层就没法再"先按槽位读、再整表校验"，而两种顺序的用途会重新混起来。
    """
    broken = dict(OUT_OF_TIME_ORDER)
    broken["shift_1_hours"] = 12  # 12 + 12 + 6 = 30 小时

    slots = parse_shift_slots(broken)

    assert [s.duration_minutes for s in slots] == [720, 720, 360]
    with pytest.raises(ConfigError, match="正好是 24 小时"):
        parse_shift_table(broken)


def test_parse_shift_slots_still_rejects_item_level_errors() -> None:
    """逐项校验**不能**因为"只做一半"而放松——写坏一项要当场说清是哪一项。"""
    broken = dict(OUT_OF_TIME_ORDER)
    broken["shift_2_hours"] = True  # bool 也是 int，必须单独挡

    with pytest.raises(ConfigError, match="shift_2_hours 必须是整数小时数"):
        parse_shift_slots(broken)


def test_parse_shift_table_agrees_with_validating_the_slots() -> None:
    """两条路是同一套解析：`parse_shift_table` 就是"逐项读 + 整表校验"。"""
    assert parse_shift_table(OUT_OF_TIME_ORDER) == validate(parse_shift_slots(OUT_OF_TIME_ORDER))


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"shift_1_name": ""}, "shift_1_name 必须是非空字符串"),
        ({"shift_1_name": 7}, "shift_1_name 必须是非空字符串"),
        ({"shift_1_start": "08:0:0"}, "时刻格式应为 HH:MM"),
        ({"shift_1_start": 800}, "shift_1_start 必须是 HH:MM 字符串"),
        ({"shift_1_start": "24:00"}, "时刻超出范围"),
        ({"shift_1_hours": True}, "shift_1_hours 必须是整数小时数"),
        ({"shift_1_hours": "12"}, "shift_1_hours 必须是整数小时数"),
        ({"shift_1_hours": 11}, "所有班次时长之和必须正好是 24 小时"),
    ],
)
def test_parse_shift_table_rejects_illegal_values(
    overrides: dict[str, object], fragment: str
) -> None:
    """每一种写坏配置的方式都要**当场抛错并说清哪一项**，不静默降级。

    `bool` 单独测一条：Python 里 `True` 也是 `int`，漏判会让 `shift_1_hours=True`
    静默变成 1 小时——那是"看起来成功但配置是错的"。
    """
    config: dict[str, object] = {
        "shift_1_name": "早班",
        "shift_1_start": "08:00",
        "shift_1_hours": 12,
        "shift_2_name": "晚班",
        "shift_2_start": "20:00",
        "shift_2_hours": 6,
        "shift_3_name": "夜班",
        "shift_3_start": "02:00",
        "shift_3_hours": 6,
    }
    config.update(overrides)
    with pytest.raises(ConfigError) as excinfo:
        parse_shift_table(config)
    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "shifts",
    [
        (),
        (Shift("A", 480, 0), Shift("B", 480, MINUTES_PER_DAY)),
        (Shift("A", 480, 11 * 60), Shift("B", 1200, 6 * 60), Shift("C", 120, 6 * 60)),
        (Shift("A", 480, 12 * 60), Shift("B", 1140, 6 * 60), Shift("C", 60, 6 * 60)),
    ],
    ids=["empty", "zero-duration", "total-not-24h", "not-contiguous"],
)
def test_validate_rejects_bad_tables(shifts: tuple[Shift, ...]) -> None:
    with pytest.raises(ConfigError):
        validate(shifts)


def test_validate_orders_by_start_minute_and_wraps() -> None:
    """排序 + 跨天相接：夜班 02:00–08:00 收在早班 08:00，最后绕回夜班。"""
    table = validate([MORNING, EVENING, NIGHT])
    assert table.start_minutes == (120, 480, 1200)
    assert NIGHT.end_minute == 8 * 60
    assert EVENING.end_minute == 2 * 60
    assert table.starting_at(480) is MORNING
    assert table.starting_at(481) is None


# --- 时刻换算 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("minute", "text"),
    [
        (0, "00:00"),
        (1, "00:01"),
        (59, "00:59"),
        (60, "01:00"),
        (479, "07:59"),
        (720, "12:00"),
        (1439, "23:59"),
        (1440, "00:00"),
    ],
)
def test_format_hhmm(minute: int, text: str) -> None:
    assert format_hhmm(minute) == text


@pytest.mark.parametrize("value", ["8:00", "08:00", "0:00", "2:00", "23:59"])
def test_parse_hhmm_accepts_padded_and_unpadded(value: str) -> None:
    assert parse_hhmm(value) == int(value.split(":")[0]) * 60 + int(value.split(":")[1])


@pytest.mark.parametrize("value", ["8:0:0", "24:00", "aa:bb", "", "1", "12:60", "-1:00"])
def test_parse_hhmm_rejects(value: str) -> None:
    with pytest.raises(ConfigError):
        parse_hhmm(value)


# --- 相邻换班时刻推导（共享概念的核心） ---------------------------------------


def test_boundaries_are_strictly_after_now_and_sorted() -> None:
    """边界必须是 `(now, now+days]`：刚好等于 now 的那一刻**不算**，
    否则「现在正在换班」会被当成"下一次换班"，提醒会立刻误触发一次。"""
    now = datetime(2026, 9, 29, 8, 0)
    found = boundaries_between(TABLE, now)
    assert found, "三天内必须有边界"
    assert all(moment > now for moment in found)
    assert found == sorted(found)
    assert found[0] == datetime(2026, 9, 29, 20, 0)


def test_boundaries_cover_each_shift_once_per_day() -> None:
    now = datetime(2026, 9, 29, 0, 1)
    found = boundaries_between(TABLE, now, days=1)
    assert [m.hour * 60 + m.minute for m in found] == [120, 480, 1200]


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 9, 29, 23, 59),  # 跨 00:00
        datetime(2026, 9, 30, 0, 1),
        datetime(2026, 9, 30, 1, 59),
        datetime(2026, 12, 31, 23, 59),  # 跨年
        datetime(2027, 1, 1, 0, 0),
        datetime(2026, 2, 28, 23, 0),  # 月末
    ],
)
def test_boundaries_survive_midnight_month_and_year_rollover(now: datetime) -> None:
    found = boundaries_between(TABLE, now)
    assert found and found == sorted(found)
    assert all(moment > now for moment in found)
    # 每个边界都必须正好落在某个班次的开始时刻上，否则下游查不到 shift。
    for moment in found:
        assert TABLE.starting_at(moment.hour * 60 + moment.minute) is not None


def test_reminders_subtract_the_lead_and_cross_midnight() -> None:
    """提前量把提醒拉到**前一天**也算数：夜班 02:00 提前 3 小时 → 前一天 23:00。"""
    now = datetime(2026, 9, 29, 22, 30)
    pairs = reminders_between(TABLE, now, lead_minutes=180)
    assert pairs, "跨天的那一侧也要给出提醒"
    first_moment, first_shift = pairs[0]
    assert first_shift.name == "夜班"
    assert first_moment == datetime(2026, 9, 30, 2, 0) - timedelta(minutes=180)


def test_reminders_exclude_moments_already_passed() -> None:
    """已经过去的提醒不再返回：否则重启后会把历史提醒补发一遍。"""
    now = datetime(2026, 9, 29, 7, 55)
    pairs = reminders_between(TABLE, now, lead_minutes=10)
    assert all(remind_at > now for remind_at, _ in pairs)
    assert datetime(2026, 9, 29, 7, 50) not in [remind_at for remind_at, _ in pairs]


def test_reminders_reject_negative_lead() -> None:
    with pytest.raises(ConfigError):
        reminders_between(TABLE, datetime(2026, 9, 29, 8, 0), lead_minutes=-1)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 00:30 仍在晚班里：晚班 20:00 起、跨天到 02:00。写成「夜班」是我的
        # 期望错了，实现对（改造前后逐字节一致的基线可以佐证）。
        (datetime(2026, 9, 29, 0, 30), "晚班"),
        (datetime(2026, 9, 29, 1, 59), "晚班"),
        (datetime(2026, 9, 29, 2, 0), "夜班"),
        (datetime(2026, 9, 29, 7, 59), "夜班"),
        (datetime(2026, 9, 29, 8, 0), "早班"),
        (datetime(2026, 9, 29, 19, 59), "早班"),
        (datetime(2026, 9, 29, 20, 0), "晚班"),
        (datetime(2026, 9, 29, 23, 59), "晚班"),
        (datetime(2026, 9, 30, 1, 59), "晚班"),
        (datetime(2026, 9, 30, 2, 0), "夜班"),
    ],
)
def test_current_shift_including_the_wrap(now: datetime, expected: str) -> None:
    assert current_shift(TABLE, now).name == expected
