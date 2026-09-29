"""WebUI 只读状态数据的组装（纯逻辑）。

页面只是**另一种入口**：这里不重新实现任何班次计算，只把模块已有的
:class:`~.strategy.Snapshot`、绑定状态与发送记录翻译成可 JSON 序列化的 dict。
判定逻辑永远只有一处（``schedule`` / ``strategy``），否则两处必然漂移。

时间一律由调用方传入——纯逻辑里不出现 ``datetime.now()``，否则测不了。

纯逻辑模块：**禁止 import astrbot**（由 ruff.toml 的 TID 禁入规则强制）。
"""

import base64
import binascii
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from .roster import ROOM_LABELS, suggested_durations, suggested_hours
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


class AvatarLookupLike(Protocol):
    """头像查表的最小能力（``avatars.AvatarIndex`` 结构上直接匹配）。

    与 :class:`SendRecordLike` 同理：只声明用得到的那一个方法，webapi 不必 import
    具体实现，测试里传个假对象即可。声明的就是页面真正要的那件事——**给一批干员名
    换一批 URL**，只保留查得到的；标量查询（``url_for``）是索引自己的内部细节。
    """

    def url_map(self, names: Iterable[str]) -> dict[str, str]:
        """给一批干员名建立 ``名字 → URL`` 表，只含有映射的那些。"""
        ...


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


def current_shift_slot(
    current_name: str | None,
    shift_order: Sequence[str] | None,
    shifts: Sequence[Mapping[str, Any]],
) -> int:
    """算出页面应**默认选中**的班次下标（0 起）——通常是「当前正在进行的那一班」。

    为什么需要映射而不是直接比名字：排班表里的班次名来自**文件**（``Shift 1 · 12h``），
    而「当前是哪一班」来自**用户配置**（``早班``）——两个来源、名字对不上。中间那层
    对应关系项目里已经有了，就是 ``shift_order``（配置顺序）→ ``plan_index``
    （``_shift_order.index(name) + 1``，见 ``module.py`` 的 _roster_extra）。
    **这里复用同一套，不另造一份**：这个项目已经因为班次顺序错位栽过两次。

    对不上时**老实退回 0**（选第一个班），不报错、不猜：排班表换了、配置改了、
    名字被改过都会走到这里，而「显示第一个班」永远是个安全答案。

    Args:
        current_name: 当前班次的名称（来自配置与时刻计算）。
        shift_order: 配置里 ``shift_1/2/3`` 的名称顺序。
        shifts: :func:`roster_view` 产出的班次序列（每项含 ``plan_index``）。

    Returns:
        应选中的下标；无法确定时为 0。
    """
    if not current_name or not shift_order:
        return 0
    try:
        plan_index = list(shift_order).index(current_name) + 1
    except ValueError:
        return 0
    for position, shift in enumerate(shifts):
        if shift.get("plan_index") == plan_index:
            return position
    return 0


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


def _avatar_block(avatars: AvatarLookupLike | None, names: Sequence[str]) -> dict[str, Any]:
    """组装页面用的头像查表块。

    **只发名单里用得到的名字**（不是把 400 多条映射整份发过去），而且发的是**完整
    URL**——模板留在服务端，前端不做字符串拼接，图源就只有一个定义处。

    ``available`` 表示「映射表有没有装载成功」，``matched`` 表示「这一份排班表里有
    几个名字真的查到了」。两个数分开报：都为零时，用户至少能看出是映射没装还是
    这一班恰好都是没有头像的干员。
    """
    if avatars is None:
        return {"available": False, "matched": 0, "by_name": {}}
    by_name = avatars.url_map(names)
    return {"available": True, "matched": len(by_name), "by_name": by_name}


