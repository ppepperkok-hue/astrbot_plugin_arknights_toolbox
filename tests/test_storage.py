"""``core/storage.py`` 的纯逻辑测试。

不 import astrbot；所有落盘都走 ``tmp_path``，绝不写进仓库。
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from core.storage import CorruptedStoreError, JsonlSendLog, JsonStateStore, SendRecord


def _record(minute: int, ok: bool = True, detail: str = "") -> SendRecord:
    return SendRecord(at=datetime(2026, 9, 29, 12, minute), shift="早班", ok=ok, detail=detail)


# --- JsonStateStore --------------------------------------------------------


def test_state_store_missing_file_returns_default(tmp_path: Path) -> None:
    """文件不存在是首次运行的正常状态，取默认值即可，不该抛错。"""
    store = JsonStateStore(tmp_path / "state.json")
    assert store.get("umo") is None
    assert store.get("umo", "未绑定") == "未绑定"


def test_state_store_roundtrip_survives_new_instance(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    JsonStateStore(path).set("umo", "aiocqhttp:FriendMessage:10001")
    assert JsonStateStore(path).get("umo") == "aiocqhttp:FriendMessage:10001"


def test_state_store_write_is_valid_json_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """原子写：正式文件必须是完整 JSON，临时文件不能留下。"""
    path = tmp_path / "nested" / "state.json"
    store = JsonStateStore(path)
    store.set("last_sent", {"key": "早班@2026-09-29T08:00"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "last_sent": {"key": "早班@2026-09-29T08:00"}
    }
    assert list(path.parent.glob("*.tmp")) == []


def test_state_store_delete_is_idempotent(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state.json")
    store.set("k", 1)
    store.delete("k")
    assert store.get("k") is None
    store.delete("k")  # 再删一次不该抛


def test_state_store_keeps_other_keys_when_setting(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path / "state.json")
    store.set("a", 1)
    store.set("b", 2)
    assert store.get("a") == 1
    assert store.get("b") == 2


def test_state_store_corrupted_file_raises(tmp_path: Path) -> None:
    """坏档必须显式报错，不许静默当空配置——那会悄悄把用户数据抹掉。"""
    path = tmp_path / "state.json"
    path.write_text('{"umo": "x"', encoding="utf-8")  # 半截 JSON

    with pytest.raises(CorruptedStoreError, match="不是合法 JSON"):
        JsonStateStore(path).get("umo")


def test_state_store_non_object_root_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(CorruptedStoreError, match="根节点必须是对象"):
        JsonStateStore(path).get("umo")


def test_state_store_empty_file_raises(tmp_path: Path) -> None:
    """空文件不是「没写过」：原子写永远不会产出空文件，出现即异常。"""
    path = tmp_path / "state.json"
    path.write_text("", encoding="utf-8")

    with pytest.raises(CorruptedStoreError):
        JsonStateStore(path).get("umo")


# --- SendRecord ------------------------------------------------------------


def test_send_record_roundtrip_uses_iso8601() -> None:
    record = SendRecord(at=datetime(2026, 9, 29, 20, 0, 30), shift="晚班", ok=True)
    line = record.to_json_line()

    assert "2026-09-29T20:00:30" in line
    assert SendRecord.from_json_line(line) == record


def test_send_record_rejects_missing_at() -> None:
    with pytest.raises(ValueError, match="缺少 at"):
        SendRecord.from_json_line('{"shift": "早班"}')


def test_send_record_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        SendRecord.from_json_line("[1, 2]")


# --- JsonlSendLog ----------------------------------------------------------


def test_send_log_missing_file_is_empty(tmp_path: Path) -> None:
    assert JsonlSendLog(tmp_path / "sends.jsonl").recent() == []


def test_send_log_recent_is_newest_first(tmp_path: Path) -> None:
    log = JsonlSendLog(tmp_path / "sends.jsonl")
    for minute in (0, 1, 2):
        log.append(_record(minute))

    assert [r.at.minute for r in log.recent(limit=3)] == [2, 1, 0]


def test_send_log_recent_respects_limit(tmp_path: Path) -> None:
    log = JsonlSendLog(tmp_path / "sends.jsonl")
    for minute in range(10):
        log.append(_record(minute))

    assert [r.at.minute for r in log.recent(limit=2)] == [9, 8]


def test_send_log_recent_zero_or_negative_limit_is_empty(tmp_path: Path) -> None:
    log = JsonlSendLog(tmp_path / "sends.jsonl")
    log.append(_record(0))

    assert log.recent(limit=0) == []
    assert log.recent(limit=-1) == []


def test_send_log_keeps_failure_detail(tmp_path: Path) -> None:
    log = JsonlSendLog(tmp_path / "sends.jsonl")
    log.append(_record(0, ok=False, detail="平台未找到"))

    got = log.recent(limit=1)[0]
    assert got.ok is False
    assert got.detail == "平台未找到"


def test_send_log_skips_half_written_last_line(tmp_path: Path) -> None:
    """追加被中断会留下半截行；它必须被跳过，而不是让整份日志读不出来。"""
    path = tmp_path / "sends.jsonl"
    log = JsonlSendLog(path)
    log.append(_record(0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"at": "2026-09-29T12:01:00", "sh')  # 写入中断

    assert [r.at.minute for r in log.recent(limit=5)] == [0]


def test_send_log_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "sends.jsonl"
    log = JsonlSendLog(path)
    log.append(_record(0))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert [r.at.minute for r in log.recent(limit=5)] == [0]


def test_send_log_truncates_to_keep(tmp_path: Path) -> None:
    path = tmp_path / "sends.jsonl"
    log = JsonlSendLog(path, keep=3)
    for minute in range(10):
        log.append(_record(minute))

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3
    assert [r.at.minute for r in log.recent(limit=3)] == [9, 8, 7]


def test_send_log_does_not_truncate_below_keep(tmp_path: Path) -> None:
    path = tmp_path / "sends.jsonl"
    log = JsonlSendLog(path, keep=50)
    for minute in range(3):
        log.append(_record(minute))

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3


def test_send_log_creates_parent_directory(tmp_path: Path) -> None:
    log = JsonlSendLog(tmp_path / "deep" / "nested" / "sends.jsonl")
    log.append(_record(0))

    assert log.path.is_file()


def test_send_log_rejects_bad_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="keep 必须 >= 1"):
        JsonlSendLog(tmp_path / "sends.jsonl", keep=0)
