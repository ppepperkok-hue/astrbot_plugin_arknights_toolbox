"""排班表导入的纯逻辑：命令参数解析、路径安全校验、落盘数据组装。

**为什么单独一个文件**：这三件事全是纯函数（输入是 str / Path / 解析结果，输出是
Path / dict），与 AstrBot 无关，因此能被 pytest 直接覆盖——装配层（`module.py`）
只负责读文件、把结果发回给用户。

安全上只做一件事，但要做到位：``/ak import <文件名>`` 里的文件名**来自用户消息**，
绝不能直接拼进路径。见 `resolve_import_path`。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .schedule_file import PlanAssignment

__all__ = [
    "SHIFT_COUNT",
    "RosterImportError",
    "build_roster",
    "describe_roster",
    "parse_import_argument",
    "resolve_import_path",
]

#: 本插件固定三班；排班表的 ``plans`` 数量必须与它一致（裁决见 implementation.md §2.6）。
SHIFT_COUNT = 3

#: 只接受 JSON（用户可能会把 zip 或 txt 一起丢进数据目录）。
ALLOWED_SUFFIX = ".json"

#: 路径分隔符：命中任意一个就说明这不是「纯文件名」。
_PATH_SEPARATORS = ("/", "\\")


class RosterImportError(ValueError):
    """导入排班表时的问题：命令用法不对、文件名非法、或班次数对不上。

    与 `schedule_file.ScheduleFileError` 的分工：那个只管**文件内容**的结构问题，
    这个管**导入过程**的问题（参数、路径、班次数）。两者都由调用方转成给用户的回执。
    """


def parse_import_argument(message_str: str) -> str:
    """从消息文本里取出 ``/ak import`` 之后的文件名；没有则返回空串。

    不依赖框架的参数解析，``/ak import a.json`` 与 ``import a.json`` 结果一致
    （宿主 `main.py` 只把第一个词当子命令转发，参数得自己从原文里取）。

    取的是 ``import`` 之后的**全部剩余文本**而不是第一个词：文件名里允许有空格，
    与其猜哪一段是名字，不如整段拿来交给「文件是否存在」裁决；多余的词会导致
    明确的「找不到文件」，而不会被静默忽略。

    Args:
        message_str: 事件里的消息原文。

    Returns:
        去掉首尾空白后的文件名；缺参数时为空串。
    """
    tokens = message_str.strip().split()
    if tokens and tokens[0].lstrip("/").lower() == "ak":
        tokens = tokens[1:]
    if not tokens or tokens[0].lower() != "import":
        return ""
    return " ".join(tokens[1:]).strip()


def resolve_import_path(base_dir: Path, name: str) -> Path:
    """把用户给的文件名解析成**允许目录内**的绝对路径。

    这是本包最要紧的一道防线：``name`` 直接来自用户消息，因此必须挡住

    - 目录穿越：``../../etc/passwd``、``..\\..\\x``
    - 绝对路径：``/etc/passwd``、``C:\\Windows\\x``
    - 符号链接指向目录外
    - 空名字、纯空白

    校验分两层：先做**字符串层**（必须是纯文件名、后缀为 `.json`），再做
    **路径层**（`resolve()` 之后要求父目录正是 `base_dir`）。只有字符串层是不够的——
    数据目录里放一个指向 `/etc` 的软链，纯字符串检查会放行，`resolve()` 才会露馅。

    Args:
        base_dir: 允许读取的目录（插件的 `plugin_data` 子目录）。
        name: 用户给的文件名。

    Returns:
        已确认落在 `base_dir` 内的绝对路径。**不保证文件存在**——是否存在于
        装配层负责报告，这样本函数保持纯粹、不碰文件系统。

    Raises:
        RosterImportError: 名字为空、含路径分隔符、含盘符、后缀不是 `.json`，
            或解析后落在 `base_dir` 之外。
    """
    candidate = name.strip()
    if not candidate:
        raise RosterImportError("缺少文件名。用法：/ak import <文件名>")

    if any(separator in candidate for separator in _PATH_SEPARATORS):
        raise RosterImportError(
            f"文件名里不能带路径：{candidate!r}。请只写文件名，并把文件放进下面这个目录。"
        )

    # Windows 盘符（如 `C:file.json`）：在 Linux 上它是合法文件名，但对用户来说
    # 一定是「填错了绝对路径」，与其放行后报「找不到」，不如当场说清。
    if len(candidate) >= 2 and candidate[1] == ":":
        raise RosterImportError(f"文件名里不能带盘符：{candidate!r}。请只写文件名。")

    if Path(candidate).suffix.lower() != ALLOWED_SUFFIX:
        raise RosterImportError(f"只支持 {ALLOWED_SUFFIX} 文件，收到 {candidate!r}。")

    base = base_dir.resolve()
    target = (base / candidate).resolve()
    if target.parent != base:
        # 走到这里只可能是软链：字符串层已经挡掉了分隔符与盘符。
        raise RosterImportError(
            f"文件 {candidate!r} 指向了数据目录之外，出于安全拒绝读取。"
            "请把排班表 JSON 直接放进数据目录（不要用快捷方式/软链）。"
        )
    return target


def build_roster(
    plans: Sequence[PlanAssignment],
    *,
    source: str,
    imported_at: datetime,
) -> dict[str, Any]:
    """把解析到的班次组装成落盘用的 JSON 结构。

    **班次身份用下标，不用名字**：排班表里的 ``plans[i].name`` 写法五花八门
    （实测有 ``12H第一班``、``第1班``、``A 组 12 H`` 等），而「第 i 个 plan 对应
    `shift_i`」是排班表与配置之间唯一稳定的对应关系。文件里的名字照存一份
    （``plan_name``）只作参考，**不作为身份**——渲染时按 `plan_index` 回配置取班次。

    Args:
        plans: `schedule_file.parse_schedule_file` 的结果，按文件顺序。
        source: 来源文件名，仅用于回执与排查。
        imported_at: 导入时刻（调用方给，本函数不读系统时间，保持可测）。

    Returns:
        可 JSON 序列化的结构：``{source, imported_at, shift_count, shifts[]}``，
        每个 shift 含 ``plan_index`` / ``plan_name`` / ``rooms[]``，每个 room 含
        ``room`` / ``index`` / ``operators`` / ``skipped``。

    Raises:
        RosterImportError: 班次数量不等于 `SHIFT_COUNT`。
    """
    if len(plans) != SHIFT_COUNT:
        raise RosterImportError(
            f"这份排班表是 {len(plans)} 班，本插件目前固定 {SHIFT_COUNT} 班，班次对不上，不能导入。"
        )

    return {
        "source": source,
        "imported_at": imported_at.isoformat(),
        "shift_count": len(plans),
        "shifts": [
            {
                "plan_index": position + 1,
                "plan_name": plan.name,
                "rooms": [
                    {
                        "room": room.room,
                        "index": room.index,
                        "operators": list(room.operators),
                        "skipped": room.skipped,
                    }
                    for room in plan.rooms
                ],
            }
            for position, plan in enumerate(plans)
        ],
    }


def describe_roster(roster: Mapping[str, Any]) -> str:
    """把落盘的排班表汇总成一句可核对的回执（导入成功时要让用户看到数字）。

    Args:
        roster: `build_roster` 的产物。

    Returns:
        多行文本：总班次数、每班的房间数与干员数、以及标了「不动」的房间数。

    Raises:
        RosterImportError: 结构不符合预期（正常不会发生——这份数据是本模块自己刚写的；
            真出现说明数据被外部改坏了，此时宁可显式报错也不打一句错数字）。
    """
    shifts = roster.get("shifts")
    if not isinstance(shifts, Sequence) or isinstance(shifts, (str, bytes)):
        raise RosterImportError(f"排班表数据损坏：shifts 不是列表（{type(shifts).__name__}）")

    lines = [f"已导入排班表：{len(shifts)} 个班次"]
    total_rooms = 0
    total_operators = 0
    total_skipped = 0

    for position, shift in enumerate(shifts):
        if not isinstance(shift, Mapping):
            raise RosterImportError(f"排班表数据损坏：shifts[{position}] 不是对象")
        rooms = shift.get("rooms")
        if not isinstance(rooms, Sequence) or isinstance(rooms, (str, bytes)):
            raise RosterImportError(f"排班表数据损坏：shifts[{position}].rooms 不是列表")

        operators = 0
        skipped = 0
        for room in rooms:
            if not isinstance(room, Mapping):
                raise RosterImportError(f"排班表数据损坏：shifts[{position}] 里有非对象房间")
            names = room.get("operators")
            count = len(names) if isinstance(names, Sequence) and not isinstance(names, str) else 0
            operators += count
            if room.get("skipped"):
                skipped += 1

        total_rooms += len(rooms)
        total_operators += operators
        total_skipped += skipped

        suffix = f"，其中 {skipped} 间标了「不动」" if skipped else ""
        lines.append(f"  第 {position + 1} 班：{len(rooms)} 个房间、{operators} 位干员{suffix}")

    lines.append(f"合计：{total_rooms} 个房间、{total_operators} 位干员，{total_skipped} 间不动。")
    return "\n".join(lines)
