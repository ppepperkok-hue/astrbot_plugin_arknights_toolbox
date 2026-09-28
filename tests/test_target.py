"""提醒目标绑定（P1.5）与时区（P1.4）的读写与校验。

这两块都是**成品的边界**：绑定目标写错会毁掉已部署实例的数据；时区写错会让提醒
在错的时刻响，或者被 AstrBot 悄悄回落到服务器时区。所以这里重点盯四件事：

- 键名必须仍然是 ``bound_umo``（改了就丢掉用户现有的绑定）；
- 绑定读写**只经** ``_get_target`` / ``_set_target`` 两个函数；
- 时区缺失/空白 → 默认值（向后兼容）；可判定的非法名 → 抛 ``ConfigError``；
- 本机**根本没有时区数据**时，不能因为「查不到」就拒掉配置，但必须留下 WARNING，
  且**配置里的时区要真的传到调度器**（这才叫生效）。
"""

import asyncio
from datetime import UTC, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.storage import JsonStateStore
from modules.shift_reminder import module as reminder_module
from modules.shift_reminder.module import (
    DEFAULT_TIMEZONE,
    UMO_KEY,
    ShiftReminderModule,
    parse_timezone,
)
from modules.shift_reminder.schedule import ConfigError

BASE_CONFIG = {
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


# --- 假件 -------------------------------------------------------------------


def make_loader(resolvable: set[str]):
    """假的时区解析器：只有名单里的名字能解析。

    本机与 CI 都可能没有时区数据（`ZoneInfo("Asia/Shanghai")` 直接抛），那样
    「名字非法」与「本机没有数据」两条路径就没法稳定地测。把「本机能力」变成
    测试可控的输入，两条路径才各有一条确定的断言。
    """

    def _load(key: str) -> Any:
        return UTC if key in resolvable else None

    return _load


class RecordingLogger:
    """记下 WARNING 文案——「查不到也不许静默」只能靠日志来验。"""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, *args: Any) -> None:
        self.warnings.append(message % args if args else message)

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeCtx:
    """够用的假 Context：只需要能发消息，并把发出去的记下来。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.sent.append((umo, chain))
        return True


class FakeEvent:
    def __init__(self, umo: str) -> None:
        self.unified_msg_origin = umo


class FakeCronManager:
    """只记下 `add_basic_job` 收到的参数——本文件要断言时区有没有传对。"""

    def __init__(self) -> None:
        self.kwargs: list[dict[str, Any]] = []

    async def list_jobs(self):
        return []

    async def delete_job(self, job_id: str) -> None:
        return None

    async def add_basic_job(self, **kwargs: Any):
        self.kwargs.append(kwargs)
        return SimpleNamespace(name=kwargs["name"], job_id=f"id-{len(self.kwargs)}")


# --- P1.4 时区校验 ----------------------------------------------------------


def test_timezone_missing_uses_default():
    assert parse_timezone({}) == DEFAULT_TIMEZONE


def test_timezone_none_uses_default():
    assert parse_timezone({"timezone": None}) == DEFAULT_TIMEZONE


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_timezone_blank_uses_default(blank):
    assert parse_timezone({"timezone": blank}) == DEFAULT_TIMEZONE


@pytest.mark.parametrize("bad", [1, 1.5, True, ["Asia/Shanghai"], {"k": "v"}])
def test_timezone_must_be_a_string(bad):
    with pytest.raises(ConfigError):
        parse_timezone({"timezone": bad})


def test_timezone_valid_name_is_returned(monkeypatch):
    monkeypatch.setattr(
        reminder_module, "_load_timezone", make_loader({DEFAULT_TIMEZONE, "Asia/Tokyo"})
    )
    assert parse_timezone({"timezone": "Asia/Tokyo"}) == "Asia/Tokyo"


def test_timezone_is_trimmed(monkeypatch):
    monkeypatch.setattr(reminder_module, "_load_timezone", make_loader({"Asia/Tokyo"}))
    assert parse_timezone({"timezone": "  Asia/Tokyo  "}) == "Asia/Tokyo"


def test_timezone_invalid_name_is_rejected_when_the_machine_can_tell(monkeypatch):
    """默认值解析得了 → 本机有时区数据 → 那么名字错就一定是错，必须抛（不许回落）。"""
    monkeypatch.setattr(reminder_module, "_load_timezone", make_loader({DEFAULT_TIMEZONE}))

    with pytest.raises(ConfigError) as exc:
        parse_timezone({"timezone": "Asia/Tokio"})

    assert "Asia/Tokio" in str(exc.value)


def test_timezone_unresolvable_machine_passes_through_but_warns(monkeypatch):
    """本机连默认值都解析不了 → 判不了对错 → 放行，但必须留下 WARNING。"""
    monkeypatch.setattr(reminder_module, "_load_timezone", make_loader(set()))
    recorder = RecordingLogger()
    monkeypatch.setattr(reminder_module, "logger", recorder)

    assert parse_timezone({"timezone": "Asia/Tokyo"}) == "Asia/Tokyo"
    assert len(recorder.warnings) == 1
    assert "时区数据" in recorder.warnings[0]


# --- P1.4 真的传到调度器 ----------------------------------------------------


def test_initialize_hands_the_configured_timezone_to_the_scheduler(monkeypatch, tmp_path):
    """P1.4 的命门：配置里的时区必须原样进 `add_basic_job`。

    否则「改了时区」只是改了个没人读的字符串——看起来生效，其实没生效
    （项目宪法 §2 第 2 条点名的最高优先级 bug）。
    """
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(
        reminder_module, "_load_timezone", make_loader({DEFAULT_TIMEZONE, "Asia/Tokyo"})
    )

    cron = FakeCronManager()
    module = ShiftReminderModule()
    config = {**BASE_CONFIG, "timezone": "Asia/Tokyo"}
    asyncio.run(module.initialize(SimpleNamespace(cron_manager=cron), config))

    assert [call["timezone"] for call in cron.kwargs] == ["Asia/Tokyo"] * 3
    assert module._tz is not None


def test_initialize_without_the_timezone_key_still_works(monkeypatch, tmp_path):
    """老用户的配置文件里没有 `timezone` 键（向后兼容），必须照常启动。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(reminder_module, "_load_timezone", make_loader({DEFAULT_TIMEZONE}))

    cron = FakeCronManager()
    asyncio.run(
        ShiftReminderModule().initialize(SimpleNamespace(cron_manager=cron), dict(BASE_CONFIG))
    )

    assert [call["timezone"] for call in cron.kwargs] == [DEFAULT_TIMEZONE] * 3


