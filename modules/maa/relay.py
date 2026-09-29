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
    """`/ak maa run` 成功排队后的回执。"""
    task = result.task or {}
    lines = [
        "已排队：{}".format(task_type_label(str(task.get("type", "")))),
        f"任务 id：{_short(result.task_id)}",
        "MAA 下次轮询（默认 1 秒一次）就会取走它，取走后就开始跑。",
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
        "想撤掉它用 /ak maa cancel。",
    ]
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


def skipped_text(slot_label: str) -> str:
    """`/ak maa skip`：记下"我自己换"。"""
    who = f"（{slot_label}）" if slot_label else ""
    return "\n".join(
        [
            f"好，这一班{who}你自己换，不给 MAA 派任务。",
            "",
            "这样 MAA 内部的班次索引**不会前进**，与你的手动操作不会错位。",
            "需要它跑的时候再发 /ak maa run。",
        ]
    )


def cancelled_text(task: PendingTask | None) -> str:
    if task is None:
        return no_pending_text()
    return "\n".join(
        [
            f"已撤掉这个任务（{task.slot_label}，id {_short(task.task_id)}）。",
            "",
            "如果它已经被 MAA 取走并在跑了，撤掉不影响那一趟；这里只是不再重复下发。",
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
