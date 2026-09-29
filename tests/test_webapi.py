"""`webapi` 纯逻辑单测：状态 JSON 的组装。

页面后端只做一件事——把模块已有的判定结果翻译成 JSON，**不重算班次**。
所以这里的断言全部落在「翻译是否正确」上，并且刻意使用**生产类型**
（`schedule.ShiftTable`、`strategy.PeriodStrategy`、`core.storage.SendRecord`），
这样一旦哪天字段改名，测试会先炸，而不是等页面白屏。
"""

import asyncio
import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.storage import SendRecord
from modules.shift_reminder import module as reminder_module
from modules.shift_reminder import webapi
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


def test_initialize_registers_the_expected_routes(tmp_path, monkeypatch) -> None:
    """路由必须带插件名前缀，否则 Dashboard 转发不到（rules.md §5）。

    三条路由各司其职，且方法不能串：状态与排班表是只读 GET，上传是 POST。
    """
    instance, registered = _boot(tmp_path, monkeypatch)

    by_route = {route: (handler, methods) for route, handler, methods, _desc in registered}
    prefix = "/astrbot_plugin_arknights_toolbox/shift-reminder"
    assert set(by_route) == {f"{prefix}/status", f"{prefix}/roster", f"{prefix}/upload"}
    assert by_route[f"{prefix}/status"] == (instance._web_status, ["GET"])
    assert by_route[f"{prefix}/roster"] == (instance._web_roster, ["GET"])
    assert by_route[f"{prefix}/upload"] == (instance._web_upload, ["POST"])


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


# --- roster_view：把落盘的排班表翻译成页面展示结构 ---------------------------


def _roster_sample() -> dict:
    """一份带「不动」房间的排班表，形状与 `roster.build_roster` 的产物一致。"""
    return {
        "source": "sample.json",
        "imported_at": "2026-09-29T02:46:36+08:00",
        "shift_count": 2,
        "shifts": [
            {
                "plan_index": 1,
                "plan_name": "第一班",
                "rooms": [
                    {
                        "room": "trading",
                        "index": 1,
                        "operators": ["黑键", "吉星"],
                        "skipped": False,
                    },
                    {"room": "dormitory", "index": 1, "operators": ["夜莺"], "skipped": True},
                ],
            },
            {
                "plan_index": 2,
                "plan_name": "第二班",
                "rooms": [
                    {"room": "manufacture", "index": 3, "operators": [], "skipped": False},
                ],
            },
        ],
    }


def test_roster_view_reports_not_imported_for_missing_roster() -> None:
    """没导入过时给 `imported: False`，页面据此显示上传引导——不是空白。"""
    assert webapi.roster_view(None) == {"imported": False}


def test_roster_view_reports_not_imported_for_wrong_shape() -> None:
    """形状不对时同样按「未导入」处理，绝不为了看起来有数据而编造结构。"""
    assert webapi.roster_view({"shifts": "不是列表"}) == {"imported": False}
    assert webapi.roster_view({}) == {"imported": False}


def test_roster_view_keeps_and_flags_skipped_rooms() -> None:
    """**本包最要紧的一条取舍**：页面上必须显示「不动」的房间并标注。

    与提醒消息刻意不同——提醒里不渲染（那是给用户的指令，让用户去改一间标明
    不要动的房就是错误信息）；页面上必须显示（那是给用户看的全貌，藏起来用户
    会以为我们读漏了）。
    """
    view = webapi.roster_view(_roster_sample())

    assert view["imported"] is True
    assert view["skipped_total"] == 1

    first = view["shifts"][0]
    assert [room["skipped"] for room in first["rooms"]] == [False, True]
    # 被标记的那间房**没有被丢掉**，而且干员名照实保留
    dorm = first["rooms"][1]
    assert dorm["room"] == "dormitory"
    assert dorm["label"] == "宿舍"
    assert dorm["operators"] == ["夜莺"]
    assert first["skipped_count"] == 1


