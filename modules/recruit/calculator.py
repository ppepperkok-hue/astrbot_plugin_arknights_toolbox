"""公开招募的匹配与组合计算。

**纯逻辑**：只依赖标准库与同目录的 `dataset`，不 import astrbot。

## 命中规则

一个干员命中，当且仅当**它的标签集合包含所选的全部标签**（`selected ⊆
operator.tags`）。这是游戏内标签的语义：选得越多，候选池越小。

## 关于「必出」的含义

`guaranteed_stars` 取命中干员里的**最低星级**——「不可能出比这更低的」。若一个
都没命中，返回 `NO_MATCH`（0），因为真实星级恒为 1–6，0 不会被误读成「必出零星」。

## 两个前提（写清楚，免得被当成无条件保证）

1. **按 9:00:00 招募**。时长不足时标签可能被系统划去，划掉一个标签候选池就变了，
   「必出」也就不再成立。本模块不模拟划标签的随机过程。
2. **未选【高级资深干员】时不会有 6★**（依据见 `dataset.py` 的模块 docstring）。
   这里的命中集合仍按集合语义给出 6★，由渲染层把这条前提写进消息——**不悄悄从
   列表里删掉**，因为删掉之后「可能出现的干员」与数据就对不上了，用户更难自查。
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .dataset import TOP_OPERATOR_TAG, Operator, RecruitData, normalize_tag

#: 命中集为空时的返回值。真实星级恒为 1–6，所以 0 不会被误读成「必出零星」。
NO_MATCH = 0

#: 游戏内一次最多能选的标签数，组合搜索的上界按它来。
MAX_COMBO_TAGS = 5

#: 允许一次传入的标签数上限。组合搜索是子集枚举，标签再多会指数爆炸；
#: 游戏里最多 5 个，超过这个数说明调用方用错了，直接显式报错而不是偷偷截断。
MAX_AVAILABLE_TAGS = 12


class RecruitInputError(ValueError):
    """调用方给的标签不合用（空、过多）。"""


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """归一化并去重，**保留用户输入顺序**。

    保留顺序是为了让输出读起来跟用户输入一致（输出里同一组标签换个顺序，
    用户会以为算的是另一组）。
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = normalize_tag(raw)
        if tag and tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return tuple(ordered)


def match_operators(tags: Sequence[str], *, data: RecruitData) -> list[Operator]:
    """命中所选标签的干员，按（星级升序、名字）排列。

    Args:
        tags: 所选标签。**至少要给一个**——空选择在集合论上「命中一切」，
            但在这里毫无意义，而且会让调用方不小心把整个池子倒出来。
        data: 招募索引。

    Returns:
        命中干员；没有命中时返回空列表（由调用方决定怎么告诉用户）。

    Raises:
        RecruitInputError: `tags` 归一化后为空。
    """
    wanted = frozenset(normalize_tags(tags))
    if not wanted:
        raise RecruitInputError("至少要给一个标签，例如：/ak recruit 输出 近战位")
    hits = [op for op in data.operators if op.has_all(wanted)]
    hits.sort(key=lambda op: (op.stars, op.name))
    return hits


def guaranteed_stars(tags: Sequence[str], *, data: RecruitData) -> int:
    """所选标签能保证的最低星级；一个都没命中时返回 `NO_MATCH`（0）。"""
    hits = match_operators(tags, data=data)
    return min((op.stars for op in hits), default=NO_MATCH)


def unknown_tags(tags: Sequence[str], *, data: RecruitData) -> list[str]:
    """输入里数据不认识的标签，保持输入顺序、去重。

    单独拎出来是为了**显式失败**：把拼错的标签当成「没有结果」，用户会以为
    这组标签真的招不到人，而实际是自己打错字了（项目宪法 §2 第 2 条）。
    """
    return [tag for tag in normalize_tags(tags) if tag not in data.tag_set]


