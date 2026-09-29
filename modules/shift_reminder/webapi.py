"""WebUI 只读状态数据的组装（纯逻辑）。

页面只是**另一种入口**：这里不重新实现任何班次计算，只把模块已有的
:class:`~.strategy.Snapshot`、绑定状态与发送记录翻译成可 JSON 序列化的 dict。
判定逻辑永远只有一处（``schedule`` / ``strategy``），否则两处必然漂移。

时间一律由调用方传入——纯逻辑里不出现 ``datetime.now()``，否则测不了。

纯逻辑模块：**禁止 import astrbot**（由 ruff.toml 的 TID 禁入规则强制）。
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

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
