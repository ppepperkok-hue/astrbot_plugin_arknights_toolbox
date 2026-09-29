"""`webapi` 纯逻辑单测：状态 JSON 的组装。

页面后端只做一件事——把模块已有的判定结果翻译成 JSON，**不重算班次**。
所以这里的断言全部落在「翻译是否正确」上，并且刻意使用**生产类型**
（`schedule.ShiftTable`、`strategy.PeriodStrategy`、`core.storage.SendRecord`），
这样一旦哪天字段改名，测试会先炸，而不是等页面白屏。
"""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from core.storage import SendRecord
from modules.shift_reminder import module as reminder_module
from modules.shift_reminder.schedule import Shift, validate
from modules.shift_reminder.strategy import PeriodStrategy
from modules.shift_reminder.webapi import (
    build_status,
    format_remaining,
    order_shifts,
    record_brief,
    shift_brief,
)

# 一套标准三班：早班 08:00×12h、晚班 20:00×6h、夜班 02:00×6h
TABLE = validate(
    [
        Shift(name="早班", start_minute=8 * 60, duration_minutes=12 * 60),
        Shift(name="晚班", start_minute=20 * 60, duration_minutes=6 * 60),
        Shift(name="夜班", start_minute=2 * 60, duration_minutes=6 * 60),
    ]
)
STRATEGY = PeriodStrategy(TABLE, lead_minutes=10)


# --- format_remaining ------------------------------------------------------


def test_format_remaining_rounds_down_to_minutes() -> None:
    assert format_remaining(timedelta(minutes=15, seconds=59)) == "15 分"


def test_format_remaining_zero_is_not_zero() -> None:
    """正好归零时说「不到 1 分钟」，而不是「0 分」。"""
    assert format_remaining(timedelta()) == "不到 1 分钟"


def test_format_remaining_never_negative() -> None:
    """换班时刻已过（时钟抖动、跨秒）时不许出现负数。"""
    assert format_remaining(timedelta(minutes=-5)) == "不到 1 分钟"


def test_format_remaining_hours_and_minutes() -> None:
    assert format_remaining(timedelta(hours=5, minutes=45)) == "5 小时 45 分"


def test_format_remaining_whole_hour_keeps_minutes() -> None:
    assert format_remaining(timedelta(hours=2)) == "2 小时 0 分"


def test_format_remaining_days() -> None:
    assert format_remaining(timedelta(days=1, hours=3, minutes=20)) == "1 天 3 小时"


# --- shift_brief / record_brief -------------------------------------------


def test_shift_brief_formats_start_and_end() -> None:
    assert shift_brief(Shift("早班", 8 * 60, 12 * 60)) == {
        "name": "早班",
        "start": "08:00",
        "end": "20:00",
    }


def test_shift_brief_cross_midnight_end_is_smaller() -> None:
    """跨天班次的结束时刻小于开始时刻——页面靠这两个值直接显示，不能取模错。"""
    assert shift_brief(Shift("夜班", 2 * 60, 6 * 60))["end"] == "08:00"
    assert shift_brief(Shift("晚班", 20 * 60, 6 * 60))["end"] == "02:00"


def test_record_brief_uses_real_send_record() -> None:
    record = SendRecord(at=datetime(2026, 9, 29, 1, 50), shift="夜班", ok=True)
    assert record_brief(record) == {
        "at": "2026-09-29T01:50:00",
        "shift": "夜班",
        "ok": True,
        "detail": "",
    }


def test_record_brief_keeps_failure_detail() -> None:
    record = SendRecord(
        at=datetime(2026, 9, 29, 7, 50),
        shift="早班",
        ok=False,
        detail="send_message 返回 False",
    )
    brief = record_brief(record)
    assert brief["ok"] is False
    assert brief["detail"] == "send_message 返回 False"


# --- build_status ----------------------------------------------------------


def _status(now: datetime, **overrides: object) -> dict:
    kwargs: dict = {
        "lead_minutes": 10,
        "bound": False,
        "recent": [],
        "roster_imported": False,
        "breaker_open": False,
        "consecutive_failures": 0,
    }
    kwargs.update(overrides)
    return build_status(STRATEGY.snapshot(now), **kwargs)


