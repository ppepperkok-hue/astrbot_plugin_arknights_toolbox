"""`modules/shift_reminder/roster.py` 的单测。

fixture 沿用 `tests/fixtures/infrast_*.json`（本项目自撰，结构与真实排班表同源；
许可证考量见 `tests/test_schedule_file.py` 顶部说明）。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from modules.shift_reminder.roster import (
    ROOM_LABELS,
    SHIFT_COUNT,
    RosterImportError,
    build_roster,
    describe_roster,
    parse_import_argument,
    render_roster_lines,
    resolve_import_path,
)
from modules.shift_reminder.schedule_file import parse_schedule_file

FIXTURES = Path(__file__).resolve().parent / "fixtures"
THREE_SHIFTS = FIXTURES / "infrast_three_shifts.json"
FOUR_SHIFTS_SKIP = FIXTURES / "infrast_four_shifts_with_skip.json"

IMPORTED_AT = datetime(2026, 1, 1, 0, 0)


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _minimal(plans: Any) -> str:
    return json.dumps({"title": "t", "planTimes": "3班", "plans": plans}, ensure_ascii=False)


def _roster_from(path: Path) -> dict[str, Any]:
    return build_roster(parse_schedule_file(_load(path)), source=path.name, imported_at=IMPORTED_AT)


# --- parse_import_argument ------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/ak import a.json", "a.json"),
        ("ak import a.json", "a.json"),
        ("  /ak   import   a.json  ", "a.json"),
        ("/AK IMPORT a.json", "a.json"),
        ("/ak import a b.json", "a b.json"),
        ("/ak import 我的排班.json", "我的排班.json"),
        ("/ak import", ""),
        ("ak import", ""),
        ("/ak status", ""),
        ("/ak", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_parse_import_argument(message: str, expected: str) -> None:
    assert parse_import_argument(message) == expected


def test_parse_import_argument_keeps_the_whole_remainder() -> None:
    """多余词不被静默丢弃——整段拿来，交给「文件是否存在」裁决。"""
    assert parse_import_argument("/ak import a.json extra") == "a.json extra"


# --- resolve_import_path：通过的路径 --------------------------------------


def test_resolves_a_plain_name_inside_the_directory(tmp_path: Path) -> None:
    base = tmp_path / "data"
    base.mkdir()
    resolved = resolve_import_path(base, "roster.json")
    assert resolved == (base / "roster.json").resolve()
    assert resolved.is_absolute()


def test_does_not_require_the_file_to_exist(tmp_path: Path) -> None:
    """纯粹性：本函数只判定路径合法性，文件存不存在由调用方报告。"""
    base = tmp_path / "data"
    base.mkdir()
    assert resolve_import_path(base, "nope.json").name == "nope.json"


def test_strips_surrounding_whitespace(tmp_path: Path) -> None:
    base = tmp_path / "data"
    base.mkdir()
    assert resolve_import_path(base, "  a.json  ").name == "a.json"


def test_accepts_uppercase_suffix(tmp_path: Path) -> None:
    base = tmp_path / "data"
    base.mkdir()
    assert resolve_import_path(base, "A.JSON").name == "A.JSON"


# --- resolve_import_path：拒绝的路径 --------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "../outside.json",
        "..\\outside.json",
        "sub/dir.json",
        "sub\\dir.json",
        "/etc/passwd",
        "\\windows\\x.json",
        "C:drive.json",
        "C:\\Windows\\x.json",
        "a.txt",
        "archive.zip",
        "noextension",
        "",
        "   ",
        "..",
        ".",
    ],
)
def test_rejects_dangerous_or_unsupported_names(tmp_path: Path, name: str) -> None:
    base = tmp_path / "data"
    base.mkdir()
    with pytest.raises(RosterImportError):
        resolve_import_path(base, name)


def test_rejects_symlink_pointing_outside_the_directory(tmp_path: Path) -> None:
    """只做字符串检查会放过软链——必须 `resolve()` 之后再比父目录。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.json"
    secret.write_text("{}", encoding="utf-8")

    base = tmp_path / "base"
    base.mkdir()
    link = base / "link.json"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):  # pragma: no cover - 取决于宿主权限
        pytest.skip("本机不允许创建符号链接（Windows 需开发者模式或管理员）")

    with pytest.raises(RosterImportError) as excinfo:
        resolve_import_path(base, "link.json")
    assert "数据目录之外" in str(excinfo.value)


def test_error_message_tells_the_user_what_to_fix(tmp_path: Path) -> None:
    base = tmp_path / "data"
    base.mkdir()
    with pytest.raises(RosterImportError) as excinfo:
        resolve_import_path(base, "../x.json")
    assert "不能带路径" in str(excinfo.value)

    with pytest.raises(RosterImportError) as excinfo:
        resolve_import_path(base, "")
    assert "用法" in str(excinfo.value)

    with pytest.raises(RosterImportError) as excinfo:
        resolve_import_path(base, "a.txt")
    assert ".json" in str(excinfo.value)