def roster_view(
    roster: Mapping[str, Any] | None,
    *,
    current_shift: str | None = None,
    shift_order: Sequence[str] | None = None,
    avatars: AvatarLookupLike | None = None,
) -> dict[str, Any]:
    """把落盘的排班表转成页面展示结构（纯逻辑，不读文件、不调时间）。

    未导入、形状不对、或结构不完整时返回 ``{"imported": False}``——页面据此显示
    上传引导，而不是空白。**绝不为了「看起来有数据」而编造结构**。

    Args:
        roster: ``JsonStateStore`` 里 ``imported_roster`` 键的值，或 None。
        current_shift: 当前班次的名字（来自配置与时刻计算）；用于算出页面默认
            应该选中哪一班。对不上时退回第一班，见 :func:`current_shift_slot`。
        shift_order: 配置里 ``shift_1/2/3`` 的名称顺序——**必须传配置的原始顺序**，
            不是 ``ShiftTable.shifts``（后者按开始时刻排过，夜班 02:00 会跑到最前）。
        avatars: 头像查表；None 表示映射不可用，页面走中文首字色块回退。

    Returns:
        可 JSON 序列化的 dict。已导入时含 ``source`` / ``imported_at`` /
        ``shift_count`` / ``skipped_total`` 与逐班逐房的明细；另有
        ``duration_hints_minutes``——从班次名字里读出的**时长建议**（分钟），
        读不出时为 ``None``。页面据此决定要不要显示「按排班表填入」按钮：
        **这不是配置，点了才生效**（裁决见 `docs/implementation/implementation.md` §2.6）。
    """
    if not isinstance(roster, Mapping):
        return {
            "imported": False,
            "current_slot": 0,
            "avatars": _avatar_block(avatars, ()),
        }

    shifts_raw = roster.get("shifts")
    if not isinstance(shifts_raw, Sequence) or isinstance(shifts_raw, (str, bytes)):
        return {
            "imported": False,
            "current_slot": 0,
            "avatars": _avatar_block(avatars, ()),
        }

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

    # 头像只查这一份排班表里出现过的名字，**并且去重**：同一名干员常常三个班都上，
    # 不去重就会把同一个查询做三遍（payload 不变，白做的事也是浪费）。
    # 用 dict.fromkeys 保序去重，输出顺序因此是稳定的。
    used_names = list(
        dict.fromkeys(
            name for shift in shifts for room in shift["rooms"] for name in room["operators"]
        )
    )

    return {
        "imported": True,
        "source": str(roster.get("source", "")),
        "imported_at": str(roster.get("imported_at", "")),
        "shift_count": len(shifts),
        "skipped_total": skipped_total,
        # 页面默认选中哪一班：算不出就退回 0（见 current_shift_slot）。
        "current_slot": current_shift_slot(current_shift, shift_order, shifts),
        "avatars": _avatar_block(avatars, used_names),
        # 顶部汇总用：让用户一眼看出「这套布局有多大」。
        "total_rooms": total_rooms,
        "total_operators": total_operators,
        # 时长建议：读出来才有（读回来时已重新校验过一次），页面据此显示「填入」按钮。
        "duration_hints_minutes": list(hinted) if (hinted := suggested_durations(roster)) else None,
        # 可以直接填进配置的小时数；含非整点小时时为 None（配置只支持整点小时），
        # 页面据此改提示而不给按钮——避免「点了填入、保存才报错」。
        "duration_suggestion_hours": (list(hours) if (hours := suggested_hours(roster)) else None),
        "shifts": shifts,
    }


# --- 页面上传的载荷解码 ------------------------------------------------------
#
# 为什么不是 multipart：AstrBot 的插件页面 bridge 用 `postMessage` 与父页面通信，而
# `FormData` **不能被结构化克隆**——真实浏览器里直接抛
# 「FormData object could not be cloned.」，文件根本递不到后端。所以前端把文件读成
# base64、走普通 JSON POST，解码与校验放在这里（纯逻辑，可测）。

#: base64 用 4 个字符编码 3 字节。
_B64_CHARS_PER_GROUP = 4
_B64_BYTES_PER_GROUP = 3

#: 按长度估出的上界与真实解码字节数最多相差这么多——base64 的填充字符不携带数据，
#: 只会让真实值更小。用它给「解码前的廉价拒绝」留出余量，避免把正好等于上限的文件误杀。
_B64_MAX_PADDING = 2

