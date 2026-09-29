"""匹配、保证星级与组合搜索的测试。

用**合成数据**：真实数据会随游戏更新变化，而这些规则（子集命中、取最低星级、
极小组合）是不变的。真实数据另在 `test_recruit_dataset.py` 里钉关键几条。
"""

from __future__ import annotations

import pytest

from modules.recruit.calculator import (
    MAX_AVAILABLE_TAGS,
    NO_MATCH,
    RecruitInputError,
    best_combinations,
    guaranteed_stars,
    match_operators,
    normalize_tags,
    six_star_needs_top_tag,
    unknown_tags,
)
from modules.recruit.dataset import (
    SENIOR_TAG,
    TOP_OPERATOR_TAG,
    build_data,
    build_operator,
)


@pytest.fixture
def data():
    """六个干员，覆盖 1–6 星与几组典型标签。"""
    return build_data(
        [
            build_operator("一星机", 1, ["支援机械", "近战位"]),
            build_operator("二星", 2, ["新手", "近战位", "输出"]),
            build_operator("三星", 3, ["输出", "近战位"]),
            build_operator("四星", 4, ["输出", "远程位", "群攻"]),
            build_operator("五星", 5, ["输出", "远程位"]),
            build_operator("六星", 6, ["输出", "远程位", "爆发"]),
        ],
        source={"api": "test"},
    )


# --- normalize_tags ---------------------------------------------------------


def test_normalize_tags_keeps_input_order_and_dedupes() -> None:
    """输出里顺序换了，用户会以为算的是另一组标签。"""
    assert normalize_tags(["输出", "远程位", "输出"]) == ("输出", "远程位")


def test_normalize_tags_drops_blank_entries() -> None:
    assert normalize_tags(["", "   ", "输出", "　"]) == ("输出",)


# --- match_operators --------------------------------------------------------


def test_match_requires_all_selected_tags(data) -> None:
    """命中 = 干员标签集合包含所选**全部**标签。"""
    names = [op.name for op in match_operators(["输出"], data=data)]
    assert names == ["二星", "三星", "四星", "五星", "六星"]

    names = [op.name for op in match_operators(["输出", "远程位"], data=data)]
    assert names == ["四星", "五星", "六星"]


def test_match_is_sorted_by_stars_then_name(data) -> None:
    hits = match_operators(["远程位"], data=data)
    assert [op.stars for op in hits] == sorted(op.stars for op in hits)


def test_match_returns_empty_for_an_impossible_combination(data) -> None:
    """「支援机械 + 群攻」在这份数据里没有交集——空结果不是错误，是一种答案。"""
    assert match_operators(["支援机械", "群攻"], data=data) == []


def test_match_normalizes_user_input(data) -> None:
    """用户多打空格不该变成「查不到」。"""
    assert match_operators(["输 出"], data=data) == match_operators(["输出"], data=data)


def test_match_rejects_an_empty_selection(data) -> None:
    """空选择在集合论上「命中一切」，在这里毫无意义且会倒出整个池子。"""
    with pytest.raises(RecruitInputError):
        match_operators([], data=data)
    with pytest.raises(RecruitInputError):
        match_operators(["   "], data=data)


# --- guaranteed_stars -------------------------------------------------------


def test_guaranteed_stars_is_the_minimum_of_the_pool(data) -> None:
    assert guaranteed_stars(["输出"], data=data) == 2
    assert guaranteed_stars(["支援机械"], data=data) == 1
    assert guaranteed_stars(["输出", "远程位"], data=data) == 4


def test_guaranteed_stars_takes_the_smallest_not_the_largest(data) -> None:
    """「必出」的含义是不可能低于它——取最大值就成了「必出六星」，那是错的。"""
    assert guaranteed_stars(["输出", "爆发"], data=data) == 6
    assert guaranteed_stars(["远程位"], data=data) == 4


def test_guaranteed_stars_returns_no_match_for_an_empty_pool(data) -> None:
    """0 与真实的 1–6 星不会混淆。"""
    assert guaranteed_stars(["支援机械", "群攻"], data=data) == NO_MATCH


def test_no_match_constant_is_outside_the_real_star_range(data) -> None:
    assert NO_MATCH == 0
    assert all(op.stars >= 1 for op in data.operators)


# --- 稀有度保证标签（按星级推导出来的那两个） -------------------------------


def test_senior_tag_matches_five_and_six_star(data) -> None:
    """【资深干员】= 必得 5★，因此它必须同时命中 5★ 与 6★。"""
    assert guaranteed_stars([SENIOR_TAG], data=data) == 5


def test_top_operator_tag_matches_only_six_star(data) -> None:
    assert guaranteed_stars([TOP_OPERATOR_TAG], data=data) == 6
    hits = match_operators([TOP_OPERATOR_TAG], data=data)
    assert {op.stars for op in hits} == {6}


def test_both_rarity_tags_together_pick_the_highest(data) -> None:
    """「同时选择时优先最高稀有度」——交集落在 6★。"""
    assert guaranteed_stars([SENIOR_TAG, TOP_OPERATOR_TAG], data=data) == 6


