"""到点询问：判定逻辑（`modules/maa/autoask.py`）与装配层行为（`modules/maa/module.py`）。

这一包要证明的**最重要**一件事不是"它会问"，而是**它只会问、不会派**：

    tick 跑一百次，队列仍然是空的；只有 /ak maa run 才会产生任务。

因为"多问一次"只是打扰，而"多派一次"会让用户的班次永久跳一班
（`docs/project-plan/10-maa-shift-switching.md` §4.3）。所以下面有一条用例
直接**数 `enqueue` 被调了几次**——数的是最危险的那一步，不是文案。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.shift_share import (
    SharedShiftTable,
    dump_shared,
    shared_shift_path,
)
from core.shifts import format_hhmm, load_timezone, parse_shift_slots
from core.storage import write_json_atomic
from modules.maa import autoask
from modules.maa import module as maa_module

TZ_NAME = "Asia/Shanghai"


# --- 纯逻辑：due_boundary ---------------------------------------------------


class _FakeShift:
    def __init__(self, name: str, start_minute: int) -> None:
        self.name = name
        self.start_minute = start_minute


class _FakeTable:
    """只实现 `starting_at`——这正是 `BoundaryLookup` 声明的全部形状。

    用它当被测对象，等于证明 `autoask` **只依赖那一点能力**（而不是整个
    `ShiftTable`），所以纯逻辑层不必 import `core`。
    """

    def __init__(self, *shifts: _FakeShift) -> None:
        self._by_minute = {shift.start_minute: shift for shift in shifts}
        self.asked: list[int] = []

    def starting_at(self, minute_of_day: int) -> _FakeShift | None:
        self.asked.append(minute_of_day)
        return self._by_minute.get(minute_of_day)


def test_right_at_the_boundary_is_due_with_zero_minutes_ago() -> None:
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    due = autoask.due_boundary(table, datetime(2026, 9, 29, 9, 0, 30), grace_minutes=10)

    assert due is not None
    assert due.name == "第 1 班"
    assert due.minutes_ago == 0
    assert due.moment == datetime(2026, 9, 29, 9, 0)  # 秒归零


def test_a_boundary_inside_the_grace_window_is_still_due() -> None:
    """窗口的意义：容器刚好在换班那一刻重启，那一分钟没人跑巡检。

    只认"正好这一分钟"会让那次换班**永远**不被问——静默漏一次，用户不会知道。
    """
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    due = autoask.due_boundary(table, datetime(2026, 9, 29, 9, 7, 10), grace_minutes=10)

    assert due is not None
    assert due.minutes_ago == 7
    assert due.moment == datetime(2026, 9, 29, 9, 0)


def test_a_boundary_outside_the_grace_window_is_not_due() -> None:
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    assert autoask.due_boundary(table, datetime(2026, 9, 29, 9, 30), grace_minutes=10) is None


def test_no_boundary_at_all_is_not_due() -> None:
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    assert autoask.due_boundary(table, datetime(2026, 9, 29, 12, 0), grace_minutes=10) is None


def test_zero_grace_accepts_only_the_exact_minute() -> None:
    """宽限为 0 是**合法选择**（"我就要准点"），只是重启会漏问。"""
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    assert autoask.due_boundary(table, datetime(2026, 9, 29, 9, 0), grace_minutes=0) is not None
    assert autoask.due_boundary(table, datetime(2026, 9, 29, 9, 1), grace_minutes=0) is None


def test_negative_grace_is_rejected_instead_of_never_firing() -> None:
    """负窗口会让判据永远为假——"永远不触发"是最难察觉的坏法，当场报错。"""
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    with pytest.raises(ValueError, match="宽限"):
        autoask.due_boundary(table, datetime(2026, 9, 29, 9, 0), grace_minutes=-1)


def test_midnight_wrap_is_handled_by_modulo_and_timedelta() -> None:
    """00:05 往回看 10 分钟，落在**前一天**的 23:55——不能算成今天的 23:55。"""
    table = _FakeTable(_FakeShift("夜班", 23 * 60 + 55))

    due = autoask.due_boundary(table, datetime(2026, 9, 30, 0, 5), grace_minutes=10)

    assert due is not None
    assert due.minutes_ago == 10
    assert due.moment == datetime(2026, 9, 29, 23, 55)


def test_the_most_recent_boundary_wins_when_several_fall_in_the_window() -> None:
    """窗口内有两个边界时取**最近**那个——那才是"刚刚开始的这一班"。

    只有很短的班次配置才会出现这种情形，但取错就会报错班次名。
    """
    table = _FakeTable(_FakeShift("旧的", 9 * 60), _FakeShift("新的", 9 * 60 + 3))

    due = autoask.due_boundary(table, datetime(2026, 9, 29, 9, 5), grace_minutes=10)

    assert due is not None
    assert due.name == "新的"
    assert due.minutes_ago == 2


def test_timezone_information_is_preserved_on_the_moment() -> None:
    """`moment` 必须带上调用方给的时区：文案与去重键都要用它，丢了会跨时区出错。

    这里用一个**固定的 offset 时区**（标准库自带），而不是 `Asia/Shanghai`——
    后者依赖本机 tzdata，有些机器上取不到，那条用例就会变成"跳过"，而跳过等于
    没验。这个用例要验的是"时区信息会不会被丢掉"，与具体哪个时区无关。
    """
    fixed = timezone(timedelta(hours=8))
    table = _FakeTable(_FakeShift("第 1 班", 9 * 60))

    due = autoask.due_boundary(
        table, datetime(2026, 9, 29, 9, 0, 5, tzinfo=fixed), grace_minutes=10
    )

    assert due is not None
    assert due.moment.tzinfo is fixed


def test_the_lookup_starts_from_now_and_walks_backwards() -> None:
    """它**复用班次表的 `starting_at`**，不自己算边界——顺序也就能钉住。"""
    table = _FakeTable()

    autoask.due_boundary(table, datetime(2026, 9, 29, 9, 2), grace_minutes=3)

    assert table.asked == [9 * 60 + 2, 9 * 60 + 1, 9 * 60, 9 * 60 - 1]


# --- 纯逻辑：ask_key --------------------------------------------------------


def test_key_is_stable_for_the_same_boundary() -> None:
    """跨进程稳定是"重启后不重问"的前提，所以键只能由时刻与名字决定。"""
    moment = datetime(2026, 9, 29, 9, 0, 45)

    first = autoask.ask_key("第 1 班", moment)
    second = autoask.ask_key("第 1 班", moment.replace(second=0, microsecond=0))

    assert first == second == "第 1 班@2026-09-29T09:00"


def test_key_ignores_seconds_and_microseconds() -> None:
    """秒与微秒参与会让同一个边界算出两个键，于是"只问一次"失效、用户被问两遍。"""
    base = datetime(2026, 9, 29, 9, 0, 0)

    assert autoask.ask_key("第 1 班", base) == autoask.ask_key(
        "第 1 班", base.replace(second=59, microsecond=999999)
    )


def test_key_distinguishes_name_and_minute() -> None:
    moment = datetime(2026, 9, 29, 9, 0)

    assert autoask.ask_key("第 1 班", moment) != autoask.ask_key("第 2 班", moment)
    assert autoask.ask_key("第 1 班", moment) != autoask.ask_key(
        "第 1 班", moment + timedelta(minutes=1)
    )


# --- 装配层：到点询问 ------------------------------------------------------


def _now_in(name: str) -> datetime:
    """与模块用同一套时区回落逻辑，免得测试读到的"现在"与它算的不一样。"""
    tz = load_timezone(name)
    return datetime.now(tz) if tz is not None else datetime.now()


def _config_first_shift_starting_at(minute: int) -> dict[str, object]:
    """造一份**合法**的三班配置：第一班从指定分钟开始，三段首尾相接共 24h。"""
    first = minute % 1440
    second = (first + 12 * 60) % 1440
    third = (second + 6 * 60) % 1440
    return {
        "shift_1_name": "第 1 班",
        "shift_1_start": format_hhmm(first),
        "shift_1_hours": 12,
        "shift_2_name": "第 2 班",
        "shift_2_start": format_hhmm(second),
        "shift_2_hours": 6,
        "shift_3_name": "第 3 班",
        "shift_3_start": format_hhmm(third),
        "shift_3_hours": 6,
    }


def _write_shared(tmp_path: Path, config: dict[str, object], *, timezone: str = TZ_NAME) -> Path:
    """按真实格式写一份共享班次表（用写入方那套函数，不手搓 JSON）。"""
    path = shared_shift_path(tmp_path, maa_module.PLUGIN_NAME)
    write_json_atomic(
        path,
        dump_shared(
            parse_shift_slots(config), timezone=timezone, generated_at=datetime(2026, 9, 29, 1, 0)
        ),
    )
    return path


def _boundary_just_now(minutes_ago: int = 2) -> dict[str, object]:
    """造一份"某班次刚开始 N 分钟"的配置，于是 tick 立刻就该问。

    用真实时钟算起点，测试因此与墙上时间无关（窗口 10 分钟远大于用例执行的抖动）。
    """
    now = _now_in(TZ_NAME)
    return _config_first_shift_starting_at((now.hour * 60 + now.minute - minutes_ago) % 1440)


class _FakeJob:
    def __init__(self, name: str, job_id: str) -> None:
        self.name = name
        self.job_id = job_id


class _FakeCronManager:
    def __init__(self) -> None:
        self.jobs: list[_FakeJob] = []
        self.added: list[dict] = []
        self.deleted: list[str] = []
        self._next = 0

    async def add_basic_job(self, **kwargs):
        self._next += 1
        self.added.append(kwargs)
        job = _FakeJob(str(kwargs.get("name", "")), f"job-{self._next}")
        self.jobs.append(job)
        return job

    async def list_jobs(self):
        return list(self.jobs)

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]


def _boot(
    monkeypatch,
    *,
    config: dict | None = None,
    cron: object | None = None,
    send_ok: bool = True,
    logs: list[str] | None = None,
) -> tuple[maa_module.MaaModule, list[tuple]]:
    """把模块跑起来，返回 (实例, 已发送的消息)。"""
    if logs is not None:
        _collect_logs(monkeypatch, sink=logs)
    sent: list[tuple] = []

    async def send_message(umo, chain):
        sent.append((umo, chain))
        return send_ok

    ctx = SimpleNamespace(
        register_web_api=lambda *a, **k: None,
        send_message=send_message,
        cron_manager=cron,
    )
    instance = maa_module.MaaModule()
    asyncio.run(instance.initialize(ctx, config if config is not None else {}))
    return instance, sent


def _collect_logs(monkeypatch, *, sink: list[str]) -> list[str]:
    """把日志按 `%` 模板渲染后收集起来（照 test_maa_module.py 的做法）。"""

    def _render(args):
        if not args:
            return ""
        template = str(args[0])
        if len(args) > 1:
            try:
                return template % tuple(args[1:])
            except Exception:  # noqa: BLE001 - 格式化失败就退回拼接
                pass
        return " ".join(str(arg) for arg in args)

    for level in ("info", "warning", "error", "exception", "debug"):
        monkeypatch.setattr(
            maa_module.logger, level, lambda *a, _s=sink, **k: _s.append(_render(a))
        )
    return sink


def _event(message: str = "/ak maa"):
    return SimpleNamespace(
        unified_msg_origin="napcat2:FriendMessage:10001",
        is_private_chat=lambda: True,
        is_admin=lambda: True,
        message_str=message,
    )


def _texts(sent: list[tuple]) -> list[str]:
    return [chain.parts[0] for _umo, chain in sent if chain.parts]


# --- 会问 -------------------------------------------------------------------


def test_tick_asks_once_when_a_boundary_just_started(monkeypatch, tmp_path) -> None:
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))  # 设上通知目标

    asyncio.run(instance._auto_ask_tick())

    texts = _texts(sent)
    assert len([t for t in texts if "该换班了" in t]) == 1, texts


def test_the_same_boundary_is_never_asked_twice(monkeypatch, tmp_path) -> None:
    """tick 每分钟跑一次，同一班次在窗口内会被看见很多次——只能问一次。"""
    _write_shared(tmp_path, _boundary_just_now(1))
    instance, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    sent.clear()

    for _ in range(20):
        asyncio.run(instance._auto_ask_tick())

    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1


def test_the_dedup_survives_a_restart(monkeypatch, tmp_path) -> None:
    """**重启后不许重复问**：去重记录落盘，新实例读到它就不再问。

    这条是"必须落盘"的全部理由——换班时刻在几小时之后，而插件随时可能重启。
    """
    _write_shared(tmp_path, _boundary_just_now(2))
    first, sent = _boot(monkeypatch, send_ok=True)
    asyncio.run(first.handle_command("maa", _event("/ak maa")))
    asyncio.run(first._auto_ask_tick())
    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1

    # 新实例，同一个数据目录（= 重启）
    second, sent2 = _boot(monkeypatch, send_ok=True)
    asyncio.run(second._auto_ask_tick())

    assert [t for t in _texts(sent2) if "该换班了" in t] == []


def test_a_different_boundary_is_asked_after_the_first(monkeypatch, tmp_path) -> None:
    """去重是按**换班标识**来的，不是"问过一次就再也不问"。"""
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    asyncio.run(instance._auto_ask_tick())
    sent.clear()

    # 直接把上一次的键换掉，模拟"下一次换班"
    instance._asked = {autoask.ask_key("另一班", datetime(2020, 1, 1, 0, 0))}
    asyncio.run(instance._auto_ask_tick())

    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1


def test_ask_text_says_it_is_only_a_question_and_never_promises_a_shift(
    monkeypatch, tmp_path
) -> None:
    """文案纪律：说清"不回就是不跑"，且**不许**出现"帮你切到第 N 班"。"""
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))

    asyncio.run(instance._auto_ask_tick())

    text = next(t for t in _texts(sent) if "该换班了" in t)
    assert "不回就是" in text
    assert "/ak maa run" in text and "/ak maa skip" in text
    for forbidden in ("切到", "帮你换", "已经换好"):
        assert forbidden not in text, f"文案里出现了承诺：{forbidden}"


def test_the_ask_does_not_happen_when_the_user_turned_it_off(monkeypatch, tmp_path) -> None:
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch, config={"auto_ask": False})
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    sent.clear()

    asyncio.run(instance._auto_ask_tick())

    assert sent == []


def test_the_grace_window_is_honoured(monkeypatch, tmp_path) -> None:
    """窗口设成 1 分钟时，2 分钟前开始的换班就不该再问。"""
    _write_shared(tmp_path, _boundary_just_now(3))
    instance, sent = _boot(monkeypatch, config={"auto_ask_grace_minutes": 1})
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    sent.clear()

    asyncio.run(instance._auto_ask_tick())

    assert [t for t in _texts(sent) if "该换班了" in t] == []


def test_turning_auto_ask_on_takes_effect_without_a_reload(monkeypatch, tmp_path) -> None:
    """面板上从「关」改成「开」之后，**下一次 tick 就会问**——不用重载插件。

    这是 `apply_config` 存在的全部意义（见 `extension.md` 第三次修订说明）：
    "只保存不更新"会让用户看到面板显示新值、行为却还是旧的——每个环节单看都正常，
    是最难查的一类错。
    """
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch, config={"auto_ask": False})
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    asyncio.run(instance._auto_ask_tick())
    assert [t for t in _texts(sent) if "该换班了" in t] == [], "关着的时候不该问"

    asyncio.run(instance.apply_config({"auto_ask": True}))
    asyncio.run(instance._auto_ask_tick())

    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1


# --- 绝不派任务（本文件最重要的一条） --------------------------------------


def test_the_tick_does_not_touch_the_queue_at_all(monkeypatch, tmp_path) -> None:
    """**「多问一次」只是打扰，「多派一次」会让班次永久跳一班。**

    这条数的是最危险的那一步：tick 跑很多次，`enqueue` 一次都不许被调用；
    而 `/ak maa run` 调用一次。两者共用同一条派发路径（`_cmd_run`），
    自动询问只是一个通知。
    """
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))

    calls: list[str] = []
    original = instance._queue.enqueue

    def _spy(slot, **kwargs):
        calls.append(slot.key)
        return original(slot, **kwargs)

    instance._queue.enqueue = _spy  # type: ignore[method-assign]

    for _ in range(5):
        asyncio.run(instance._auto_ask_tick())

    assert calls == [], "到点询问不许产生任务"
    assert instance._queue.pending is None
    assert instance._queue.snapshot().created_total == 0

    asyncio.run(instance.handle_command("maa", _event("/ak maa run")))

    assert len(calls) == 1, "确认之后才该有且只有一个任务"


def test_manual_run_still_works_without_any_shift_table(monkeypatch, tmp_path) -> None:
    """读不到班次表**只影响到点询问**——手动触发必须照常可用。"""
    instance, sent = _boot(monkeypatch)  # 没有写任何共享文件
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))

    asyncio.run(instance.handle_command("maa", _event("/ak maa run")))

    assert instance._queue.pending is not None
    assert any("已排队" in t for t in _texts(sent))


# --- 读不到 / 读坏了：不许崩、不许静默 --------------------------------------


def test_a_missing_shared_table_is_reported_but_not_fatal(monkeypatch, tmp_path) -> None:
    """「对方模块没开」是**正常状态**：状态文案要说清，但模块照常可用。"""
    instance, _sent = _boot(monkeypatch)

    asyncio.run(instance._auto_ask_tick())

    assert instance.unavailable_reason is None, "读不到班次表不算不可用（手动还能干活）"
    status = instance._status_text()
    assert "还没读到" in status
    assert "到点询问：开" in status


def test_status_admits_it_when_the_auto_ask_job_is_not_registered(monkeypatch, tmp_path) -> None:
    """**「开关开着」不等于「真的会问」**——取不到 cron 时状态必须直说。

    少了这句，用户会看到「到点询问：开」然后等到天荒地老；而"看起来正常但什么都
    没发生"是本项目最高优先级的 bug。
    """
    instance, _sent = _boot(monkeypatch, cron=None)

    status = instance._status_text()

    assert "到点询问：开" in status
    assert "定时任务没注册上" in status
    assert "实际不会问" in status


def test_status_stays_quiet_about_the_job_when_it_is_registered(monkeypatch, tmp_path) -> None:
    """反过来：任务正常时不许出现那句警告（否则警告会被当成常态，等于没有）。"""
    instance, _sent = _boot(monkeypatch, cron=_FakeCronManager())

    assert "定时任务没注册上" not in instance._status_text()


def test_a_corrupt_shared_table_is_reported_with_the_reason(monkeypatch, tmp_path) -> None:
    """文件坏了是**异常**，理由要落到状态里，而不是笼统一句"没读到"。"""
    path = shared_shift_path(tmp_path, maa_module.PLUGIN_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")

    instance, _sent = _boot(monkeypatch)
    asyncio.run(instance._auto_ask_tick())

    assert instance.unavailable_reason is None
    status = instance._status_text()
    assert "不是合法 JSON" in status


def test_a_structurally_invalid_shared_table_is_distinguished_from_a_missing_one(
    monkeypatch, tmp_path
) -> None:
    """结构不对（这里是版本不认识）与"文件不在"必须说成两句不同的话。"""
    path = shared_shift_path(tmp_path, maa_module.PLUGIN_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 999}), encoding="utf-8")

    instance, _sent = _boot(monkeypatch)
    asyncio.run(instance._auto_ask_tick())

    assert "版本不认识" in instance._status_text()


def test_a_half_written_file_does_not_crash_the_module(monkeypatch, tmp_path) -> None:
    """原子写的意义就在这里：读取方可能正好看到写了一半的内容。

    真把半截 JSON 摆进去，模块必须**照常活着**（解析失败 → 报出来 → 不抛）。
    """
    path = shared_shift_path(tmp_path, maa_module.PLUGIN_NAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dump_shared(
        parse_shift_slots(_config_first_shift_starting_at(0)),
        timezone=TZ_NAME,
        generated_at=datetime(2026, 9, 29),
    )
    text = json.dumps(payload, ensure_ascii=False)
    path.write_text(text[: len(text) // 2], encoding="utf-8")  # 半截

    instance, _sent = _boot(monkeypatch)
    asyncio.run(instance._auto_ask_tick())  # 不许抛

    assert instance.unavailable_reason is None
    assert "不是合法 JSON" in instance._status_text()


def test_the_repeated_lookup_does_not_spam_the_log(monkeypatch, tmp_path) -> None:
    """一分钟查一次文件，**状态没变就不许重复记**——否则又是一场日志洪水。

    这条是 2026-09-29 那个"每秒一条"的同类：修过一次的地方，换个位置又会犯。
    """
    logs: list[str] = []
    instance, _sent = _boot(monkeypatch, logs=logs)

    for _ in range(30):
        asyncio.run(instance._auto_ask_tick())

    complaints = [line for line in logs if "共享班次表" in line]
    assert len(complaints) == 1, f"状态没变却记了 {len(complaints)} 条：{complaints}"


def test_the_log_says_something_when_the_state_changes(monkeypatch, tmp_path) -> None:
    """从"没有"变成"有"要留一条 INFO——否则用户永远不知道它接上了没有。"""
    logs: list[str] = []
    instance, _sent = _boot(monkeypatch, logs=logs)
    assert any("还没读到共享班次表" in line for line in logs)

    _write_shared(tmp_path, _boundary_just_now(2))
    asyncio.run(instance._auto_ask_tick())

    assert any("已读到共享班次表" in line for line in logs)


def test_a_shared_table_with_an_unloadable_timezone_falls_back_and_says_so(
    monkeypatch, tmp_path
) -> None:
    """本机没有时区数据时**回落到本地时间**（与提醒模块同一取舍），并说出来。"""
    _write_shared(tmp_path, _config_first_shift_starting_at(0), timezone="Not/AZone")
    instance, _sent = _boot(monkeypatch)

    asyncio.run(instance._auto_ask_tick())

    assert instance.unavailable_reason is None
    assert "时区" in instance._status_text()


# --- 通知目标 ---------------------------------------------------------------


def test_without_a_target_it_does_not_mark_the_boundary_as_asked(monkeypatch, tmp_path) -> None:
    """没有通知目标时**不记"问过"**：用户中途补设目标，这一班仍然该收到询问。"""
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, sent = _boot(monkeypatch)  # 还没发过 /ak maa，没有目标

    asyncio.run(instance._auto_ask_tick())
    assert sent == []
    assert instance._asked == set()

    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    sent.clear()
    asyncio.run(instance._auto_ask_tick())

    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1


def test_the_no_target_warning_is_logged_once_per_boundary(monkeypatch, tmp_path) -> None:
    """一分钟一条十分钟就是十条一样的日志——每个换班只提醒一次。"""
    _write_shared(tmp_path, _boundary_just_now(2))
    logs: list[str] = []
    instance, _sent = _boot(monkeypatch, logs=logs)

    for _ in range(10):
        asyncio.run(instance._auto_ask_tick())

    warnings = [line for line in logs if "没有通知目标" in line]
    assert len(warnings) == 1, warnings


def test_a_failed_delivery_is_retried_within_the_window(monkeypatch, tmp_path) -> None:
    """发送失败**不记"问过"**：下一分钟还会再试，用户补上平台连接就收得到。"""
    _write_shared(tmp_path, _boundary_just_now(2))
    instance, _sent = _boot(monkeypatch, send_ok=False)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))

    asyncio.run(instance._auto_ask_tick())

    assert instance._asked == set(), "发送失败的换班不该被记成「问过了」"


def test_the_target_is_remembered_across_restarts(monkeypatch, tmp_path) -> None:
    """通知目标必须落盘：否则每次重启后到点询问都发不出去（功能等于死的）。"""
    first, _sent = _boot(monkeypatch)
    asyncio.run(first.handle_command("maa", _event("/ak maa")))

    second, _sent2 = _boot(monkeypatch)

    assert second._umo == "napcat2:FriendMessage:10001"


# --- 存档坏掉时的降级 -------------------------------------------------------


def test_a_corrupt_state_store_degrades_visibly_and_still_asks(monkeypatch, tmp_path) -> None:
    """存档坏了**不等于功能停了**：去重退化成内存，状态里说明，询问照发。

    这条区分很重要：去重记录只是"别重复问"的**礼貌**，而真正防止多跑一趟的是
    队列的不变量。所以这里选择继续工作 + 说清楚，而不是整个停摆。
    """
    data_dir = tmp_path / maa_module.PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / maa_module.STATE_FILENAME).write_text("这不是 JSON", encoding="utf-8")
    _write_shared(tmp_path, _boundary_just_now(2))

    instance, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa")))
    sent.clear()

    asyncio.run(instance._auto_ask_tick())

    assert len([t for t in _texts(sent) if "该换班了" in t]) == 1, "存档坏了不该让询问停摆"
    assert "状态存档" in instance._status_text()


def test_the_state_store_is_not_written_into_the_repo(monkeypatch, tmp_path) -> None:
    """测试隔离：任何落盘都只能落在临时目录里。

    原先 stub 给的默认路径是**仓库内**的 `.pytest-plugin-data`（且它不在
    `.gitignore` 里），于是"顺手写点状态"就会在仓库里留下未跟踪文件。
    """
    instance, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event("/ak maa run")))

    written = list(tmp_path.rglob("state.json"))
    assert written, "状态应该写进本次测试的临时目录"
    assert all(tmp_path in path.parents for path in written)


# --- 与共享表的类型契约 -----------------------------------------------------


def test_the_loaded_table_type_is_the_shared_one(monkeypatch, tmp_path) -> None:
    """装配层拿到的是 `SharedShiftTable`（`autoask` 只用它的 `.table` 与 `.shifts`）。"""
    _write_shared(tmp_path, _config_first_shift_starting_at(0))
    instance, _sent = _boot(monkeypatch)

    assert isinstance(instance._shared_table, SharedShiftTable)
    assert instance._shared_table is not None
    assert instance._shared_table.names == ("第 1 班", "第 2 班", "第 3 班")


def test_the_shift_reminder_module_writes_what_this_one_reads(monkeypatch, tmp_path) -> None:
    """**端到端那条缝**：用写入方那套函数写、用读取方那套读，必须对得上。

    这条不启动 `shift_reminder` 实例（那要另一套夹具），而是把中间的协议钉住：
    两个模块只能经由 `core/shift_share` 的这两个函数打交道，所以这一段通了，
    真实的两端就通。
    """
    config = _config_first_shift_starting_at(9 * 60)
    _write_shared(tmp_path, config)
    instance, _sent = _boot(monkeypatch)

    assert instance._shared_table is not None
    assert instance._shared_table.names == ("第 1 班", "第 2 班", "第 3 班")
    assert "读到了 3 个班次" in instance._status_text()
