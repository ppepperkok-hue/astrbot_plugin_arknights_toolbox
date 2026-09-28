"""三班制的数据模型、配置校验与换班时刻计算。

纯逻辑模块：**禁止 import astrbot**（由 ruff.toml 的 TID 禁入规则强制），
因此可以被 pytest 直接覆盖，不需要 AstrBot 运行时。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

MINUTES_PER_DAY = 24 * 60


class ConfigError(ValueError):
    """配置不合法。消息面向使用者，必须能直接看懂哪里错了。"""


@dataclass(frozen=True)
class Shift:
    """一个班次。``start_minute`` 是当天分钟数（0..1439）。"""

    name: str
    start_minute: int
    duration_minutes: int

    @property
    def end_minute(self) -> int:
        """结束时刻（跨天时取模）。"""
        return (self.start_minute + self.duration_minutes) % MINUTES_PER_DAY


@dataclass(frozen=True)
class ShiftTable:
    """校验通过的班次表，按起始时刻升序。"""

    shifts: tuple[Shift, ...]

    @property
    def start_minutes(self) -> tuple[int, ...]:
        return tuple(s.start_minute for s in self.shifts)

    def starting_at(self, minute_of_day: int) -> Shift | None:
        """返回在指定分钟开始的班次，没有则 None。"""
        for shift in self.shifts:
            if shift.start_minute == minute_of_day:
                return shift
        return None


def format_hhmm(minute_of_day: int) -> str:
    """把当天分钟数格式化成 ``HH:MM``。"""
    minute_of_day %= MINUTES_PER_DAY
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def parse_hhmm(value: str) -> int:
    """把 ``HH:MM`` 解析成当天分钟数；格式错误抛 ConfigError。"""
    if not isinstance(value, str):
        raise ConfigError(f"时刻必须是字符串，收到 {type(value).__name__}")
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ConfigError(f"时刻格式应为 HH:MM，收到 {value!r}")
    if len(parts[0]) > 2 or len(parts[1]) > 2:
        raise ConfigError(f"时刻格式应为 HH:MM，收到 {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"时刻超出范围：{value!r}")
    return hour * 60 + minute


def validate(shifts: Iterable[Shift]) -> ShiftTable:
    """校验班次表：非空、时长为正、总时长正好 24 小时、首尾相接、名称唯一。"""
    items = list(shifts)
    if not items:
        raise ConfigError("至少要配置一个班次")
    for shift in items:
        if shift.duration_minutes <= 0:
            raise ConfigError(f"班次「{shift.name}」的时长必须大于 0")
    total = sum(s.duration_minutes for s in items)
    if total != MINUTES_PER_DAY:
        raise ConfigError(f"所有班次时长之和必须正好是 24 小时，当前为 {total / 60:g} 小时")
    names = [s.name for s in items]
    if len(set(names)) != len(names):
        raise ConfigError("班次名称不能重复")

    ordered = tuple(sorted(items, key=lambda s: s.start_minute))
    for index, current in enumerate(ordered):
        following = ordered[(index + 1) % len(ordered)]
        if current.end_minute != following.start_minute:
            raise ConfigError(
                f"班次不连续：「{current.name}」结束于 {format_hhmm(current.end_minute)}，"
                f"但下一班「{following.name}」从 {format_hhmm(following.start_minute)} 开始"
            )
    return ShiftTable(ordered)


def current_shift(table: ShiftTable, now: datetime) -> Shift:
    """``now`` 落在哪个班次里（跨天由取模自然处理）。"""
    minute = now.hour * 60 + now.minute
    chosen = table.shifts[-1]
    for shift in table.shifts:
        if shift.start_minute <= minute:
            chosen = shift
    return chosen


def boundaries_between(table: ShiftTable, now: datetime, days: int = 3) -> list[datetime]:
    """``(now, now + days]`` 内的换班时刻，升序。"""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found: list[datetime] = []
    for offset in range(days + 2):
        day = midnight + timedelta(days=offset)
        for minute in table.start_minutes:
            moment = day + timedelta(minutes=minute)
            if moment > now:
                found.append(moment)
    found.sort()
    return found[: len(table.shifts) * days]


def reminders_between(
    table: ShiftTable,
    now: datetime,
    lead_minutes: int,
    days: int = 3,
) -> list[tuple[datetime, Shift]]:
    """未来的提醒时刻及其对应班次：``提醒时刻 = 换班时刻 - 提前量``。"""
    if lead_minutes < 0:
        raise ConfigError("提前量不能为负数")
    pairs: list[tuple[datetime, Shift]] = []
    for moment in boundaries_between(table, now, days=days):
        shift = table.starting_at(moment.hour * 60 + moment.minute)
        if shift is None:
            continue
        remind_at = moment - timedelta(minutes=lead_minutes)
        if remind_at > now:
            pairs.append((remind_at, shift))
    return pairs