def test_build_status_reports_current_and_upcoming() -> None:
    data = _status(datetime(2026, 9, 29, 1, 45))

    assert data["current"] == {"name": "晚班", "start": "20:00", "end": "02:00"}
    assert data["upcoming"]["name"] == "夜班"
    assert data["upcoming"]["start"] == "02:00"
    assert data["upcoming"]["change_at"] == "2026-09-29T02:00:00"
    assert data["upcoming"]["remaining"] == "15 分"
    assert data["upcoming"]["remaining_minutes"] == 15


def test_build_status_counts_down_across_midnight() -> None:
    """跨天倒计时：23:50 时下一班是次日 02:00，还有 2 小时 10 分。"""
    data = _status(datetime(2026, 9, 29, 23, 50))

    assert data["current"]["name"] == "晚班"
    assert data["upcoming"]["change_at"] == "2026-09-30T02:00:00"
    assert data["upcoming"]["remaining"] == "2 小时 10 分"
    assert data["upcoming"]["remaining_minutes"] == 130


def test_build_status_passes_through_flags() -> None:
    data = _status(
        datetime(2026, 9, 29, 1, 45),
        lead_minutes=30,
        bound=True,
        roster_imported=True,
        breaker_open=True,
        consecutive_failures=3,
    )

    assert data["lead_minutes"] == 30
    assert data["binding"] == {"bound": True}
    assert data["roster"] == {"imported": True}
    assert data["breaker"] == {"open": True, "consecutive_failures": 3}


def test_build_status_defaults_are_the_unhappy_path() -> None:
    """默认（未绑定、无记录、未导入、未熔断）必须如实反映，不许美化。"""
    data = _status(datetime(2026, 9, 29, 1, 45))

    assert data["binding"]["bound"] is False
    assert data["roster"]["imported"] is False
    assert data["breaker"]["open"] is False
    assert data["recent"] == []


def test_build_status_maps_recent_records_in_order() -> None:
    records = [
        SendRecord(at=datetime(2026, 9, 29, 1, 50), shift="夜班", ok=True),
        SendRecord(at=datetime(2026, 9, 28, 19, 50), shift="晚班", ok=False, detail="掉了"),
    ]
    data = _status(datetime(2026, 9, 29, 1, 45), recent=records)

    assert [item["shift"] for item in data["recent"]] == ["夜班", "晚班"]
    assert data["recent"][0]["ok"] is True
    assert data["recent"][1]["detail"] == "掉了"


def test_build_status_is_json_serializable() -> None:
    """Web API 的返回值必须过得了 JSON——datetime 漏在里面就是 500。"""
    data = _status(
        datetime(2026, 9, 29, 7, 45),
        recent=[SendRecord(at=datetime(2026, 9, 29, 1, 50), shift="夜班", ok=True)],
    )

    text = json.dumps(data, ensure_ascii=False)
    assert json.loads(text)["current"]["name"] == "夜班"


def test_build_status_exposes_shift_definitions_for_the_editor() -> None:
    """页面表单要预填当前三班，所以状态里必须带上定义（**只读用途**）。

    **不传 `shift_order` 时**，出来的是 `validate()` 排过序的顺序：夜班(02:00) →
    早班(08:00) → 晚班(20:00)，而不是配置里写的第 1/2/3 班。

    这条只钉住「原样透传」这个向后兼容行为。**页面要用的是配置顺序**，
    见 `test_build_status_restores_config_order_for_the_editor`——两者不能混。
    """
    data = _status(datetime(2026, 9, 29, 1, 45), shifts=TABLE.shifts)

    assert [item["name"] for item in data["shifts"]] == ["夜班", "早班", "晚班"]
    assert data["shifts"][1]["start"] == "08:00"
    assert data["shifts"][0]["hours"] == 6


def test_build_status_restores_config_order_for_the_editor() -> None:
    """传了 `shift_order` 就要按**配置顺序**给，否则页面会把时刻写错段。

    这是修一个真缺陷：`ShiftTable.shifts` 按开始时刻排序（夜班跑到最前），而页面的
    「第一班」对应 `shift_1`。若直接拿排序结果填位置，用户的早班位置会显示夜班，
    保存时就把夜班时刻写进 `shift_1`。
    """
    data = _status(
        datetime(2026, 9, 29, 1, 45),
        shifts=TABLE.shifts,
        shift_order=("早班", "晚班", "夜班"),
    )

    assert [item["name"] for item in data["shifts"]] == ["早班", "晚班", "夜班"]
    assert [item["slot"] for item in data["shifts"]] == [1, 2, 3]
    assert data["shifts"][0]["start"] == "08:00"
    assert data["shifts"][0]["hours"] == 12