#: `FileReader.readAsDataURL` 产出的字符串带这个前缀。
_DATA_URL_MARKER = ";base64,"


class UploadPayloadError(ValueError):
    """页面提交的上传内容不可用。

    Attributes:
        status_code: 建议回给页面的 HTTP 状态码——参数问题用 400，体积超限用 413。
            由异常自己带状态码，是为了让「哪一类失败对应哪个码」只有一处定义。
    """

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def estimate_decoded_bytes(content_b64: str) -> int:
    """按 base64 文本长度估**解码后字节数的上界**（不解码）。

    每 :data:`_B64_CHARS_PER_GROUP` 个字符编码 :data:`_B64_BYTES_PER_GROUP` 字节，
    所以长度的 3/4 就是上界；填充字符不携带数据，真实值只会更小。

    它的用途是**解码前的廉价拒绝**：先按长度把明显超限的字符串挡掉，免得为了量体积
    反而把一个超大字符串解码进内存。

    Args:
        content_b64: base64 文本（可含空白；空白会占长度，但那只让上界更保守）。

    Returns:
        解码后字节数的上界。
    """
    return (len(content_b64) // _B64_CHARS_PER_GROUP) * _B64_BYTES_PER_GROUP


def _strip_data_url_prefix(text: str) -> str:
    """去掉可选的 ``data:...;base64,`` 前缀。

    自己的前端会先切掉它，但换个写法（或有人手搓请求）就可能带上来——容错这一下的
    代价是两行，收益是一整类「内容明明没问题却上传失败」。
    """
    if text.startswith("data:") and _DATA_URL_MARKER in text:
        return text.split(_DATA_URL_MARKER, 1)[1]
    return text


def _too_large_message(max_bytes: int) -> str:
    return f"文件太大：上限 {max_bytes // 1024} KB，排班表通常只有几十 KB。"


def decode_schedule_upload(content_b64: Any, *, max_bytes: int) -> bytes:
    """校验并解码页面提交的 base64 文件内容。

    Args:
        content_b64: 请求体里的 base64 文本（可带 data URL 前缀、可含空白）。
        max_bytes: **解码后**允许的最大字节数。

    Returns:
        解码出的原始字节。

    Raises:
        UploadPayloadError: 内容缺失、为空、不是合法 base64，或超过上限。
            超限带 ``status_code=413``，其余为 400。
    """
    if not isinstance(content_b64, str):
        raise UploadPayloadError("请求体里缺少 content_b64（应该是 base64 文本）。")

    text = _strip_data_url_prefix(content_b64.strip())
    # 容忍内部空白（有些工具导出的 base64 会折行）；其余非法字符交给 `validate=True` 挡。
    text = "".join(text.split())
    if not text:
        raise UploadPayloadError("文件是空的。")

    # 解码前先按长度估上界。留 `_B64_MAX_PADDING` 个字节的余量是必须的：上界比真实值
    # 最多大这么多，卡死在 max_bytes 会把「正好等于上限」的文件误杀。
    if estimate_decoded_bytes(text) > max_bytes + _B64_MAX_PADDING:
        raise UploadPayloadError(_too_large_message(max_bytes), status_code=413)

    # 补齐缺失的 `=`：base64 要求长度是 4 的倍数，而有些导出工具会省掉填充。
    # 补上不改变解码结果（`=` 不携带数据），只是少一类「明明内容没问题却格式不对」。
    padded = text + "=" * (-len(text) % _B64_CHARS_PER_GROUP)

    try:
        data = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadPayloadError("文件内容不是有效的 base64，请重新导出后再试。") from exc

    # 上界只是估算，真实大小以解码结果为准——**上限必须按解码后的字节数算**。
    if len(data) > max_bytes:
        raise UploadPayloadError(_too_large_message(max_bytes), status_code=413)
    if not data:
        raise UploadPayloadError("文件是空的。")
    return data