def test_initialize_rejects_a_bad_timezone_name(monkeypatch, tmp_path):
    """非法时区必须让加载失败——绝不允许悄悄按服务器时区跑。"""
    monkeypatch.setattr(reminder_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(reminder_module, "_load_timezone", make_loader({DEFAULT_TIMEZONE}))

    cron = FakeCronManager()
    config = {**BASE_CONFIG, "timezone": "Nowhere/Nothing"}
    with pytest.raises(ConfigError):
        asyncio.run(ShiftReminderModule().initialize(SimpleNamespace(cron_manager=cron), config))

    assert cron.kwargs == []


def test_now_carries_the_configured_timezone():
    """现在的时刻要带配置时区——否则 cron 按东京响、内容却按服务器时区算。"""
    module = ShiftReminderModule()
    module._tz = UTC

    now = module._now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_now_falls_back_to_local_time_without_tz_data():
    """本机没有时区数据时退回 naive 本地时间，与 AstrBot 调度器的回落行为一致。"""
    module = ShiftReminderModule()
    module._tz = None

    assert module._now().tzinfo is None


# --- P1.5 绑定读写封装 ------------------------------------------------------


def make_module(tmp_path) -> ShiftReminderModule:
    module = ShiftReminderModule()
    module._store = JsonStateStore(tmp_path / "state.json")
    module._ctx = FakeCtx()
    return module


def test_bound_key_name_is_unchanged():
    """键名一旦改动，已部署实例的绑定就丢了——钉死它。"""
    assert UMO_KEY == "bound_umo"


def test_target_is_none_before_binding(tmp_path):
    assert make_module(tmp_path)._get_target() is None


def test_target_round_trip_lands_under_the_original_key(tmp_path):
    module = make_module(tmp_path)
    module._set_target("default:FriendMessage:10001")

    assert module._get_target() == "default:FriendMessage:10001"
    # 另开一个 store 直接读文件：确认落的还是原来那个键，没有偷偷换名字
    assert JsonStateStore(tmp_path / "state.json").get(UMO_KEY) == "default:FriendMessage:10001"


def test_target_is_single_valued_and_overwritten(tmp_path):
    """当前就是单目标、覆盖式改绑（多绑定是用户明确决定**不做**的）。"""
    module = make_module(tmp_path)
    module._set_target("default:FriendMessage:10001")
    module._set_target("napcat2:FriendMessage:10001")

    assert module._get_target() == "napcat2:FriendMessage:10001"


@pytest.mark.parametrize("stored", ["", 123, ["a", "b"], {"k": "v"}])
def test_target_ignores_a_stored_value_that_is_not_a_usable_string(tmp_path, stored):
    """手工改坏了 state.json 也不该让推送往一个奇怪的值上发。"""
    module = make_module(tmp_path)
    module._store.set(UMO_KEY, stored)

    assert module._get_target() is None


def test_cmd_bind_first_time_reports_bound(tmp_path):
    module = make_module(tmp_path)
    asyncio.run(module._cmd_bind(FakeEvent("default:FriendMessage:10001")))

    assert module._get_target() == "default:FriendMessage:10001"
    assert "已绑定" in module._ctx.sent[-1][1].parts[0]


def test_cmd_bind_second_time_reports_the_previous_target(tmp_path):
    module = make_module(tmp_path)
    asyncio.run(module._cmd_bind(FakeEvent("default:FriendMessage:10001")))
    asyncio.run(module._cmd_bind(FakeEvent("napcat2:FriendMessage:10001")))

    assert module._get_target() == "napcat2:FriendMessage:10001"
    assert "原来是" in module._ctx.sent[-1][1].parts[0]


def test_cmd_bind_to_the_same_session_says_bound_not_rebound(tmp_path):
    """同一个会话重复绑定，不该说「改到本会话（原来是它自己）」。"""
    module = make_module(tmp_path)
    same = "default:FriendMessage:10001"
    asyncio.run(module._cmd_bind(FakeEvent(same)))
    asyncio.run(module._cmd_bind(FakeEvent(same)))

    assert "原来是" not in module._ctx.sent[-1][1].parts[0]
