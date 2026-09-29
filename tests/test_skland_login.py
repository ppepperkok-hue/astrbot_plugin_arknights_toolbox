"""`modules/skland/login.py` 的测试：响应解析与「不猜」的判据。

重点在**三条设计边界**上，它们比函数本身重要：

1. **「已扫码」按结构判定**（出现非空 `scanCode`），不按某个魔数 `status`。
2. **没证据就不判死**：轮询只认「拿到 `scanCode`」为成功、只认「明确的失效信号」为终态，
   **其余一律继续等**。这条是 2026-09-29 线上事故换来的——真实的等待态 `status=101`
   当时没有实测过，被判成失败，于是轮询在用户点确认之前就中止了。
3. **通行证侧用 `status`、森空岛侧用 `code`**，两套协议不能混。
"""

from __future__ import annotations

import base64

import pytest

from modules.skland import login

# --- 第一步：取二维码票据 ---------------------------------------------------


def test_parse_scan_ticket_reads_id_and_url() -> None:
    ticket = login.parse_scan_ticket(
        {"status": 0, "data": {"scanId": "S-1", "scanUrl": "hypergryph://scan_login?scanId=S-1"}}
    )
    assert ticket.scan_id == "S-1"
    assert ticket.scan_url.startswith("hypergryph://scan_login")


@pytest.mark.parametrize(
    "payload",
    [
        {"status": 0},
        {"status": 0, "data": {}},
        {"status": 0, "data": {"scanId": "", "scanUrl": "x"}},
        {"status": 0, "data": {"scanId": "S", "scanUrl": ""}},
        "not-an-object",
    ],
)
def test_parse_scan_ticket_rejects_incomplete_payloads(payload: object) -> None:
    with pytest.raises(login.ScanLoginError):
        login.parse_scan_ticket(payload)


def test_scan_ticket_error_never_echoes_the_payload() -> None:
    """错误消息只能给**字段名**，不能把响应体整段回显（未来可能带凭据）。"""
    with pytest.raises(login.ScanLoginError) as info:
        login.parse_scan_ticket({"status": 0, "data": {"secret": "SHOULD-NOT-APPEAR"}})
    assert "SHOULD-NOT-APPEAR" not in str(info.value)


# --- 第二步：轮询状态的判据 -------------------------------------------------


def test_waiting_when_no_scan_code_and_status_is_the_measured_not_scanned_value() -> None:
    reading = login.interpret_scan_status({"msg": "未扫码", "status": 100, "type": "A"})
    assert reading.state is login.ScanState.WAITING
    assert reading.raw_status == 100
    assert "未扫码" in reading.message


def test_scanned_is_decided_by_presence_of_scan_code_not_by_a_magic_number() -> None:
    """**成功判据是结构，不是数字。** 换任何一个 status 值都该照结构走。"""
    for status in (0, 1, 2, 200, 999):
        reading = login.interpret_scan_status({"status": status, "data": {"scanCode": "QUJD"}})
        assert reading.state is login.ScanState.SCANNED, status
        assert reading.scan_code == "QUJD"


def test_scan_code_is_also_accepted_at_the_top_level() -> None:
    reading = login.interpret_scan_status({"status": 0, "scanCode": "QUJD"})
    assert reading.state is login.ScanState.SCANNED


def test_unknown_status_keeps_waiting_instead_of_being_judged_dead() -> None:
    """不认识的 `status` **继续等**，不判死——但原始信息要原样留着供定位。

    这条**以前断言的是 FAILED**，2026-09-29 的线上事故把那个设计推翻了：真实存在的
    等待态 `status=101`（已扫码待确认）当时没有被实测过，于是落进「未知 → 失败」那一支，
    **轮询在用户点确认之前就中止了**。两者的代价不对称——多等一会儿只是慢，
    误判会掐死一次合法授权。
    """
    reading = login.interpret_scan_status({"status": 5, "msg": "说不清"})
    assert reading.state is login.ScanState.WAITING
    assert reading.raw_status == 5
    assert reading.message == "说不清", "原始 msg 要留着，否则排查时又变成'什么都没有'"


def test_the_measured_awaiting_confirmation_status_is_its_own_state() -> None:
    """**实测现场**：`status=101`「已扫码待确认」= 已扫到、正在等手机确认。

    它与「还没扫」分开，因为**该对用户说的话不同**：这时候他要做的是看手机点确认，
    而不是"再扫一次"。线上那次失败就是在这个状态下说了「失败」。
    """
    reading = login.interpret_scan_status({"msg": "已扫码待确认", "status": 101, "type": "A"})
    assert reading.state is login.ScanState.AWAITING_CONFIRMATION
    assert reading.raw_status == 101
    assert reading.scan_code == ""
    assert "确认" in reading.message