def test_order_shifts_falls_back_when_names_do_not_match() -> None:
    """顺序对不上时**原样返回**，不许因为改过名字就丢班次。"""
    assert order_shifts(TABLE.shifts, ("早班", "夜班")) == TABLE.shifts
    assert order_shifts(TABLE.shifts, None) == TABLE.shifts


def test_order_shifts_keeps_every_shift_exactly_once() -> None:
    """重排是一次置换：不重复、不丢失。"""
    ordered = order_shifts(TABLE.shifts, ("晚班", "夜班", "早班"))
    assert sorted(s.name for s in ordered) == sorted(s.name for s in TABLE.shifts)


def test_build_status_shifts_default_to_empty_list() -> None:
    """没传定义时给空列表而不是 None——前端 `forEach` 才不会炸。"""
    assert _status(datetime(2026, 9, 29, 1, 45))["shifts"] == []


# --- 装配层接线（路由名、handler、能否真的取到数据） ------------------------
#
# 本机没有 AstrBot 运行时，页面渲染与 HTTP 转发只能在服务器上验；但「路由注册成
# 功、路由名对、handler 能取到数据并给出可 JSON 化的结果」这三点可以在本地钉住。
# 这里刻意碰私有方法：不这么做就得起一套 HTTP 栈，成本远高于收益。

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


class _FakeCronManager:
    """够用的假调度器：只记 job，不真的按时触发。"""

    def __init__(self) -> None:
        self.jobs: list[SimpleNamespace] = []

    async def list_jobs(self) -> list[SimpleNamespace]:
        return list(self.jobs)

    async def delete_job(self, job_id: str) -> None:
        self.jobs = [job for job in self.jobs if job.job_id != job_id]

    async def add_basic_job(self, **kwargs: object) -> SimpleNamespace:
        job = SimpleNamespace(job_id=f"id-{len(self.jobs)}", **kwargs)
        self.jobs.append(job)
        return job


def _boot(tmp_path, monkeypatch) -> tuple[object, list[tuple]]:
    """把模块跑起来，返回 (模块, 已注册的 Web API 列表)。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    registered: list[tuple] = []
    ctx = SimpleNamespace(
        cron_manager=_FakeCronManager(),
        register_web_api=lambda route, handler, methods, desc: registered.append(
            (route, handler, methods, desc)
        ),
    )
    instance = reminder_module.ShiftReminderModule()
    asyncio.run(instance.initialize(ctx, dict(CONFIG)))
    return instance, registered


def test_initialize_registers_the_status_route(tmp_path, monkeypatch) -> None:
    """路由必须带插件名前缀，否则 Dashboard 转发不到（rules.md §5）。"""
    instance, registered = _boot(tmp_path, monkeypatch)

    assert len(registered) == 1
    route, handler, methods, _desc = registered[0]
    assert route == "/astrbot_plugin_arknights_toolbox/shift-reminder/status"
    assert methods == ["GET"]
    assert handler == instance._web_status


def test_web_status_returns_serializable_payload(tmp_path, monkeypatch) -> None:
    """handler 端到端跑通：取数据 → 组装 → 可 JSON 化。"""
    instance, _ = _boot(tmp_path, monkeypatch)

    payload = asyncio.run(instance._web_status())

    assert payload["current"]["name"] in {"早班", "晚班", "夜班"}
    assert payload["upcoming"]["name"] in {"早班", "晚班", "夜班"}
    assert payload["binding"]["bound"] is False
    assert payload["roster"]["imported"] is False
    assert payload["recent"] == []
    json.dumps(payload, ensure_ascii=False)


def test_web_status_refuses_before_initialize() -> None:
    """没初始化就访问必须先说清楚，而不是抛 AttributeError 给用户看。"""
    payload = asyncio.run(reminder_module.ShiftReminderModule()._web_status())

    assert payload == {
        "error": "换班提醒模块尚未初始化完成，请稍后重试",
        "status_code": 503,
    }


def test_register_web_api_absent_degrades_loudly(monkeypatch) -> None:
    """Context 没有 register_web_api 时只降级页面，不拖垮提醒。"""
    messages: list[str] = []
    monkeypatch.setattr(
        reminder_module.logger, "warning", lambda *args, **kwargs: messages.append(str(args))
    )

    reminder_module.ShiftReminderModule()._register_web_api(SimpleNamespace())

    assert any("register_web_api" in message for message in messages)
