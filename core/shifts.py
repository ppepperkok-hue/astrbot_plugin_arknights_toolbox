"""班次模型：三班的时刻、校验与换班时刻推导（**单一事实来源**）。

为什么它在 `core/` 而不是某个模块里
------------------------------------
"什么时候该换班"是**跨模块的共享概念**：`shift_reminder` 要按它注册提醒，
`maa` 要在同一个时刻询问用户要不要让 MAA 跑一次。两边各写一套的后果不是
"重复劳动"，而是**两份时刻一旦漂移就会出错**——`maa` 会在错误的时刻询问，
用户一确认，MAA 就把它的计划索引永久前进一班（见
`docs/project-plan/10-maa-shift-switching.md`：`触发次数 = 换班次数`）。

所以按规定（`docs/architecture/extension.md` §4「与已有模块共享逻辑 → 抽到
`core/`，两个模块共用」）把它放在这里，**只留一份实现**。

`core/` 层的约束（`docs/architecture/rules.md` §2）
--------------------------------------------------
本文件是纯逻辑：**不 import astrbot**，也不 import `core` 里的其它东西。
它只收普通 Python 值（字符串/整数/`datetime`），不接收 event、不接收配置对象之外的东西。

装配层（`modules/*/module.py`）用既有的「先绝对、失败回退相对」导入链拿到它。
模块内的**纯逻辑**文件不许 import `core`（AstrBot 把插件当包加载时顶层没有
`core`），它们经由 `modules/shift_reminder/schedule.py` 这个薄壳取用——
那个壳同样是那条回退链，并在 `ruff.toml` 里有一条**逐文件**的豁免
（不用通配：`schedule.py` 这名字容易被人往里写实现，通配会静默放开）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

MINUTES_PER_DAY = 24 * 60

#: 配置里的三个班次槽位。扁平键形如 ``shift_1_name`` / ``shift_1_start`` /
#: ``shift_1_hours``。三个槽位是**配置格式的一部分**，凡是读这份配置的人都要知道，
#: 所以它属于共享模型而不是某个模块的实现细节。
SHIFT_SLOTS = (1, 2, 3)


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


def parse_shift_table(config: Mapping[str, Any]) -> ShiftTable:
    """把扁平配置键组装成**已校验**的班次表。

    原先住在 `modules/shift_reminder/module.py` 的装配层里。移到这里的原因：
    "从配置里读出三班"是共享概念的一部分——任何要按班次做事的模块都得用**同一套**
    解析与校验，否则两边对"什么叫合法配置"的判断会分叉（而分叉的代价是
    `maa` 在错误时刻询问，见模块 docstring）。

    配置非法一律抛 `ConfigError`——加载失败要让人当场看见，不静默降级
    （项目宪法 §2 第 2 条）。
    """
    shifts: list[Shift] = []
    for slot in SHIFT_SLOTS:
        prefix = f"shift_{slot}"
        name = config.get(f"{prefix}_name")
        start = config.get(f"{prefix}_start")
        hours = config.get(f"{prefix}_hours")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{prefix}_name 必须是非空字符串，收到 {name!r}")
        if not isinstance(start, str):
            raise ConfigError(f"{prefix}_start 必须是 HH:MM 字符串，收到 {start!r}")
        if isinstance(hours, bool) or not isinstance(hours, int):
            raise ConfigError(f"{prefix}_hours 必须是整数小时数，收到 {hours!r}")
        shifts.append(
            Shift(
                name=name.strip(),
                start_minute=parse_hhmm(start),
                duration_minutes=hours * 60,
            )
        )
    return validate(shifts)