def test_roster_view_counts_and_labels() -> None:
    """计数与中文房型名要正确——用户就是靠这些数字判断「读进去没有」。"""
    view = webapi.roster_view(_roster_sample())

    assert view["source"] == "sample.json"
    assert view["shift_count"] == 2

    first = view["shifts"][0]
    assert first["room_count"] == 2
    assert first["operator_count"] == 3  # 黑键、吉星、夜莺（含 skipped 房间的人）
    assert first["rooms"][0]["where"] == "贸易站1"

    second = view["shifts"][1]
    assert second["rooms"][0]["where"] == "制造站3"
    assert second["operator_count"] == 0


def test_roster_view_survives_dirty_entries() -> None:
    """脏条目不能让整张表作废：过滤掉非 Mapping 与非字符串干员名。"""
    dirty = {
        "shifts": [
            {
                "plan_index": 1,
                "plan_name": "第一班",
                "rooms": [
                    "不是对象",
                    {"room": "trading", "index": 1, "operators": ["黑键", "", None, 42]},
                ],
            }
        ]
    }
    view = webapi.roster_view(dirty)

    rooms = view["shifts"][0]["rooms"]
    assert len(rooms) == 1
    assert rooms[0]["operators"] == ["黑键"]


# --- 排班表的展示结构（纯逻辑） ----------------------------------------------
#
# 页面主要靠 `groups` 成块展示；「每行一间房」的平铺结构也保留着，供其它调用方取用。


def test_roster_view_exposes_groups_and_totals() -> None:
    """页面主要靠 `groups` 成块展示；顶部汇总要有数字可核对。"""
    view = webapi.roster_view(_roster_sample())

    assert view["total_rooms"] == 3
    assert view["total_operators"] == 3
    first = view["shifts"][0]
    assert [group["room"] for group in first["groups"]] == ["trading", "dormitory"]


def test_group_rooms_orders_known_types_before_unknown() -> None:
    """已知房型按 ROOM_ORDER 排，未知房型排最后——但没有被丢掉。"""
    rooms = [
        {"room": "dormitory", "index": 1, "operators": ["a"], "skipped": False},
        {"room": "trading", "index": 2, "operators": ["b"], "skipped": False},
        {"room": "power", "index": 1, "operators": ["c"], "skipped": False},
        {"room": "weird_room", "index": 1, "operators": ["d"], "skipped": False},
    ]
    groups = webapi.group_rooms(rooms)

    assert [group["room"] for group in groups] == [
        "trading",
        "power",
        "dormitory",
        "weird_room",
    ]
    # 未知房型用 key 兜底当 label，不显示空字符串。
    assert groups[-1]["label"] == "weird_room"


def test_group_rooms_sorts_within_a_type_and_counts() -> None:
    """同类型内按 index 升序，并自报数量——这才是「几间贸易站」的答案。"""
    rooms = [
        {"room": "trading", "index": 2, "operators": ["x"], "skipped": False},
        {"room": "trading", "index": 1, "operators": ["a", "b"], "skipped": False},
        {"room": "trading", "index": 3, "operators": [], "skipped": True},
    ]
    (group,) = webapi.group_rooms(rooms)

    assert group["label"] == "贸易站"
    assert group["count"] == 3
    assert group["operator_count"] == 3
    assert group["skipped_count"] == 1
    assert [room["index"] for room in group["rooms"]] == [1, 2, 3]


def test_group_rooms_handles_empty_input() -> None:
    """没有房间时返回空列表，不做任何编造。"""
    assert webapi.group_rooms([]) == []
    assert webapi.group_rooms([{"no_room_key": True}])[0]["room"] == ""


def test_group_rooms_ignores_items_with_a_bad_index() -> None:
    """`index` 不是整数时排到**最后**，房间本身仍然保留。

    为什么是最后而不是最前：排最前会让未知项冒充「第一间」，用户会以为那就是
    1 号房——宁可它出现在末尾，也不要显示一个错误的位置。
    """
    rooms = [
        {"room": "power", "index": "3", "operators": ["a"], "skipped": False},
        {"room": "power", "index": 1, "operators": ["b"], "skipped": False},
    ]
    (group,) = webapi.group_rooms(rooms)

    assert group["count"] == 2
    assert group["rooms"][0]["index"] == 1
    assert group["rooms"][-1]["index"] == "3"


