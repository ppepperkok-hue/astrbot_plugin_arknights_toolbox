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

## 可诊断性（2026-09-29 线上失败促成的改造）

用户扫码后收到的是「**授权流程失败（没有拿到凭据）：响应里没有 data 对象：字段有 msg**」，
而**服务端日志里什么都没有**——既不知道是哪一步，也不知道服务端到底回了什么。
这条报错的判据是对的（缺 `data` 确实算失败），**缺的是定位信息**。

现在的做法：

1. **每个解析器都知道自己是第几步**，失败时抛出的 `ScanLoginError` 带 `step`。
2. **失败一律走 :func:`describe_response`** 渲染摘要：步骤、HTTP 状态、判据字段
   （顶层 `code` / `status` / `type`）、服务端消息，以及 `data` 里**出现了哪些键**。
3. **凭据绝不进日志**：`scanCode` / `token` / `cred` / 授权 `code`（以及 `scanId`、
   `scanUrl` 这类登录票据）只输出 `<已隐去 len=N sha256=前8位>`；`data` 里**非凭据键
   连值都不打印**（只给类型与长度）；服务端消息再过一道 :func:`scrub` 抹掉长 token
   形状的串。**顶层 `code` 例外**——它是状态码（判据就是它），必须记录；授权 `code`
   只出现在 `data.code`，由位置区分。
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

__all__ = [
    "CRED_KIND_SKLAND",
    "GRANT_TYPE_PERSONAL",
    "STEP_CRED",
    "STEP_GRANT",
    "STEP_POLL",
    "STEP_SCAN_TICKET",
    "STEP_TOKEN",
    "ScanLoginError",
    "ScanState",
    "ScanStatusReading",
    "ScanTicket",
    "Step",
    "cred_request",
    "decode_scan_code",
    "describe_response",
    "grant_request",
    "interpret_scan_status",
    "parse_credential",
    "parse_grant_code",
    "parse_scan_ticket",
    "parse_token_by_scan_code",
    "redact_marker",
    "scrub",
    "step_failure",
    "suggest_action",
    "token_request",
]

#: `oauth2/v2/grant` 的 `type`：实测参考实现用 0（个人）。
GRANT_TYPE_PERSONAL: Final[int] = 0

#: `generate_cred_by_code` 的 `kind`：实测缺它会 400（`code=10001`）。
CRED_KIND_SKLAND: Final[int] = 1

#: `scan_status` 实测「未扫码」的取值。**只用于显示，不作为成功判据。**
_STATUS_NOT_SCANNED: Final[int] = 100

#: `scan_status` 的「二维码已失效」取值。
#: `UNVERIFIED`：来自参考实现（`morizero_main.py:525` 判 `status == 102` 为
#: 「二维码已失效（超时未确认）」），**我们没有独立实测**。它只影响**提示措辞**
#: （比「没有拿到凭据」有用），不参与成功判据——成功仍然只看有没有 `scanCode`。
_STATUS_QR_EXPIRED: Final[int] = 102

#: `scan_status` 的「已扫码但还没在手机上确认」。同样来自参考实现
#: （它在 `status == 0` 但拿不到 `scanCode` 时继续等）。**这是正常中间态，不是失败。**
_STATUS_SCANNED_PENDING: Final[int] = 0


@dataclass(frozen=True)
class Step:
    """链路里的一步。带上序号是为了让日志/回执能直接说「第几步」。"""

    index: int
    title: str

    @property
    def label(self) -> str:
        return f"第 {self.index} 步（{self.title}）"


#: 五步的标识。解析器与装配层都用这些常量，**不写裸字符串**。
STEP_SCAN_TICKET: Final[Step] = Step(1, "取二维码票据")
STEP_POLL: Final[Step] = Step(2, "轮询扫码状态")
STEP_TOKEN: Final[Step] = Step(3, "用 scanCode 换取通行证 token")
STEP_GRANT: Final[Step] = Step(4, "用通行证 token 换取授权 code")
STEP_CRED: Final[Step] = Step(5, "用授权 code 换取森空岛凭据")


# --- 脱敏（本文件的硬红线）--------------------------------------------------