# --- build_roster ---------------------------------------------------------


def test_build_roster_from_the_three_shift_fixture() -> None:
    plans = parse_schedule_file(_load(THREE_SHIFTS))
    roster = build_roster(plans, source="三班.json", imported_at=datetime(2026, 9, 29, 3, 0))

    assert roster["source"] == "三班.json"
    assert roster["shift_count"] == 3
    assert roster["imported_at"] == "2026-09-29T03:00:00"
    assert [shift["plan_index"] for shift in roster["shifts"]] == [1, 2, 3]
    assert [shift["plan_name"] for shift in roster["shifts"]] == ["第一班", "第二班", "第三班"]


def test_build_roster_matches_the_file_contents() -> None:
    first = _roster_from(THREE_SHIFTS)["shifts"][0]["rooms"]

    assert [room["operators"] for room in first] == [
        ["黑键", "吉星", "可露希尔"],
        ["森蚺", "温蒂", "清流"],
        ["Lancet-2"],
        ["承曦格雷伊"],
    ]
    assert [room["room"] for room in first] == ["trading", "manufacture", "manufacture", "power"]
    assert [room["index"] for room in first] == [1, 1, 2, 1]


def test_build_roster_is_json_serialisable() -> None:
    roster = _roster_from(THREE_SHIFTS)
    assert json.loads(json.dumps(roster, ensure_ascii=False))["shift_count"] == 3


@pytest.mark.parametrize("plan_count", [0, 1, 2, 4, 5])
def test_build_roster_rejects_any_shift_count_other_than_three(plan_count: int) -> None:
    """4 班表在真实样本里存在；静默取前 3 会产出与配置对不上的错数据。"""
    plans = () if plan_count == 0 else parse_schedule_file(_minimal([{"rooms": {}}] * plan_count))

    with pytest.raises(RosterImportError) as excinfo:
        build_roster(plans, source="x.json", imported_at=IMPORTED_AT)
    assert f"{plan_count} 班" in str(excinfo.value)


def test_build_roster_rejects_the_four_shift_fixture() -> None:
    plans = parse_schedule_file(_load(FOUR_SHIFTS_SKIP))
    assert len(plans) == 4, "fixture 应当仍是 4 班表"

    with pytest.raises(RosterImportError) as excinfo:
        build_roster(plans, source="4班.json", imported_at=IMPORTED_AT)
    message = str(excinfo.value)
    assert "4 班" in message
    assert f"{SHIFT_COUNT} 班" in message


def test_skip_flag_is_carried_into_the_roster() -> None:
    """`skip: true` 的房间**不被丢弃**，而是带标记落盘，由渲染层决定不渲染。"""
    text = _minimal(
        [
            {"rooms": {"trading": [{"skip": True, "operators": ["甲", "乙"]}]}},
            {"rooms": {"trading": [{"skip": False, "operators": ["丙"]}]}},
            {"rooms": {}},
        ]
    )
    roster = build_roster(parse_schedule_file(text), source="x.json", imported_at=IMPORTED_AT)

    first_room = roster["shifts"][0]["rooms"][0]
    assert first_room["skipped"] is True
    assert first_room["operators"] == ["甲", "乙"], "跳过的房间也必须保留干员，不能丢数据"

    assert roster["shifts"][1]["rooms"][0]["skipped"] is False


def test_missing_skip_field_is_not_treated_as_skipped() -> None:
    """字段缺失算「要换」，否则会凭空少提醒一片房间。"""
    text = _minimal(
        [
            {"rooms": {"trading": [{"operators": ["甲"]}]}},
            {"rooms": {}},
            {"rooms": {}},
        ]
    )
    roster = build_roster(parse_schedule_file(text), source="x.json", imported_at=IMPORTED_AT)
    assert roster["shifts"][0]["rooms"][0]["skipped"] is False


# --- describe_roster ------------------------------------------------------


def test_describe_roster_reports_counts_that_can_be_checked() -> None:
    text = describe_roster(_roster_from(THREE_SHIFTS))

    assert "3 个班次" in text
    assert "第 1 班：4 个房间、8 位干员" in text
    assert "第 2 班：3 个房间、6 位干员" in text
    assert "第 3 班：1 个房间、3 位干员" in text
    assert "8 个房间、17 位干员" in text


def test_describe_roster_counts_skipped_rooms_separately() -> None:
    text = _minimal(
        [
            {"rooms": {"trading": [{"skip": True, "operators": ["甲"]}]}},
            {"rooms": {"trading": [{"skip": False, "operators": ["乙"]}]}},
            {"rooms": {}},
        ]
    )
    described = describe_roster(
        build_roster(parse_schedule_file(text), source="x.json", imported_at=IMPORTED_AT)
    )

    assert "其中 1 间标了「不动」" in described
    assert "2 个房间、2 位干员，1 间不动" in described


