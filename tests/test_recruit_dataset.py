"""招募数据模型与装载的测试。

分两层：

1. **合成数据**测装载与校验的每条分支（畸形输入必须显式报错）。
2. **真实数据文件**做少量但关键的断言——它保证随代码发布的那份表没有被截断、
   且两个稀有度标签确实按星级推导出来了。真实数据会随游戏更新增长，所以这里
   断言「至少多少」和「必须包含谁」，**不写死总数**：写死了每次更新数据都要回来
   改测试，那种护栏迟早被人图省事删掉。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.recruit.dataset import (
    MAX_STARS,
    MIN_STARS,
    SENIOR_TAG,
    TOP_OPERATOR_TAG,
    RecruitDataError,
    build_data,
    build_operator,
    default_data_path,
    derive_rarity_tags,
    load_data,
    normalize_tag,
    parse_data,
)

# --- 归一化 -----------------------------------------------------------------


def test_normalize_tag_strips_all_whitespace() -> None:
    """手打标签常多敲空格；多一个空格不该被当成「未知标签」。"""
    assert normalize_tag("  输出  ") == "输出"
    assert normalize_tag("近 战 位") == "近战位"
    assert normalize_tag("　输出") == "输出"  # 全角空格
    assert normalize_tag("输出\u3000") == "输出"


def test_normalize_tag_folds_fullwidth_forms() -> None:
    """NFKC：全角与兼容字符折成标准形，避免同一标签两种写法各自成键。"""
    assert normalize_tag("123") == normalize_tag("１２３")


# --- 稀有度标签的推导 -------------------------------------------------------


@pytest.mark.parametrize(
    ("stars", "expected"),
    [
        (1, set()),
        (4, set()),
        (5, {SENIOR_TAG}),
        (6, {SENIOR_TAG, TOP_OPERATOR_TAG}),
    ],
)
def test_derive_rarity_tags(stars: int, expected: set[str]) -> None:
    """6★ 同时带两个标签，才能让「只选资深干员」命中 5★∪6★ → 必出 5★。"""
    assert set(derive_rarity_tags(stars)) == expected


def test_six_star_carries_both_rarity_tags() -> None:
    """这条是「同时选择时优先最高稀有度」能成立的结构前提。"""
    op = build_operator("测试六星", 6, ["输出"])
    assert SENIOR_TAG in op.tags
    assert TOP_OPERATOR_TAG in op.tags


@pytest.mark.parametrize("stars", [0, 7, -1, 99])
def test_build_operator_rejects_out_of_range_stars(stars: int) -> None:
    with pytest.raises(RecruitDataError):
        build_operator("越界", stars, ["输出"])


# --- 装载：每一条畸形输入都要显式报错 ---------------------------------------


def _minimal_payload() -> dict:
    return {
        "schema": 1,
        "source": {"api": "test"},
        "operators": [{"name": "芬", "stars": 3, "tags": ["输出", "近战位"]}],
    }


def test_parse_data_accepts_a_well_formed_payload() -> None:
    data = parse_data(_minimal_payload())
    assert [op.name for op in data.operators] == ["芬"]
    assert "输出" in data.tag_set
    assert data.source["api"] == "test"


def test_parse_data_rejects_missing_source() -> None:
    """没有出处的数据无法核对——宁可拒绝装载。"""
    payload = _minimal_payload()
    del payload["source"]
    with pytest.raises(RecruitDataError, match="source"):
        parse_data(payload)


def test_parse_data_rejects_empty_source() -> None:
    payload = _minimal_payload()
    payload["source"] = {}
    with pytest.raises(RecruitDataError, match="source"):
        parse_data(payload)


def test_parse_data_rejects_non_mapping_root() -> None:
    with pytest.raises(RecruitDataError):
        parse_data(["not", "a", "mapping"])


def test_parse_data_rejects_missing_operators() -> None:
    payload = _minimal_payload()
    del payload["operators"]
    with pytest.raises(RecruitDataError, match="operators"):
        parse_data(payload)


def test_parse_data_rejects_empty_operators() -> None:
    payload = _minimal_payload()
    payload["operators"] = []
    with pytest.raises(RecruitDataError):
        parse_data(payload)


@pytest.mark.parametrize(
    "broken",
    [
        {"stars": 3, "tags": ["输出"]},  # 缺 name
        {"name": "  ", "stars": 3, "tags": ["输出"]},  # 空白 name
        {"name": "芬", "stars": "3", "tags": ["输出"]},  # stars 是字符串
        {"name": "芬", "stars": True, "tags": ["输出"]},  # bool 不是星级
        {"name": "芬", "stars": 3, "tags": "输出"},  # tags 不是数组
        {"name": "芬", "stars": 3},  # 缺 tags
    ],
)
def test_parse_data_rejects_malformed_operator(broken: dict) -> None:
    payload = _minimal_payload()
    payload["operators"] = [broken]
    with pytest.raises(RecruitDataError):
        parse_data(payload)


def test_parse_data_rejects_duplicate_names() -> None:
    """重复干员会让人数统计和池子大小失真。"""
    payload = _minimal_payload()
    payload["operators"] = [
        {"name": "芬", "stars": 3, "tags": ["输出"]},
        {"name": "芬", "stars": 3, "tags": ["输出"]},
    ]
    with pytest.raises(RecruitDataError, match="重复"):
        parse_data(payload)


def test_build_data_rejects_empty_operator_list() -> None:
    with pytest.raises(RecruitDataError):
        build_data([])


# --- 装载：文件层 -----------------------------------------------------------


def test_load_data_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RecruitDataError, match="找不到"):
        load_data(tmp_path / "nope.json")


def test_load_data_reports_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RecruitDataError, match="JSON"):
        load_data(bad)


def test_load_data_round_trips_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(_minimal_payload(), ensure_ascii=False), encoding="utf-8")
    data = load_data(path)
    assert [op.name for op in data.operators] == ["芬"]


# --- 真实数据文件（随代码发布的那份） ---------------------------------------


@pytest.fixture(scope="module")
def real_data():
    return load_data()


def test_bundled_data_file_exists() -> None:
    assert default_data_path().is_file(), "招募数据文件是随代码发布的资源，必须存在"


def test_bundled_data_has_a_traceable_source(real_data) -> None:
    """数据必须写得出出处与抓取时间——否则将来没人能核对它是否过期。"""
    assert real_data.source.get("api")
    assert real_data.source.get("fetched_at")


def test_bundled_data_is_not_truncated(real_data) -> None:
    """只断言下界：真实数据会随游戏更新增长，写死总数会让每次更新都要改测试。"""
    assert len(real_data.operators) >= 150


@pytest.mark.parametrize("name", ["芬", "银灰", "能天使", "水月", "羽毛笔"])
def test_bundled_data_contains_known_recruitable_operators(real_data, name: str) -> None:
    assert name in {op.name for op in real_data.operators}


def test_bundled_data_rarities_are_in_range(real_data) -> None:
    assert all(MIN_STARS <= op.stars <= MAX_STARS for op in real_data.operators)


@pytest.mark.parametrize("tag", ["输出", "近战位", "远程位", "支援机械", "新手", "先锋", "减速"])
def test_bundled_data_declares_core_tags(real_data, tag: str) -> None:
    assert tag in real_data.tag_set


def test_bundled_data_includes_both_rarity_tags(real_data) -> None:
    """推导出来的标签也必须进标签表，否则用户填了却说「不认识」。"""
    assert SENIOR_TAG in real_data.tag_set
    assert TOP_OPERATOR_TAG in real_data.tag_set


def test_bundled_data_derives_rarity_tags_consistently(real_data) -> None:
    """每个 6★ 带两个稀有度标签、每个 5★ 只带【资深干员】——推导规则的一致性护栏。"""
    for op in real_data.operators:
        assert (TOP_OPERATOR_TAG in op.tags) == (op.stars >= 6), op.name
        assert (SENIOR_TAG in op.tags) == (op.stars >= 5), op.name
