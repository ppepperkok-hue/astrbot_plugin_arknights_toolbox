"""`modules/shift_reminder/roster.py` 的单测。

fixture 沿用 `tests/fixtures/infrast_*.json`（本项目自撰，结构与真实排班表同源；
许可证考量见 `tests/test_schedule_file.py` 顶部说明）。
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from modules.shift_reminder.roster import (
    MAX_ROSTER_LINES,
    ROOM_LABELS,
    SHIFT_COUNT,
    RosterImportError,
    build_roster,
    describe_duration_hint,
    describe_roster,
    parse_import_argument,
    render_roster_lines,
    resolve_import_path,
    suggested_durations,
    suggested_hours,
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


# --- 长度上限（V1.5.4：别让提醒刷屏） ----------------------------------------


def _displayable_room(n: int) -> dict[str, Any]:
    """第 n 间可显示的房间；干员名带上序号，方便断言截断截在了哪。"""
    return {"room": "manufacture", "index": n, "operators": [f"干员{n}"], "skipped": False}


def test_render_roster_lines_at_exactly_the_limit_has_no_ellipsis() -> None:
    """恰好用满额度时不出现省略提示，且内容与加限制之前一致。"""
    body_budget = MAX_ROSTER_LINES - 1  # 扣掉标题那一行
    lines = render_roster_lines(
        _roster_with([_displayable_room(i) for i in range(1, body_budget + 1)]), 1
    )

    assert len(lines) == MAX_ROSTER_LINES
    assert not any("未显示" in line for line in lines), "没超限就不该有省略提示"
    assert lines[0] == "【本班配制】"
    assert lines[1] == "制造站1：干员1"
    assert lines[-1] == f"制造站{body_budget}：干员{body_budget}"


def test_render_roster_lines_ellipsis_counts_the_omitted_rooms() -> None:
    """超限时截断，且提示里的数字**等于真被省略的项数**（最容易算错的地方）。"""
    total = MAX_ROSTER_LINES + 3
    lines = render_roster_lines(
        _roster_with([_displayable_room(i) for i in range(1, total + 1)]), 1
    )

    assert len(lines) == MAX_ROSTER_LINES, "截断后总行数不得超上限"
    assert lines[0] == "【本班配制】"
    shown_rooms = len(lines) - 2  # 扣掉标题行与省略提示行
    assert shown_rooms > 0, "额度再小也该显示至少一间房，否则等于没有信息"
    assert lines[-1] == f"……还有 {total - shown_rooms} 间未显示"


@pytest.mark.parametrize("count", range(1, 15))
def test_render_roster_lines_never_exceeds_the_limit(count: int) -> None:
    """边界扫描：从 1 间到远超上限，返回值都不许超过 MAX_ROSTER_LINES。"""
    lines = render_roster_lines(
        _roster_with([_displayable_room(i) for i in range(1, count + 1)]), 1
    )
    assert len(lines) <= MAX_ROSTER_LINES


def test_render_roster_lines_skipped_rooms_neither_show_nor_count_as_omitted() -> None:
    """标了「不动」的房间不占额度，也不算进「未显示」的数字里。"""
    displayable = MAX_ROSTER_LINES + 3
    rooms = [_displayable_room(i) for i in range(1, displayable + 1)]
    for i in range(1, 4):
        rooms.insert(0, {"room": "dormitory", "index": i, "operators": ["甲"], "skipped": True})

    lines = render_roster_lines(_roster_with(rooms), 1)

    assert len(lines) == MAX_ROSTER_LINES
    assert "宿舍" not in "".join(lines), "标了不动的房间不该出现"
    shown_rooms = len(lines) - 2
    assert lines[-1] == f"……还有 {displayable - shown_rooms} 间未显示"


# --- 从名字里读出的时长提示（V1.5.6） ----------------------------------------
#
# 这一组测的是「排班表里的班次时长如何被读出来、落盘、以及怎么告诉用户」。
# 名字解析本身的边界用例在 `tests/test_plan_name.py`，这里只管**集成与落盘形状**。


def _plans_named(names: list[str]) -> str:
    """造一份只关心班次名字的最小排班表（房间内容与本题无关）。"""
    return _minimal(
        [{"name": name, "rooms": {"trading": [{"operators": ["A"]}]}} for name in names]
    )


def _roster_named(names: list[str]) -> dict[str, Any]:
    return build_roster(
        parse_schedule_file(_plans_named(names)), source="x.json", imported_at=IMPORTED_AT
    )


def test_build_roster_records_the_rhythm_read_from_names() -> None:
    """真实导出格式：12/6/6 被读出来 → 落盘成分钟数。"""
    roster = _roster_named(["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"])

    assert roster["duration_hints_minutes"] == [720, 360, 360]


def test_build_roster_leaves_no_hint_when_names_carry_none() -> None:
    """现有三班 fixture 的名字（第一班/第二班/第三班）不含时长 → 明确为 None。"""
    roster = _roster_from(THREE_SHIFTS)

    assert roster["duration_hints_minutes"] is None


def test_build_roster_rejects_a_rhythm_that_is_not_24_hours() -> None:
    """每个名字都读得出，但合计 1950 分钟 ≠ 24 小时 → 整条建议作废。"""
    roster = _roster_named(["A 组 12 H", "B 组 12H", "C 组 8.5H"])

    assert roster["duration_hints_minutes"] is None


def test_build_roster_never_writes_config_keys() -> None:
    """**设计保证**：导入只产出排班表数据，绝不夹带班次配置。

    否则「导入一下就把用户的班次设置改了」——那是最不能接受的一种副作用。
    """
    roster = _roster_named(["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"])

    assert set(roster) == {
        "source",
        "imported_at",
        "shift_count",
        "duration_hints_minutes",
        "shifts",
    }
    # 配置键的形状是 `shift_<序号>_<字段>`（如 `shift_1_name`）与 `lead_minutes`。
    # 只按前缀 `shift_` 判断会误伤 `shift_count`，所以要按真实形状挡。
    config_like = [
        key for key in roster if re.fullmatch(r"shift_\d+_\w+", key) or key == "lead_minutes"
    ]
    assert config_like == [], f"导入结果里混进了配置键：{config_like}"


def test_roster_with_hints_is_still_json_serialisable() -> None:
    roster = _roster_named(["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"])

    assert json.loads(json.dumps(roster, ensure_ascii=False))["duration_hints_minutes"] == [
        720,
        360,
        360,
    ]


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {},
        {"duration_hints_minutes": None},
        {"duration_hints_minutes": "720,360,360"},
        # 个数不对
        {"duration_hints_minutes": [720, 720]},
        {"duration_hints_minutes": [720, 360, 360, 0]},
        # 非整数 / 布尔 / 非正
        {"duration_hints_minutes": [720.5, 360, 359.5]},
        {"duration_hints_minutes": [True, 720, 720]},
        {"duration_hints_minutes": [0, 720, 720]},
        {"duration_hints_minutes": [-720, 1080, 1080]},
        # 合计不是 24 小时
        {"duration_hints_minutes": [720, 720, 720]},
    ],
)
def test_suggested_durations_refuses_broken_stored_shapes(stored: Any) -> None:
    """落盘的数据可能被外部改坏或被旧版本写成别的形状——那时当作「没有提示」。"""
    assert suggested_durations(stored) is None


def test_suggested_durations_reads_back_a_good_rhythm() -> None:
    assert suggested_durations(_roster_named(["12h", "6h", "6h"])) == (720, 360, 360)


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        # 全是整点小时 → 可以直接填进配置
        (["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"], (12, 6, 6)),
        (["A+B 16H", "A+C 4H", "B+C 4H"], (16, 4, 4)),
        (["8h", "8h", "8h"], (8, 8, 8)),
        # 含非整点小时 → 配置存不下（`shift_N_hours` 是整数），不给建议
        (["8.5h", "8.5h", "7h"], None),
        # 读不出节奏
        (["第一班", "第二班", "第三班"], None),
    ],
)
def test_suggested_hours_only_offers_what_the_config_can_store(
    names: list[str], expected: tuple[int, ...] | None
) -> None:
    assert suggested_hours(_roster_named(names)) == expected


def test_suggested_hours_refuses_broken_stored_shapes() -> None:
    assert suggested_hours(None) is None
    assert suggested_hours({"duration_hints_minutes": [720, 360, 360, 0]}) is None


def test_describe_duration_hint_is_actionable_when_a_rhythm_was_read() -> None:
    text = describe_duration_hint(_roster_named(["Shift 1 · 12h", "Shift 2 · 6h", "Shift 3 · 6h"]))

    assert "12 小时" in text and "6 小时" in text
    assert "按排班表填入" in text, "要告诉用户去哪儿用它，否则这句话没有可操作性"
    assert "不动你的设置" in text, "必须说清点了才生效"


def test_describe_duration_hint_explains_when_it_cannot_auto_fill() -> None:
    """含非整点小时：说清为什么不能自动填，而不是让用户点了才在保存时吃错。"""
    text = describe_duration_hint(_roster_named(["8.5h", "8.5h", "7h"]))

    assert "8 小时 30 分" in text
    assert "整点" in text
    assert "按排班表填入" not in text, "不能自动填时不该引导他去点按钮"


def test_describe_duration_hint_says_so_when_nothing_was_read() -> None:
    text = describe_duration_hint(_roster_from(THREE_SHIFTS))

    assert "照旧" in text or "手动" in text
    assert "按排班表填入" not in text


def test_describe_duration_hint_distinguishes_an_inconsistent_rhythm() -> None:
    """**读出来了但合计不对** ≠ **读不出来** —— 这两句对用户是完全不同的信息。

    实测样本 ``333_layout_for_Orundum`` 就是这种：名字里有 12/12/8.5，合计 32.5 小时。
    若笼统报「名字里没有可用的时长信息」，用户会以为插件读不出时长而反复重传。
    """
    text = describe_duration_hint(_roster_named(["A 组 12 H", "B 组 12H", "C 组 8.5H"]))

    assert "12 小时" in text and "8 小时 30 分" in text, "读到的节奏要摊出来"
    assert "不是 24 小时" in text, "要说清拒绝的理由"
    assert "没有可用的时长信息" not in text
    assert "按排班表填入" not in text


def test_describe_duration_hint_handles_missing_roster() -> None:
    assert describe_duration_hint(None)  # 返回一句人话，不抛异常
