"""判定策略测试（纯逻辑，不需要 AstrBot）。"""

from datetime import datetime

from modules.shift_reminder.schedule import Shift, validate
from modules.shift_reminder.strategy import PeriodStrategy

DEFAULT = [
    Shift("早班", 8 * 60, 12 * 60),
    Shift("晚班", 20 * 60, 6 * 60),
    Shift("夜班", 2 * 60, 6 * 60),
]


def strategy(lead_minutes: int = 10) -> PeriodStrategy:
    return PeriodStrategy(validate(DEFAULT), lead_minutes)


def test_snapshot_before_shift_change():
    snap = strategy().snapshot(datetime(2026, 9, 29, 19, 30))
    assert snap.current.name == "早班"
    assert snap.upcoming.name == "晚班"
    assert snap.change_at == datetime(2026, 9, 29, 20, 0)


def test_snapshot_after_midnight_stays_in_late_shift():
    snap = strategy().snapshot(datetime(2026, 9, 29, 0, 30))
    assert snap.current.name == "晚班"
    assert snap.upcoming.name == "夜班"
    assert snap.change_at == datetime(2026, 9, 29, 2, 0)


def test_snapshot_exactly_at_change_point_moves_to_new_shift():
    snap = strategy().snapshot(datetime(2026, 9, 29, 20, 0))
    assert snap.current.name == "晚班"
    assert snap.upcoming.name == "夜班"


def test_strategy_exposes_lead_minutes():
    assert strategy(lead_minutes=15).lead_minutes == 15


def test_snapshot_is_consistent_across_a_full_day():
    """一天里任意时刻都应能定位到某个班次，且下一班必在当前班之后开始。"""
    engine = strategy()
    for hour in range(24):
        snap = engine.snapshot(datetime(2026, 9, 29, hour, 0))
        assert snap.current.name in {s.name for s in DEFAULT}
        assert snap.change_at > snap.now
