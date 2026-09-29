"""`modules/maa/queue.py` 的纯逻辑测试。

这里要钉住的是**两条硬不变量**，它们直接决定 MAA 会不会多跑一趟：

1. **同一次换班 ⇒ 同一个任务 id**（`task_id_for` 是纯函数）；
2. **同一时刻最多一个未结任务**（重复确认不产生第二个）。

再加上两种过期窗口的行为差异（「从来没人取」不记已做过、「取走了没回报」记已做过）
——那个差别是刻意的，理由写在 `queue.py` 的模块 docstring 里。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from modules.maa import queue as q
from modules.maa import tasks
from modules.maa.queue import EnqueueOutcome

T0 = datetime(2026, 9, 29, 8, 50, 0)
SLOT_A = q.Slot(key="2026-09-29T09:00", label="09-29 09:00 早班")
SLOT_B = q.Slot(key="2026-09-29T21:00", label="09-29 21:00 晚班")


def _queue(**kwargs) -> q.TaskQueue:
    return q.TaskQueue(clock=lambda: T0, **kwargs)


# --- 任务 id 的稳定性（不变量一的地基） --------------------------------------


def test_task_id_is_a_pure_function_of_the_slot_key() -> None:
    assert q.task_id_for("2026-09-29T09:00") == q.task_id_for("2026-09-29T09:00")


def test_different_slots_get_different_ids() -> None:
    assert q.task_id_for(SLOT_A.key) != q.task_id_for(SLOT_B.key)


def test_task_id_looks_like_a_uuid() -> None:
    """协议说 id 是字符串；用 UUID 是因为它天然唯一且看不出业务含义。"""
    value = q.task_id_for(SLOT_A.key)
    assert len(value) == 36
    assert value.count("-") == 4


# --- 入队与去重（不变量二） --------------------------------------------------


def test_enqueue_creates_one_task() -> None:
    queue = _queue()

    result = queue.enqueue(SLOT_A, now=T0)

    assert result.outcome is q.EnqueueOutcome.CREATED
    assert result.task == {"id": q.task_id_for(SLOT_A.key), "type": tasks.LINK_START}
    assert queue.pending is not None


def test_enqueueing_the_same_slot_twice_creates_only_one_task() -> None:
    """用户连点两次确认 ⇒ 仍然只有一个任务。"""
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)

    second = queue.enqueue(SLOT_A, now=T0)

    assert second.outcome is q.EnqueueOutcome.ALREADY_PENDING
    assert second.task is None
    assert second.pending is not None


def test_a_different_slot_is_also_refused_while_one_is_outstanding() -> None:
    """**这条比"同 slot 去重"更强**：只要有一个未结任务，谁都不许再进来。

    挡的是「上一个还没回报，用户又确认了一次」——那时两个任务 id 不同，
    MAA 侧的同 id 去重救不了我们，它会跑完第一趟接着跑第二趟。
    """
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)

    result = queue.enqueue(SLOT_B, now=T0)

    assert result.outcome is q.EnqueueOutcome.ALREADY_PENDING
    assert queue.pending is not None
    assert queue.pending.slot_key == SLOT_A.key


def test_after_ack_the_same_slot_is_refused_for_good() -> None:
    """结清之后，同一次换班**不许再派**——那就是两次执行。"""
    queue = _queue()
    first = queue.enqueue(SLOT_A, now=T0)
    queue.ack(first.task_id, now=T0)

    again = queue.enqueue(SLOT_A, now=T0)

    assert again.outcome is q.EnqueueOutcome.ALREADY_DONE
    assert again.done_at is not None


def test_after_ack_a_new_slot_is_allowed() -> None:
    queue = _queue()
    first = queue.enqueue(SLOT_A, now=T0)
    queue.ack(first.task_id, now=T0)

    result = queue.enqueue(SLOT_B, now=T0 + timedelta(hours=12))

    assert result.outcome is q.EnqueueOutcome.CREATED


def test_enqueue_rejects_a_task_type_outside_the_whitelist() -> None:
    queue = _queue()

    with pytest.raises(tasks.TaskTypeError):
        queue.enqueue(SLOT_A, task_type="LinkStart-Combat", now=T0)

    assert queue.pending is None, "非法类型不该留下半个任务"


# --- 下发是幂等的（协议允许重复返回） ----------------------------------------


def test_polling_repeatedly_returns_the_same_task_and_id() -> None:
    """协议原文：端点应当「重复返回需要执行的任务」，同 id 不会被执行两次。

    所以**不能**做成「只给一次」——MAA 若丢了本地状态就再也取不到，
    而我们会以为它跑了。
    """
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)

    deliveries = [queue.take_for_delivery(now=T0) for _ in range(5)]

    assert all(len(batch) == 1 for batch in deliveries)
    ids = {batch[0]["id"] for batch in deliveries}
    assert len(ids) == 1
    assert queue.pending is not None
    assert queue.pending.fetch_count == 5


def test_delivery_is_empty_when_nothing_was_confirmed() -> None:
    queue = _queue()

    assert queue.take_for_delivery(now=T0) == ()


def test_first_delivery_stamps_time_and_later_ones_do_not_reset_it() -> None:
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)

    queue.take_for_delivery(now=T0 + timedelta(minutes=1))
    stamped = queue.pending.first_fetch_at
    queue.take_for_delivery(now=T0 + timedelta(minutes=9))

    assert stamped == T0 + timedelta(minutes=1)
    assert queue.pending.first_fetch_at == stamped


# --- 结清 -------------------------------------------------------------------


def test_ack_clears_the_task() -> None:
    queue = _queue()
    result = queue.enqueue(SLOT_A, now=T0)

    assert queue.ack(result.task_id, now=T0) is q.AckOutcome.ACKED
    assert queue.pending is None
    assert queue.take_for_delivery(now=T0) == ()


def test_ack_of_an_unknown_id_is_reported_as_unknown() -> None:
    """插件重启后队列会丢，而 MAA 那次照跑照汇报——**如实报不认识，不假装收到**。"""
    queue = _queue()

    assert queue.ack("some-other-id", now=T0) is q.AckOutcome.UNKNOWN


def test_ack_is_idempotent() -> None:
    queue = _queue()
    result = queue.enqueue(SLOT_A, now=T0)
    queue.ack(result.task_id, now=T0)

    assert queue.ack(result.task_id, now=T0) is q.AckOutcome.ALREADY


def test_ack_does_not_care_about_the_status() -> None:
    """结清与「跑成没跑成」无关——协议明说它通常不论成败都报 SUCCESS。"""
    queue = _queue()
    result = queue.enqueue(SLOT_A, now=T0)

    assert queue.ack(result.task_id, now=T0) is q.AckOutcome.ACKED


# --- 过期：两种窗口的差别是刻意的 --------------------------------------------


def test_a_task_nobody_fetched_expires_with_a_notice() -> None:
    queue = _queue(task_ttl_minutes=30)
    queue.enqueue(SLOT_A, now=T0)

    assert queue.sweep(now=T0 + timedelta(minutes=29)) == ()

    notices = queue.sweep(now=T0 + timedelta(minutes=31))

    assert len(notices) == 1
    assert notices[0].reason is q.SweepReason.NEVER_FETCHED
    assert notices[0].slot_label == SLOT_A.label
    assert notices[0].waited_minutes == 31
    assert queue.pending is None


def test_an_unfetched_expiry_can_be_retried_with_the_same_id() -> None:
    """电脑一会儿开机后重来一次**仍然是对的**——同 id，MAA 最多执行一次。

    所以这条路径**不**记进「已做过」。反过来说：如果这里记了，用户就永远
    没法补这一次换班了。
    """
    queue = _queue(task_ttl_minutes=30)
    first = queue.enqueue(SLOT_A, now=T0)
    queue.sweep(now=T0 + timedelta(minutes=31))

    again = queue.enqueue(SLOT_A, now=T0 + timedelta(minutes=32))

    assert again.outcome is q.EnqueueOutcome.CREATED
    assert again.task_id == first.task_id, "同一次换班必须还是同一个 id"


def test_a_fetched_task_that_never_reports_expires_and_is_marked_done() -> None:
    """它多半已经跑了：**不能再给第二次机会**，所以这一条记进「已做过」。"""
    queue = _queue(fetched_ttl_minutes=60)
    queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)

    notices = queue.sweep(now=T0 + timedelta(minutes=61))

    assert len(notices) == 1
    assert notices[0].reason is q.SweepReason.NO_REPORT
    assert queue.pending is None
    assert queue.enqueue(SLOT_A, now=T0 + timedelta(minutes=62)).outcome is (
        q.EnqueueOutcome.ALREADY_DONE
    )


def test_a_late_report_after_an_expiry_is_duplicate_not_unknown() -> None:
    """过期之后迟到的汇报要判成「重复」，不是「对不上号」。

    判成 unknown 会让用户以为我们漏了一条记录，而去查一个并不存在的问题。
    （这条断言钉住 `_acked_ids` 与 `_done` 是两套键——第一版把 task id 拿去
    slot key 的表里查，永远查不到，是测试把它逮住的。）
    """
    queue = _queue(fetched_ttl_minutes=60)
    result = queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)
    queue.sweep(now=T0 + timedelta(minutes=61))

    assert queue.ack(result.task_id, now=T0 + timedelta(minutes=62)) is q.AckOutcome.ALREADY


def test_a_fetched_task_is_not_swept_early() -> None:
    """跑一整套可能很久，窗口没到就不能动它。"""
    queue = _queue(fetched_ttl_minutes=240)
    queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)

    assert queue.sweep(now=T0 + timedelta(minutes=239)) == ()
    assert queue.pending is not None


def test_sweep_with_an_empty_queue_is_a_no_op() -> None:
    assert _queue().sweep(now=T0) == ()


# --- 撤销 -------------------------------------------------------------------


def test_cancel_drops_the_task_without_marking_the_slot_done() -> None:
    queue = _queue()
    result = queue.enqueue(SLOT_A, now=T0)

    cancelled = queue.cancel()

    assert cancelled is not None
    assert cancelled.task_id == result.task_id
    assert queue.pending is None
    assert queue.enqueue(SLOT_A, now=T0).outcome is q.EnqueueOutcome.CREATED


def test_cancel_with_nothing_pending_is_harmless() -> None:
    assert _queue().cancel() is None


# --- 配置热更新：换窗口但留住在途任务 ----------------------------------------


def test_retune_keeps_the_pending_task() -> None:
    """重建队列会把 MAA 可能已经取走的任务丢掉——那会让用户看到「派了但查不到」。"""
    queue = _queue(task_ttl_minutes=30)
    result = queue.enqueue(SLOT_A, now=T0)

    queue.retune(task_ttl_minutes=5, fetched_ttl_minutes=600)

    assert queue.pending is not None
    assert queue.pending.task_id == result.task_id
    # 新窗口立刻生效：5 分钟后就该作废
    notices = queue.sweep(now=T0 + timedelta(minutes=6))
    assert len(notices) == 1


def test_retune_rejects_a_nonpositive_window() -> None:
    with pytest.raises(ValueError):
        _queue().retune(task_ttl_minutes=0, fetched_ttl_minutes=10)


# --- 构造参数与快照 ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_ttl_minutes": 0},
        {"task_ttl_minutes": -1},
        {"fetched_ttl_minutes": 0},
        {"done_memory": 0},
    ],
)
def test_invalid_construction_is_rejected(kwargs) -> None:
    with pytest.raises(ValueError):
        q.TaskQueue(**kwargs)


def test_snapshot_reports_pending_and_counters() -> None:
    queue = _queue()
    assert queue.snapshot().has_pending is False

    result = queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)
    snap = queue.snapshot()

    assert snap.has_pending is True
    assert snap.pending_task_id == result.task_id
    assert snap.pending_slot_label == SLOT_A.label
    assert snap.fetch_count == 1
    assert snap.created_total == 1
    assert snap.acked_total == 0

    queue.ack(result.task_id, now=T0)
    after = queue.snapshot()

    assert after.has_pending is False
    assert after.acked_total == 1
    assert after.done_count == 1
    assert after.created_total == 1


def test_done_memory_is_bounded() -> None:
    """很久以前处理过的换班不该无限占内存——但它只影响「记住与否」，
    真正保命的是「同 slot ⇒ 同 id」（那条是纯函数，与内存无关）。"""
    queue = _queue(done_memory=3)
    keys = [f"slot-{index}" for index in range(5)]
    for key in keys:
        result = queue.enqueue(q.Slot(key=key, label=key), now=T0)
        queue.ack(result.task_id, now=T0)

    assert queue.snapshot().done_count == 3
    assert queue.has_done(keys[-1]) is True
    assert queue.has_done(keys[0]) is False


def test_expired_counter_is_exposed() -> None:
    queue = _queue(task_ttl_minutes=1)
    queue.enqueue(SLOT_A, now=T0)
    queue.sweep(now=T0 + timedelta(minutes=2))

    assert queue.snapshot().expired_total == 1


# --- 控制指令（StopTask）-------------------------------------------------------
#
# 起因是一个真 bug：用户撤销了一个任务，MAA 却依旧执行。根因是任务进队列后
# **3 毫秒**就被领走了（MAA 每秒轮询），而「撤销」只删了我们自己的记录，
# 没有把官方的「结束当前任务」指令送出去。
#
# 这几条钉住的是：控制指令能送出去，而且**绝不参与换班计数**。


def test_control_task_is_delivered_alongside_the_pending_task() -> None:
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)

    queue.enqueue_control("StopTask", now=T0)
    delivered = queue.take_for_delivery(now=T0)

    types = [t["type"] for t in delivered]
    assert "StopTask" in types
    assert "LinkStart" in types


def test_control_is_delivered_even_when_the_queue_is_empty() -> None:
    """撤掉之后队列是空的，但停止指令**必须还能送出去**——
    否则「撤销一个已被取走的任务」就永远发不出停止命令了。
    """
    queue = _queue()
    queue.enqueue(SLOT_A, now=T0)
    queue.take_for_delivery(now=T0)
    queue.cancel()

    queue.enqueue_control("StopTask", now=T0)
    delivered = queue.take_for_delivery(now=T0)

    assert [t["type"] for t in delivered] == ["StopTask"]


def test_control_does_not_count_as_a_shift_task() -> None:
    """控制指令**不是换班任务**：不许影响 created/acked/expired 任何计数。

    这条是本次改动的核心约束——`触发次数 = 换班次数`，多算一次班次就会多前进一班。
    """
    queue = _queue()
    before = queue.snapshot()

    queue.enqueue_control("StopTask", now=T0)
    queue.take_for_delivery(now=T0)
    after = queue.snapshot()

    assert after.created_total == before.created_total
    assert after.acked_total == before.acked_total
    assert after.expired_total == before.expired_total
    assert after.has_pending is before.has_pending


def test_control_does_not_block_a_new_shift_task() -> None:
    """在途的控制指令**不得**让新的换班任务被当成「已有一个未结任务」。"""
    queue = _queue()
    queue.enqueue_control("StopTask", now=T0)

    result = queue.enqueue(SLOT_A, now=T0)

    assert result.outcome is EnqueueOutcome.CREATED


def test_control_expires_so_it_does_not_haunt_every_poll() -> None:
    """指令有 TTL：否则「每轮都带一条 StopTask」会永远持续下去。"""
    queue = _queue()
    queue.enqueue_control("StopTask", ttl_minutes=10, now=T0)

    assert queue.pending_control_count() == 1
    queue.take_for_delivery(now=T0 + timedelta(minutes=11))

    assert queue.pending_control_count() == 0


def test_each_control_gets_a_fresh_id() -> None:
    """重复撤销要能真的再发一次：同 id 会被 MAA 忽略，所以不能复用。"""
    queue = _queue()
    first = queue.enqueue_control("StopTask", now=T0)
    second = queue.enqueue_control("StopTask", now=T0)

    assert first["id"] != second["id"]
