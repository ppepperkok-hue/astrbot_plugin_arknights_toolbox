"""扫码授权的流程层（**纯逻辑**：响应解析与状态判定；网络由 `api.SklandClient` 负责）。

## 依据与「不猜」的边界

链路来自 `docs/project-plan/07-skland-api.md` §1 的**实测**五步：

```
POST {hyper}/general/v1/gen_scan/login      {appCode}          → scanId + scanUrl
GET  {hyper}/general/v1/scan_status         ?scanId=<id>       → {"msg":"未扫码","status":100}
POST {hyper}/user/auth/v1/token_by_scan_code {scanCode: b64}   → 通行证 token
POST {hyper}/user/oauth2/v2/grant           {appCode, token, type:0} → 授权 code
POST {zonai}/user/auth/generate_cred_by_code {code, kind:1}     → cred + token
```

⚠️ **「已扫码」的判据我刻意不写数字。** 实测只拿到「未扫码 = `status: 100`」这一个取值，
**成功时的 status 是多少没有实测过**。所以这里按**结构**判定：响应里出现非空的
`scanCode` 才算扫到了；其余取值一律当作「还没好」，并把原始 `status`/`msg` 原样带出来
给用户看。编一个「status == 0 就是成功」是照文档猜，本项目在 `FormData` 那条 API 事实上
栽过一次，不重复。

⚠️ 通行证侧（`as.hypergryph.com`）用 `status` 字段而不是 `code`，两个侧的判据不同——
本文件按各自的响应形态处理。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CRED_KIND_SKLAND",
    "GRANT_TYPE_PERSONAL",
    "ScanLoginError",
    "ScanState",
    "ScanStatusReading",
    "ScanTicket",
    "cred_request",
    "decode_scan_code",
    "grant_request",
    "interpret_scan_status",
    "parse_credential",
    "parse_grant_code",
    "parse_scan_ticket",
    "parse_token_by_scan_code",
    "token_request",
]

#: `oauth2/v2/grant` 的 `type`：实测参考实现用 0（个人）。
GRANT_TYPE_PERSONAL: Final[int] = 0

#: `generate_cred_by_code` 的 `kind`：实测缺它会 400（`code=10001`）。
CRED_KIND_SKLAND: Final[int] = 1

#: `scan_status` 实测「未扫码」的取值。**只用于显示，不作为成功判据。**
_STATUS_NOT_SCANNED: Final[int] = 100


class ScanLoginError(Exception):
    """扫码链路某一环失败。消息里**不带凭据**（凭据只在成功时才产生）。"""


class ScanState(StrEnum):
    """轮询状态的三种结局。"""

    WAITING = "waiting"
    """用户还没扫（或还没在手机上点确认）。"""

    SCANNED = "scanned"
    """拿到了 `scanCode`，可以进入换 token 那一步。"""

    FAILED = "failed"
    """服务端明确说了失败（响应里没有 `data`/`scanCode` 但有错误消息）。"""


@dataclass(frozen=True)
class ScanTicket:
    """第一步拿到的二维码票据。"""

    scan_id: str
    scan_url: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ScanStatusReading:
    """一次轮询的解读结果。"""

    state: ScanState
    message: str
    scan_code: str = ""
    raw_status: int | None = None


def _data_of(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ScanLoginError(f"响应不是对象：{type(payload).__name__}")
    data = payload.get("data")
    if isinstance(data, Mapping):
        return data
    raise ScanLoginError(f"响应里没有 data 对象：{_short(payload)}")


def _short(payload: Any) -> str:
    """给错误消息用的短摘要。**绝不包含凭据**——这些响应里还没有凭据。"""
    if isinstance(payload, Mapping):
        keys = ", ".join(sorted(str(k) for k in payload)[:8])
        return f"字段有 {keys}"
    return str(payload)[:80]


def _non_empty_str(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def parse_scan_ticket(payload: Any) -> ScanTicket:
    """解析第一步的响应，取出 `scanId` 与 `scanUrl`。"""
    data = _data_of(payload)
    scan_id = _non_empty_str(data.get("scanId"))
    scan_url = _non_empty_str(data.get("scanUrl"))
    if not scan_id or not scan_url:
        raise ScanLoginError(f"响应里缺少 scanId 或 scanUrl（{_short(payload)}）")
    return ScanTicket(scan_id=scan_id, scan_url=scan_url, raw=data)


def interpret_scan_status(payload: Any) -> ScanStatusReading:
    """解读一次轮询。

    **成功判据是「出现了非空的 `scanCode`」**，不是某个数字——见模块 docstring。
    """
    if not isinstance(payload, Mapping):
        raise ScanLoginError(f"轮询响应不是对象：{type(payload).__name__}")

    raw_status = payload.get("status")
    status_number = (
        raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
    )
    message = ""
    for key in ("msg", "message"):
        message = _non_empty_str(payload.get(key))
        if message:
            break

    data = payload.get("data")
    scan_code = ""
    if isinstance(data, Mapping):
        scan_code = _non_empty_str(data.get("scanCode"))
    if not scan_code:
        scan_code = _non_empty_str(payload.get("scanCode"))

    if scan_code:
        return ScanStatusReading(ScanState.SCANNED, "已扫码", scan_code, status_number)

    if status_number in (None, _STATUS_NOT_SCANNED):
        # 未扫码是**正常中间态**，不是失败。
        return ScanStatusReading(ScanState.WAITING, message or "还没扫码", "", status_number)

    # 既没有 scanCode、状态又不是已知的「未扫码」——如实报失败并把原文带出来，
    # 不猜它是什么意思。
    return ScanStatusReading(
        ScanState.FAILED,
        message or f"扫码状态异常（status={status_number}）",
        "",
        status_number,
    )


def parse_token_by_scan_code(payload: Any) -> str:
    """解析通行证 token。

    `UNVERIFIED`：成功响应体的字段名未真机验证。按**多个候选键**读取并逐一校验，
    全都取不到就抛错——**不返回空串假装成功**。
    """
    data = _data_of(payload)
    for key in ("token", "accessToken", "access_token"):
        token = _non_empty_str(data.get(key))
        if token:
            return token
    raise ScanLoginError(f"响应里没有 token（{_short(payload)}）")


def parse_grant_code(payload: Any) -> str:
    """解析 `oauth2/v2/grant` 返回的授权 code。

    `UNVERIFIED`：字段名未真机验证，同样按候选键读取。
    """
    data = _data_of(payload)
    for key in ("code", "grantCode", "grant_code"):
        code = _non_empty_str(data.get(key))
        if code:
            return code
    raise ScanLoginError(f"响应里没有授权 code（{_short(payload)}）")


def parse_credential(payload: Any) -> tuple[str, str]:
    """解析 `generate_cred_by_code` 返回的 `(cred, token)`。

    `UNVERIFIED`：字段名未真机验证。两者缺一不可——只拿到一个不算授权成功。
    """
    data = _data_of(payload)
    cred = _non_empty_str(data.get("cred"))
    token = _non_empty_str(data.get("token"))
    if not cred or not token:
        raise ScanLoginError(f"响应里缺少 cred 或 token（{_short(payload)}）")
    return cred, token


def decode_scan_code(scanned_value: str) -> str:
    """扫码得到的值可能是 data URL 或裸 base64，统一成裸 base64。

    实测第 3 步的验证器要求 `scanCode` 必须是 base64，所以这里只做**去掉 data URL 前缀**
    与空白，不额外编码——把已经正确的值再编码一次就会失败。
    """
    value = scanned_value.strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    if not value:
        raise ScanLoginError("扫到的 scanCode 是空的")
    try:
        base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 - 统一转成领域错误
        raise ScanLoginError(f"scanCode 不是合法 base64：{type(exc).__name__}") from exc
    return value


def token_request(scan_code: str) -> dict[str, Any]:
    """第 3 步的请求体：`{scanCode: <base64>}`。"""
    return {"scanCode": decode_scan_code(scan_code)}


def grant_request(app_code: str, passport_token: str) -> dict[str, Any]:
    """第 4 步的请求体：`{appCode, token, type}`。"""
    return {"appCode": app_code, "token": passport_token, "type": GRANT_TYPE_PERSONAL}


def cred_request(grant_code: str) -> dict[str, Any]:
    """第 5 步的请求体：`{code, kind}`。实测缺 `kind` 会 400（`code=10001`）。"""
    return {"code": grant_code, "kind": CRED_KIND_SKLAND}
