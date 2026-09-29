"""`modules/maa/relay.py` 的文案契约测试。

**这个文件存在的唯一理由**，是钉住一条容易被后人改坏的规则：

> 官方原文说 `status` 「一般不论任务做成没做成只会报 SUCCESS」。
> ⇒ 任何 `SUCCESS` 的转述都**不得**让用户以为「班换好了」。

所以下面有一条**遍历所有文案**的断言：**一个「成功」都不许出现**。
它不是文字洁癖——用户据此以为班换好了、结果没换，是这类功能里最坏的一种错。
同一段注释也从反面把「不许承诺帮你切到第 N 班」钉住了（协议里根本没有班次字段）。
"""

from __future__ import annotations

import re
from datetime import datetime

from modules.maa import relay, tasks
from modules.maa.queue import (
    AckOutcome,
    EnqueueOutcome,
    Slot,
    SweepNotice,
    SweepReason,
    TaskQueue,
)

T0 = datetime(2026, 9, 29, 8, 50, 0)
SLOT = Slot(key="2026-09-29T09:00", label="09-29 09:00 早班")


def _results() -> dict[str, object]:
    """把三种入队结局都真实地跑出来（而不是手工构造 dataclass，避免与实现漂移）。"""
    queue = TaskQueue(clock=lambda: T0)
    created = queue.enqueue(SLOT, now=T0)
    pending = queue.enqueue(SLOT, now=T0)
    queue.ack(created.task_id, now=T0)
    done = queue.enqueue(SLOT, now=T0)
    assert created.outcome is EnqueueOutcome.CREATED
    assert pending.outcome is EnqueueOutcome.ALREADY_PENDING
    assert done.outcome is EnqueueOutcome.ALREADY_DONE
    return {"created": created, "pending": pending, "done": done}


def _all_texts() -> dict[str, str]:
    results = _results()
    task = results["created"].task or {}
    return {
        "HELP": relay.HELP,
        "inline_hint": relay.inline_hint,
        "queued": relay.queued_text(results["created"]),
        "already_pending": relay.already_pending_text(results["pending"]),
        "already_done": relay.already_done_text(results["done"]),
        "skipped": relay.skipped_text(SLOT.label),
        "cancelled": relay.cancelled_text(None),
        "no_pending": relay.no_pending_text(),
        "report_success": relay.report_text(
            status="SUCCESS", slot_label=SLOT.label, payload_shape=""
        ),
        "report_failed": relay.report_text(status="FAILED", slot_label=SLOT.label),
        "report_unknown": relay.report_text(status="", slot_label=SLOT.label),
        "timeout_never_fetched": relay.timeout_text(
            SweepNotice(
                reason=SweepReason.NEVER_FETCHED,
                task_id="t" * 36,
                slot_label=SLOT.label,
                waited_minutes=31,
            )
        ),
        "timeout_no_report": relay.timeout_text(
            SweepNotice(
                reason=SweepReason.NO_REPORT,
                task_id="t" * 36,
                slot_label=SLOT.label,
                waited_minutes=300,
            )
        ),
        "unknown_report": relay.unknown_report_text("z" * 36),
        "_task_type": tasks.task_type_label(str(task.get("type", ""))),
    }


# --- 核心契约：不许出现「成功」 ---------------------------------------------


def test_no_relay_text_ever_contains_the_word_for_success() -> None:
    """**本文件最重要的一条。**

    `status=SUCCESS` 只表示「这条指令我处理完了」，不表示换班做对了。
    任何把它写成「换班成功」的文案都会让用户以为班换好了——那正是我们最怕的假话。
    """
    offenders = {name: text for name, text in _all_texts().items() if "成功" in text}

    assert offenders == {}, f"这些文案里出现了「成功」二字：{sorted(offenders)}"


def test_success_relay_says_executed_and_carries_the_caveat() -> None:
    text = relay.report_text(status="SUCCESS", slot_label=SLOT.label)

    assert "已执行" in text
    assert "SUCCESS" in text
    assert "凭证" in text, "必须说清它不是「班换对了」的凭证"


def test_success_relay_does_not_promise_a_shift_number() -> None:
    """协议给不了班次（`10-maa-shift-switching.md` §4.1）——文案不许暗示能。"""
    pattern = re.compile(r"第\s*\d+\s*班")
    offenders = {name: text for name, text in _all_texts().items() if pattern.search(text)}

    assert offenders == {}, f"这些文案里承诺了班次：{sorted(offenders)}"


