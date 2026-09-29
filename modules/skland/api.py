"""森空岛请求层（**纯标准库**）：签名、错误码语义、限流。

## 依据

全部来自 `docs/project-plan/07-skland-api.md` 的**实测**记录，不是社区文档——
那份文档的第一条结论就是「社区文档里的扫码链路已全面过时」。本文件里凡是**未实测**的
地方都显式标注 `UNVERIFIED`，不装作已经验过。

## 三条来自实测的硬约束

1. **判据一律看响应体的 `code`，不看 HTTP 状态码。** 实测同一个 `10001` 出现过
   HTTP 400 与 HTTP 500——把 5xx 当成「服务器故障、重试」会让客户端**永久重试一个
   永久性错误**。
2. **健康检查必须打真实取数接口**（这里用 `game/player/binding`）。**绝不能用
   `auth/refresh`**：实测它对**无效凭据**也返回 `code:0` 和一个 token，
   拿它做健康检查会得到「一切正常」然后每个取数接口都 10002。
3. **带我们自己的 User-Agent。** 实测五种 UA（含空 UA）结果完全一致，
   所以**不需要伪装官方客户端**——这条边界守得住。

## 不做的

- 不实现 `dId` 设备指纹（那要打第三方指纹服务并嵌死伪造数据块）。
- 不做任何绕过、模拟客户端、反检测。
- 签到以外的写操作一概不做。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

__all__ = [
    "APP_CODE_SKLAND",
    "HONEST_USER_AGENT",
    "HYPERGRYPH_BASE",
    "ZONAI_BASE",
    "CachePolicy",
    "HttpResponse",
    "SklandClient",
    "SklandError",
    "SklandLoginExpired",
    "SklandParamError",
    "SklandRateLimited",
    "SklandTransportError",
    "SklandUnauthorized",
    "sign_headers",
]

#: 通行证侧（扫码、换 token）的基础地址。
HYPERGRYPH_BASE: Final[str] = "https://as.hypergryph.com"

#: 森空岛业务侧的基础地址（签名接口都在这里）。
ZONAI_BASE: Final[str] = "https://zonai.skland.com/api/v1"

#: 森空岛应用的 appCode。**三个 appCode 别用错**：这是实测出来的森空岛那一个。
APP_CODE_SKLAND: Final[str] = "4ca99fa6b56cc2ba"

#: 诚实的 UA。实测 UA 与结果无关，所以不必伪装成官方 App——
#: 假装成官方客户端属于「模拟客户端」，本项目不做。
HONEST_USER_AGENT: Final[str] = "astrbot-plugin-arknights-toolbox/0.1 (+personal use)"

#: 错误码 → 含义（实测 + 读码一致）。
CODE_SUCCESS: Final[int] = 0
CODE_BAD_CRED: Final[int] = 10000
CODE_BAD_PARAM: Final[int] = 10001
CODE_NOT_LOGGED_IN: Final[int] = 10002


class SklandError(Exception):
    """森空岛接口返回了非成功结果。"""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SklandUnauthorized(SklandError):
    """`10000`：凭据无效／未授权——**要用户重新扫码授权**。"""


class SklandLoginExpired(SklandError):
    """`10002`：未登录／登录失效——**同样要重新授权**，但与 10000 分开便于措辞。"""


class SklandParamError(SklandError):
    """`10001`：参数错误。**不要重试**——重试一百次还是错的。"""


class SklandTransportError(SklandError):
    """网络层失败（连不上、超时、非法 JSON）。**这类才值得重试**。"""


class SklandRateLimited(SklandError):
    """本地限流拦下的请求（**不是**服务端返回的 429）。"""


def sign_headers(
    *,
    cred: str,
    token: str,
    path: str,
    query_or_body: str = "",
    timestamp: int | None = None,
) -> dict[str, str]:
    """按实测算法生成签名头。

    Args:
        path: 由 `/api/v1` 之后开始（含 `/`），例如 `"/game/player/binding"`。
        query_or_body: GET 传**原始 query 串**（不含 `?`）；POST 传**实际发出的
            JSON 文本**。签名与实际发出的字节必须一致——所以这里收的是字符串，
            不是对象，免得调用方以为可以随手中转一次 `json.dumps`。
        timestamp: 仅测试用；默认取当前时间**减 1 秒**（实测做法，规避时钟偏差）。

    Returns:
        可直接合并进请求的头部字典。
    """
    ts = int(time.time()) - 1 if timestamp is None else timestamp
    header = {"platform": "", "timestamp": str(ts), "dId": "", "vName": ""}
    secret = path + query_or_body + str(ts) + json.dumps(header, separators=(",", ":"))
    digest = hmac.new(token.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "cred": cred,
        "sign": hashlib.md5(digest.encode("utf-8")).hexdigest(),
        "platform": "",
        "timestamp": str(ts),
        "dId": "",
        "vName": "",
        "User-Agent": HONEST_USER_AGENT,
    }


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    """可注入的传输层（测试用假的，运行用 `urllib`）。"""

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResponse: ...


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, *, timeout: float = 15.0
) -> HttpResponse:
    """默认传输层：标准库 `urllib`（本项目运行时零第三方依赖）。"""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 固定 https
            return HttpResponse(status=response.status, body=response.read())
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也带上响应体：**判据在 body 里**，不是状态码。丢掉 body 等于丢掉真信息。
        return HttpResponse(status=exc.code, body=exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SklandTransportError(f"网络请求失败：{type(exc).__name__}: {exc}") from exc


@dataclass
class CachePolicy:
    """按 key 缓存 + **按端点**冷却。

    实测**未能确认**服务端频率限制（约 60 次探测未触发任何 429），所以这里按保守估计走：
    没触发不等于不存在。

    冷却**必须按端点记，不能全局**：扫码授权本身是连着三步的顺序调用，
    全局冷却会把它们互相卡死——那是个会让登录永远失败的 bug。

    时钟由 `clock` 提供，测试可以注入假时钟而不必 `sleep`。
    """

    ttl_seconds: float = 120.0
    cooldown_seconds: float = 1.0
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    _last_call: dict[str, float] = field(default_factory=dict)

    def check_cooldown(self, key: str) -> None:
        now = self.clock()
        previous = self._last_call.get(key)
        if previous is not None and now - previous < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - previous)
            raise SklandRateLimited(f"请求太频繁，请 {remaining:.1f} 秒后再试")
        self._last_call[key] = now

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self.clock() - stored_at >= self.ttl_seconds:
            del self._cache[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        self._cache[key] = (self.clock(), value)

    def clear(self) -> None:
        """凭据换了要清缓存，否则会拿旧账号的数据当新账号的。冷却表也一起清。"""
        self._cache.clear()
        self._last_call.clear()


def _extract_code(payload: Any) -> int | None:
    """从响应体里取 `code`；取不到返回 `None`（**不猜**）。"""
    if isinstance(payload, Mapping):
        value = payload.get("code")
        if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
            return None
        if isinstance(value, int):
            return value
    return None


def _message_of(payload: Any, fallback: str) -> str:
    if isinstance(payload, Mapping):
        for key in ("message", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return fallback


class SklandClient:
    """带签名的森空岛客户端。

    所有网络都经 `transport`（默认 `urllib_transport`），所以测试可以在**不发真实请求**
    的前提下覆盖每一类响应——包括那些只有服务端才造得出来的错误码组合。
    """

    def __init__(
        self,
        *,
        cred: str = "",
        token: str = "",
        transport: Transport = urllib_transport,
        cache: CachePolicy | None = None,
    ) -> None:
        self._cred = cred
        self._token = token
        self._transport = transport
        self.cache = cache or CachePolicy()

    # --- 凭据 ---------------------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return bool(self._cred and self._token)

    def update_credentials(self, cred: str, token: str) -> None:
        self._cred, self._token = cred, token
        self.cache.clear()  # 换号必须清缓存，见 CachePolicy.clear

    # --- 传输 ---------------------------------------------------------------

    def _call(self, method: str, url: str, *, body: Any = None, signed: bool = True) -> Any:
        self.cache.check_cooldown(urllib.parse.urlsplit(url).path)

        payload_bytes = (
            None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )
        headers: dict[str, str] = {"User-Agent": HONEST_USER_AGENT}
        if signed:
            if not self.has_credentials:
                raise SklandUnauthorized("还没有授权，先扫码")
            parsed = urllib.parse.urlsplit(url)
            path = (
                parsed.path[len("/api/v1") :] if parsed.path.startswith("/api/v1") else parsed.path
            )
            # GET 签原始 query；POST 签**实际发出的** JSON 文本。
            signed_part = (
                parsed.query if method == "GET" else (payload_bytes or b"").decode("utf-8")
            )
            headers.update(
                sign_headers(
                    cred=self._cred, token=self._token, path=path, query_or_body=signed_part
                )
            )
            headers["Content-Type"] = "application/json"

        response = self._transport(method, url, headers, payload_bytes)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SklandTransportError(
                f"响应不是合法 JSON（HTTP {response.status}）：{exc}"
            ) from exc

        code = _extract_code(payload)
        if code is None:
            # 通行证侧（hypergryph）用 status 字段而不是 code，所以只有在带了 code
            # 的接口上才要求它存在。这里不猜——交给调用方按各自协议判。
            return payload
        if code == CODE_SUCCESS:
            return payload
        message = _message_of(payload, f"接口返回 code={code}")
        if code == CODE_BAD_CRED:
            raise SklandUnauthorized(f"凭据无效，需要重新授权：{message}", code=code)
        if code == CODE_NOT_LOGGED_IN:
            raise SklandLoginExpired(f"登录已失效，需要重新授权：{message}", code=code)
        if code == CODE_BAD_PARAM:
            raise SklandParamError(f"接口参数错误（不会重试）：{message}", code=code)
        raise SklandError(f"接口返回 code={code}：{message}", code=code)

    # --- 有实测依据的接口 ---------------------------------------------------

    def call_unauthenticated(self, method: str, url: str, body: Any = None) -> Any:
        """通行证侧的调用（**不需要凭据，也不签名**）。

        扫码链路的前四步都在 `as.hypergryph.com`，它们用的是 `status` 字段而不是
        `code`，所以这里**不做 `code` 判定**——把原始 payload 交给 `login.py` 的解析器，
        由那一层按各自的响应形态处理。顺手过了同一个冷却，免得把通行证侧打爆。
        """
        return self._call(method, url, body=body, signed=False)

    def health_check(self) -> dict[str, Any]:
        """健康检查：打**真实取数接口**，以 `code == 0` 为准。

        刻意**不用** `auth/refresh`——实测它对无效凭据也返回成功（见模块 docstring 第 2 条）。
        """
        return self._call("GET", f"{ZONAI_BASE}/game/player/binding")

    def sign_in(self) -> dict[str, Any]:
        """每日签到。实测该路由存在（无凭据时返回 401/10002）。

        Note:
            `UNVERIFIED`：**请求方法与成功响应体未经真实授权验证**——没有一次真机扫码就
            无法验证。失败会被如实抛出，不会静默当成功。
        """
        return self._call("POST", f"{ZONAI_BASE}/game/attendance")

    def player_info(self, uid: str) -> dict[str, Any]:
        """玩家信息。实测该路由存在（缺 `uid` → 10001；带 `uid` 无凭据 → 10002）。

        Note:
            `UNVERIFIED`：成功响应体的字段结构未经真机验证，所以本方法**只做透传**，
            不在这一层解析任何字段——照文档猜字段名正是本项目栽过的坑。
        """
        query = urllib.parse.urlencode({"uid": uid})
        return self._call("GET", f"{ZONAI_BASE}/game/player/info?{query}")

    def binding_uid(self) -> str:
        """从绑定信息里取 uid；取不到就抛——**不返回空字符串假装成功**。"""
        payload = self.health_check()
        data = payload.get("data") if isinstance(payload, Mapping) else None
        candidates: list[Any] = []
        if isinstance(data, Mapping):
            candidates.append(data.get("uid"))
            for key in ("bindingList", "list"):
                items = data.get(key)
                if isinstance(items, list):
                    candidates.extend(
                        item.get("uid") for item in items if isinstance(item, Mapping)
                    )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                return candidate
        raise SklandError("绑定信息里没有 uid（响应结构与预期不符）")
