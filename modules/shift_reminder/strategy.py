"""换班判定策略。

V1 只有周期策略（按三班时刻）。V2 的森空岛心情策略应实现同样的
``snapshot`` 签名，这样调度层不需要知道跑的是哪一种策略（S2 插槽）。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .schedule import Shift, ShiftTable, boundaries_between, current_shift


@dataclass(frozen=True)
class Snapshot:
    """某一时刻的判定结果。"""

    now: datetime
    current: Shift
    upcoming: Shift
    change_at: datetime


class Strategy(Protocol):
    """判定策略接口（S2 插槽）。"""

    def snapshot(self, now: datetime) -> Snapshot:
        """返回 ``now`` 时刻的状态快照。"""
        ...


class PeriodStrategy:
    """周期策略：只看三班时刻，不需要任何外部数据，因此永远可用。"""

    def __init__(self, table: ShiftTable, lead_minutes: int) -> None:
        self._table = table
        self._lead_minutes = lead_minutes

    @property
    def lead_minutes(self) -> int:
        return self._lead_minutes

    @property
    def table(self) -> ShiftTable:
        return self._table

    def snapshot(self, now: datetime) -> Snapshot:
        upcoming_change = boundaries_between(self._table, now, days=1)[0]
        upcoming = self._table.starting_at(
            upcoming_change.hour * 60 + upcoming_change.minute,
        )
        if upcoming is None:  # pragma: no cover - boundaries 必然来自班次表
            raise RuntimeError("换班时刻不在班次表内，这是内部错误")
        return Snapshot(
            now=now,
            current=current_shift(self._table, now),
            upcoming=upcoming,
            change_at=upcoming_change,
        )
