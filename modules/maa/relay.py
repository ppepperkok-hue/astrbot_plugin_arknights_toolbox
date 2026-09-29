"""给用户看的文案（**纯逻辑**）。

单独成文件是因为这里有一条**容易被后人改坏的约束**：

> 官方《远程控制协议》原文：`status` 是 `SUCCESS` / `FAILED`，
> 且「**一般不论任务执行成功与否只会返回 SUCCESS**，只有特殊情况才会返回 FAILED」。

⇒ `SUCCESS` **不是「换班做对了」的凭证**，它更接近「这条指令我处理完了」。
本文件里**任何** `SUCCESS` 的转述都不得出现「成功」二字——`tests/test_maa_relay.py`
用子串断言钉住这一点，防的是后人写着写着把它改回「换班成功」。这不是洁癖：
用户据此以为班换好了、结果没换，是这类功能里最坏的一种错。

同理，本模块**任何文案都不许承诺「帮你切到第 N 班」**：协议里没有班次字段
（`10-maa-shift-switching.md` §4.1），我们只能「让它跑一次」，跑完前进到哪一班
是 MAA 按它自己的规则定的。
"""

from __future__ import annotations

from datetime import datetime

from .queue import EnqueueResult, PendingTask, SweepNotice, SweepReason
from .tasks import task_type_label

__all__ = [
    "HELP",
    "already_done_text",
    "already_pending_text",
    "cancelled_text",
    "inline_hint",
    "no_pending_text",
    "queued_text",
    "report_text",
    "skipped_text",
    "timeout_text",
    "unknown_report_text",
]

HELP = (
    "MAA 远程控制用法：\n"
    "/ak maa —— 看状态（连上没有、队列里有什么、最近回报）\n"
    "/ak maa run —— 让它跑一次：排一个任务，等 MAA 下次来取\n"
    "/ak maa skip —— 这一班我自己换，不给 MAA 派任务\n"
    "/ak maa cancel —— 撤掉还没被取走的任务"
)

#: 首次接触时的一句话提示。它是**说明白边界**的地方：我们只能让它跑。
inline_hint = "（我只负责让它跑一次；跑完前进到哪一班由 MAA 自己按排班决定。）"


def _short(task_id: str) -> str:
    """任务 id 只显示前 8 位——够对上号，又不至于刷满一屏。"""
    return task_id[:8] if task_id else "（无）"


def _stamp(moment: datetime | None) -> str:
    return moment.strftime("%m-%d %H:%M:%S") if moment else "未知"


def queued_text(result: EnqueueResult) -> str:
    """`/ak maa run` 成功排队后的回执。

    ⚠️ 这里必须**提前**说清「约一秒就取走、之后只能叫停」——现场实测任务从排队
    到被领走**只隔 3 毫秒**，用户以为"先派上，不对再撤"，而撤销的窗口几乎不存在。
    把代价放在确认之后才说，等于用文案掩盖（`cancel` 的措辞同理）。
    """
    task = result.task or {}
    lines = [
        "已排队：{}".format(task_type_label(str(task.get("type", "")))),
        f"任务 id：{_short(result.task_id)}",
        "MAA 下次轮询（默认 1 秒一次）就会取走它，取走后就开始跑。",
        "",
        "⚠️ **它会在约一秒内被取走，之后就没法「撤」了**——协议只提供一条"
        "「尝试结束当前任务」的指令，能不能拦住要看它跑到哪一步。"
        "所以**想反悔要趁现在**（`/ak maa cancel`），别等它跑起来。",
        "",
        "⚠️ 它跑完之后，班次会按 MAA 自己的规则前进一格。所以"
        "**一次换班只派一次**——多派一次就多跳一班，而且会写进它的配置、不会自动纠正。",
        inline_hint,
    ]
    return "\n".join(lines)


