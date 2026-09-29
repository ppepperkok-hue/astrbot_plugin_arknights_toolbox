"""`avatars` 纯逻辑单测：干员名字到头像外链的映射。

这一层的价值全在**边界**上：有映射时给出正确 URL 是容易的，难的是「没有映射时
不许编 URL」——编出来的地址会让页面显示一个断图，比直接退回首字色块更糟。
所以下面的用例重点覆盖各种「查不到」的形态。
"""

import json
from pathlib import Path

from modules.shift_reminder import avatars
from modules.shift_reminder.avatars import (
    DEFAULT_URL_TEMPLATE,
    AvatarIndex,
    load_avatar_index,
    parse_avatar_payload,
)

# --- AvatarIndex.url_for -------------------------------------------------------


def test_url_for_builds_url_from_id() -> None:
    """有映射时按模板拼出完整 URL。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_for("阿米娅") == "https://cdn.example/002_amiya.png"


def test_url_for_returns_none_for_unknown_name() -> None:
    """没有映射时返回 None——**绝不编 URL**（编了就是断图，比色块更糟）。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_for("某个没映射的干员") is None


def test_url_for_tolerates_surrounding_whitespace() -> None:
    """名字两侧有空白也要命中：排班表里的名字偶尔带空格。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_for(" 阿米娅 ") == "https://cdn.example/002_amiya.png"


def test_url_for_rejects_non_string() -> None:
    """传进来的不是字符串时返回 None，不抛异常（页面数据可能被改坏）。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_for(None) is None  # type: ignore[arg-type]
    assert index.url_for(12) is None  # type: ignore[arg-type]


