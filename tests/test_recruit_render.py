"""渲染层测试。

重点盯两条渲染纪律（都来自已经踩过的坑）：

1. 截断必须**说出来**还剩多少——默默砍掉会让人以为「就这些」。
2. 前提必须写进消息——「必出 X★」只在 9:00 招募时成立，不写这句用户会以为算错。
"""

from __future__ import annotations

import pytest

from modules.recruit.dataset import TOP_OPERATOR_TAG, build_data, build_operator
from modules.recruit.render import render_query, render_tag_list


@pytest.fixture
def data():
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


def _query(data, tags, *, max_operators=20, max_combinations=6):
    return render_query(data, tags, max_operators=max_operators, max_combinations=max_combinations)


# --- 标签列表 ---------------------------------------------------------------


def test_tag_list_mentions_every_tag_and_the_rarity_pair(data) -> None:
    text = render_tag_list(data)
    for tag in data.tags:
        assert tag in text
    assert "用法" in text


def test_tag_list_explains_what_the_rarity_tags_mean(data) -> None:
    """这两个标签不是「干员特性」而是「保证星级」，不说清用户不知道能期待什么。"""
    text = render_tag_list(data)
    assert "必出 5★" in text
    assert "必出 6★" in text


def test_tag_list_states_the_nine_oclock_assumption(data) -> None:
    text = render_tag_list(data)
    assert "9:00:00" in text


# --- 正常结果 ---------------------------------------------------------------


def test_query_reports_candidates_and_guarantee(data) -> None:
    text = _query(data, ["输出"])
    assert "输出" in text
    assert "候选 5 位" in text
    assert "保证 2★" in text


def test_query_states_the_nine_oclock_assumption(data) -> None:
    """保证只在按 9:00:00 招募时成立——不写这句，用户按 1 小时没出就会以为算错。"""
    assert "9:00:00" in _query(data, ["输出"])


def test_query_lists_operators_by_star(data) -> None:
    text = _query(data, ["输出"])
    assert "6★" in text and "六星" in text
    assert "2★" in text and "二星" in text


# --- 输入有问题时 -----------------------------------------------------------


def test_query_warns_about_unknown_tags_and_still_answers(data) -> None:
    """拼错的标签必须先说，否则用户会把这组标签的结果当成实际结果。"""
    text = _query(data, ["输出", "拼错啦"])
    assert "拼错啦" in text
    assert "不认识" in text
    assert "候选" in text  # 仍然给出可算的那部分


def test_query_falls_back_when_nothing_is_recognised(data) -> None:
    text = _query(data, ["全都不认识"])
    assert "全都不认识" in text
    assert "不带参数" in text


def test_query_handles_a_completely_empty_selection(data) -> None:
    """装配层会走「列标签」那条路；这里兜底也必须是句人话，不能打出空列表。"""
    text = _query(data, [])
    assert "没给标签" in text


def test_query_says_so_when_nothing_matches(data) -> None:
    """全选上招不到时要说清楚——但组合建议照给，那正是最想问的时候。"""
    text = _query(data, ["支援机械", "群攻"])
    assert "招不到" in text
    assert "推荐组合" in text


# --- 截断要说明 -------------------------------------------------------------


def test_query_says_how_many_operators_were_hidden(data) -> None:
    """5 个候选只显示 2 个，剩下 3 个必须报出来。"""
    text = _query(data, ["输出"], max_operators=2)
    assert "还有 3 位未显示" in text


def test_query_truncates_mid_tier_with_an_ellipsis_and_a_single_total() -> None:
    """预算在同一档内部用尽时：名字后接省略号，剩余数量只在末尾报一次。

    早先写成「这一档还有 N 位」+「合计还有 N 位」，截断最高档时两句话报同一个数，
    看着像出错。
    """
    data = build_data(
        [
            build_operator("六星甲", 6, ["输出"]),
            build_operator("六星乙", 6, ["输出"]),
            build_operator("六星丙", 6, ["输出"]),
        ],
        source={"api": "test"},
    )
    text = _query(data, ["输出"], max_operators=2)

    assert "…" in text
    assert "合计还有 1 位未显示" in text
    assert "这一档还有" not in text


def test_query_says_how_many_combinations_were_hidden() -> None:
    data = build_data(
        [
            build_operator("低星", 1, ["输出", "近战位"]),
            build_operator("爆发六星", 6, ["输出", "爆发"]),
            build_operator("减速六星", 6, ["输出", "减速"]),
        ],
        source={"api": "test"},
    )
    text = _query(data, ["输出", "爆发", "减速", "近战位"], max_combinations=1)
    assert "还有 1 组" in text


def test_query_lists_recommended_combinations(data) -> None:
    text = _query(data, ["输出", "爆发", "近战位"])
    assert "推荐组合" in text
    assert "爆发" in text


# --- 6★ 的前提 --------------------------------------------------------------


def test_query_states_the_six_star_premise(data) -> None:
    """命中里有 6★ 且没选【高级资深干员】时必须提示，否则用户以为六星可能来。"""
    text = _query(data, ["输出"])
    assert TOP_OPERATOR_TAG in text
    assert "不会出 6★" in text


def test_query_omits_the_six_star_premise_when_top_tag_selected(data) -> None:
    text = _query(data, ["输出", TOP_OPERATOR_TAG])
    assert "不会出 6★" not in text


def test_query_omits_the_six_star_premise_when_no_six_star_in_pool(data) -> None:
    text = _query(data, ["支援机械"])
    assert "不会出 6★" not in text


# --- 太多标签导致组合搜索放弃时要说实话 -------------------------------------


def test_query_admits_when_combination_search_was_skipped() -> None:
    """标签多到超出组合搜索上限时，如实说「没法给建议」，不假装「没有更好的组合」。

    注意要凑够**已知**标签：拼错的标签会被忽略，不占组合搜索的额度。
    """
    data = build_data(
        [build_operator(f"干员{index}", 3, [f"标签{index}"]) for index in range(14)],
        source={"api": "test"},
    )
    text = _query(data, [f"标签{index}" for index in range(13)])
    assert "没法给组合建议" in text
    assert "推荐组合" in text
