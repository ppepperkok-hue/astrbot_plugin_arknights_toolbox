"""WebUI 只读状态数据的组装（纯逻辑）。

页面只是**另一种入口**：这里不重新实现任何班次计算，只把模块已有的
:class:`~.strategy.Snapshot`、绑定状态与发送记录翻译成可 JSON 序列化的 dict。
判定逻辑永远只有一处（``schedule`` / ``strategy``），否则两处必然漂移。

时间一律由调用方传入——纯逻辑里不出现 ``datetime.now()``，否则测不了。

纯逻辑模块：**禁止 import astrbot**（由 ruff.toml 的 TID 禁入规则强制）。
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from .roster import ROOM_LABELS
from .schedule import Shift, format_hhmm
from .strategy import Snapshot


class SendRecordLike(Protocol):
    """发送记录的形状（``core.storage.SendRecord`` 结构上直接匹配）。

    只声明用得到的字段就够了：纯逻辑层没必要 import ``core.storage``——那会牵进
    「插件以包加载 / 仓库根跑 pytest」两种场景的导入差异，而这里一个字都用不上。
    """

    at: datetime
    shift: str
    ok: bool
    detail: str


def format_remaining(delta: timedelta) -> str:
    """把「还有多久」说成人话。

    Args:
        delta: 距下次换班的时间差。已在过去时给「不到 1 分钟」，不出现负数。

    Returns:
        例如 ``"5 小时 45 分"``、``"18 分"``、``"1 天 3 小时"``。
    """
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "不到 1 分钟"
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def shift_brief(shift: Shift) -> dict[str, str]:
    """班次的名称与起止时刻（只读展示用）。跨天班次的 ``end`` 会小于 ``start``。"""
    return {
        "name": shift.name,
        "start": format_hhmm(shift.start_minute),
        "end": format_hhmm(shift.end_minute),
    }


def order_shifts(
    shifts: Sequence[Any], shift_order: Sequence[str] | None = None
) -> tuple[Any, ...]:
    """按**配置里的原始顺序**重排班次。

    为什么需要它：``ShiftTable.shifts`` 是**按开始时刻升序**排过的（夜班 02:00 会跑到
    最前），而配置里的 ``shift_1/2/3`` 有自己的顺序，页面表单与写回都按后者。直接拿
    排过序的元组填「第一班」，用户的早班位置就会显示夜班——**保存时把时刻写错段**。

    Args:
        shifts: 班次序列（可能已被按时刻排序）。
        shift_order: 配置里的班次名顺序（``shift_1`` → ``shift_2`` → ``shift_3``）。
            为空或与 ``shifts`` 对不上（例如名字被改过）时**原样返回**——宁可顺序不理想，
            也不能丢班次。

    Returns:
        重排后的元组；无法重排时是 ``shifts`` 的原顺序副本。
    """
    items = tuple(shifts)
    if not shift_order:
        return items
    by_name = {shift.name: shift for shift in items}
    ordered = tuple(by_name[name] for name in shift_order if name in by_name)
    if len(ordered) != len(items):
        return items
    return ordered


def record_brief(record: SendRecordLike) -> dict[str, Any]:
    """一条发送记录。时间用 ISO 8601，交给前端自己决定怎么显示。"""
    return {
        "at": record.at.isoformat(),
        "shift": record.shift,
        "ok": bool(record.ok),
        "detail": record.detail,
    }


def build_status(
    snapshot: Snapshot,
    *,
    lead_minutes: int,
    bound: bool,
    recent: Sequence[SendRecordLike],
    roster_imported: bool,
    breaker_open: bool,
    consecutive_failures: int,
    shifts: Sequence[Any] = (),
    shift_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """组装页面要的状态 JSON。

    Args:
        snapshot: 模块已有的判定结果——**这里不重算班次**。
        lead_minutes: 提前量（分钟）。
        bound: 是否已绑定提醒目标。
        recent: 最近的发送记录，最新的在前。
        roster_imported: 排班表是否已导入。V1.5 才会写入该状态，未实现时为 False。
        breaker_open: 推送熔断是否已打开。
        consecutive_failures: 连续失败次数。
        shifts: 当前三班定义，供页面表单预填（**只读用途**；校验与写入在服务端，
            前端那份只是显示）。
        shift_order: 配置里 ``shift_1/2/3`` 的名称顺序。**传了才会把 ``shifts`` 从
            「按时刻排序」恢复成「按配置顺序」**——页面表单与写回都按后者，
            顺序错了会把时刻写进错误的段（见 :func:`order_shifts`）。

    Returns:
        可 JSON 序列化的 dict；字段形状见本模块 docstring 与测试。
    """
    remaining = snapshot.change_at - snapshot.now
    slots = order_shifts(shifts, shift_order)
    return {
        "now": snapshot.now.isoformat(),
        "current": shift_brief(snapshot.current),
        "upcoming": {
            **shift_brief(snapshot.upcoming),
            "change_at": snapshot.change_at.isoformat(),
            "remaining": format_remaining(remaining),
            "remaining_minutes": max(0, int(remaining.total_seconds() // 60)),
        },
        "lead_minutes": lead_minutes,
        "binding": {"bound": bound},
        "breaker": {"open": breaker_open, "consecutive_failures": consecutive_failures},
        "roster": {"imported": roster_imported},
        "shifts": [
            {
                **shift_brief(shift),
                "slot": slot,
                "hours": shift.duration_minutes // 60,
                "minutes": shift.duration_minutes,
            }
            for slot, shift in enumerate(slots, start=1)
        ],
        "recent": [record_brief(item) for item in recent],
    }


def _room_view(room: Mapping[str, Any]) -> dict[str, Any] | None:
    """把落盘的一个房间条目转成展示结构；形状不可用时返回 None。"""
    room_key = str(room.get("room", ""))
    names = room.get("operators")
    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
        operators: list[str] = []
    else:
        operators = [name for name in names if isinstance(name, str) and name.strip()]

    index = room.get("index")
    label = ROOM_LABELS.get(room_key, room_key)
    return {
        "room": room_key,
        "label": label,
        "index": index if isinstance(index, int) else None,
        "where": f"{label}{index}" if isinstance(index, int) else label,
        "operators": operators,
        # 「不动」的房间在这里**照实保留并标注**。注意与提醒消息的取舍刻意不同：
        # 提醒里不渲染它们（那是给用户的**指令**，让用户去改一间标明不要动的房
        # 就是错误信息）；而页面上必须显示（那是给用户看的**全貌**，藏起来用户
        # 会以为我们读漏了）。同一个数据，两种用途，取舍不同。
        "skipped": bool(room.get("skipped")),
    }


#: 房型在页面上的展示顺序。按基建里的实际动线排：产资源的在前、后勤在后。
#: 这不是"重要程度"，只是让每次打开页面的排列都一样——顺序随数据浮动会让人
#: 每次都要重新找位置。未列出的房型排在最后（按名称），不会因此被丢掉。
ROOM_ORDER = (
    "trading",
    "manufacture",
    "power",
    "dormitory",
    "control",
    "meeting",
    "hire",
    "processing",
)


def group_rooms(rooms: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把一班的房间**按房型分组**，供页面成块展示（纯逻辑）。

    为什么需要分组：平铺一列 13~15 行时，用户看不出「这套布局有几间贸易站、几间
    制造站」——而那恰恰是基建布局最要紧的信息。分组后每块自报数量。

    Args:
        rooms: :func:`roster_view` 产出的房间展示结构序列（每项含 ``room`` /
            ``label`` / ``index`` / ``where`` / ``operators`` / ``skipped``）。

    Returns:
        分组列表，每项为 ``{"room", "label", "count", "operator_count",
        "skipped_count", "rooms": [...]}``；组内房间按 ``index`` 升序，
        组间按 :data:`ROOM_ORDER`，未列出的房型按中文名排在最后。
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for room in rooms:
        if not isinstance(room, Mapping):
            continue
        key = str(room.get("room", ""))
        buckets.setdefault(key, []).append(room)

    def sort_key(key: str) -> tuple[int, str]:
        if key in ROOM_ORDER:
            return (ROOM_ORDER.index(key), "")
        # 未知房型排在已知之后；用中文名作次级键，保证顺序稳定。
        return (len(ROOM_ORDER), key)

    def room_key(room: Mapping[str, Any]) -> tuple[int, int]:
        index = room.get("index")
        # 序号缺失或不是整数时排到**最后**：让它去冒充「第一间」比排在末尾更糟
        # （用户会以为那就是 1 号房）。
        if isinstance(index, int):
            return (0, index)
        return (1, 0)

    groups: list[dict[str, Any]] = []
    for key in sorted(buckets, key=sort_key):
        items = buckets[key]
        ordered = sorted(items, key=room_key)
        groups.append(
            {
                "room": key,
                "label": ROOM_LABELS.get(key, key) or key,
                "count": len(ordered),
                "operator_count": sum(len(room.get("operators") or []) for room in ordered),
                "skipped_count": sum(1 for room in ordered if room.get("skipped")),
                "rooms": list(ordered),
            }
        )
    return groups


def roster_view(roster: Mapping[str, Any] | None) -> dict[str, Any]:
    """把落盘的排班表转成页面展示结构（纯逻辑，不读文件、不调时间）。

    未导入、形状不对、或结构不完整时返回 ``{"imported": False}``——页面据此显示
    上传引导，而不是空白。**绝不为了「看起来有数据」而编造结构**。

    Args:
        roster: ``JsonStateStore`` 里 ``imported_roster`` 键的值，或 None。

    Returns:
        可 JSON 序列化的 dict。已导入时含 ``source`` / ``imported_at`` /
        ``shift_count`` / ``skipped_total`` 与逐班逐房的明细。
    """
    if not isinstance(roster, Mapping):
        return {"imported": False}

    shifts_raw = roster.get("shifts")
    if not isinstance(shifts_raw, Sequence) or isinstance(shifts_raw, (str, bytes)):
        return {"imported": False}

    shifts: list[dict[str, Any]] = []
    skipped_total = 0
    for position, item in enumerate(shifts_raw, start=1):
        if not isinstance(item, Mapping):
            continue
        rooms_raw = item.get("rooms")
        if not isinstance(rooms_raw, Sequence) or isinstance(rooms_raw, (str, bytes)):
            rooms_raw = []

        rooms: list[dict[str, Any]] = []
        for room in rooms_raw:
            if not isinstance(room, Mapping):
                continue
            view = _room_view(room)
            if view is None:
                continue
            if view["skipped"]:
                skipped_total += 1
            rooms.append(view)

        plan_index = item.get("plan_index")
        if not isinstance(plan_index, int):
            plan_index = position
        shifts.append(
            {
                "plan_index": plan_index,
                "plan_name": str(item.get("plan_name", "")) or f"第 {plan_index} 班",
                "rooms": rooms,
                # 分组是页面的主要呈现方式；`rooms` 平铺保留，方便调用方按需取用。
                "groups": group_rooms(rooms),
                "room_count": len(rooms),
                "operator_count": sum(len(room["operators"]) for room in rooms),
                "skipped_count": sum(1 for room in rooms if room["skipped"]),
            }
        )

    total_rooms = sum(shift["room_count"] for shift in shifts)
    total_operators = sum(shift["operator_count"] for shift in shifts)

    return {
        "imported": True,
        "source": str(roster.get("source", "")),
        "imported_at": str(roster.get("imported_at", "")),
        "shift_count": len(shifts),
        "skipped_total": skipped_total,
        # 顶部汇总用：让用户一眼看出「这套布局有多大」。
        "total_rooms": total_rooms,
        "total_operators": total_operators,
        "shifts": shifts,
    }