#: 凭据类键名（**小写比较**）。出现在任何一层都不落原文。
#:
#: 为什么 `scanid` / `scanurl` 也在内：那两样是**登录票据**——谁拿到它们就能顶替
#: 用户走完扫码链路，性质与 `scanCode` 同级。
#:
#: ⚠️ `code` 在这里指**授权 code**（`data.code`，拿去换 cred 的那个）。顶层 `code`
#: 是**状态码**，必须记录（判据就是它）。两者的区别由**位置**决定，见
#: :func:`describe_response`。
_CREDENTIAL_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "scancode",
        "scanid",
        "scanurl",
        "token",
        "accesstoken",
        "access_token",
        "cred",
        "code",
        "grantcode",
        "grant_code",
    }
)

#: 看起来像 token 的长串（base64/hex/URL-safe）。服务端消息里若夹带了凭据，靠它兜底。
_LONG_RUN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/=_\-]{16,}")


class ScanLoginError(Exception):
    """扫码链路某一环失败。消息里**不带凭据**（凭据只在成功时才产生）。

    Attributes:
        step: 失败发生在哪一步（`None` 表示尚未归到具体步骤）。
        http_status: 该步的 HTTP 状态码（能拿到就有）。
        detail: 已经渲染好的响应摘要（**保证不含凭据**），可直接进日志。
        reason: 失败原因本身（未加步骤前缀）。装配层要在外层补上步骤信息时用它，
            否则会拼出「第 3 步 失败：第 3 步 失败：…」这种套娃。
        action: 给用户的**可执行**建议，由 :func:`suggest_action` 按同一份证据生成。
            **刻意在抛出时就计算好**——这样装配层不需要把响应体带出去，
            也就不存在"某条路径忘了脱敏"的机会。
    """

    def __init__(
        self,
        message: str,
        *,
        step: Step | None = None,
        http_status: int | None = None,
        detail: str = "",
        reason: str = "",
        action: str = "",
    ) -> None:
        super().__init__(message)
        self.step = step
        self.http_status = http_status
        self.detail = detail
        self.reason = reason
        self.action = action


def redact_marker(value: Any) -> str:
    """凭据值的可诊断替身：**长度 + 不可逆摘要**，两者都不泄露内容。

    为什么要留摘要：重试一次时如果摘要变了，说明服务端给的是**新**票据（而不是
    同一张被复用了）——这类判断以前完全做不了。
    """
    if not isinstance(value, str):
        return f"<非字符串 {type(value).__name__}>"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"<已隐去 len={len(value)} sha256={digest}>"


def scrub(text: str) -> str:
    """抹掉文本里 token 形状的长串，并截断。

    服务端消息理论上是给人看的，但**不能假设它不回显我们发过去的 scanCode**——
    第 3 步的验证器报错就带着请求上下文。这道兜底宁可多抹。
    """
    return _LONG_RUN.sub(lambda m: f"<已隐去 len={len(m.group(0))}>", text)[:120]


