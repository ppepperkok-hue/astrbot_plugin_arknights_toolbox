"""凭据存取的纯逻辑测试。

这一层的价值全在「三态分得清」上：**「没授权」与「存档坏了」必须能区分**——
混在一起会让用户反复重新扫码却永远好不了。所以坏档的用例是本文件的重点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.storage import CorruptedStoreError
from modules.skland.credentials import (
    CRED_FIELD,
    CREDENTIALS_FILENAME,
    TOKEN_FIELD,
    USER_ID_FIELD,
    CredentialState,
    SklandCredential,
    SklandCredentialStore,
)

CRED = "fake-cred-value-1234567890"
TOKEN = "fake-token-value-abcdefghij"


def _store(tmp_path: Path) -> SklandCredentialStore:
    return SklandCredentialStore(tmp_path)


def _write_raw(tmp_path: Path, payload: object) -> Path:
    """直接写存档文件，用来构造坏档与半截档。"""
    path = tmp_path / CREDENTIALS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    return path


# --- 状态三态 ---------------------------------------------------------------


def test_status_is_missing_before_any_authorisation(tmp_path: Path) -> None:
    status = _store(tmp_path).status()
    assert status.state is CredentialState.MISSING
    assert not status.usable


def test_save_then_status_is_present(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(SklandCredential(cred=CRED, token=TOKEN, user_id="u-1"))

    status = store.status()
    assert status.state is CredentialState.PRESENT
    assert status.usable
    assert status.user_id == "u-1"


def test_corrupt_json_is_reported_as_corrupt_not_missing(tmp_path: Path) -> None:
    """**本文件最重要的一条**：坏档不许退化成「没授权」。"""
    _write_raw(tmp_path, "{ this is not json")

    status = _store(tmp_path).status()
    assert status.state is CredentialState.CORRUPT
    assert not status.usable
    assert "JSON" in status.detail or "json" in status.detail


def test_half_written_file_is_corrupt(tmp_path: Path) -> None:
    """有 cred 没 token（写一半断电）也要算坏档，不能当成可用。"""
    _write_raw(tmp_path, {CRED_FIELD: CRED})

    status = _store(tmp_path).status()
    assert status.state is CredentialState.CORRUPT
    assert not status.usable


@pytest.mark.parametrize(
    "payload",
    [
        {CRED_FIELD: 123, TOKEN_FIELD: TOKEN},
        {CRED_FIELD: "", TOKEN_FIELD: TOKEN},
        {CRED_FIELD: CRED, TOKEN_FIELD: None},
        {CRED_FIELD: CRED, TOKEN_FIELD: ""},
        [CRED, TOKEN],  # 根节点不是对象
    ],
)
def test_wrong_shapes_are_corrupt(tmp_path: Path, payload: object) -> None:
    _write_raw(tmp_path, payload)
    assert _store(tmp_path).status().state is CredentialState.CORRUPT


# --- 读写往返 ---------------------------------------------------------------


def test_load_round_trips_the_credential(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cred = SklandCredential(cred=CRED, token=TOKEN, user_id="u-9")
    store.save(cred)

    loaded = store.load()
    assert loaded is not None
    assert loaded.cred == CRED
    assert loaded.token == TOKEN
    assert loaded.user_id == "u-9"


def test_load_returns_none_when_missing_or_corrupt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.load() is None

    _write_raw(tmp_path, "not json at all")
    assert store.load() is None


def test_clear_returns_to_missing_and_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(SklandCredential(cred=CRED, token=TOKEN))
    store.clear()
    store.clear()  # 重复调用必须安全

    assert store.status().state is CredentialState.MISSING
    assert store.load() is None


def test_refuses_to_overwrite_a_corrupt_file(tmp_path: Path) -> None:
    """覆盖坏档会掩掉「它曾经坏过」这个事实，所以宁可拒绝。"""
    _write_raw(tmp_path, "definitely not json")

    with pytest.raises(CorruptedStoreError):
        _store(tmp_path).save(SklandCredential(cred=CRED, token=TOKEN))


def test_save_rejects_a_non_credential(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _store(tmp_path).save("not a credential")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cred", "token"),
    [("", TOKEN), (CRED, "")],
)
def test_credential_rejects_empty_parts(cred: str, token: str) -> None:
    with pytest.raises(ValueError):
        SklandCredential(cred=cred, token=token)


# --- 脱敏（安全相关，必须钉住） ---------------------------------------------


def test_masked_never_leaks_any_secret_character(tmp_path: Path) -> None:
    """凭据一旦进了日志就等于泄露。这里断言脱敏结果里**一个真字符都不含**。"""
    credential = SklandCredential(cred=CRED, token=TOKEN, user_id="u-1")
    masked = credential.masked()

    rendered = json.dumps(masked, ensure_ascii=False)
    assert CRED not in rendered
    assert TOKEN not in rendered
    # 连片段都不许出现：取两段有代表性的子串来试
    for secret in (CRED, TOKEN):
        for start in range(0, len(secret) - 4, 4):
            assert secret[start : start + 4] not in rendered


def test_masked_reports_lengths_which_is_what_debugging_needs() -> None:
    masked = SklandCredential(cred=CRED, token=TOKEN).masked()
    assert str(len(CRED)) in masked[CRED_FIELD]
    assert str(len(TOKEN)) in masked[TOKEN_FIELD]
    assert masked[USER_ID_FIELD] == "未知"


# --- 展示文案 ---------------------------------------------------------------


def test_describe_covers_all_three_states(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert "未授权" in store.status().describe()

    store.save(SklandCredential(cred=CRED, token=TOKEN, user_id="u-1"))
    assert "已授权" in store.status().describe()
    assert "u-1" in store.status().describe()

    _write_raw(tmp_path, "broken")
    assert "损坏" in store.status().describe()


# --- 路径 -------------------------------------------------------------------


def test_path_is_fixed_and_inside_the_given_directory(tmp_path: Path) -> None:
    """文件名固定、由目录拼出——不由任何外部输入参与，路径穿越没有入口。"""
    store = _store(tmp_path)
    assert store.path.name == CREDENTIALS_FILENAME
    assert store.path.parent == tmp_path
    # 未授权时**不该**顺手把文件建出来（读不该有副作用）
    assert not store.path.exists()