def _schedule_json() -> bytes:
    """一份结构合法的最小排班表（3 班，与解析器的要求一致）。"""
    payload = {
        "plans": [
            {
                "name": f"第 {i} 班",
                "rooms": {
                    "trading": [{"operators": ["黑键"], "skip": False}],
                },
            }
            for i in (1, 2, 3)
        ]
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --- 页面上传：载荷解码（纯函数） --------------------------------------------
#
# 上传走 **base64 + JSON POST**，不是 multipart：AstrBot 的插件页面 bridge 用
# `postMessage` 传数据，而 `FormData` **不能被结构化克隆**——真实浏览器里直接抛
# 「FormData object could not be cloned.」，文件根本递不到后端（这条是线上实测
# 出来的，不是推测）。解码与体积判断因此搬进纯逻辑层，也就能在这里精确测。
#
# 下面几条直接用很小的 `max_bytes` 打到两条体积防线的边界，不必造几 MB 的字符串。


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _upload_body(raw: bytes, filename: str = "roster.json") -> dict:
    """页面实际发的请求体形状：base64 文本 +（仅供展示的）原文件名。"""
    return {"filename": filename, "content_b64": _b64(raw)}


def _patch_upload(monkeypatch, body) -> None:
    """把 `request.json()` 换成返回给定的请求体。"""

    async def _json(default=None):
        return default if body is None else body

    monkeypatch.setattr(reminder_module.request, "json", _json)


def test_decode_accepts_plain_base64() -> None:
    assert webapi.decode_schedule_upload(_b64(b"hello"), max_bytes=5) == b"hello"


def test_decode_allows_a_payload_exactly_at_the_limit() -> None:
    """正好等于上限要**放行**。

    解码前的长度估算会把上界算得比真实值大（填充字符不携带数据），所以那条廉价
    拒绝必须留余量——卡死在 `max_bytes` 就会把合法文件误杀。这条是那个余量的护栏。
    """
    assert webapi.decode_schedule_upload(_b64(b"12345"), max_bytes=5) == b"12345"


def test_decode_rejects_oversize_after_decoding() -> None:
    """解码后超限要拒，且认的是**真实字节数**，不是那个估算值。"""
    with pytest.raises(webapi.UploadPayloadError) as caught:
        webapi.decode_schedule_upload(_b64(b"12345"), max_bytes=4)

    assert caught.value.status_code == 413
    assert "太大" in str(caught.value)


def test_decode_rejects_oversize_before_decoding(monkeypatch) -> None:
    """明显超限的载荷要在**解码前**就被挡掉。

    否则为了量体积反而先把一个超大字符串解码进内存——那正是这条廉价判断存在的理由。
    用「把 b64decode 换成必炸的函数」来证明它确实没被调用。
    """

    def _explode(*_args, **_kwargs):
        raise AssertionError("超大载荷不应该走到解码这一步")

    monkeypatch.setattr(webapi.base64, "b64decode", _explode)

    with pytest.raises(webapi.UploadPayloadError) as caught:
        webapi.decode_schedule_upload(_b64(b"x" * 1000), max_bytes=8)

    assert caught.value.status_code == 413


def test_decode_rejects_invalid_base64() -> None:
    with pytest.raises(webapi.UploadPayloadError) as caught:
        webapi.decode_schedule_upload("!!!not base64!!!", max_bytes=1024)

    assert caught.value.status_code == 400
    assert "base64" in str(caught.value)


def test_decode_rejects_non_ascii_text() -> None:
    """中文字符不是 base64——要给 400，不是让它冒成 500。"""
    with pytest.raises(webapi.UploadPayloadError) as caught:
        webapi.decode_schedule_upload("这不是 base64", max_bytes=1024)

    assert caught.value.status_code == 400


def test_decode_tolerates_a_data_url_prefix() -> None:
    """`FileReader.readAsDataURL` 的产物带前缀；自己的前端会切掉，别人可能不切。"""
    prefixed = "data:application/json;base64," + _b64(b'{"a":1}')

    assert webapi.decode_schedule_upload(prefixed, max_bytes=1024) == b'{"a":1}'


def test_decode_tolerates_whitespace_and_missing_padding() -> None:
    """折行与省略填充都是真实导出工具会干的事；容错它们并不降低校验强度。"""
    encoded = _b64(b"hello world")
    unpadded = encoded.rstrip("=")
    folded = " \n ".join(unpadded[i : i + 4] for i in range(0, len(unpadded), 4))

    assert webapi.decode_schedule_upload(folded, max_bytes=1024) == b"hello world"


def test_decode_rejects_empty_and_blank() -> None:
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(webapi.UploadPayloadError) as caught:
            webapi.decode_schedule_upload(blank, max_bytes=1024)
        assert caught.value.status_code == 400
        assert "空" in str(caught.value)


def test_decode_rejects_base64_of_nothing() -> None:
    """空字符串的 base64 解出来也是空的——同样要说「文件是空的」。"""
    with pytest.raises(webapi.UploadPayloadError) as caught:
        webapi.decode_schedule_upload(_b64(b""), max_bytes=1024)

    assert "空" in str(caught.value)


def test_decode_rejects_a_missing_or_wrong_typed_field() -> None:
    """字段缺失/类型不对是 400，不是 500。"""
    for bad in (None, 123, b"bytes", ["a"]):
        with pytest.raises(webapi.UploadPayloadError) as caught:
            webapi.decode_schedule_upload(bad, max_bytes=1024)
        assert caught.value.status_code == 400


def test_decode_keeps_a_bom_in_the_bytes() -> None:
    """BOM 由**装配层**剥（它要先把字节解成文本）；解码函数只如实交出字节。"""
    raw = b"\xef\xbb\xbf{}"

    assert webapi.decode_schedule_upload(_b64(raw), max_bytes=1024) == raw


def test_estimate_is_an_upper_bound() -> None:
    """估算是**上界**——否则解码前的廉价拒绝会误杀合法文件。"""
    for size in (0, 1, 2, 3, 4, 5, 100, 1000):
        assert webapi.estimate_decoded_bytes(_b64(b"x" * size)) >= size


# --- 上传 handler（装配层） --------------------------------------------------
#
# 真实的「浏览器 → bridge → 后端」链路本机测不了（没有 AstrBot 运行时）。这里用
# stub 顶住 `request.json()`，覆盖**装配层自己的逻辑**：固定文件名、体积、内容
# 校验、错误码、回执口径。真实上传留服务器验收。


def test_web_upload_refuses_before_initialize() -> None:
    """没初始化就上传要先说清楚，而不是抛 AttributeError 给用户看。"""
    payload = asyncio.run(reminder_module.ShiftReminderModule()._web_upload())

    assert payload["status_code"] == 503


def test_web_upload_rejects_a_body_without_content(tmp_path, monkeypatch) -> None:
    """缺 content_b64 要说清字段名，而不是含糊的「失败」。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, {"filename": "x.json"})

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 400
    assert "content_b64" in payload["error"]


def test_web_upload_rejects_a_non_object_body(tmp_path, monkeypatch) -> None:
    """请求体不是对象时要明确拒绝——别让 `payload.get` 的异常抛给用户看。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, ["not", "an", "object"])

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 400
    assert "JSON 对象" in payload["error"]


