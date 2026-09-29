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

from .plan_name import MINUTES_PER_DAY, parse_duration_hint, suggest_shift_minutes
from .schedule_file import PlanAssignment

__all__ = [
    "MAX_ROSTER_LINES",
    "ROOM_LABELS",
    "SHIFT_COUNT",
    "RosterImportError",
    "build_roster",
    "describe_duration_hint",
    "describe_roster",
    "parse_import_argument",
    "render_roster_lines",
    "resolve_import_path",
    "suggested_durations",
    "suggested_hours",
]

#: 本插件固定三班；排班表的 ``plans`` 数量必须与它一致（裁决见 implementation.md §2.6）。
SHIFT_COUNT = 3

#: `render_roster_lines` 返回的**总行数**上限（含标题与省略提示行）。
#:
#: 取值理由：这是**体验取舍，不是技术限制**。QQ 消息在手机上一屏大约十几行，
#: 而换班提醒本身已经占了 3~4 行（换班时刻、当前班、即将班），留给「本班配制」
#: 的余量取 8 行——足够把常见的三四个房间说完，又不至于把消息顶成一整屏。
#: 真实排班表单班可达 8~10 个房间、十几个干员名，没有上限就会刷屏。
#:
#: **语义是总行数而不是房间行数**：这样「承诺的上限」与用户实际看到的行数一致，
#: 截断后再加省略提示行也不会超出。
MAX_ROSTER_LINES = 8

