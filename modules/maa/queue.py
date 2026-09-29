"""待下发任务的队列：**稳定 id、去重、过期**（纯逻辑）。

这是本模块唯一「记得住状态」的地方，也是整套安全性的所在。为什么这么说：

MAA 每跑完一次基建任务，会把**它内部的计划索引永久前进一格**，写回配置、
**不会自动纠正**（实测与官方文档双重确认，见
`docs/project-plan/10-maa-shift-switching.md` §4.3）。所以：

    触发次数 必须严格等于 换班次数

多下发一次，用户的排班就跳一班。于是本队列有两条硬不变量：

**不变量一：同一次换班 ⇒ 同一个任务 id。**
`task_id_for(slot.key)` 是纯函数、跨进程重启稳定。配合官方原文「对于相同的 id，
不会重复执行」，**重复确认、重复轮询、重试都不会变成「多跑一次」**。
这是本题唯一的「结构性安全」——它不依赖我们记得住什么。

**不变量二：同一时刻最多一个未结任务。**
`enqueue` 在已有未结任务时返回 `ALREADY_PENDING`，**不产生第二个**。
这条挡的是另一类错：上一个任务还没回报，用户又确认了一次，MAA 会在跑完第一趟
之后接着跑第二趟——那时两个任务的 id 不同，MAA 侧的去重救不了我们。

**过期（两条不同的窗口，因为成因不同）**：

- 派出去**从来没人取**（电脑没开）→ 超过 `task_ttl_minutes` 作废并**明确告知**。
  作废**不记进"已做过"**：这台电脑一会儿开机之后重来一次仍然是对的（同 id，MAA
  最多执行一次），而"静默留在队列里等三小时再突然跑一次"才是错的——那会让班次在
  一个与换班无关的时刻前进。
- 取走了**却一直没汇报** → 超过 `fetched_ttl_minutes` 作废，**并记进"已做过"**
  （它多半已经跑了，不能给第二次机会）。

**只放内存**（与模块既有的到达统计同一取舍）：插件重启会丢掉未结任务与它的通知
目标。**这不影响安全性**（id 由 slot 确定，MAA 侧对同 id 不会重复执行），只是会丢掉
一次通知——所以 `sweep` 的告知文本要把「可能已经跑过」说清楚，不能让用户以为没跑。
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .tasks import DEFAULT_TASK_TYPE, build_task

__all__ = [
    "AckOutcome",
    "EnqueueOutcome",
    "EnqueueResult",
    "PendingTask",
    "QueueSnapshot",
    "Slot",
    "SweepNotice",
    "SweepReason",
    "TaskQueue",
    "task_id_for",
]

#: 派生任务 id 的命名空间。**写死且不许改**：改掉它等于让「同一次换班」在升级后
#: 变成一个新 id，MAA 会把它当成新任务再跑一遍——那正好是我们要防的事。
_ID_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/ppepperkok-hue/astrbot_plugin_arknights_toolbox/maa",
)


def task_id_for(slot_key: str) -> str:
    """由「一次换班」的稳定标识派生任务 id。

    纯函数：同一 `slot_key` 在任何进程、任何时刻都得到同一个 id。
    **整个去重就靠这一条**——它意味着「重试」在协议层面自动变成「重复下发同一条」，
    而官方保证同 id 不会重复执行（见模块 docstring）。
    """
    return str(uuid.uuid5(_ID_NAMESPACE, slot_key))


@dataclass(frozen=True)
class Slot:
    """一次「该换班了」。

    Attributes:
        key: **稳定标识**，必须是纯函数可复现的（例如 `2026-09-29T09:00` 或
            `manual-20260929-1345`）。同一个 key 永远派同一个任务。
        label: 给人看的名字，例如「09-29 09:00 早班」。
    """

    key: str
    label: str


class EnqueueOutcome(StrEnum):
    """`enqueue` 的三种结局。用 StrEnum 而非裸字符串：拼错会当场 AttributeError，
    而不是悄悄落进某个 `else` 分支（本项目最怕的「静默匹配不上」）。"""

    CREATED = "created"
    """真的产生了一个任务。"""

    ALREADY_PENDING = "already_pending"
    """已有一个未结任务——**不产生第二个**（不变量二）。"""

    ALREADY_DONE = "already_done"
    """这一次换班已经派过并结清了——**不重复派**（不变量一在时间维度上的延伸）。"""


@dataclass(frozen=True)
class EnqueueResult:
    """`enqueue` 的完整结果，供调用方如实回话。"""

    outcome: EnqueueOutcome
    task_id: str
    task: dict[str, str] | None = None
    pending: PendingTask | None = None
    done_at: datetime | None = None


class AckOutcome(StrEnum):
    """`ack` 的三种结局。"""

    ACKED = "acked"
    """正常结清。"""

    UNKNOWN = "unknown"
    """收到的 id 我们从没派过（多半是插件重启过、队列丢了）。
    **如实报「不认识」，不假装收到**——那是本项目最忌讳的静默。"""

    ALREADY = "already"
    """这个 id 已经结清过了（MAA 重复汇报）。幂等，不算错。"""


class SweepReason(StrEnum):
    """任务为什么被作废。两种成因不同，文案也必须不同。"""

    NEVER_FETCHED = "never_fetched"
    """派出去了一直没人来取——电脑没开／MAA 没运行。"""

    NO_REPORT = "no_report"
    """取走了却一直没汇报——多半跑了但没回话。"""


@dataclass(frozen=True)
class SweepNotice:
    """一条「这次没成」的告知素材。**必须发给用户**，不许静默。"""

    reason: SweepReason
    task_id: str
    slot_label: str
    waited_minutes: int


@dataclass
class PendingTask:
    """一个已经派出、尚未结清的任务。"""

    task_id: str
    slot_key: str
    slot_label: str
    type: str
    created_at: datetime
    first_fetch_at: datetime | None = None
    fetch_count: int = 0

    def body(self) -> dict[str, str]:
        """下发给 MAA 的任务体。**只有 id 与 type**（见 `tasks.build_task`）。"""
        return {"id": self.task_id, "type": self.type}


@dataclass(frozen=True)
class QueueSnapshot:
    """给状态文案用的只读快照。"""

    pending_task_id: str = ""
    pending_slot_label: str = ""
    pending_type: str = ""
    created_at: datetime | None = None
    first_fetch_at: datetime | None = None
    fetch_count: int = 0
    done_count: int = 0
    created_total: int = 0
    acked_total: int = 0
    expired_total: int = 0

    @property
    def has_pending(self) -> bool:
        return bool(self.pending_task_id)


_Clock = Callable[[], datetime]


class TaskQueue:
    """至多一个未结任务的队列。**不是线程安全**：调用点都在同一个事件循环上。"""

    def __init__(
        self,
        *,
        task_ttl_minutes: int = 30,
        fetched_ttl_minutes: int = 240,
        done_memory: int = 64,
        clock: _Clock | None = None,
    ) -> None:
        if task_ttl_minutes <= 0:
            raise ValueError("任务 TTL 必须为正数")
        if fetched_ttl_minutes <= 0:
            raise ValueError("已取走任务的 TTL 必须为正数")
        if done_memory <= 0:
            raise ValueError("已做过记录的容量必须为正数")
        self._task_ttl = timedelta(minutes=task_ttl_minutes)
        self._fetched_ttl = timedelta(minutes=fetched_ttl_minutes)
        self._done_memory = done_memory
        self._clock: _Clock = clock or datetime.now

        self._pending: PendingTask | None = None
        #: 「这一次换班已经处理过」的 slot key，**有界**（防止无限增长）。
        self._done: set[str] = set()
        self._done_order: deque[str] = deque()
        self._done_at: dict[str, datetime] = {}
        #: 已经结清的**任务 id**，同样有界。它与 `_done` 是两套键：`_done` 按
        #: 「哪一次换班」（slot key）去重，这里按「哪一条任务」（task id）判重复汇报。
        #: 两者不能混用——task id 是 slot key 的 uuid5 摘要，**不可逆**，
        #: 拿它去 `_done` 里查永远查不到（这正是第一版写错的地方，被测试逮住）。
        self._acked_ids: set[str] = set()
        self._acked_order: deque[str] = deque()
        self._created_total = 0
        self._acked_total = 0
        self._expired_total = 0

    # --- 入队 ---------------------------------------------------------------

    def enqueue(
        self,
        slot: Slot,
        *,
        task_type: str = DEFAULT_TASK_TYPE,
        now: datetime | None = None,
    ) -> EnqueueResult:
        """把一次换班排进队列。**幂等且至多一个未结任务。**

        三种结局都如实返回，由调用方决定怎么跟用户说（文案在 `relay.py`）：
        重复确认既不能变成「多跑一次」，也不该被当成出错。

        Raises:
            TaskTypeError: 类型不在白名单（由 `tasks.build_task` 抛）。
        """
        moment = now or self._clock()
        task_id = task_id_for(slot.key)

        if self._pending is not None:
            return EnqueueResult(
                outcome=EnqueueOutcome.ALREADY_PENDING,
                task_id=self._pending.task_id,
                pending=self._pending,
            )
        if slot.key in self._done:
            return EnqueueResult(
                outcome=EnqueueOutcome.ALREADY_DONE,
                task_id=task_id,
                done_at=self._done_at.get(slot.key),
            )

        task = PendingTask(
            task_id=task_id,
            slot_key=slot.key,
            slot_label=slot.label,
            type=task_type,
            created_at=moment,
        )
        # 先让 build_task 校验类型：非法类型要在这里抛，不能等到下发那一刻
        # 才发现——那时用户已经确认过了。
        build_task(task_id, task_type)
        self._pending = task
        self._created_total += 1
        return EnqueueResult(outcome=EnqueueOutcome.CREATED, task_id=task_id, task=task.body())

    # --- 下发 ---------------------------------------------------------------

    @property
    def pending(self) -> PendingTask | None:
        """当前未结任务（只读，无副作用）。"""
        return self._pending

    def take_for_delivery(self, *, now: datetime | None = None) -> tuple[dict[str, str], ...]:
        """给 `getTask` 用的任务列表，并记一次「被取走」。

        **可以重复调用**：官方原文说端点应当「重复返回需要执行的任务」，
        且「对于相同的 id，不会重复执行」——所以这里不需要（也不该）只给一次。
        只给一次反而危险：MAA 拿到后如果本地丢了状态，就再也取不到，而我们会
        以为它跑了。
        """
        task = self._pending
        if task is None:
            return ()
        moment = now or self._clock()
        if task.first_fetch_at is None:
            task.first_fetch_at = moment
        task.fetch_count += 1
        return (task.body(),)

    # --- 结清 / 作废 ---------------------------------------------------------

    def ack(self, task_id: str, *, now: datetime | None = None) -> AckOutcome:
        """MAA 汇报了某条任务的结果 → 结清它。

        **不校验 status**：结清这件事与「跑成没跑成」无关（协议明说它通常不论成败都
        报 SUCCESS）。状态怎么转述是 `relay.py` 的事。
        """
        moment = now or self._clock()
        task = self._pending
        if task is not None and task.task_id == task_id:
            self._remember_done(task.slot_key, moment)
            self._remember_acked(task_id)
            self._pending = None
            self._acked_total += 1
            return AckOutcome.ACKED
        if task_id in self._acked_ids:
            return AckOutcome.ALREADY
        # 我们没派过这个 id：多半是插件重启把队列丢了，而 MAA 那次跑完了。
        # 如实报「不认识」——假装收到会把「跑过没跑过」这件事彻底弄混。
        return AckOutcome.UNKNOWN

    def cancel(self, *, now: datetime | None = None) -> PendingTask | None:
        """撤掉未结任务（用户改主意了：**不记进"已做过"**）。

        与 `sweep` 的「从未取走」同一取舍：这么做的意义就是让同一个 slot 还能重来，
        而同 id 保证重来也不会跑两次。
        """
        task = self._pending
        self._pending = None
        return task

    def retune(self, *, task_ttl_minutes: int, fetched_ttl_minutes: int) -> None:
        """配置改了之后换两个窗口，**保留未结任务**（`apply_config` 用）。

        为什么不干脆重建队列：重建会把未结任务丢掉，而 MAA 那边**可能已经取走了它**
        ——于是我们既不会再下发它（MAA 若丢了本地状态就再也取不回），也不会收到它的
        结清，用户看到的就是「派了但查不到」。只换窗口、留住任务，才是「让改动生效」
        又不破坏在途状态的做法。
        """
        if task_ttl_minutes <= 0 or fetched_ttl_minutes <= 0:
            raise ValueError("任务 TTL 必须为正数")
        self._task_ttl = timedelta(minutes=task_ttl_minutes)
        self._fetched_ttl = timedelta(minutes=fetched_ttl_minutes)

    def sweep(self, *, now: datetime | None = None) -> tuple[SweepNotice, ...]:
        """作废超时任务，返回要告知用户的内容。**调用方必须把这些发出去。**

        两种窗口不同（见模块 docstring），而且是否记进「已做过」也不同——
        这一点是刻意的，改动前请先读那段理由。
        """
        moment = now or self._clock()
        task = self._pending
        if task is None:
            return ()

        if task.first_fetch_at is None:
            waited = moment - task.created_at
            if waited <= self._task_ttl:
                return ()
            self._pending = None
            self._expired_total += 1
            # **不记 done**：电脑一会儿开机后重来一次仍然是对的（同 id）。
            return (
                SweepNotice(
                    reason=SweepReason.NEVER_FETCHED,
                    task_id=task.task_id,
                    slot_label=task.slot_label,
                    waited_minutes=int(waited.total_seconds() // 60),
                ),
            )

        waited = moment - task.first_fetch_at
        if waited <= self._fetched_ttl:
            return ()
        self._pending = None
        self._remember_done(task.slot_key, moment)
        # 任务 id 也记上：它已经跑过，之后**迟到的汇报**应当被判为「重复」而不是
        # 「对不上号」——后者会让用户以为我们漏了一条记录。
        self._remember_acked(task.task_id)
        self._expired_total += 1
        # 记 done：它多半已经跑过了，不能再给第二次机会。
        return (
            SweepNotice(
                reason=SweepReason.NO_REPORT,
                task_id=task.task_id,
                slot_label=task.slot_label,
                waited_minutes=int(waited.total_seconds() // 60),
            ),
        )

    # --- 快照 ---------------------------------------------------------------

    def snapshot(self) -> QueueSnapshot:
        task = self._pending
        if task is None:
            return QueueSnapshot(
                done_count=len(self._done),
                created_total=self._created_total,
                acked_total=self._acked_total,
                expired_total=self._expired_total,
            )
        return QueueSnapshot(
            pending_task_id=task.task_id,
            pending_slot_label=task.slot_label,
            pending_type=task.type,
            created_at=task.created_at,
            first_fetch_at=task.first_fetch_at,
            fetch_count=task.fetch_count,
            done_count=len(self._done),
            created_total=self._created_total,
            acked_total=self._acked_total,
            expired_total=self._expired_total,
        )

    def has_done(self, slot_key: str) -> bool:
        """这一次换班是不是已经处理过了（供调用方在入队前自查）。"""
        return slot_key in self._done

    # --- 内部 ---------------------------------------------------------------

    def _remember_done(self, slot_key: str, moment: datetime) -> None:
        """记下「这次换班处理过了」，容量有界（超出丢最早的）。"""
        if slot_key in self._done:
            self._done_at[slot_key] = moment
            return
        while len(self._done_order) >= self._done_memory:
            oldest = self._done_order.popleft()
            self._done.discard(oldest)
            self._done_at.pop(oldest, None)
        self._done_order.append(slot_key)
        self._done.add(slot_key)
        self._done_at[slot_key] = moment

    def _remember_acked(self, task_id: str) -> None:
        """记下「这条任务结清过了」，容量与 `_done` 同源。"""
        if task_id in self._acked_ids:
            return
        while len(self._acked_order) >= self._done_memory:
            self._acked_ids.discard(self._acked_order.popleft())
        self._acked_order.append(task_id)
        self._acked_ids.add(task_id)
