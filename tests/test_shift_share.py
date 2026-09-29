"""共享班次表的格式与解析（`core/shift_share.py`）。

这一层是**跨模块那条通道的地基**，所以测试的重点不是"能读能写"，而是
**读不出来的那些情况必须分得开**：文件不存在（正常）、文件坏了（异常）、
格式版本不认识（升级不同步）、字段自相矛盾（手改过）。分成几句话说，
用户才不会被引去查一个不存在的问题——那是本项目反复吃过的亏。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.shift_share import (
    FORMAT_VERSION,
    SHARED_SHIFT_FILENAME,
    SharedShiftError,
    dump_shared,
    load_shared,
    shared_shift_path,
)
from core.shifts import parse_shift_slots

CONFIG = {
    "shift_1_name": "第 1 班",
    "shift_1_start": "08:00",
    "shift_1_hours": 12,
    "shift_2_name": "第 2 班",
    "shift_2_start": "20:00",
    "shift_2_hours": 6,
    "shift_3_name": "第 3 班",
    "shift_3_start": "02:00",
    "shift_3_hours": 6,
}

AT = datetime(2026, 9, 29, 14, 30, 15)


def _payload(**overrides: object) -> dict:
    payload = dump_shared(parse_shift_slots(CONFIG), timezone="Asia/Shanghai", generated_at=AT)
    payload.update(overrides)
    return payload


# --- 路径 -------------------------------------------------------------------


def test_path_is_inside_the_plugin_data_dir_and_named_for_its_purpose() -> None:
    """路径由**同一个函数**算出来，两端不会各拼一次而拼岔。

    文件名带 `shared_` 前缀：它和模块自己的 `state.json` 并排放在同一个目录里，
    排障的人要一眼看得出哪个是"给别的模块读的"。
    """
    path = shared_shift_path(Path("/data/plugin_data"), "astrbot_plugin_x")
    assert path == Path("/data/plugin_data/astrbot_plugin_x") / SHARED_SHIFT_FILENAME
    assert "shared" in SHARED_SHIFT_FILENAME
    assert path.name == SHARED_SHIFT_FILENAME


# --- 正常路径 ---------------------------------------------------------------


def test_round_trip_keeps_slot_order_names_times_and_durations() -> None:
    """写进去再读回来，**槽位顺序**必须原样保留。

    顺序是排班表 `plans` 下标对应的依据，丢了顺序就会"第 i 班配了另一班的干员"。
    """
    slots = parse_shift_slots(CONFIG)
    loaded = load_shared(dump_shared(slots, timezone="Asia/Shanghai", generated_at=AT))

    assert loaded.names == ("第 1 班", "第 2 班", "第 3 班")
    assert [s.start_minute for s in loaded.shifts] == [8 * 60, 20 * 60, 2 * 60]
    assert [s.duration_minutes for s in loaded.shifts] == [720, 360, 360]
    assert loaded.timezone == "Asia/Shanghai"
    assert loaded.generated_at == AT.isoformat()
    assert loaded.version == FORMAT_VERSION


def test_validated_table_is_sorted_by_start_time_while_slots_keep_config_order() -> None:
    """两套顺序是**有意不同**的，同时提供，谁都不许拿错。

    `shifts` 是槽位顺序（第 1/2/3 班），`table` 按开始时刻排序（02:00 在最前）。
    排班表对齐用前者，算换班时刻用后者。
    """
    loaded = load_shared(dump_shared(parse_shift_slots(CONFIG), timezone="UTC", generated_at=AT))

    assert loaded.names == ("第 1 班", "第 2 班", "第 3 班")
    assert [s.name for s in loaded.table.shifts] == ["第 3 班", "第 1 班", "第 2 班"]


def test_payload_is_json_serialisable_and_versioned() -> None:
    """落盘的必须是能 `json.dumps` 的普通结构，且带版本号。"""
    payload = _payload()

    text = json.dumps(payload, ensure_ascii=False)
    assert json.loads(text) == payload
    assert payload["version"] == FORMAT_VERSION
    assert payload["timezone"] == "Asia/Shanghai"


def test_missing_generated_at_is_tolerated_because_it_is_only_diagnostic() -> None:
    """`generated_at` 只用于排障，缺了就缺了——它不参与任何判断。"""
    payload = _payload()
    del payload["generated_at"]

    assert load_shared(payload).generated_at == ""


# --- 拒绝路径：每一种都要说清是"什么不对" ---------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ([], "根节点必须是对象"),
        ("不是对象", "根节点必须是对象"),
        (None, "根节点必须是对象"),
    ],
)
def test_non_object_roots_are_rejected(payload, expected) -> None:
    with pytest.raises(SharedShiftError, match=expected):
        load_shared(payload)


def test_unknown_version_is_reported_as_version_mismatch_not_corruption() -> None:
    """版本不认识要和"文件坏了"分开说：前者是升级不同步，后者是数据坏了。

    混成一句会让用户去查一个根本不存在的问题——本项目反复吃这个亏。
    """
    with pytest.raises(SharedShiftError, match="版本不认识"):
        load_shared(_payload(version=FORMAT_VERSION + 1))


@pytest.mark.parametrize("bad", [None, "", "   ", 123])
def test_bad_timezone_is_rejected(bad) -> None:
    with pytest.raises(SharedShiftError, match="timezone"):
        load_shared(_payload(timezone=bad))


@pytest.mark.parametrize("bad", [None, {}, "shifts", []])
def test_shifts_must_be_a_non_empty_list(bad) -> None:
    with pytest.raises(SharedShiftError, match="shifts"):
        load_shared(_payload(shifts=bad))


def test_shift_entry_must_be_an_object() -> None:
    with pytest.raises(SharedShiftError, match="必须是对象"):
        load_shared(_payload(shifts=["第 1 班"]))


@pytest.mark.parametrize("bad", [None, "", "  ", 7])
def test_shift_name_must_be_a_non_empty_string(bad) -> None:
    payload = _payload()
    payload["shifts"][0]["name"] = bad

    with pytest.raises(SharedShiftError, match="name"):
        load_shared(payload)


def test_bad_start_is_reported_with_the_reason_from_the_shift_model() -> None:
    """时刻格式的判据只有一处（`core.shifts.parse_hhmm`），这里只负责转述。"""
    payload = _payload()
    payload["shifts"][0]["start"] = "8点"

    with pytest.raises(SharedShiftError, match="start 不合法"):
        load_shared(payload)


def test_contradictory_start_and_start_minute_is_rejected() -> None:
    """两个字段互相矛盾时**不许挑一个信**——那正是静默错位的温床。"""
    payload = _payload()
    payload["shifts"][0]["start_minute"] = 999

    with pytest.raises(SharedShiftError, match="自相矛盾"):
        load_shared(payload)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("start_minute", "480"), ("duration_minutes", "720"), ("duration_minutes", None)],
)
def test_numeric_fields_must_be_ints(field, bad) -> None:
    payload = _payload()
    payload["shifts"][0][field] = bad

    with pytest.raises(SharedShiftError, match=field):
        load_shared(payload)


def test_bool_is_not_accepted_as_a_number() -> None:
    """`bool` 是 `int` 的子类：不单独挡一下，`True` 会被当成第 1 分钟。"""
    payload = _payload()
    payload["shifts"][0]["start_minute"] = True

    with pytest.raises(SharedShiftError, match="start_minute"):
        load_shared(payload)


def test_wrong_slot_numbers_are_rejected() -> None:
    """槽位必须是 1..N 且按顺序——它是 plans 下标对应的依据，不能靠猜。"""
    payload = _payload()
    payload["shifts"][0]["slot"] = 9

    with pytest.raises(SharedShiftError, match="槽位"):
        load_shared(payload)


def test_missing_slot_is_rejected() -> None:
    payload = _payload()
    del payload["shifts"][1]["slot"]

    with pytest.raises(SharedShiftError, match="slot"):
        load_shared(payload)


def test_a_table_that_fails_whole_table_validation_is_rejected_with_its_reason() -> None:
    """整表校验（时长和 = 24h）沿用 `core.shifts.validate`，不在这里另写一套。"""
    payload = _payload()
    for item in payload["shifts"]:
        item["duration_minutes"] = 60  # 三小时，不是二十四小时
        item["start_minute"] = 0
        item["start"] = "00:00"

    with pytest.raises(SharedShiftError, match="整体校验不通过"):
        load_shared(payload)


def test_null_shift_entry_row_is_rejected() -> None:
    """`{"shifts": [null, ...]}` 这种也要给出可读的错误，而不是 TypeError。"""
    payload = _payload()
    payload["shifts"][0] = None

    with pytest.raises(SharedShiftError, match="必须是对象"):
        load_shared(payload)


# --- 写入侧的自我检查 -------------------------------------------------------


def test_dump_rejects_the_wrong_number_of_slots() -> None:
    """写入方自己的错误要当场暴露——写出一份读不回来的文件，读取方要几小时后才发现。"""
    with pytest.raises(SharedShiftError, match="槽位数量"):
        dump_shared((), timezone="UTC", generated_at=AT)


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_dump_rejects_a_blank_timezone(bad) -> None:
    with pytest.raises(SharedShiftError, match="时区名"):
        dump_shared(parse_shift_slots(CONFIG), timezone=bad, generated_at=AT)


def test_two_modules_would_agree_on_the_name_and_shape() -> None:
    """格式只有一份：这条用例钉的是"读取方拿到的东西与写入方写下的完全对应"。

    它不测两个模块（那需要真的起两个模块），而是钉住**中间的协议**——
    两侧都只能经由这两个函数打交道。
    """
    payload = _payload()
    assert set(payload) == {"version", "generated_at", "timezone", "shifts"}
    assert set(payload["shifts"][0]) == {
        "slot",
        "name",
        "start",
        "start_minute",
        "duration_minutes",
    }