#: 房型英文键 → 中文名。排班表里是游戏内部的英文房型名，直接发给用户看不懂。
#: 未知房型**原样显示英文键**而不是丢弃——丢一间房等于给错名单，比显示一个生词更糟。
ROOM_LABELS = {
    "trading": "贸易站",
    "manufacture": "制造站",
    "power": "发电站",
    "dormitory": "宿舍",
    "control": "控制中枢",
    "meeting": "会客室",
    "hire": "办公室",
    "processing": "加工站",
}

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
        ``room`` / ``index`` / ``operators`` / ``skipped``；另有
        ``duration_hints_minutes``——从名字里读出的**时长提示**（读不出时为 ``None``）。

    Raises:
        RosterImportError: 班次数量不等于 `SHIFT_COUNT`。

    Note:
        ``duration_hints_minutes`` 是**建议，不是配置**：本函数**不碰** ``shifts``
        配置项，用户没点「填入」之前，他的班次设置一个字都不会变。要用它必须
        由用户在页面上确认（见 `docs/implementation/implementation.md` §2.6）。
    """
    if len(plans) != SHIFT_COUNT:
        raise RosterImportError(
            f"这份排班表是 {len(plans)} 班，本插件目前固定 {SHIFT_COUNT} 班，班次对不上，不能导入。"
        )

    hints = suggest_shift_minutes([plan.name for plan in plans], expected_count=SHIFT_COUNT)

    return {
        "source": source,
        "imported_at": imported_at.isoformat(),
        "shift_count": len(plans),
        # 读得出才是列表；读不出就是 None，页面据此不显示「一键填入」。
        "duration_hints_minutes": list(hints) if hints else None,
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


def suggested_durations(roster: Mapping[str, Any] | None) -> tuple[int, ...] | None:
    """把落盘的时长提示读回来；形状不对或没读出来都返回 ``None``。

    读回来时**重新校验一次**：必须是 `SHIFT_COUNT` 个正整数、且合计 24 小时。
    这份数据虽然是我们自己写的，但它躺在磁盘上、可能被外部改坏或被旧版本写成别的形状——
    那时宁可当作「没有提示」，也不能拿一个坏值去建议用户改配置。

    Args:
        roster: `build_roster` 的产物，或 None。

    Returns:
        分钟数元组；不成立时 ``None``。
    """
    if not isinstance(roster, Mapping):
        return None

    raw = roster.get("duration_hints_minutes")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    if len(raw) != SHIFT_COUNT:
        return None

    minutes: list[int] = []
    for item in raw:
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            return None
        minutes.append(item)

    if sum(minutes) != MINUTES_PER_DAY:
        return None
    return tuple(minutes)


def suggested_hours(roster: Mapping[str, Any] | None) -> tuple[int, ...] | None:
    """把时长建议换算成**小时**；含非整点小时时返回 ``None``。

    ``_conf_schema.json`` 的 ``shift_N_hours`` 是整数（模块侧也按整数小时校验），
    所以「8 小时 30 分」这种建议**存不进去**。与其让页面填一个保存时必然被拒的值、
    让用户点了「填入」再在保存时吃一个错，不如在服务端就判定「这条建议不能直接填入」，
    让页面改成提示手动调整——**不允许出现「点了填入、保存才报错」这种半成功**。

    （真实样本里确实出现过 ``C 组 8.5H``，所以这不是假想的情况。）

    Args:
        roster: `build_roster` 的产物，或 None。

    Returns:
        小时数元组（如 ``(12, 6, 6)``）；建议不存在或含非整点小时时为 ``None``。
    """
    hinted = suggested_durations(roster)
    if hinted is None or any(item % 60 for item in hinted):
        return None
    return tuple(item // 60 for item in hinted)


def _format_minutes(minutes: int) -> str:
    """分钟数 → 「12 小时」/「8 小时 30 分」，用于回执。"""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} 分钟"
    return f"{hours} 小时" if not rest else f"{hours} 小时 {rest} 分"


def _individual_hints(roster: Mapping[str, Any] | None) -> list[int | None]:
    """逐个班次名字读时长（**不做** 24 小时闸门），用于把「为什么没给建议」说准。

    闸门把两类完全不同的情况都归成 ``None``：①名字里压根没有时长；②每个名字都读得出、
    但合计不是 24 小时。对用户而言这两句解释天差地别——第②种若说成「没读出来」，
    用户会以为插件读不出时长（实测 ``333_layout_for_Orundum`` 的 12/12/8.5 就是这种），
    于是重复上传、反复折腾。**失败要显式，也要准确。**
    """
    if not isinstance(roster, Mapping):
        return []
    shifts = roster.get("shifts")
    if not isinstance(shifts, Sequence) or isinstance(shifts, (str, bytes)):
        return []

    hints: list[int | None] = []
    for item in shifts:
        if not isinstance(item, Mapping):
            return []
        hints.append(parse_duration_hint(item.get("plan_name", "")))
    return hints


def describe_duration_hint(roster: Mapping[str, Any] | None) -> str:
    """给用户的**一句可操作**的话：读出了节奏、还是没读出。

    三种结局都要说准（宪法 §2 第 2 条：看起来成功但什么都没发生是最高优先级 bug，
    而「说错原因」同样会让人白折腾）：

    1. 读出且能直接填 → 摊出节奏并说明去哪儿用（页面上有「填入」按钮）；
    2. 读出了但**合计不是 24 小时** → 摊出读到的东西并说明为什么没采用；
       不能笼统说成「读不出」——那会让用户以为插件没这个能力，于是反复重传；
    3. 压根读不出（名字里没有时长）→ 明说「照旧手填」。

    Args:
        roster: `build_roster` 的产物，或 None。

    Returns:
        一行中文提示（不含换行）。
    """
    hinted = suggested_durations(roster)
    if hinted is None:
        # 区分「压根读不出」与「都读出了但合计不是 24 小时」——对用户是两件事。
        partial = _individual_hints(roster)
        if partial and all(item is not None for item in partial):
            readable = [item for item in partial if item is not None]
            rhythm = " / ".join(_format_minutes(item) for item in readable)
            total = _format_minutes(sum(readable))
            return (
                f"从名字里读出了节奏：{rhythm}，但合计 {total}，不是 24 小时，"
                "所以没有采用——班次时刻请自己设置。"
            )
        return "这份排班表的名字里没有可用的时长信息，班次时刻请照旧自己设置。"

    rhythm = " / ".join(_format_minutes(item) for item in hinted)
    if suggested_hours(roster) is None:
        return (
            f"从名字里读出了节奏：{rhythm}；但它含非整点小时，"
            "而班次时长只支持整点小时，请手动调整。"
        )
    return (
        f"从名字里读出了节奏：{rhythm}。想用它在页面上点「按排班表填入」即可，不点就不动你的设置。"
    )


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


def render_roster_lines(roster: Mapping[str, Any] | None, plan_index: int) -> list[str]:
    """把某一班的干员名单渲染成提醒消息的附加行。

    **跳过 `skipped` 的房间**：排班表作者用这个标记表示「这一班这间房不要动」，
    渲染进提醒就等于让用户去改一间不该改的房——那是**错误指令**，不是冗余信息
    （裁决见 docs/implementation/implementation.md §2.6）。干员为空的房间同样不占行。

    **有长度上限**：房间多的时候会把提醒顶成一整屏，所以返回的行数受
    `MAX_ROSTER_LINES` 限制——它是**总行数**上限（含 `【本班配制】` 标题与省略提示行）。
    超出时截断，并在最后一行注明**还有多少间未显示**（`……还有 N 间未显示`）：
    默默砍掉会让用户以为这就是全部，那比不显示更糟。

    Args:
        roster: `build_roster` 的产物；从未导入过时为 None。
        plan_index: 班次下标，**从 1 开始**（与 `build_roster` 写入的一致）。

    Returns:
        可直接拼进 ``notify.render_reminder(extra=...)`` 的行列表，**长度不超过
        `MAX_ROSTER_LINES`**。未导入、数据形状不对、下标越界、或该班没有任何可显示
        的房间时返回**空列表**——调用方据此保持「没导入过排班表就与从前完全一样」的行为。
    """
    if not isinstance(roster, Mapping):
        return []

    shifts = roster.get("shifts")
    if not isinstance(shifts, Sequence) or isinstance(shifts, (str, bytes)):
        return []
    if not 1 <= plan_index <= len(shifts):
        return []

    shift = shifts[plan_index - 1]
    if not isinstance(shift, Mapping):
        return []

    rooms = shift.get("rooms")
    if not isinstance(rooms, Sequence) or isinstance(rooms, (str, bytes)):
        return []

    body: list[str] = []
    for room in rooms:
        if not isinstance(room, Mapping):
            continue
        if room.get("skipped"):
            # 「这一班这间房不动」——渲染出去会让用户去改一间不该改的房。
            continue

        names = room.get("operators")
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            continue
        clean = [name for name in names if isinstance(name, str) and name.strip()]
        if not clean:
            continue

        room_key = str(room.get("room", ""))
        label = ROOM_LABELS.get(room_key, room_key)
        index = room.get("index")
        where = f"{label}{index}" if isinstance(index, int) else label
        body.append(f"{where}：{'、'.join(clean)}")

    if not body:
        return []

    header = "【本班配制】"
    # 上限是**总行数**：先扣掉标题占的那一行，剩下的才是房间行能用的额度。
    body_budget = MAX_ROSTER_LINES - 1
    if len(body) <= body_budget:
        return [header, *body]

    # 超限时还得给省略提示留一行，否则「截断后总行数不超过上限」这条就破了。
    # `max(0, ...)` 是为了让常量被改得极小时函数依然自洽（不出现负数切片）。
    shown = max(0, body_budget - 1)
    omitted = len(body) - shown
    return [header, *body[:shown], f"……还有 {omitted} 间未显示"]
