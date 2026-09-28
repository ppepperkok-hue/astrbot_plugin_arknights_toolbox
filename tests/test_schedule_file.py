"""`modules/shift_reminder/schedule_file.py` 的单测。

fixture 来源说明
----------------
`tests/fixtures/infrast_*.json` 是**本项目自撰**的文件：字段名与嵌套形状与真实排班表
（MAA `resource/custom_infrast` 与 riic.autos 导出同源格式）一致，内容自撰。

**为什么不直接裁剪真实样本**：MAA 仓库是 **AGPL-3.0**（核实方式：
`api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights` 返回
`license.spdx_id == "AGPL-3.0"`）。把它的文件复制进本 MIT 仓库等于引入许可证传染，
正是立项调研 R5 点名要避开的。**格式与结构不受版权保护**，因此按结构自撰；
真实样本只在开发期用于人工核对字段形状。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from modules.shift_reminder.schedule_file import (
    PlanAssignment,
    ScheduleFileError,
    parse_schedule_file,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
THREE_SHIFTS = FIXTURES / "infrast_three_shifts.json"
FOUR_SHIFTS_SKIP = FIXTURES / "infrast_four_shifts_with_skip.json"


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _minimal_plans(plans: Any) -> str:
    """最小的合法外壳：只换 plans，其余字段与真实文件同形。"""
    return _dump({"title": "t", "planTimes": "3班", "plans": plans})


# --- 真实形状样本 ---------------------------------------------------------


def test_parses_the_three_shift_fixture() -> None:
    plans = parse_schedule_file(_load(THREE_SHIFTS))

    assert len(plans) == 3
    assert [plan.name for plan in plans] == ["第一班", "第二班", "第三班"]


def test_first_room_of_first_plan_matches_the_file() -> None:
    plan = parse_schedule_file(_load(THREE_SHIFTS))[0]

    assert isinstance(plan, PlanAssignment)
    first = plan.rooms[0]
    assert first.room == "trading"
    assert first.index == 1
    assert first.operators == ("黑键", "吉星", "可露希尔")


def test_rooms_keep_the_file_order() -> None:
    """房间顺序必须照抄文件——重排会让「第几个制造站」对不上游戏里的位置。"""
    plan = parse_schedule_file(_load(THREE_SHIFTS))[0]

    assert [r.room for r in plan.rooms] == [
        "trading",
        "manufacture",
        "manufacture",
        "power",
    ]


def test_index_counts_within_a_room_type_from_one() -> None:
    plan = parse_schedule_file(_load(THREE_SHIFTS))[0]
    manufacture = [r for r in plan.rooms if r.room == "manufacture"]

    assert [r.index for r in manufacture] == [1, 2]
    assert manufacture[1].operators == ("Lancet-2",)


def test_empty_operator_entry_is_skipped_but_others_survive() -> None:
    """第一班的宿舍 `operators` 是空列表（真实样本里 29/340 条如此）→ 跳过该条目。"""
    plan = parse_schedule_file(_load(THREE_SHIFTS))[0]

    assert "dormitory" not in {r.room for r in plan.rooms}
    assert len(plan.rooms) == 4  # 原本 5 条，宿舍那条被跳过


def test_four_plan_file_is_parsed_without_truncation() -> None:
    """四班表照单全收——「班次数与 plans 数量不等」是调用方要面对的问题，不是解析器的。"""
    plans = parse_schedule_file(_load(FOUR_SHIFTS_SKIP))

    assert len(plans) == 4
    assert [p.name for p in plans] == ["1 7h", "2 5h", "3 7h", "4 5h"]


def test_skipped_rooms_are_not_filtered_out() -> None:
    """当前实现**不过滤** `skip: true` 的房间。

    这是刻意的：任务包把范围限定在 `operators` 一件事上，而在解析器里静默过滤会让调用方
    看不到数据被丢。真实数据的分布是 skip=False/True 各 322/18 条，其中 15 条 skip=True
    仍带着干员名——这属于渲染层要做的决定（已单独上报总监）。
    如果哪天决定过滤，这条断言就是那个行为的看门人，必须一起改。
    """
    plans = parse_schedule_file(_load(FOUR_SHIFTS_SKIP))
    second = plans[1]

    assert [r.operators for r in second.rooms] == [
        ("空弦", "黑键", "但书"),  # skip: true
        ("清流", "温蒂", "森蚺"),  # skip: true
        ("槐琥", "阿罗玛", "砾"),  # skip: false
        ("缪尔赛思",),  # skip: true
    ]


# --- 条目级瑕疵：跳过该条目，保留其余 -------------------------------------


def test_entry_without_operators_is_skipped() -> None:
    text = _minimal_plans(
        [
            {
                "name": "第一班",
                "rooms": {"trading": [{"skip": False, "sort": True}, {"operators": ["甲"]}]},
            }
        ]
    )

    rooms = parse_schedule_file(text)[0].rooms

    assert len(rooms) == 1
    assert rooms[0].index == 2, "跳过条目不得让后续 index 重排"


def test_operators_that_is_not_a_list_is_skipped() -> None:
    text = _minimal_plans([{"name": "第一班", "rooms": {"trading": [{"operators": "甲"}]}}])

    assert parse_schedule_file(text)[0].rooms == ()


def test_blank_and_non_string_operators_are_filtered() -> None:
    text = _minimal_plans(
        [{"name": "第一班", "rooms": {"trading": [{"operators": ["  ", "甲", 7, "", "乙"]}]}}]
    )

    assert parse_schedule_file(text)[0].rooms[0].operators == ("甲", "乙")


def test_entry_whose_operators_filter_down_to_nothing_is_skipped() -> None:
    """全是脏数据等于没有可用内容——跳过，而不是产出一个空名单的房间。"""
    text = _minimal_plans(
        [{"name": "第一班", "rooms": {"trading": [{"operators": ["", "   ", 3]}]}}]
    )

    assert parse_schedule_file(text)[0].rooms == ()


def test_non_dict_entry_is_treated_as_having_no_operators() -> None:
    text = _minimal_plans(
        [{"name": "第一班", "rooms": {"trading": ["坏条目", {"operators": ["甲"]}]}}]
    )

    rooms = parse_schedule_file(text)[0].rooms

    assert len(rooms) == 1
    assert rooms[0].index == 2


# --- 展示用字段缺失时不许编造 ---------------------------------------------


def test_missing_plan_name_becomes_empty_string() -> None:
    text = _minimal_plans([{"rooms": {"trading": [{"operators": ["甲"]}]}}])

    assert parse_schedule_file(text)[0].name == ""


def test_non_string_plan_name_becomes_empty_string() -> None:
    text = _minimal_plans([{"name": 123, "rooms": {"trading": []}}])

    assert parse_schedule_file(text)[0].name == ""


# --- 结构性错误：整份不可用，必须抛 ---------------------------------------


def test_rejects_text_that_is_not_json() -> None:
    with pytest.raises(ScheduleFileError, match="不是合法的 JSON"):
        parse_schedule_file("{ not json")


def test_rejects_root_that_is_not_an_object() -> None:
    with pytest.raises(ScheduleFileError, match="根节点应为对象"):
        parse_schedule_file("[1, 2, 3]")


def test_rejects_missing_plans() -> None:
    with pytest.raises(ScheduleFileError, match="缺少 plans"):
        parse_schedule_file(_dump({"title": "没有 plans"}))


def test_rejects_plans_that_is_not_a_list() -> None:
    with pytest.raises(ScheduleFileError, match="缺少 plans"):
        parse_schedule_file(_dump({"plans": {"a": 1}}))


def test_rejects_empty_plans() -> None:
    with pytest.raises(ScheduleFileError, match="plans 为空"):
        parse_schedule_file(_dump({"plans": []}))


def test_rejects_plan_that_is_not_an_object() -> None:
    with pytest.raises(ScheduleFileError, match=r"plans\[1\] 应为对象"):
        parse_schedule_file(_dump({"plans": [{"rooms": {}}, "坏班次"]}))


def test_rejects_missing_rooms() -> None:
    with pytest.raises(ScheduleFileError, match=r"plans\[0\]\.rooms 应为对象"):
        parse_schedule_file(_dump({"plans": [{"name": "第一班"}]}))


def test_rejects_rooms_that_is_not_an_object() -> None:
    with pytest.raises(ScheduleFileError, match=r"plans\[0\]\.rooms 应为对象"):
        parse_schedule_file(_dump({"plans": [{"rooms": []}]}))


def test_rejects_room_whose_value_is_not_a_list() -> None:
    """房型的值不是列表 → 结构性错误。

    分界理由：容器（我们要按名字进去的东西）走不通，说明这不是我们认的格式；
    而叶子（某个条目的 operators）没内容只是瑕疵。**宁可显式失败，也不静默丢掉一间房。**
    """
    with pytest.raises(ScheduleFileError, match=r"rooms\['trading'\] 应为列表"):
        parse_schedule_file(_dump({"plans": [{"rooms": {"trading": {"operators": []}}}]}))


def test_error_message_points_at_the_offending_plan_index() -> None:
    with pytest.raises(ScheduleFileError) as excinfo:
        parse_schedule_file(_dump({"plans": [{"rooms": {}}, {"rooms": {}}, {"rooms": 5}]}))

    assert "plans[2]" in str(excinfo.value)