def test_describe_roster_rejects_broken_data() -> None:
    """数据被外部改坏时宁可显式报错，也不打一句错数字。"""
    with pytest.raises(RosterImportError):
        describe_roster({"shifts": "not-a-list"})
    with pytest.raises(RosterImportError):
        describe_roster({"shifts": [{"rooms": "not-a-list"}]})
    with pytest.raises(RosterImportError):
        describe_roster({"shifts": [{"rooms": ["not-a-mapping"]}]})


# --- render_roster_lines（V1.5.3：提醒里带干员） ----------------------------


def _roster_with(rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """只关心第一班，另外两班给空房间——凑够三班即可。"""
    return {
        "shifts": [
            {"plan_index": 1, "plan_name": "一班", "rooms": rooms},
            {"plan_index": 2, "plan_name": "二班", "rooms": []},
            {"plan_index": 3, "plan_name": "三班", "rooms": []},
        ]
    }


def test_render_roster_lines_returns_empty_for_none() -> None:
    """从未导入过排班表时返回空——提醒必须与从前一字不差。"""
    assert render_roster_lines(None, 1) == []


@pytest.mark.parametrize("plan_index", [0, 4, -1, 99])
def test_render_roster_lines_rejects_out_of_range_index(plan_index: int) -> None:
    assert (
        render_roster_lines(
            _roster_with([{"room": "trading", "index": 1, "operators": ["甲"]}]), plan_index
        )
        == []
    )


def test_render_roster_lines_skips_rooms_marked_skipped() -> None:
    """`skip: true` = 这一班这间房不要动。渲染出去就是**错误指令**。"""
    roster = _roster_with(
        [
            {"room": "trading", "index": 1, "operators": ["甲", "乙"], "skipped": False},
            {"room": "trading", "index": 2, "operators": ["丙"], "skipped": True},
        ]
    )

    lines = render_roster_lines(roster, 1)

    assert lines[0] == "【本班配制】"
    assert any("甲" in line for line in lines)
    assert not any("丙" in line for line in lines)


def test_render_roster_lines_skips_empty_rooms() -> None:
    roster = _roster_with(
        [
            {"room": "trading", "index": 1, "operators": []},
            {"room": "power", "index": 1, "operators": ["甲"]},
        ]
    )

    lines = render_roster_lines(roster, 1)

    assert len(lines) == 2  # 标题 + 一间房
    assert "发电站1" in lines[1]


def test_render_roster_lines_returns_empty_when_nothing_to_show() -> None:
    """全是空房/跳过的房间时返回空列表，而不是只回一个孤零零的标题。"""
    roster = _roster_with(
        [
            {"room": "trading", "index": 1, "operators": [], "skipped": False},
            {"room": "power", "index": 1, "operators": ["甲"], "skipped": True},
        ]
    )

    assert render_roster_lines(roster, 1) == []


def test_render_roster_lines_uses_chinese_room_labels() -> None:
    roster = _roster_with([{"room": "manufacture", "index": 2, "operators": ["甲", "乙"]}])

    assert render_roster_lines(roster, 1) == ["【本班配制】", "制造站2：甲、乙"]


def test_render_roster_lines_keeps_unknown_room_key() -> None:
    """未知房型**原样显示英文键**而不是丢掉——丢一间房等于给错名单。"""
    roster = _roster_with([{"room": "brand_new_room", "index": 1, "operators": ["甲"]}])

    assert render_roster_lines(roster, 1) == ["【本班配制】", "brand_new_room1：甲"]


def test_render_roster_lines_filters_blank_names() -> None:
    """干员名里的空白项要过滤掉，不能让它进到提醒文案里。"""
    roster = _roster_with([{"room": "trading", "index": 1, "operators": ["甲", "  ", "", "乙"]}])

    assert render_roster_lines(roster, 1) == ["【本班配制】", "贸易站1：甲、乙"]


def test_render_roster_lines_handles_broken_shapes_without_raising() -> None:
    """数据形状不对时安静返回空——提醒不能因为排班表坏了就不发。"""
    assert render_roster_lines({"shifts": "nope"}, 1) == []
    assert render_roster_lines({"shifts": [{"rooms": "nope"}]}, 1) == []
    assert render_roster_lines({"shifts": [{"rooms": ["nope"]}]}, 1) == []
    assert render_roster_lines({"shifts": [{"rooms": [{"operators": "甲"}]}]}, 1) == []


def test_room_labels_cover_every_room_type_in_fixtures() -> None:
    """fixture 里出现的房型都要有中文名——不然用户会看到生词。"""
    text = _load(THREE_SHIFTS)
    roster = build_roster(
        parse_schedule_file(text), source=THREE_SHIFTS.name, imported_at=IMPORTED_AT
    )

    seen = {room["room"] for shift in roster["shifts"] for room in shift["rooms"]}
    assert seen, "fixture 里应当有房间"
    assert seen <= set(ROOM_LABELS)