def test_url_map_only_includes_matched_names() -> None:
    """只回有映射的那些名字：没映射的留空，页面据此走色块。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    result = index.url_map(["阿米娅", "没映射的", "阿米娅"])

    assert result == {"阿米娅": "https://cdn.example/002_amiya.png"}


def test_url_map_is_empty_when_nothing_matches() -> None:
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_map(["甲", "乙"]) == {}


def test_url_map_handles_empty_input() -> None:
    index = AvatarIndex({"阿米娅": "002_amiya"}, "https://cdn.example/{avatar_id}.png")

    assert index.url_map([]) == {}


def test_size_reports_entry_count() -> None:
    index = AvatarIndex({"阿": "a", "乙": "b"}, "https://cdn.example/{avatar_id}.png")

    assert index.size == 2


def test_empty_template_falls_back_to_default() -> None:
    """模板为空时用默认值——空模板会拼出一堆相同地址，等于全是错图。"""
    index = AvatarIndex({"阿米娅": "002_amiya"}, "")

    assert index.url_template == DEFAULT_URL_TEMPLATE
    assert index.url_for("阿米娅") == DEFAULT_URL_TEMPLATE.replace("{avatar_id}", "002_amiya")


# --- parse_avatar_payload ------------------------------------------------------


def test_parse_accepts_well_formed_payload() -> None:
    payload = {
        "names": {"阿米娅": "002_amiya"},
        "avatar_url_template": "https://cdn.example/{avatar_id}.png",
    }

    index = parse_avatar_payload(payload)

    assert index is not None
    assert index.url_for("阿米娅") == "https://cdn.example/002_amiya.png"


def test_parse_returns_none_for_non_mapping() -> None:
    """形状不对时给 None，让调用方记 WARNING——不抛异常、也不造空表。"""
    assert parse_avatar_payload(None) is None
    assert parse_avatar_payload(["不是对象"]) is None
    assert parse_avatar_payload("字符串") is None


def test_parse_returns_none_when_names_missing_or_wrong_type() -> None:
    assert parse_avatar_payload({}) is None
    assert parse_avatar_payload({"names": "不是对象"}) is None
    assert parse_avatar_payload({"names": None}) is None


def test_parse_returns_none_when_names_empty() -> None:
    """一条有效映射都没有，等于没有映射——返回 None 而不是空索引。"""
    assert parse_avatar_payload({"names": {}}) is None
    assert parse_avatar_payload({"names": {"": ""}}) is None


def test_parse_skips_malformed_entries_but_keeps_good_ones() -> None:
    """个别条目坏掉不该让整份映射作废——但坏条目要被丢掉，不能变成假数据。"""
    payload = {
        "names": {"阿米娅": "002_amiya", "坏的": 123, "空的": "", "另一个": "003_kalts"},
    }

    index = parse_avatar_payload(payload)

    assert index is not None
    assert index.size == 2
    assert index.url_for("阿米娅") is not None
    assert index.url_for("坏的") is None
    assert index.url_for("空的") is None


def test_parse_strips_whitespace_in_names_and_ids() -> None:
    payload = {"names": {" 阿米娅 ": " 002_amiya "}}

    index = parse_avatar_payload(payload)

    assert index is not None
    assert index.url_for("阿米娅") == DEFAULT_URL_TEMPLATE.replace("{avatar_id}", "002_amiya")


def test_parse_falls_back_when_template_lacks_placeholder() -> None:
    """模板缺了占位符就拼不出 URL——与其拼出同一个地址，不如用默认模板。"""
    payload = {
        "names": {"阿米娅": "002_amiya"},
        "avatar_url_template": "https://cdn.example/fixed.png",
    }

    index = parse_avatar_payload(payload)

    assert index is not None
    assert index.url_template == DEFAULT_URL_TEMPLATE
    assert index.url_for("阿米娅") == DEFAULT_URL_TEMPLATE.replace("{avatar_id}", "002_amiya")


# --- load_avatar_index ---------------------------------------------------------


def test_load_reads_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "avatar_map.json"
    path.write_text(
        json.dumps({"names": {"阿米娅": "002_amiya"}}, ensure_ascii=False), encoding="utf-8"
    )

    index = load_avatar_index(path)

    assert index is not None
    assert index.size == 1


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    """文件不在时返回 None（调用方记 WARNING 并退色块）——不抛异常。"""
    assert load_avatar_index(tmp_path / "不存在.json") is None


def test_load_returns_none_for_broken_json(tmp_path: Path) -> None:
    path = tmp_path / "avatar_map.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    assert load_avatar_index(path) is None


def test_load_returns_none_for_wrong_shape(tmp_path: Path) -> None:
    path = tmp_path / "avatar_map.json"
    path.write_text(json.dumps(["列表不是对象"]), encoding="utf-8")

    assert load_avatar_index(path) is None


# --- 真实数据文件 --------------------------------------------------------------
#
# 这一条盯的是「随包发布的数据文件到底能不能用」——它是页面观感的唯一来源，
# 而文件缺失/格式坏掉只会在运行时暴露（WARNING + 一片色块）。所以在这里钉死。


def test_shipped_data_file_is_usable() -> None:
    """随包发布的 avatar_map.json 必须能装载，且带来源、许可与占位符。"""
    path = Path(avatars.__file__).resolve().parent / "data" / "avatar_map.json"

    index = load_avatar_index(path)

    assert index is not None, "随包的头像映射装载失败——页面会退化成一堆色块"
    assert index.size > 100, f"映射条目太少（{index.size}），疑似数据文件残缺"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert avatars.AVATAR_ID_PLACEHOLDER in payload["avatar_url_template"]
    assert payload["source"]["license"] == "MIT"
    # 图片必须是外链引用——打包进仓库就踩到游戏素材的版权线了。
    assert "cdn" in payload["avatar_url_template"]


def test_shipped_data_file_covers_known_operators() -> None:
    """抽几个常见干员，确认映射真的指向头像文件而不是空串。"""
    path = Path(avatars.__file__).resolve().parent / "data" / "avatar_map.json"
    index = load_avatar_index(path)

    assert index is not None
    for name in ("阿米娅", "玫兰莎", "12F"):
        url = index.url_for(name)
        assert url is not None, f"{name} 没有映射"
        assert url.endswith(".png")