def _type_label(value: Any) -> str:
    """`data` 里**非凭据**字段的呈现：只给类型与长度，**不给值**。

    比"什么都不打"多出来的信息是形状（是空串？是空列表？是对象？），
    这恰恰是排查"字段名猜错了"时最需要的那一条。
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"bool={value}"
    if isinstance(value, (int, float)):
        return type(value).__name__
    if isinstance(value, str):
        return f"str len={len(value)}"
    if isinstance(value, Mapping):
        return "对象{" + "、".join(sorted(str(k) for k in value)[:6]) + "}"
    if isinstance(value, (list, tuple)):
        return f"列表 len={len(value)}"
    return type(value).__name__


def _data_field(key: Any, value: Any) -> str:
    name = str(key)
    if name.lower() in _CREDENTIAL_KEY_NAMES:
        return f"{name}={redact_marker(value)}"
    return f"{name}=<{_type_label(value)}>"


def _scalar(value: Any) -> str:
    """顶层判据字段的呈现。数字直接给，字符串过一遍 :func:`scrub`。"""
    if isinstance(value, (int, float)):  # bool 也是 int，会渲染成 True/False，够用
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        if len(value) > 60:
            return f"<str len={len(value)}>"
        return f"'{scrub(value)}'"
    if isinstance(value, Mapping):
        return "对象{" + "、".join(sorted(str(k) for k in value)[:6]) + "}"
    if isinstance(value, (list, tuple)):
        return f"列表 len={len(value)}"
    return type(value).__name__


def describe_response(payload: Any = None, *, http_status: int | None = None) -> str:
    """把一次响应渲染成**可进日志的摘要**（凭据零外泄）。

    输出包含：HTTP 状态、顶层判据字段（`code` / `status` / `type`）、服务端消息
    （`msg` / `message` / `error`）、以及 `data` 里出现了哪些键。

    **不包含**：任何凭据值（`data` 里的凭据键只给长度与摘要）、`data` 里非凭据键的值。
    """
    parts: list[str] = []
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    if payload is None:
        parts.append("没有响应体")
        return "；".join(parts)
    if not isinstance(payload, Mapping):
        parts.append(f"响应不是对象（{type(payload).__name__}）")
        return "；".join(parts)

    for key in ("code", "status", "type"):
        if key in payload:
            parts.append(f"{key}={_scalar(payload[key])}")
    for key in ("msg", "message", "error"):
        text = payload.get(key)
        if isinstance(text, str) and text:
            parts.append(f"{key}='{scrub(text)}'")
            break

    data = payload.get("data")
    if isinstance(data, Mapping):
        fields = "、".join(_data_field(k, v) for k, v in sorted(data.items(), key=_key_text)[:12])
        parts.append(f"data{{{fields}}}")
    elif "data" not in payload:
        parts.append("没有 data 字段")
    else:
        parts.append(f"data 不是对象（{type(data).__name__}）")
    return "；".join(parts)


def _key_text(item: tuple[Any, Any]) -> str:
    return str(item[0])


def step_failure(
    step: Step,
    reason: str,
    payload: Any = None,
    *,
    http_status: int | None = None,
) -> ScanLoginError:
    """构造一个**带步骤与响应摘要**的失败。

    回执与日志都用它的 `str()`，所以这一段文案同时要满足两个读者：用户想知道
    「我该做什么」，我们想知道「哪一步、服务端回了什么」。
    """
    detail = describe_response(payload, http_status=http_status)
    message = f"{step.label} 失败：{reason}"
    if detail:
        message += f"｜服务端：{detail}"
    return ScanLoginError(
        message,
        step=step,
        http_status=http_status,
        detail=detail,
        reason=reason,
        action=suggest_action(step=step, payload=payload, http_status=http_status),
    )


def _int_field(payload: Any, key: str) -> int | None:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _text_field(payload: Any) -> str:
    if isinstance(payload, Mapping):
        for key in ("msg", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def suggest_action(
    *,
    step: Step | None = None,
    payload: Any = None,
    http_status: int | None = None,  # noqa: ARG001 - 保留参数，措辞可能按状态分化
) -> str:
    """按**能观测到的证据**给一句用户真能做的动作。

    「没有拿到凭据」对用户等于没说——他不知道该重扫、该等、还是该放弃。这里的原则是
    **只从响应里确实读到的字段下判断**，读不到就给通用重试建议，不编具体原因。
    """
    status = _int_field(payload, "status")
    message = _text_field(payload)
    lowered = message.lower()
    if (
        status == _STATUS_QR_EXPIRED
        or "过期" in message
        or "失效" in message
        or "expired" in lowered
    ):
        return (
            "二维码已过期、或这次扫码的服务端登录态已作废——重新发一次 /ak skland login，再扫一次。"
        )
    if step in (STEP_TOKEN, STEP_GRANT):
        return (
            "二维码可能已被用过（同一个码只能换一次），或在手机上确认得太慢。"
            "重新发一次 /ak skland login 即可；若反复失败，请把上面这句『第 N 步…服务端：…』原样发我。"
        )
    return "稍后可再试一次；若重复失败，请把上面这句『第 N 步…服务端：…』原样发我，那就是定位所需的全部信息。"


class ScanState(StrEnum):
    """轮询状态的四种结局。"""

    WAITING = "waiting"
    """用户还没扫（或已经扫了但还没在手机上点确认）。"""

    SCANNED = "scanned"
    """拿到了 `scanCode`，可以进入换 token 那一步。"""

    EXPIRED = "expired"
    """二维码失效了（`status == 102`）——**这不是错误，是"该重来一次"**。

    `UNVERIFIED`：判据来自参考实现，非实测。它只影响**措辞**（说"过期了，重扫"
    比说"失败了"有用），所以即使这个数字将来不对，代价也只是措辞不够准。
    """

    FAILED = "failed"
    """服务端明确说了失败（既没有 `scanCode`，状态又不是已知的正常中间态）。"""


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


def _data_of(payload: Any, step: Step) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise step_failure(step, f"响应不是对象（{type(payload).__name__}）", payload)
    data = payload.get("data")
    if isinstance(data, Mapping):
        return data
    raise step_failure(step, "响应里没有 data 对象", payload)


def _non_empty_str(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def parse_scan_ticket(payload: Any) -> ScanTicket:
    """解析第一步的响应，取出 `scanId` 与 `scanUrl`。"""
    data = _data_of(payload, STEP_SCAN_TICKET)
    scan_id = _non_empty_str(data.get("scanId"))
    scan_url = _non_empty_str(data.get("scanUrl"))
    if not scan_id or not scan_url:
        raise step_failure(STEP_SCAN_TICKET, "响应里缺少 scanId 或 scanUrl", payload)
    return ScanTicket(scan_id=scan_id, scan_url=scan_url, raw=data)


def interpret_scan_status(payload: Any) -> ScanStatusReading:
    """解读一次轮询。

    **成功判据是「出现了非空的 `scanCode`」**，不是某个数字——见模块 docstring。

    两种正常中间态**不算失败**（此前会误报为「扫码失败」，是个会让用户以为
    二维码坏了而放弃的假故障）：

    - `status == 100`：还没扫（实测值）。
    - `status == 0` 但还没有 `scanCode`：**已经扫了、还在手机上等确认**
      （来自参考实现；我们此前把它判成 FAILED）。
    """
    if not isinstance(payload, Mapping):
        raise step_failure(STEP_POLL, f"轮询响应不是对象（{type(payload).__name__}）", payload)

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

    if status_number == _STATUS_QR_EXPIRED:
        return ScanStatusReading(
            ScanState.EXPIRED,
            message or "二维码已失效（超时未确认）",
            "",
            status_number,
        )

    if status_number in (None, _STATUS_NOT_SCANNED, _STATUS_SCANNED_PENDING):
        # 未扫码、或「扫了但还没确认」都是**正常中间态**，不是失败。
        detail = "已扫码，等待手机确认" if status_number == _STATUS_SCANNED_PENDING else "还没扫码"
        return ScanStatusReading(ScanState.WAITING, message or detail, "", status_number)

    # 既没有 scanCode、状态又不是已知的正常中间态——如实报失败并把原文带出来，
    # 不猜它是什么意思。
    return ScanStatusReading(
        ScanState.FAILED,
        message or f"扫码状态异常（status={status_number}）",
        "",
        status_number,
    )


def parse_token_by_scan_code(payload: Any) -> str:
    """解析通行证 token（第 3 步）。

    `UNVERIFIED`：成功响应体的字段名未真机验证。按**多个候选键**读取并逐一校验，
    全都取不到就抛错——**不返回空串假装成功**。
    """
    data = _data_of(payload, STEP_TOKEN)
    for key in ("token", "accessToken", "access_token"):
        token = _non_empty_str(data.get(key))
        if token:
            return token
    raise step_failure(STEP_TOKEN, "响应里没有 token", payload)


def parse_grant_code(payload: Any) -> str:
    """解析 `oauth2/v2/grant` 返回的授权 code（第 4 步）。

    `UNVERIFIED`：字段名未真机验证，同样按候选键读取。
    """
    data = _data_of(payload, STEP_GRANT)
    for key in ("code", "grantCode", "grant_code"):
        code = _non_empty_str(data.get(key))
        if code:
            return code
    raise step_failure(STEP_GRANT, "响应里没有授权 code", payload)


def parse_credential(payload: Any) -> tuple[str, str]:
    """解析 `generate_cred_by_code` 返回的 `(cred, token)`（第 5 步）。

    `UNVERIFIED`：字段名未真机验证。两者缺一不可——只拿到一个不算授权成功。
    """
    data = _data_of(payload, STEP_CRED)
    cred = _non_empty_str(data.get("cred"))
    token = _non_empty_str(data.get("token"))
    if not cred or not token:
        raise step_failure(STEP_CRED, "响应里缺少 cred 或 token", payload)
    return cred, token


def decode_scan_code(scanned_value: str) -> str:
    """扫码得到的值可能是 data URL 或裸 base64，统一成裸 base64。

    实测第 3 步的验证器要求 `scanCode` 必须是 base64，所以这里只做**去掉 data URL 前缀**
    与空白，不额外编码——把已经正确的值再编码一次就会失败。

    **报错时绝不回显 `scanCode`**（它是凭据），只说长度与形状。
    """
    value = scanned_value.strip()
    if value.startswith("data:"):
        _, _, value = value.partition(",")
    if not value:
        raise step_failure(STEP_TOKEN, "扫到的 scanCode 是空的")
    try:
        base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001 - 统一转成领域错误
        raise step_failure(
            STEP_TOKEN,
            f"扫到的 scanCode 不是合法 base64（{type(exc).__name__}，"
            f"{redact_marker(scanned_value)}）",
        ) from exc
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
