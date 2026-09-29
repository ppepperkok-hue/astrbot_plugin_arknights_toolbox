"""换班提醒的定时任务清理：只删自己的，且失败要响。

这是**删除操作**前的最后一关。AstrBot 的 cron 表由所有插件共用，
过滤条件一旦放宽，就会删掉用户别的功能的定时任务——所以这里重点盯
「只认自己的前缀」，以及「清理失败必须抛出、不许照常再注册一批」。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from modules.shift_reminder import module as reminder_module
from modules.shift_reminder.module import (
    JOB_PREFIX,
    ROSTER_KEY,
    ShiftReminderModule,
    stale_job_ids,
)


def job(name, job_id):
    """够用的假 job：真实对象只需要 `name` 与 `job_id` 两个属性。"""
    return SimpleNamespace(name=name, job_id=job_id)


class FakeCronManager:
    """只实现本模块用到的那三个方法，不模拟 AstrBot 的任何其它行为。"""

    def __init__(self, jobs=(), fail_list=False, fail_delete=False):
        self._jobs = list(jobs)
        self.deleted = []
        self.added = []
        self.added_kwargs = []
        self.fail_list = fail_list
        self.fail_delete = fail_delete

    async def list_jobs(self):
        if self.fail_list:
            raise RuntimeError("list_jobs 炸了")
        return list(self._jobs)

    async def delete_job(self, job_id):
        if self.fail_delete:
            raise RuntimeError("delete_job 炸了")
        self.deleted.append(job_id)
        self._jobs = [j for j in self._jobs if j.job_id != job_id]

    async def add_basic_job(self, **kwargs):
        new = job(kwargs["name"], f"new-{len(self.added) + 1}")
        self.added.append(kwargs["name"])
        self.added_kwargs.append(kwargs)
        self._jobs.append(new)
        return new


CONFIG = {
    "shift_1_name": "早班",
    "shift_1_start": "08:00",
    "shift_1_hours": 12,
    "shift_2_name": "晚班",
    "shift_2_start": "20:00",
    "shift_2_hours": 6,
    "shift_3_name": "夜班",
    "shift_3_start": "02:00",
    "shift_3_hours": 6,
    "lead_minutes": 10,
}


# --- 纯选择器 ---------------------------------------------------------------


def test_picks_only_our_prefixed_jobs():
    jobs = [
        job(f"{JOB_PREFIX}早班", "id-1"),
        job(f"{JOB_PREFIX}晚班", "id-2"),
        job(f"{JOB_PREFIX}夜班", "id-3"),
    ]
    assert stale_job_ids(jobs) == ["id-1", "id-2", "id-3"]


def test_never_touches_other_plugins_jobs():
    jobs = [
        job("astrbot_plugin_angel_heart:daily", "other-1"),
        job("ak_toolbox:maa:link_start", "other-2"),
        job("daily_sharing:morning", "other-3"),
        job(f"{JOB_PREFIX}早班", "mine-1"),
    ]
    assert stale_job_ids(jobs) == ["mine-1"]


def test_prefix_must_be_at_the_start():
    """名字中间出现前缀不算——否则别人的 job 会被误删。"""
    jobs = [job(f"other:{JOB_PREFIX}早班", "other-1")]
    assert stale_job_ids(jobs) == []


def test_empty_input():
    assert stale_job_ids([]) == []


def test_skips_jobs_without_usable_name_or_id():
    jobs = [
        job(None, "no-name"),
        job("", "empty-name"),
        job(123, "int-name"),
        job(f"{JOB_PREFIX}早班", ""),
        job(f"{JOB_PREFIX}晚班", None),
        job(f"{JOB_PREFIX}夜班", "mine-1"),
    ]
    assert stale_job_ids(jobs) == ["mine-1"]


def test_id_is_coerced_to_str():
    assert stale_job_ids([job(f"{JOB_PREFIX}早班", 42)]) == ["42"]


def test_custom_prefix_is_honoured():
    jobs = [job("custom:early", "c-1"), job(f"{JOB_PREFIX}早班", "d-1")]
    assert stale_job_ids(jobs, prefix="custom:") == ["c-1"]


def test_repeated_calls_are_stable():
    jobs = [job(f"{JOB_PREFIX}早班", "id-1"), job("other", "x")]
    assert stale_job_ids(jobs) == stale_job_ids(jobs) == ["id-1"]


# --- 清理动作 ---------------------------------------------------------------


def test_purge_deletes_our_jobs_only_and_returns_count():
    fake = FakeCronManager(
        [
            job(f"{JOB_PREFIX}早班", "mine-1"),
            job("other_plugin:job", "other-1"),
        ]
    )
    purged = asyncio.run(ShiftReminderModule()._purge_stale_jobs(fake))

    assert purged == 1
    assert fake.deleted == ["mine-1"]
    assert [j.job_id for j in fake._jobs] == ["other-1"]


def test_purge_is_idempotent_when_nothing_left():
    fake = FakeCronManager([])
    module = ShiftReminderModule()

    assert asyncio.run(module._purge_stale_jobs(fake)) == 0
    assert asyncio.run(module._purge_stale_jobs(fake)) == 0
    assert fake.deleted == []


def test_purge_failure_is_loud():
    """清理失败必须抛出来，不能吞掉后照常再注册一批。"""
    module = ShiftReminderModule()

    with pytest.raises(RuntimeError):
        asyncio.run(module._purge_stale_jobs(FakeCronManager(fail_list=True)))

    leftover = FakeCronManager([job(f"{JOB_PREFIX}早班", "mine-1")], fail_delete=True)
    with pytest.raises(RuntimeError):
        asyncio.run(module._purge_stale_jobs(leftover))


def test_initialize_purges_previous_run_before_registering(monkeypatch, tmp_path):
    """重载/重启后不该留下上一轮的任务：先清干净，再注册新的三条。

    顺序是这条修复的命门——若先注册后清理，那一轮新注册的 job 也会被自己删掉，
    断言的 `_jobs == _job_ids` 会直接失败。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager(
        [
            job(f"{JOB_PREFIX}早班", "old-1"),
            job(f"{JOB_PREFIX}晚班", "old-2"),
            job(f"{JOB_PREFIX}夜班", "old-3"),
            job("other_plugin:keep-me", "other-1"),
        ]
    )
    module = ShiftReminderModule()
    module._job_ids = ["left-over-from-previous-run"]

    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))

    assert fake.deleted == ["old-1", "old-2", "old-3"]
    assert len(module._job_ids) == 3
    assert sorted(fake.added) == sorted(f"{JOB_PREFIX}{name}" for name in ("早班", "晚班", "夜班"))
    # 表里恰好只剩「别人的」+「本轮新注册的」
    assert [j.job_id for j in fake._jobs] == ["other-1", *module._job_ids]