def already_pending_text(result: EnqueueResult) -> str:
    """重复确认，但已经有一个未结任务——**没有产生第二个**。"""
    pending: PendingTask | None = result.pending
    label = pending.slot_label if pending else "（未知）"
    created = _stamp(pending.created_at if pending else None)
    fetched = pending.first_fetch_at if pending else None
    lines = [
        f"队列里已经有一个任务了（{label}，创建于 {created}），**没有再派第二个**。",
        f"任务 id：{_short(result.task_id)}"
        + (f"，已被取走 {pending.fetch_count} 次" if pending and fetched else "，还没被取走"),
        "",
        "一个时刻只允许一个任务：多派一次，班次就会多前进一班。",
    ]
    if fetched:
        lines.append(
            "⚠️ 它**已经被 MAA 取走**了，所以 `/ak maa cancel` 只能给它发一条"
            "「尝试结束」的指令，**不一定拦得住**。"
        )
    else:
        lines.append("它还没被取走，现在用 /ak maa cancel 还来得及拦住。")
    return "\n".join(lines)


def already_done_text(result: EnqueueResult) -> str:
    """这一次换班已经派过并结清了——**不重复派**。"""
    return "\n".join(
        [
            f"这一次换班已经派过任务并结清了（{_stamp(result.done_at)}），**不再重复派**。",
            "",
            "同一个任务 id 不会被 MAA 执行两次，所以「重试」是安全的；"
            "但「再派一次」不是——那是两次执行。",
            "如果你确实想再让它跑一趟，稍后再发一次 /ak maa run。",
        ]
    )


def skipped_text(slot_label: str, *, stop_sent: bool = False) -> str:
    """`/ak maa skip`：记下"我自己换"。

    ⚠️ 这里原来写的是「这样 MAA 内部的班次索引**不会前进**」——**那句话在任务已被
    取走时是假的**：`skip` 内部也走 `cancel`，而取走过的任务拦不住（只能叫停）。
    现在按是否真的拦住了分成两种说法。**不许把保证写成无条件的。**
    """
    who = f"（{slot_label}）" if slot_label else ""
    head = f"好，这一班{who}你自己换，不给 MAA 派任务。"
    if stop_sent:
        return "\n".join(
            [
                head,
                "",
                "⚠️ 不过那一个任务**已经被 MAA 取走**了，所以「不派」并不能让它停下——"
                "我另外给它发了「尝试结束当前任务」的指令，**不保证拦得住**。",
                "",
                "如果它还是跑完了，**班次索引仍然会前进一格**，那就和你的手动操作错位了。"
                "到时候以 MAA 那边的实际状态为准。",
            ]
        )
    return "\n".join(
        [
            head,
            "",
            "它还没被取走，所以确实不会被跑掉——MAA 内部的班次索引**不会前进**，"
            "与你的手动操作不会错位。",
            "需要它跑的时候再发 /ak maa run。",
        ]
    )


def cancelled_text(task: PendingTask | None, *, stop_sent: bool = False) -> str:
    """撤销的结果。

    `stop_sent` 表示「这条任务已被 MAA 取走，所以我们给它下发了一条 `StopTask`」。
    两种情况必须分开说，因为**用户能做的下一步不同**：没取走的只是不再下发；
    取走过的要等 MAA 那边真的停下来，而官方说 `StopTask` 只是「**尝试**结束」，
    所以这里**不许**出现「已取消/已停止」这种把结果说死的措辞。
    """
    if task is None:
        return no_pending_text()
    head = f"已把这个任务从队列里撤掉（{task.slot_label}，id {_short(task.task_id)}）。"
    if not stop_sent:
        return "\n".join(
            [
                head,
                "",
                "它还没被 MAA 取走，所以不会再被下发。",
            ]
        )
    return "\n".join(
        [
            head,
            "",
            "⚠️ 它**已经被 MAA 取走了**，所以光撤队列拦不住它——我另外给它下发了一条"
            "「结束当前任务」指令（StopTask）。",
            "",
            "官方原文说这条指令只是「**尝试**结束当前运行的任务」，而且**不会等待确认**，"
            "所以我现在只能说「已发送」，**不能说已经停了**。"
            "要确认它真的停下，看 MAA 那边还在不在跑。",
        ]
    )


