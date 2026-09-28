"""基建排班表解析（纯逻辑）。

只做一件事：把排班表 JSON 里的 ``plans[].rooms[].operators[]`` 提取成提醒可用的
最小结构。**刻意不解析班次时长**——班次时刻由用户在插件配置里填，且不同来源的排班表
在 ``plans[].name`` 上写法各异（实测样本里有 ``12H第一班``、``A+B 16H``、``1 7h``、
``第1班``、``A 组 12 H`` 五种），靠名字解析时长既脆弱又没必要。

纯逻辑：不 import astrbot、不碰文件系统（输入是**文本**）、不做网络。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PlanAssignment",
    "RoomAssignment",
    "ScheduleFileError",
    "parse_schedule_file",
]


class ScheduleFileError(ValueError):
    """排班表的结构不符合预期。

    只表示**结构性**问题——文件整份不可用。单个房间条目的瑕疵不算结构性错误，
    只跳过那条并保留其余（判定分界见 `parse_schedule_file` 的 Note）。
    """


@dataclass(frozen=True)
class RoomAssignment:
    """一间房在这一班要安排哪些干员。"""

    room: str
    """房间类型，如 ``"trading"``。"""

    index: int
    """该房型下的第几个，**从 1 开始**，等于 ``rooms[room]`` 列表下标 + 1。

    用的是**原始列表**的下标：中间有条目被跳过时，后续条目的 index 不会重排——
    否则「第 2 个制造站」会指向一个用户找不到的房间。
    """

    operators: tuple[str, ...]
    """干员名，已滤掉空串与非字符串；**至少一个**（空条目会被跳过）。"""


@dataclass(frozen=True)
class PlanAssignment:
    """一个班次（对应排班表里的一个 plan）。"""

    name: str
    """``plans[i].name``；缺失或不是字符串时为空串——**不编造名字**，怎么兜底由渲染层决定。"""

    rooms: tuple[RoomAssignment, ...]
    """该班的房间安排，按文件里的原始顺序排列。"""


def parse_schedule_file(text: str) -> tuple[PlanAssignment, ...]:
    """解析排班表 JSON 文本，按 ``plans`` 顺序返回各班次。

    Args:
        text: 排班表 JSON 的**文本**（不是路径）。读文件由调用方负责，本模块因此
            不必碰文件系统，也就能被纯逻辑单测直接覆盖。

    Returns:
        按 ``plans`` 顺序排列的班次；每班的房间按文件原始顺序排列。

    Raises:
        ScheduleFileError: 结构性错误——不是合法 JSON、根不是对象、``plans``
            缺失/不是列表/为空、某个 plan 不是对象、``rooms`` 缺失或不是对象、
            某个房型的值不是列表。这些文件整份不可用，必须显式失败。

    Note:
        **严格与宽松的分界**（实测 6 份真实样本后定的）：

        - *容器*走不通 = 结构性错误 → 抛错。容器是我们要按名字进去的东西，
          进不去就说明这份文件不是我们认的格式。
        - *叶子*没有可用内容 = 条目瑕疵 → 跳过该条目、保留其余。实测 340 个房间条目里
          **29 个的 ``operators`` 是空的**（MAA 里表示「这间房不填」），那是正常状态而非
          故障，没有理由因为一间房没人就让整份排班表作废。

        按这条分界，`operators` 缺失、不是列表、或过滤后一个人都不剩，都算叶子瑕疵；
        而「房型的值不是列表」算容器问题。**宁可显式失败，也不静默丢掉一间房。**
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScheduleFileError(f"不是合法的 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise ScheduleFileError(f"排班表根节点应为对象，实际是 {type(payload).__name__}")

    plans = payload.get("plans")
    if not isinstance(plans, list):
        raise ScheduleFileError(f"排班表缺少 plans 列表，实际是 {type(plans).__name__}")
    if not plans:
        raise ScheduleFileError("排班表的 plans 为空，没有任何班次可导入")

    return tuple(_parse_plan(plan, position) for position, plan in enumerate(plans))


def _parse_plan(plan: Any, position: int) -> PlanAssignment:
    """取一个班次；标签用 ``plans[i]``，出错时能直接定位到文件里的位置。"""
    label = f"plans[{position}]"
    if not isinstance(plan, dict):
        raise ScheduleFileError(f"{label} 应为对象，实际是 {type(plan).__name__}")

    rooms = plan.get("rooms")
    if not isinstance(rooms, dict):
        raise ScheduleFileError(f"{label}.rooms 应为对象，实际是 {type(rooms).__name__}")

    name = plan.get("name")

    return PlanAssignment(
        name=name if isinstance(name, str) else "",
        rooms=tuple(
            assignment
            for room_type, entries in rooms.items()
            for assignment in _parse_room(str(room_type), entries, label)
        ),
    )


def _parse_room(room_type: str, entries: Any, label: str) -> list[RoomAssignment]:
    """取一间房的所有条目；瑕疵条目跳过，不连累其余。"""
    if not isinstance(entries, list):
        raise ScheduleFileError(
            f"{label}.rooms[{room_type!r}] 应为列表，实际是 {type(entries).__name__}"
        )

    assignments: list[RoomAssignment] = []
    for position, entry in enumerate(entries):
        operators = _operators_of(entry)
        if operators:
            assignments.append(
                RoomAssignment(room=room_type, index=position + 1, operators=operators)
            )
    return assignments


def _operators_of(entry: Any) -> tuple[str, ...]:
    """从一条房间条目里取干员名；没有可用内容时返回空元组。

    「非 dict」「``operators`` 缺失或不是列表」「滤完为空」三种情况一并返回空元组——
    对调用方而言它们的含义相同：这条没有可报告的干员。
    """
    if not isinstance(entry, dict):
        return ()

    raw = entry.get("operators")
    if not isinstance(raw, list):
        return ()

    return tuple(stripped for item in raw if isinstance(item, str) and (stripped := item.strip()))