def test_initialize_refuses_to_start_when_purge_fails(monkeypatch, tmp_path):
    """清理不了就不许注册：否则会把重复任务越堆越多。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([job(f"{JOB_PREFIX}早班", "old-1")], fail_delete=True)
    with pytest.raises(RuntimeError):
        asyncio.run(ShiftReminderModule().initialize(SimpleNamespace(cron_manager=fake), CONFIG))

    assert fake.added == []


# --- 配置热更新（页面改完班次要**真的生效**） -------------------------------


def test_apply_config_rebuilds_jobs_with_the_new_times(monkeypatch, tmp_path):
    """页面改了班次之后，定时任务必须按**新**时刻重建。

    `save_config` 只更新内存与磁盘，**不会**动已经注册的 cron 任务（已核源码，
    见 implementation.md §2.6）。所以这条是「改了配置真的生效」的唯一保证——
    少了它，用户会看到面板显示新时刻、提醒却仍按旧时刻跑。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([])
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))
    # 按**名字**取，不按下标：注册顺序跟 ShiftTable.shifts 走（按开始时刻排过，
    # 夜班 02:00 在最前），不是配置里第 1/2/3 班的顺序。
    first_run = {kw["name"]: kw["cron_expression"] for kw in fake.added_kwargs}

    # 三班整体后移一小时（09:00×12h → 21:00×6h → 03:00×6h，正好闭合）。
    # 单改一个班的开始时刻会破坏「首尾相接」，那样 validate 会拒绝——那也是对的。
    shifted = dict(
        CONFIG,
        shift_1_start="09:00",
        shift_2_start="21:00",
        shift_3_start="03:00",
    )
    fake.added_kwargs.clear()
    asyncio.run(module.apply_config(shifted))

    second_run = {kw["name"]: kw["cron_expression"] for kw in fake.added_kwargs}
    assert first_run[f"{JOB_PREFIX}早班"] == "50 7 * * *"
    assert second_run[f"{JOB_PREFIX}早班"] == "50 8 * * *"
    assert second_run[f"{JOB_PREFIX}晚班"] == "50 20 * * *"
    assert second_run[f"{JOB_PREFIX}夜班"] == "50 2 * * *"
    # 表里只剩本轮的三条（上一轮被自己清掉了），不会越堆越多
    assert [j.job_id for j in fake._jobs] == module._job_ids
    assert len(module._job_ids) == 3