def test_failed_relay_says_failed_plainly() -> None:
    text = relay.report_text(status="FAILED", slot_label=SLOT.label)

    assert "FAILED" in text
    assert "没做成" in text


def test_a_report_without_status_is_reported_as_unknown_not_guessed() -> None:
    """拿不准就说拿不准——**不猜**。"""
    text = relay.report_text(status="", slot_label=SLOT.label)

    assert "没有给出 status" in text or "不知道" in text


# --- 入队回执 ---------------------------------------------------------------


def test_queued_text_states_the_one_trigger_per_shift_rule() -> None:
    results = _results()

    text = relay.queued_text(results["created"])

    assert "已排队" in text
    assert "只派一次" in text or "一次换班只派一次" in text
    assert "前进一格" in text


def test_already_pending_text_says_no_second_task_was_created() -> None:
    results = _results()

    text = relay.already_pending_text(results["pending"])

    assert "没有再派第二个" in text


def test_already_done_text_explains_retry_is_safe_but_resend_is_not() -> None:
    results = _results()

    text = relay.already_done_text(results["done"])

    assert "不再重复派" in text
    assert "重试" in text


def test_skipped_text_says_the_index_will_not_advance() -> None:
    """用户自己换班时，MAA 的计划索引不动——这正是我们要说清的效果。"""
    text = relay.skipped_text(SLOT.label)

    assert "不会前进" in text
    assert SLOT.label in text


# --- 超时告知 ---------------------------------------------------------------


def test_never_fetched_notice_says_the_shift_did_not_advance() -> None:
    """电脑没开 ⇒ 它压根没跑 ⇒ 班次没动。这句话必须说出来，否则用户会以为换过了。"""
    notice = SweepNotice(
        reason=SweepReason.NEVER_FETCHED,
        task_id="t" * 36,
        slot_label=SLOT.label,
        waited_minutes=31,
    )

    text = relay.timeout_text(notice)

    assert "没来取" in text
    assert "31" in text
    assert "没有前进" in text


def test_no_report_notice_warns_it_may_already_have_run() -> None:
    notice = SweepNotice(
        reason=SweepReason.NO_REPORT,
        task_id="t" * 36,
        slot_label=SLOT.label,
        waited_minutes=300,
    )

    text = relay.timeout_text(notice)

    assert "可能已经跑了" in text
    assert "不再补派" in text


def test_unknown_report_text_says_it_cannot_be_matched() -> None:
    text = relay.unknown_report_text("z" * 36)

    assert "没派过" in text


# --- 撤销 -------------------------------------------------------------------


def test_cancelled_text_mentions_the_task() -> None:
    queue = TaskQueue(clock=lambda: T0)
    result = queue.enqueue(SLOT, now=T0)
    cancelled = queue.cancel()

    text = relay.cancelled_text(cancelled)

    assert result.task_id[:8] in text
    assert "撤掉" in text
    # 这一条说的是「没被取走」那种情况：只撤队列，不该出现停止指令的说法。
    assert "StopTask" not in text


def test_cancelled_text_says_stop_was_only_attempted_when_it_was_fetched() -> None:
    """已被取走的任务：必须说明「已发送停止指令」，而且**不许把结果说死**。

    官方原文是 StopTask「将会**尝试**结束当前运行的任务」，所以措辞里不能出现
    「已取消 / 已停止」这类断言——那是把「试过了」说成「成功了」。
    """
    queue = TaskQueue(clock=lambda: T0)
    queue.enqueue(SLOT, now=T0)
    queue.take_for_delivery(now=T0)  # MAA 取走了它
    cancelled = queue.cancel()

    text = relay.cancelled_text(cancelled, stop_sent=True)

    assert "StopTask" in text
    assert "尝试" in text
    assert "已取消" not in text
    assert "已停止" not in text


def test_cancelling_nothing_falls_back_to_the_empty_queue_text() -> None:
    assert relay.cancelled_text(None) == relay.no_pending_text()


# --- 顺带：确认没有把 _results 的断言写空 ------------------------------------


def test_the_result_fixtures_are_real() -> None:
    results = _results()

    assert results["created"].outcome is EnqueueOutcome.CREATED
    assert results["pending"].outcome is EnqueueOutcome.ALREADY_PENDING
    assert results["done"].outcome is EnqueueOutcome.ALREADY_DONE
    assert AckOutcome.ACKED is not None