# --- unknown_tags -----------------------------------------------------------


def test_unknown_tags_preserves_order_and_dedupes(data) -> None:
    assert unknown_tags(["输出", "拼错了", "另一个错的", "拼错了"], data=data) == [
        "拼错了",
        "另一个错的",
    ]


def test_unknown_tags_is_empty_when_everything_is_known(data) -> None:
    assert unknown_tags(["输出", "远程位"], data=data) == []


# --- six_star_needs_top_tag -------------------------------------------------


def test_six_star_needs_top_tag_when_the_pool_contains_six_star(data) -> None:
    assert six_star_needs_top_tag(["输出"], data=data) is True


def test_six_star_needs_top_tag_is_false_when_top_tag_selected(data) -> None:
    assert six_star_needs_top_tag(["输出", TOP_OPERATOR_TAG], data=data) is False


def test_six_star_needs_top_tag_is_false_without_any_six_star(data) -> None:
    assert six_star_needs_top_tag(["支援机械"], data=data) is False


# --- best_combinations ------------------------------------------------------


def test_best_combinations_returns_minimal_combos_only(data) -> None:
    """`输出` 保证 2★、`输出+爆发` 保证 6★；极小解是 `爆发` 单独一组。

    `输出+爆发` 也能保证 6★，但它是 `爆发` 的超集，报出来只会把答案淹掉。
    """
    combos = best_combinations(["输出", "爆发", "近战位"], data=data)
    assert [c.tags for c in combos] == [("爆发",)]
    assert combos[0].stars == 6
    assert combos[0].pool_size == 1


def test_best_combinations_can_need_two_tags() -> None:
    """构造一个「任何单标签都不够、必须两个一起」的例子，钉住不能只找单标签。"""
    data = build_data(
        [
            build_operator("甲", 5, ["输出", "减速"]),
            build_operator("乙", 5, ["输出", "近战位"]),
            build_operator("丙", 6, ["输出", "减速", "近战位"]),
        ],
        source={"api": "test"},
    )
    combos = best_combinations(["输出", "减速", "近战位"], data=data)
    assert [c.tags for c in combos] == [("减速", "近战位")]
    assert combos[0].stars == 6
    assert combos[0].pool_size == 1


def test_best_combinations_lists_all_equally_good_minimal_answers() -> None:
    """两个互不包含、同样最优的组合都要给出来。"""
    data = build_data(
        [
            build_operator("低星", 1, ["输出", "近战位"]),
            build_operator("爆发六星", 6, ["输出", "爆发"]),
            build_operator("减速六星", 6, ["输出", "减速"]),
        ],
        source={"api": "test"},
    )
    combos = best_combinations(["输出", "爆发", "减速", "近战位"], data=data)
    assert {c.tags for c in combos} == {("爆发",), ("减速",)}
    assert all(c.stars == 6 for c in combos)
    assert all(len(c.tags) == 1 for c in combos), "不该给出任何超集"


def test_best_combinations_respects_max_tags() -> None:
    """把组合上限压到 1，就找不到那个需要两个标签的解——说明这个参数真的在起作用。"""
    data = build_data(
        [
            build_operator("甲", 5, ["输出", "减速"]),
            build_operator("乙", 5, ["输出", "近战位"]),
            build_operator("丙", 6, ["输出", "减速", "近战位"]),
        ],
        source={"api": "test"},
    )
    full = best_combinations(["输出", "减速", "近战位"], data=data)
    assert [c.tags for c in full] == [("减速", "近战位")]
    assert full[0].stars == 6

    limited = best_combinations(["输出", "减速", "近战位"], data=data, max_tags=1)
    assert all(len(c.tags) == 1 for c in limited)
    assert max(c.stars for c in limited) == 5
    assert {c.tags for c in limited} == {("输出",), ("减速",), ("近战位",)}


def test_best_combinations_returns_empty_when_no_tag_matches_anyone(data) -> None:
    """一个标签都命中不了任何人时，没有任何组合可给。"""
    assert best_combinations(["根本没有这个标签"], data=data) == []


def test_best_combinations_rejects_an_empty_selection(data) -> None:
    with pytest.raises(RecruitInputError):
        best_combinations([], data=data)


def test_best_combinations_rejects_too_many_tags(data) -> None:
    """子集枚举会指数爆炸；游戏里一次只给 5 个标签，超了就是调用方用错了。"""
    too_many = [f"标签{i}" for i in range(MAX_AVAILABLE_TAGS + 1)]
    with pytest.raises(RecruitInputError, match="最多"):
        best_combinations(too_many, data=data)


def test_best_combinations_ignores_unknown_tags(data) -> None:
    """拼错的标签匹配不到人，自然不参与组合；由渲染层单独提醒用户。"""
    known_only = best_combinations(["爆发"], data=data)
    with_unknown = best_combinations(["爆发", "不存在的标签"], data=data)
    assert with_unknown == known_only
