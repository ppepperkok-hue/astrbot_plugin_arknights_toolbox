"""班次表的**跨模块共享文件**：格式、路径与解析（纯逻辑，零框架依赖）。

## 为什么需要一条通道

`maa` 要在**换班时刻**问用户「要不要让 MAA 跑一次」，而班次时刻只存在于
`shift_reminder` 的配置段里——宿主只给每个模块**自己那一段**配置
（`docs/architecture/extension.md` §2「契约不变项」：模块「不得看到整份插件配置」），
模块之间又**不许互相 import**。于是四条路只剩一条不违反任何一条规则：

    shift_reminder 把**已校验**的班次表写进 plugin_data/，maa 只读它。

**为什么这条路是安全的**（而不是"绕过去"）：

1. **写入方唯一**：全仓只有 `shift_reminder` 写这个文件（`git grep` 可证），
   所以「班次时刻」只有一个来源——这正是本项目最在意的那条性质；
2. **格式只有一份**：本文件同时提供 :func:`dump_shared` 与 :func:`load_shared`，
   两个模块用的是**同一套**序列化与校验；
3. **不动宿主、不造新机制**：没有给 `Module` 加"共享视图"之类的开关
   （那等于为模块间通信开一个口子），也没有复制一份时刻到 `maa` 的配置段
   （那会让同一个事实有两份实现——本项目为此栽过多次）。

## 漂移的代价（为什么这里值得这么小心）

MAA 每跑完一次基建任务就把**它内部的计划索引永久前进一格**，写回配置、
不会自动纠正（`docs/project-plan/10-maa-shift-switching.md` §4.3）。所以：

    触发次数 必须严格等于 换班次数

若两边对"什么时候该换班"有了不同答案，用户会在**错误的时刻**被询问，一确认，
MAA 就多跑一趟、班次永久跳一班。**这就是本文件存在的全部理由。**

## 内容的取舍

写的是**完整**的三班（槽位、名字、开始时刻、时长），不是"刚好够 `maa` 用"的最小集：

* `slot` 是**稳定身份**——排班表的 `plans` 就是按下标对应槽位的
  （见 `modules/shift_reminder/roster.py`），光有名字对不上；
* `duration_minutes` 现在 `maa` 不用，但将来的模块（例如"这一班还剩多久"）要用，
  而**只写刚好够用的字段会迫使下一个模块重新发明解析**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .shifts import (
    SHIFT_SLOTS,
    ConfigError,
    Shift,
    ShiftTable,
    format_hhmm,
    parse_hhmm,
    validate,
)

__all__ = [
    "FORMAT_VERSION",
    "SHARED_SHIFT_FILENAME",
    "SharedShiftError",
    "SharedShiftTable",
    "dump_shared",
    "load_shared",
    "shared_shift_path",
]

#: 文件名**带 `shared_` 前缀**：它在 `plugin_data/` 里与各模块自己的 `state.json`
#: 并排，而排障的人一眼要看得出「这是给别的模块读的、不是本模块的私有存档」。
SHARED_SHIFT_FILENAME = "shared_shift_table.json"

#: 格式版本。**改动格式必须同时改它**：读取方据此能把「格式变了」与「文件坏了」
#: 分开说，而不是笼统地报一句读不出来。
FORMAT_VERSION = 1


class SharedShiftError(ValueError):
    """共享班次表读不出来（结构不对、字段非法、或整表校验不通过）。

    消息面向使用者，必须能直接看懂哪里不对——它会被原样拼进用户可见的状态文案，
    而不是只留在日志里。
    """


@dataclass(frozen=True)
class SharedShiftTable:
    """从共享文件读出来的班次表。

    Attributes:
        shifts: **槽位顺序**的三班（``shifts[i]`` 对应配置里的 ``shift_{i+1}``）。
            与排班表 ``plans`` 的下标对应关系就建立在这个顺序上，所以**不能**用
            :attr:`table` 的顺序去索引 ``plans``。
        table: 已通过 :func:`core.shifts.validate` 的班次表（按开始时刻升序）。
            算换班时刻要用它。
        timezone: 写下这份表时用的 IANA 时区名。**读取方必须用它来算"现在"**，
            否则跨时区部署时询问会落在错误的钟点上。
        generated_at: 写入时刻（ISO 字符串）。**只用于排障**（"这份表是什么时候写的"），
            不参与任何判断，所以缺失时按空串处理而不是报错。
        version: 文件格式版本。
    """

    shifts: tuple[Shift, ...]
    table: ShiftTable
    timezone: str
    generated_at: str = ""
    version: int = FORMAT_VERSION

    @property
    def names(self) -> tuple[str, ...]:
        """三班的名字（槽位顺序）。"""
        return tuple(shift.name for shift in self.shifts)


def shared_shift_path(data_root: Path | str, plugin_name: str) -> Path:
    """共享班次表的固定路径。

    抽成函数是为了让**写入方与读取方算出同一个路径**：两边各拼一次路径字符串，
    迟早会因为谁多写了个子目录而读不到对方写的文件，而那种错**不会报错**，
    只会表现为「班次表一直读不到」。
    """
    return Path(data_root) / plugin_name / SHARED_SHIFT_FILENAME


def dump_shared(
    slots: Sequence[Shift],
    *,
    timezone: str,
    generated_at: datetime,
) -> dict[str, Any]:
    """把**槽位顺序**的三班打包成可落盘的字典。

    Args:
        slots: ``core.shifts.parse_shift_slots`` 的结果（保留槽位顺序，未排序）。
        timezone: 已校验的 IANA 时区名。
        generated_at: 写入时刻（调用方给，便于测试注入）。

    Returns:
        可直接 ``json.dumps`` 的字典。

    Raises:
        SharedShiftError: 槽位数量与 :data:`SHIFT_SLOTS` 对不上，或时区名为空。
            **写入方自己的错误要当场暴露**——写出一份读不回来的文件，
            读取方只会在几小时后的换班时刻才发现。
    """
    if len(slots) != len(SHIFT_SLOTS):
        raise SharedShiftError(
            f"班次槽位数量应为 {len(SHIFT_SLOTS)}，收到 {len(slots)}；"
            "写入方必须传 parse_shift_slots 的结果（槽位顺序）"
        )
    if not isinstance(timezone, str) or not timezone.strip():
        raise SharedShiftError(f"时区名不能为空，收到 {timezone!r}")

    return {
        "version": FORMAT_VERSION,
        "generated_at": generated_at.isoformat(),
        "timezone": timezone.strip(),
        "shifts": [
            {
                "slot": slot_number,
                "name": shift.name,
                "start": format_hhmm(shift.start_minute),
                "start_minute": shift.start_minute,
                "duration_minutes": shift.duration_minutes,
            }
            for slot_number, shift in zip(SHIFT_SLOTS, slots, strict=True)
        ],
    }


def load_shared(payload: Any) -> SharedShiftTable:
    """解析共享班次表；**任何结构问题都抛** :class:`SharedShiftError`。

    刻意做得严格，而且把「文件坏了」的判定留在这一层：调用方据此能把
    「文件不存在」（首次运行／对方模块没开，是正常状态）与「文件坏了」
    （必须让人看见）分开说——前者安静、后者响亮。

    Args:
        payload: 已 ``json.loads`` 出来的对象。

    Returns:
        校验通过的 :class:`SharedShiftTable`。

    Raises:
        SharedShiftError: 不是对象、版本不认识、缺字段、字段类型不对、
            ``start`` 与 ``start_minute`` 自相矛盾、槽位不是 1..N、
            或整表校验不过（时长和 ≠ 24 小时、班次不连续等）。
    """
    if not isinstance(payload, Mapping):
        raise SharedShiftError(f"共享班次表的根节点必须是对象，实际是 {type(payload).__name__}")

    version = payload.get("version")
    if version != FORMAT_VERSION:
        raise SharedShiftError(
            f"共享班次表的格式版本不认识：{version!r}（本版本只认 {FORMAT_VERSION}）。"
            "多半是两个模块的版本不一致，升级到同一版本即可。"
        )

    timezone = payload.get("timezone")
    if not isinstance(timezone, str) or not timezone.strip():
        raise SharedShiftError(f"共享班次表缺少可用的 timezone，收到 {timezone!r}")

    raw_shifts = payload.get("shifts")
    if not isinstance(raw_shifts, list) or not raw_shifts:
        raise SharedShiftError("共享班次表的 shifts 必须是非空数组")

    shifts: list[Shift] = []
    seen_slots: list[int] = []
    for index, item in enumerate(raw_shifts, start=1):
        shifts.append(_parse_one(item, index))
        if isinstance(item, Mapping):
            slot = item.get("slot")
            if isinstance(slot, bool) or not isinstance(slot, int):
                raise SharedShiftError(f"第 {index} 个班次的 slot 必须是整数，收到 {slot!r}")
            seen_slots.append(slot)

    if tuple(seen_slots) != SHIFT_SLOTS:
        raise SharedShiftError(
            f"共享班次表的槽位应为 {list(SHIFT_SLOTS)}（按顺序），实际是 {seen_slots}；"
            "槽位顺序是排班表 plans 下标对应的依据，不能靠猜"
        )

    try:
        table = validate(shifts)
    except ConfigError as exc:
        raise SharedShiftError(f"共享班次表整体校验不通过：{exc}") from exc

    generated_at = payload.get("generated_at")
    return SharedShiftTable(
        shifts=tuple(shifts),
        table=table,
        timezone=timezone.strip(),
        generated_at=generated_at if isinstance(generated_at, str) else "",
        version=FORMAT_VERSION,
    )


def _parse_one(item: Any, index: int) -> Shift:
    """解析一个班次条目；`start` 与 `start_minute` 必须**互相印证**。

    为什么两个都查：它们一旦不一致（手改、或将来某处写坏），以谁为准都说得通，
    而**说不通的那种"都说得通"正是静默错位的温床**。宁可当场报错。
    """
    if not isinstance(item, Mapping):
        raise SharedShiftError(f"第 {index} 个班次必须是对象，实际是 {type(item).__name__}")

    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SharedShiftError(f"第 {index} 个班次的 name 必须是非空字符串，收到 {name!r}")

    start = item.get("start")
    if not isinstance(start, str):
        raise SharedShiftError(f"第 {index} 个班次的 start 必须是 HH:MM 字符串，收到 {start!r}")
    try:
        start_minute = parse_hhmm(start)
    except ConfigError as exc:
        raise SharedShiftError(f"第 {index} 个班次的 start 不合法：{exc}") from exc

    declared = item.get("start_minute")
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise SharedShiftError(f"第 {index} 个班次的 start_minute 必须是整数，收到 {declared!r}")
    if declared != start_minute:
        raise SharedShiftError(
            f"第 {index} 个班次自相矛盾：start={start!r} 表示第 {start_minute} 分钟，"
            f"而 start_minute={declared}。两者必须一致，否则不知道该信哪个"
        )

    duration = item.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise SharedShiftError(
            f"第 {index} 个班次的 duration_minutes 必须是整数，收到 {duration!r}"
        )

    return Shift(name=name.strip(), start_minute=start_minute, duration_minutes=duration)
