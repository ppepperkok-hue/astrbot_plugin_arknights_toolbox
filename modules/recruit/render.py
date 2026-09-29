"""把招募计算结果渲染成一条能直接发出去的文本。

**纯逻辑**：只做字符串拼接，不 import astrbot，不读系统时间。

## 两条渲染纪律（都来自已经踩过的坑）

1. **截断必须说出来**。结果被砍掉时一定要写「还有 N 个未显示」——默默砍掉会让
   用户以为「就这些」，而排班提醒那边已经证明过这类静默有多难查。
2. **前提要写进消息**。「必出 X★」只在按 9:00:00 招募时成立；不写这句，用户
   按 1 小时招募没出对应星级，就会认为插件算错了。
"""

from __future__ import annotations

from collections.abc import Sequence

from .calculator import (
    Combination,
    RecruitInputError,
    best_combinations,
    match_operators,
    normalize_tags,
    six_star_needs_top_tag,
    unknown_tags,
)
from .dataset import SENIOR_TAG, TOP_OPERATOR_TAG, Operator, RecruitData

#: 标签列表每行放几个。中文标签宽度不一，按个数控制比按字符宽更稳。
TAGS_PER_LINE = 8

STARS = "★"


def _stars_text(stars: int) -> str:
    return f"{stars}{STARS}"


def render_tag_list(data: RecruitData) -> str:
    """列出全部可填标签（指令不带参数时用）。

    把两个稀有度标签单独说一句，是因为它们不是「干员特性」而是「保证星级」，
    混在同一个列表里用户不知道该期待什么。
    """
    plain = [tag for tag in data.tags if tag not in (TOP_OPERATOR_TAG, SENIOR_TAG)]
    lines = [f"【公开招募】可填标签（{len(data.tags)} 个）", ""]
    for start in range(0, len(plain), TAGS_PER_LINE):
        lines.append("　".join(plain[start : start + TAGS_PER_LINE]))
    lines.append("")
    lines.append("另有稀有度标签：资深干员（必出 5★）、高级资深干员（必出 6★）。")
    lines.append("用法：/ak recruit 标签1 标签2 …（例如 /ak recruit 输出 近战位）")
    lines.append("结果按 9:00:00 招募计算——时长不足时标签可能被划去，保证就不成立。")
    return "\n".join(lines)


def _render_operators(hits: Sequence[Operator], *, max_names: int) -> list[str]:
    """按星级从高到低列出命中干员，超出预算就说明还剩多少。

    只在末尾报**一个总数**。早先的写法是「这一档还有 N 位」+「合计还有 N 位」，
    当被截断的正好是最高那一档时两句话报的是同一个数——同一件事说两遍看着像
    出错，所以只留总数。
    """
    # 用户最关心高星，所以从高往低排；这样预算用尽时砍掉的是不重要的那些。
    by_stars: dict[int, list[str]] = {}
    for op in hits:
        by_stars.setdefault(op.stars, []).append(op.name)

    lines: list[str] = []
    shown = 0
    for stars in sorted(by_stars, reverse=True):
        names = sorted(by_stars[stars])
        room = max_names - shown
        if room <= 0:
            break
        visible = names[:room]
        text = "、".join(visible)
        if len(names) > room:
            text += "…"
        lines.append(f"{_stars_text(stars)}：{text}")
        shown += len(visible)

    if shown < len(hits):
        lines.append(f"（合计还有 {len(hits) - shown} 位未显示）")
    return lines


def _render_combinations(combos: Sequence[Combination], *, limit: int) -> list[str]:
    """渲染推荐组合。

    `combos` 由调用方保证非空：渲染到这里时 `known` 至少有一个标签，而「已知标签」
    一定来自某个干员（标签表就是从干员的标签集合里汇总出来的），所以它自己那个
    单标签组合必然被计入过，最优组合必然存在。这里不再为「空」留一条永远走不到
    的分支（那是死代码，宪法 §5.7 F5）。
    """
    lines = []
    for combo in combos[:limit]:
        joined = " + ".join(combo.tags)
        lines.append(f"　{joined} → 必出 {_stars_text(combo.stars)}（{combo.pool_size} 人）")
    if len(combos) > limit:
        lines.append(f"（还有 {len(combos) - limit} 组同样最优的组合未显示）")
    return lines


def render_query(
    data: RecruitData,
    tags: Sequence[str],
    *,
    max_operators: int,
    max_combinations: int,
) -> str:
    """渲染一次「给标签、要结果」的输出。

    Args:
        data: 招募索引。
        tags: 用户给的标签（可以含拼错的）。
        max_operators: 最多列几位干员。
        max_combinations: 最多列几组推荐组合。

    Returns:
        可直接发送的文本。**任何输入都有输出**：标签全不认识时给可用标签与提示，
        而不是抛异常让指令静默失败。

    Note:
        「所选标签放在一起没有命中」**不是**提前返回的理由。游戏里手上那 5 个标签
        全选上时交集常常是空的，而「这几个标签怎么组最赚」恰恰是这时候最想问的，
        所以推荐组合那一段照常给。
    """
    normalized = normalize_tags(tags)
    if not normalized:
        # 装配层不给标签时会走「列出全部标签」那条路，所以这里只是兜底：
        # 宁可回一句用法，也不要打出一行「这些标签我不认识：」后面什么都没有。
        return (
            "【公开招募】没给标签。"
            "发 /ak recruit 不带参数可以看全部可填标签；"
            "带标签则算结果，例如 /ak recruit 输出 近战位。"
        )

    unknown = unknown_tags(normalized, data=data)
    known = [tag for tag in normalized if tag in data.tag_set]

    if not known:
        lines = ["【公开招募】这些标签我不认识：" + "、".join(unknown or normalized)]
        lines.append("发 /ak recruit 不带参数可以看全部可填标签（注意是游戏内的中文标签）。")
        return "\n".join(lines)

    hits = match_operators(known, data=data)

    lines = ["【公开招募】所选：" + "、".join(known)]
    if unknown:
        # 拼错的标签必须先说，否则用户会把这组标签算出来的结果当成实际结果。
        lines.append(f"⚠ 这几个标签不认识、已忽略：{'、'.join(unknown)}")

    if hits:
        guaranteed = min(op.stars for op in hits)
        lines.append(
            f"候选 {len(hits)} 位，保证 {_stars_text(guaranteed)}"
            f"（指不会低于这一档；按 9:00:00 招募计算）"
        )
        lines.append("")
        lines.append("可能出现的干员：")
        lines.extend(_render_operators(hits, max_names=max_operators))
    else:
        lines.append("这几个标签全选上时一个都招不到（选得太死）。看其中一部分能保证什么：")

    lines.append("")
    lines.append("推荐组合：")
    try:
        combos = best_combinations(known, data=data)
    except RecruitInputError as exc:
        # 标签给得太多导致组合搜索放弃：如实说，不假装「没有更好的组合」。
        lines.append(f"（没法给组合建议：{exc}）")
    else:
        lines.extend(_render_combinations(combos, limit=max_combinations))

    if six_star_needs_top_tag(known, data=data):
        lines.append("")
        lines.append(f"※ 未选【{TOP_OPERATOR_TAG}】时不会出 6★——上面那一档要选了它才会出现。")
    return "\n".join(lines)