@pytest.mark.parametrize("status", [0, 1, 2, 3, 7, 99, 100, 103, 500])
def test_no_status_without_a_scan_code_is_treated_as_terminal(status: int) -> None:
    """**没有任何实测证据支持"轮询终态失败"的取值**，所以一个都不判死。

    刻意包含 `0`：参考实现管它叫"已扫待确认"，但 **`0` 在本协议里是通用成功码**
    （第 1 步 `gen_scan/login` 的成功响应就是它），所以代码不给它特殊含义。
    这条断言的作用是**挡住"再加一个魔法数字"**这种改法。
    """
    reading = login.interpret_scan_status({"status": status, "msg": "x"})
    assert reading.state in (
        login.ScanState.WAITING,
        login.ScanState.AWAITING_CONFIRMATION,
    ), f"status={status} 不该被当成终态"


def test_expiry_wording_is_terminal_even_under_an_unmeasured_status() -> None:
    """失效的**消息措辞**是第二条独立线索：状态值域我们没有权威表，消息是服务端写的。

    命中它只是"提前告诉用户该重来"，没命中就继续等——两个方向都不会掐死合法授权。
    """
    reading = login.interpret_scan_status({"status": 7, "msg": "二维码已过期，请重新获取"})
    assert reading.state is login.ScanState.EXPIRED
    assert "过期" in reading.message


def test_expiry_state_carries_a_resend_action() -> None:
    """终态必须给出**可操作**的措辞，而不是"失败了"。"""
    reading = login.interpret_scan_status({"status": 102, "msg": "二维码已失效"})
    assert reading.state is login.ScanState.EXPIRED
    action = login.suggest_action(payload={"status": 102, "msg": "二维码已失效"})
    assert "/ak skland login" in action


def test_missing_status_is_treated_as_still_waiting() -> None:
    """缺 status 字段时**不要**当失败——把「不知道」当成「还没好」是安全的。"""
    reading = login.interpret_scan_status({"msg": "未扫码"})
    assert reading.state is login.ScanState.WAITING


@pytest.mark.parametrize("payload", ["nope", 42, None])
def test_polling_rejects_non_object_payloads(payload: object) -> None:
    with pytest.raises(login.ScanLoginError):
        login.interpret_scan_status(payload)


# --- 第三到五步：字段名用候选键读，取不到就抛 -------------------------------


@pytest.mark.parametrize("key", ["token", "accessToken", "access_token"])
def test_passport_token_accepts_several_key_spellings(key: str) -> None:
    assert login.parse_token_by_scan_code({"status": 0, "data": {key: "T"}}) == "T"


def test_passport_token_missing_raises_instead_of_returning_empty() -> None:
    """返回空串等于「假装拿到了」，下游会带着空 token 去发请求。"""
    with pytest.raises(login.ScanLoginError):
        login.parse_token_by_scan_code({"status": 0, "data": {"token": ""}})


@pytest.mark.parametrize("key", ["code", "grantCode", "grant_code"])
def test_grant_code_accepts_several_key_spellings(key: str) -> None:
    assert login.parse_grant_code({"status": 0, "data": {key: "G"}}) == "G"


def test_grant_code_missing_raises() -> None:
    with pytest.raises(login.ScanLoginError):
        login.parse_grant_code({"status": 0, "data": {}})


def test_credential_requires_both_halves() -> None:
    assert login.parse_credential({"code": 0, "data": {"cred": "C", "token": "T"}}) == ("C", "T")
    for payload in (
        {"code": 0, "data": {"cred": "C"}},
        {"code": 0, "data": {"token": "T"}},
        {"code": 0, "data": {"cred": "", "token": "T"}},
    ):
        with pytest.raises(login.ScanLoginError):
            login.parse_credential(payload)


# --- scanCode 的形态 --------------------------------------------------------


def test_decode_scan_code_strips_a_data_url_prefix() -> None:
    raw = base64.b64encode(b"payload").decode()
    assert login.decode_scan_code(f"data:text/plain;base64,{raw}") == raw
    assert login.decode_scan_code(f"  {raw}  ") == raw


def test_decode_scan_code_rejects_garbage() -> None:
    """实测服务端验证器要求 base64；本地先挡一道，免得白跑一趟网络。"""
    with pytest.raises(login.ScanLoginError, match="base64"):
        login.decode_scan_code("not base64 ***")


def test_decode_scan_code_rejects_empty() -> None:
    with pytest.raises(login.ScanLoginError):
        login.decode_scan_code("   ")


# --- 请求体形状（字段名错一处就是 400/10001） -------------------------------


def test_request_payload_shapes_match_the_measured_contract() -> None:
    raw = base64.b64encode(b"scan").decode()

    assert login.token_request(raw) == {"scanCode": raw}
    assert login.grant_request("APP", "PASSPORT") == {
        "appCode": "APP",
        "token": "PASSPORT",
        "type": login.GRANT_TYPE_PERSONAL,
    }
    # kind 缺失时实测返回 400 / code=10001，所以这个字段是硬要求
    assert login.cred_request("GRANT") == {"code": "GRANT", "kind": login.CRED_KIND_SKLAND}
    assert login.CRED_KIND_SKLAND == 1
    assert login.GRANT_TYPE_PERSONAL == 0