def test_apply_config_updates_the_lead_minutes(monkeypatch, tmp_path):
    """提前量也要跟着变——它是配置里另一个会改动触发时刻的项。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([])
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))

    fake.added_kwargs.clear()
    asyncio.run(module.apply_config(dict(CONFIG, lead_minutes=30)))

    by_name = {kw["name"]: kw["cron_expression"] for kw in fake.added_kwargs}
    assert by_name[f"{JOB_PREFIX}早班"] == "30 7 * * *"
    assert module._lead_minutes == 30


def test_apply_config_rejects_illegal_config_and_keeps_jobs(monkeypatch, tmp_path):
    """非法配置必须抛错（宿主据此拒绝保存），且**不许**把既有任务拆掉。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([])
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))
    before = list(module._job_ids)

    # 三段时长合计 23 小时：非法
    bad = dict(CONFIG, shift_1_hours=11)
    with pytest.raises(reminder_module.ConfigError):
        asyncio.run(module.apply_config(bad))

    assert module._job_ids == before
    assert [j.job_id for j in fake._jobs] == before


# --- 干员名单与班次的对应（回归护栏） ---------------------------------------


def test_roster_extra_matches_plans_by_config_order(monkeypatch, tmp_path):
    """排班表的 ``plans`` 按「第 1/2/3 班」对应，**不是**按开始时刻排序后的顺序。

    回归护栏：`validate()` 会把夜班(02:00)排到最前。若拿排序后的下标去索引
    ``plans``，早班的提醒里就会显示晚班的干员——**内容全对、只是配错了人**，
    这种错最难被发现。这条断言把它钉死。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([])
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))

    module._store.set(
        ROSTER_KEY,
        {
            "shifts": [
                {
                    "plan_index": 1,
                    "rooms": [{"room": "trading", "index": 1, "operators": ["一班的"]}],
                },
                {
                    "plan_index": 2,
                    "rooms": [{"room": "trading", "index": 1, "operators": ["二班的"]}],
                },
                {
                    "plan_index": 3,
                    "rooms": [{"room": "trading", "index": 1, "operators": ["三班的"]}],
                },
            ]
        },
    )

    by_name = {shift.name: shift for shift in module._strategy.table.shifts}
    assert module._roster_extra(by_name["早班"]) == ["【本班配制】", "贸易站1：一班的"]
    assert module._roster_extra(by_name["晚班"]) == ["【本班配制】", "贸易站1：二班的"]
    assert module._roster_extra(by_name["夜班"]) == ["【本班配制】", "贸易站1：三班的"]


def test_roster_extra_returns_none_without_import(monkeypatch, tmp_path):
    """没导入过排班表时返回 None——提醒必须与从前一字不差。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))

    fake = FakeCronManager([])
    module = ShiftReminderModule()
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=fake), CONFIG))

    current = module._strategy.table.shifts[0]
    assert module._roster_extra(current) is None


# --- 推送失败路径（2026-09-29 线上事故的回归护栏） ---------------------------