def stopped_text(*, confirmed: bool, slot_label: str = "") -> str:
    """心跳确认之后的结论。`confirmed=False` 表示仍看到它没停。"""
    who = f"（{slot_label}）" if slot_label else ""
    if confirmed:
        return "\n".join(
            [
                f"确认过了：MAA 那边{who}已经没有在跑的任务，停止指令生效了。",
                "",
                "注意「停了」不等于「班次没前进」——已经跑掉的部分是收不回来的。",
            ]
        )
    return "\n".join(
        [
            f"仍未确认停掉{who}：心跳回报显示它还在执行。",
            "",
            "可能是停止指令还没轮到它，也可能是这一趟已经接近收尾。"
            "可以稍后再发一次 /ak maa cancel。",
        ]
    )


def no_pending_text() -> str:
    return "队列里没有待取的任务。想派一个就用 /ak maa run。"


def report_text(*, status: str, slot_label: str, payload_shape: str = "") -> str:
    """把 `reportStatus` 的结果转述给用户。**措辞必须诚实**（见模块 docstring）。"""
    who = f"（{slot_label}）" if slot_label else ""
    if status == "SUCCESS":
        # ⚠️ 这段里**不许出现「成功」二字**（被测试钉住）。改之前先读模块 docstring。
        lines = [
            f"MAA 回报{who}：已执行（SUCCESS）",
            "",
            "它把这条任务处理完了——但**这不能当作「班换对了」的凭证**。",
            "官方原文：它一般不论任务做成没做成、只会报 SUCCESS，"
            "只有特殊情况才报 FAILED。真实结果要看 MAA 那边。",
        ]
    elif status == "FAILED":
        lines = [
            f"MAA 回报{who}：它自己报的是 FAILED，也就是这一次它认为没做成。",
            "",
            "FAILED 不常见（官方说只有特殊情况才会报它），值得去 MAA 那边看一眼。",
        ]
    else:
        lines = [
            f"MAA 回报{who}，但没有给出 status 字段。",
            "",
            "我们照实说：不知道它做成没做成。",
        ]
    if payload_shape:
        lines += ["", f"回报数据形状：{payload_shape}"]
    return "\n".join(lines)


def timeout_text(notice: SweepNotice) -> str:
    """任务作废时**必须**发出去的话（不许静默）。两种成因文案不同。"""
    if notice.reason is SweepReason.NEVER_FETCHED:
        return "\n".join(
            [
                f"【MAA 没来取任务】{notice.slot_label}",
                "",
                f"派出去 {notice.waited_minutes} 分钟，一直没有人来取——"
                "多半是那台电脑没开，或者 MAA 没在运行。",
                "班次没有前进（它压根没跑）。开机之后如果还想让它跑，再发一次 /ak maa run。",
            ]
        )
    return "\n".join(
        [
            f"【MAA 取走了任务却没回报】{notice.slot_label}",
            "",
            f"距离它取走已经 {notice.waited_minutes} 分钟。它**可能已经跑了**，只是一直没回话。",
            "为安全起见这一次不再补派：同一个任务 id 不会被 MAA 执行两次，"
            "但「再派一次」就是多跑一趟，班次会多跳一班。",
            "要确认结果，只能去 MAA 那边看。",
        ]
    )


def unknown_report_text(task_id: str) -> str:
    """收到了一个我们对不上号的回报。**如实说对不上**，不假装收到。"""
    return "\n".join(
        [
            f"收到一个回报，但任务 id（{_short(task_id)}）我们没派过。",
            "",
            "多半是插件重启过、队列丢了，而 MAA 那边照常跑完了。"
            "这不影响它——只是我们没法把它对上号。",
        ]
    )