def six_star_needs_top_tag(tags: Sequence[str], *, data: RecruitData) -> bool:
    """命中里有 6★ 且没选【高级资深干员】——渲染层据此加一行前提说明。

    规则本身见 `dataset.py` 模块 docstring（6★ 必须靠该标签才会出现）。
    """
    if TOP_OPERATOR_TAG in normalize_tags(tags):
        return False
    return any(op.stars >= 6 for op in match_operators(tags, data=data))


@dataclass(frozen=True)
class Combination:
    """一组标签，以及它能保证的星级。

    Attributes:
        tags: 组合里的标签（保持传入顺序的子序列）。
        stars: 该组合保证的最低星级。
        pool_size: 该组合的候选人数——同样的保证星级下，池子大的更值得传。
    """

    tags: tuple[str, ...]
    stars: int
    pool_size: int


def best_combinations(
    available_tags: Sequence[str],
    *,
    data: RecruitData,
    max_tags: int = MAX_COMBO_TAGS,
    max_available: int = MAX_AVAILABLE_TAGS,
) -> list[Combination]:
    """从可用标签里找出能保证最高星级的组合。

    ## 为什么返回的是「极小组合」而不是「全部组合」

    命中集随标签增加只会变小，所以最低星级**单调不降**。这意味着「全部能达到
    最高星级的组合」必然包含每个组合的所有超集——把 5 个标签全选上永远也是
    最高星级之一。把那些一并列出来，用户要的那句「这几个标签怎么组最赚」就被
    淹没了。所以这里只返回**极小**的那些：它们自己达标，而任何真子集都不达标。

    Args:
        available_tags: 手上可用的标签。
        data: 招募索引。
        max_tags: 组合最多几个标签；默认是游戏内的上限 5。
        max_available: 可用标签数量上限；超过则报错（子集枚举会爆炸，
            而且游戏里根本给不出这么多标签）。

    Returns:
        极小最优组合，按（标签数、标签顺序）排序。没有任何组合能命中干员时返回空列表。

    Raises:
        RecruitInputError: 归一化后没有可用标签，或标签数量超过 `max_available`。
    """
    ordered = normalize_tags(available_tags)
    if not ordered:
        raise RecruitInputError("至少要给一个可用标签")
    if len(ordered) > max_available:
        raise RecruitInputError(
            f"一次最多分析 {max_available} 个标签（收到 {len(ordered)} 个）；"
            "游戏里一次只会给 5 个，请只填实际看到的标签"
        )

    # 预先按标签建索引：子集枚举里会反复求交集，逐次扫全表会慢得多。
    stars_of = {op.name: op.stars for op in data.operators}
    names_by_tag: dict[str, set[str]] = {}
    for op in data.operators:
        for tag in op.tags:
            names_by_tag.setdefault(tag, set()).add(op.name)

    scored: dict[tuple[str, ...], tuple[int, int]] = {}
    upper = min(max_tags, len(ordered))
    for size in range(1, upper + 1):
        for combo in itertools.combinations(ordered, size):
            pool = set(names_by_tag.get(combo[0], ()))
            for tag in combo[1:]:
                pool &= names_by_tag.get(tag, set())
                if not pool:
                    break
            if not pool:
                continue
            scored[combo] = (min(stars_of[name] for name in pool), len(pool))

    if not scored:
        return []

    best = max(stars for stars, _ in scored.values())

    # 极小化：按标签数升序扫，若已有达标组合是它的子集，就跳过它。
    achievers = sorted(
        (combo for combo, (stars, _) in scored.items() if stars == best),
        key=lambda combo: (len(combo), combo),
    )
    kept: list[Combination] = []
    kept_sets: list[frozenset[str]] = []
    for combo in achievers:
        combo_set = frozenset(combo)
        if any(prev <= combo_set for prev in kept_sets):
            continue
        kept_sets.append(combo_set)
        kept.append(Combination(tags=combo, stars=best, pool_size=scored[combo][1]))
    return kept