class FakeCtx:
    """够用的假 Context：只实现本模块真正用到的那样东西（`send_message`）。

    可以配置成「抛异常」或「返回 False」，用来分别演练两种失败形态——
    线上事故正是**抛异常**那种（QQ 号掉线）。
    """

    def __init__(self, *, raises=None, returns=True):
        self.cron_manager = FakeCronManager([])
        self.sent = []
        self._raises = raises
        self._returns = returns

    async def send_message(self, umo, chain):
        if self._raises is not None:
            raise self._raises
        self.sent.append((umo, chain))
        return self._returns


def _module_ready_to_push(monkeypatch, tmp_path, ctx):
    """装好模块并绑定提醒目标：让 `_push` 能走到真正发送那一步。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    module = ShiftReminderModule()
    asyncio.run(module.initialize(ctx, CONFIG))
    module._set_target("test:FriendMessage:10001")
    return module


def test_push_records_failure_when_send_message_raises(monkeypatch, tmp_path):
    """`send_message` **抛异常**时也要走失败路径，而不是崩出去。

    线上事故（2026-09-29 07:50）：QQ 号掉线，`send_message` 抛异常，而 `_push`
    只处理了「返回 False」那一支。后果是熔断计数永不增长（熔断形同虚设）、
    `sends.jsonl` 里看不到这次失败、用户毫无提示。
    """
    ctx = FakeCtx(raises=RuntimeError("平台已离线"))
    module = _module_ready_to_push(monkeypatch, tmp_path, ctx)

    asyncio.run(module._push("夜班"))  # 断言点之一：不许把异常抛出来

    records = module._send_log.recent(5)
    assert len(records) == 1
    assert records[0].ok is False
    assert records[0].shift == "夜班"
    # detail 要带异常类型与消息——排查时就靠这两个
    assert "RuntimeError" in records[0].detail
    assert "平台已离线" in records[0].detail
    # 熔断计数必须涨，否则它永远不会打开
    assert module._breaker.consecutive_failures == 1


def test_push_failure_does_not_write_idempotency_key(monkeypatch, tmp_path):
    """失败**不能**留下幂等键。

    这是最要紧的一条：幂等键的语义是「这次换班已经通知过了」。若失败也写键，
    这次换班提醒就被永久标记为已发——用户再也不会收到它，而且任何日志都看不出
    「漏了一次」。
    """
    ctx = FakeCtx(raises=RuntimeError("平台已离线"))
    module = _module_ready_to_push(monkeypatch, tmp_path, ctx)

    asyncio.run(module._push("夜班"))
    asyncio.run(module._push("夜班"))

    # 幂等键没被写 → 第二次仍走失败记录（若被写了，第二次会静默跳过、只剩 1 条）
    assert len(module._send_log.recent(5)) == 2
    assert module._breaker.consecutive_failures == 2

    # 直接证据：状态文件里只有绑定目标，没有任何幂等键
    state_files = list(tmp_path.rglob("state.json"))
    assert len(state_files) == 1, f"期望恰好一个状态文件，实际 {state_files}"
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert set(state) == {"bound_umo"}, f"失败不该写幂等键，实际键：{set(state)}"


def test_push_returning_false_still_takes_the_same_path(monkeypatch, tmp_path):
    """原有路径不许回归：返回 `False` 仍要记录 + 计失败。"""
    ctx = FakeCtx(returns=False)
    module = _module_ready_to_push(monkeypatch, tmp_path, ctx)

    asyncio.run(module._push("夜班"))

    records = module._send_log.recent(5)
    assert len(records) == 1
    assert records[0].ok is False
    assert "返回 False" in records[0].detail
    assert module._breaker.consecutive_failures == 1


def test_successful_push_still_writes_key_and_clears_breaker(monkeypatch, tmp_path):
    """成功路径不许被这次改动影响：写幂等键、清熔断计数。"""
    ctx = FakeCtx(returns=True)
    module = _module_ready_to_push(monkeypatch, tmp_path, ctx)
    module._breaker.record_failure()

    asyncio.run(module._push("夜班"))

    records = module._send_log.recent(5)
    assert len(records) == 1
    assert records[0].ok is True
    assert module._breaker.consecutive_failures == 0
    # 幂等键写了 → 再推一次会被跳过，不再新增记录
    asyncio.run(module._push("夜班"))
    assert len(module._send_log.recent(5)) == 1