def test_web_upload_uses_a_fixed_filename_not_the_supplied_one(tmp_path, monkeypatch) -> None:
    """**本包的安全核心**：上传者给什么文件名都无所谓，落盘一律用固定名。

    防的是「文件名即输入」——`../../evil.json` 一旦参与路径拼接，就能写到数据
    目录之外。这里断言恶意名字**没有**被用于任何落盘路径（它只被回显给用户看）。
    """
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(_schedule_json(), filename="../../evil.json"))

    payload = asyncio.run(instance._web_upload())

    assert payload["saved"] is True
    assert payload["filename"] == reminder_module.UPLOAD_FILENAME
    # 落盘位置由 self._data_dir 决定（插件数据目录），不是上传者说了算
    assert (instance._data_dir / reminder_module.UPLOAD_FILENAME).is_file()
    assert not (tmp_path.parent / "evil.json").exists()
    # 回执与 /ak import 同口径：带得出数字
    assert "班次" in payload["summary"]


def test_web_upload_echoes_the_original_name_for_display_only(tmp_path, monkeypatch) -> None:
    """原文件名只用于让用户确认「传的是哪个文件」；落盘名仍是固定名。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(_schedule_json(), filename="我的排班.json"))

    payload = asyncio.run(instance._web_upload())

    assert payload["original_filename"] == "我的排班.json"
    assert payload["filename"] == reminder_module.UPLOAD_FILENAME


def test_web_upload_sanitises_the_echoed_name(tmp_path, monkeypatch) -> None:
    """文件名是用户完全可控的字符串：换行能在日志里伪造出一整行，回显前要削掉。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(_schedule_json(), filename="a\nFAKE LOG LINE"))

    payload = asyncio.run(instance._web_upload())

    assert "\n" not in payload["original_filename"]


def test_web_upload_records_the_roster_in_the_store(tmp_path, monkeypatch) -> None:
    """导入成功必须真的落进 `ROSTER_KEY`，否则页面显示"已导入"却没有内容。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(_schedule_json()))

    asyncio.run(instance._web_upload())

    stored = instance._store.get(reminder_module.ROSTER_KEY)
    assert stored is not None
    assert stored["shift_count"] == 3


def test_web_upload_rejects_oversized_payload(tmp_path, monkeypatch) -> None:
    """体积上限要能挡住——不是"够用就行"，是别让大文件把内存撑爆。

    这条打的是**解码前的廉价拒绝**：给一段超长 base64 文本即可，不必真造几 MB 字节。
    """
    instance, _ = _boot(tmp_path, monkeypatch)
    huge = "A" * (reminder_module.MAX_UPLOAD_BYTES * 2)
    _patch_upload(monkeypatch, {"content_b64": huge})

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 413
    assert "太大" in payload["error"]


def test_web_upload_rejects_empty_payload(tmp_path, monkeypatch) -> None:
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, {"content_b64": ""})

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 400
    assert "空" in payload["error"]


def test_web_upload_reports_parser_error_not_500(tmp_path, monkeypatch) -> None:
    """坏 JSON 要带出解析器的原因、返回 400——不是 500 让用户面对"服务器错误"。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(b'{"plans": "not a list"}'))

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 400
    assert "导入失败" in payload["error"]


def test_web_upload_rejects_non_utf8(tmp_path, monkeypatch) -> None:
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(b"\xff\xfe\x00\x01"))

    payload = asyncio.run(instance._web_upload())

    assert payload["status_code"] == 400
    assert "UTF-8" in payload["error"]


def test_web_upload_tolerates_a_utf8_bom(tmp_path, monkeypatch) -> None:
    """带 BOM 的 UTF-8 要能导入。

    这不罕见：Windows 记事本另存 JSON 会带 BOM，而 `json.loads` 见到开头的
    `\\ufeff` 直接报错——用户看到「格式不对」根本猜不到是这个原因。
    """
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(b"\xef\xbb\xbf" + _schedule_json()))

    payload = asyncio.run(instance._web_upload())

    assert payload["saved"] is True, payload


def test_web_upload_tolerates_a_data_url_prefix(tmp_path, monkeypatch) -> None:
    """前端切了前缀，但换个写法可能带上来——多一层容错，少一类"内容没问题却失败"。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(
        monkeypatch,
        {"content_b64": "data:application/json;base64," + _b64(_schedule_json())},
    )

    payload = asyncio.run(instance._web_upload())

    assert payload["saved"] is True, payload


def test_web_roster_returns_view_of_stored_roster(tmp_path, monkeypatch) -> None:
    """页面查询走的是同一份落盘数据，不该另算一套。"""
    instance, _ = _boot(tmp_path, monkeypatch)
    _patch_upload(monkeypatch, _upload_body(_schedule_json()))
    asyncio.run(instance._web_upload())

    payload = asyncio.run(instance._web_roster())

    assert payload["imported"] is True
    assert payload["shift_count"] == 3
    assert payload["shifts"][0]["rooms"][0]["label"] == "贸易站"


def test_web_roster_before_import_shows_not_imported(tmp_path, monkeypatch) -> None:
    """未导入时给 False，页面据此显示上传引导。"""
    instance, _ = _boot(tmp_path, monkeypatch)

    payload = asyncio.run(instance._web_roster())

    assert payload == {"imported": False}
