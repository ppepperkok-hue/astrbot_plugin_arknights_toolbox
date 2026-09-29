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

## 签名：本地已比对到字节，服务端是否接受仍未验证

签名是**纯函数**，所以「我们算的」与「参考实现算的」可以逐字节比对——
`docs/project-plan/07-skland-api.md` §14 做过这件事，并因此抓到一处真缺陷：
调用点曾把 `path` 里的 `/api/v1` 前缀剥掉，**签名整体错位**，而真机上只表现为
「凭据无效（`code=10000`）」，错误码完全指不到真因。现在签的是**完整路径**。

**但"与参考实现一致"不等于"服务端认"**：服务端对未登录请求不校验签名（§3），
所以只有一次真实授权能证。`/ak skland check` 就是那个探针——签名错了会回
10001/10000 而不是 10002。

## 绑定信息是三层结构（四条参考实现一致，真机踩过一次）

`GET game/player/binding` 的**成功**响应（`code=0`）里，uid **不在** `data.uid`，
也不在 `data.bindingList[]`，而在**第三层**：:

    data.list[]          每个元素是一个 app
      appCode            "arknights" / "endfield" …
      defaultUid         该 app 的默认 uid（可能缺）
      bindingList[]      该 app 下的角色
        uid              ← 角色 uid 在这里

真机第一次拿到成功响应时我们报「绑定信息里没有 uid（响应结构与预期不符）」——
因为原实现只试了 `data.uid` / `data.bindingList[].uid` / `data.list[].uid`，
**每一个都少一层**。逐条出处与比对见 `docs/project-plan/07-skland-api.md` §15。

**按 `appCode == "arknights"` 过滤**：一个森空岛账号可同时绑多个游戏，
取错了会拿终末地的 uid 去查方舟（服务端只会回一个含糊的参数错误）。

## 不做的

- 不实现 `dId` 设备指纹（那要打第三方指纹服务并嵌死伪造数据块；生成它还需要
  RSA+AES 运算，引第三方密码学库会破坏本项目的零运行时依赖）。
  **代价要如实说**：三个 AstrBot 参考实现都用**非空** `dId`，我们发空串。
  服务端必须从我们发出的头部重建被签名的 JSON（否则它无从知道客户端自选的
  `dId`），所以空串在**签名层面**自洽；**它是否被额外校验，仍是未验证项**（§14.4）。
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
    "BINDING_APP_ARKNIGHTS",
    "HONEST_USER_AGENT",
    "HYPERGRYPH_BASE",
    "ZONAI_BASE",
    "CachePolicy",
    "HttpResponse",
    "RawCall",
    "SklandClient",
    "SklandError",
    "SklandLoginExpired",
    "SklandParamError",
    "SklandRateLimited",
    "SklandTransportError",
    "SklandUnauthorized",
    "arknights_uid",
    "binding_app_codes",
    "describe_structure",
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
    """森空岛接口返回了非成功结果。

    Attributes:
        http_status: 该次请求的 HTTP 状态码（能拿到就有）。**判据在 body 里而不是这里**，
            但排查时它与 `code` 要一起看——实测同一个 `10001` 出现过 400 与 500。
        payload: 服务端返回的已解析响应体。**仅供程序读取诊断字段**，绝不可整段进日志
            （里面可能带凭据）；渲染摘要请走 `login.describe_response`。
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        http_status: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.payload = payload


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
        path: **完整的请求路径，含 `/api/v1` 前缀**，例如 `"/api/v1/game/player/binding"`。
            这里踩过一个把签名整体弄错的坑：本模块原先按「`/api/v1` 之后开始」拼串
            （即 `"/game/player/binding"`），而**四个参考实现与我们的调研探针全都签完整
            路径**。签名是纯函数，差一个前缀就是另一个值——真机上表现为
            `HTTP 401 / code=10000`（凭据无效），看起来像凭据坏了，实际是签名从未对上。
            见 `docs/project-plan/07-skland-api.md` §14 的逐字节比对。
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


def _content_type_label(value: Any) -> str:
    """媒体类型可以安全展示——它是协议常量，不是凭据；其它头一律只报名字。

    `Content-Type` **缺失**时要明说是缺失，并点出 `urllib` 会替我们补什么：
    那个自动补的默认值正是第 3 步事故的成因，把它藏起来等于把线索藏起来。
    """
    if not isinstance(value, str) or not value.strip():
        return "<未设置（urllib 会替我们补 application/x-www-form-urlencoded）>"
    text = value.strip()
    main, _, sub = text.partition("/")
    if main.isalpha() and sub and all(char.isalnum() or char in ".+-" for char in sub):
        return text
    return "<非标准媒体类型，已隐去>"


def describe_request_shape(body: Any = None, *, content_type: Any = None) -> str:
    """请求体的**形状**：键名、类型、长度——**绝不含任何值**。

    为什么需要它：线上第 3 步失败时，服务端只说 `scanCode` 的 `required` 校验没过。
    光看那句话**无法区分**「我们压根没发这个字段」与「发了，但服务端没把它当 JSON 读」。
    形状能区分：它会显示 `scanCode=str(len=120)`，方向立刻从"值不对"转到"传输不对"。
    这次的真因（`Content-Type` 被 `urllib` 默认成表单）就是这样被定位的。

    **凭据红线照旧**：只给键名、类型与长度；任何值都不出现，截断的也不行。
    """
    parts: list[str] = [f"Content-Type={_content_type_label(content_type)}"]
    if body is None:
        parts.append("请求体=无")
        return "；".join(parts)
    if not isinstance(body, Mapping):
        parts.append(f"请求体={type(body).__name__}（非对象）")
        return "；".join(parts)

    fields: list[str] = []
    for key, value in body.items():
        name = str(key)
        if isinstance(value, str):
            fields.append(f"{name}=str(len={len(value)})")
        elif isinstance(value, bool):
            fields.append(f"{name}=bool")
        elif isinstance(value, (int, float)):
            fields.append(f"{name}={type(value).__name__}")
        elif isinstance(value, Mapping):
            fields.append(f"{name}=对象{{{len(value)} 键}}")
        elif isinstance(value, (list, tuple)):
            fields.append(f"{name}=列表(len={len(value)})")
        else:
            fields.append(f"{name}={type(value).__name__}")
    parts.append("请求体={" + "、".join(fields) + "}" if fields else "请求体={}（空对象）")
    return "；".join(parts)


#: 结构摘要的长度上限。它会同时出现在日志与 QQ 回执里（本项目约定二者同文案），
#: 太长会把回执淹掉。超长时截断**并明说截断**——静默截断会让人以为看全了。
_STRUCTURE_MAX_CHARS: Final[int] = 480


def describe_structure(
    value: Any = None, *, max_depth: int = 5, max_items: int = 2, max_keys: int = 10
) -> str:
    """把一段 JSON 渲染成**只有形状**的摘要：键名、类型、长度——**绝不含任何值**。

    与 :func:`describe_request_shape` 的分工：那个描述**我们发出去的**请求体，
    一层就够（要证明的是「值非空」）；这个描述**服务端发回来的**嵌套结构，
    必须往下钻——真机那次失败正是栽在「少了一层嵌套」上（见 `07-skland-api.md` §15）。

    **为什么是「永不渲染值」而不是「按凭据键名脱敏」**：前者**结构上不可能泄露**。
    `game/player/binding` 的响应里没有 token，但有 `uid` / `nickName` 这类个人数据，
    而「哪些键算敏感」是会随接口变的——不渲染值，就不必跟着变。

    列表只渲染前 ``max_items`` 项的**形状**（去重），并报出总项数：
    用来判断「这个 app 下面有几个角色」够用，又不至于把整棵树倒出来。
    """

    def shape(node: Any, depth: int) -> str:
        if node is None:
            return "null"
        if isinstance(node, bool):
            return "bool"
        if isinstance(node, (int, float)):
            return type(node).__name__
        if isinstance(node, str):
            return f"str(len={len(node)})"
        if isinstance(node, Mapping):
            keys = sorted(node, key=str)
            if depth >= max_depth:
                return f"对象{{{len(keys)} 键}}"
            shown = keys[:max_keys]
            parts = [f"{key}={shape(node[key], depth + 1)}" for key in shown]
            if len(keys) > len(shown):
                parts.append(f"…共 {len(keys)} 键")
            return "{" + "、".join(parts) + "}"
        if isinstance(node, (list, tuple)):
            if not node:
                return "列表(空)"
            if depth >= max_depth:
                return f"列表(len={len(node)})"
            variants: list[str] = []
            for item in node[:max_items]:
                # 列表项**不额外消耗一层深度**：列表只是同类元素的容器，
                # 让它算一层会让「数组套对象」凭空多一层，而这一层预算恰恰要留给
                # `bindingList[]` 里面的字段（`uid` 就在那儿）。
                text = shape(item, depth)
                if text not in variants:
                    variants.append(text)
            tail = f"，共 {len(node)} 项" if len(node) > max_items else ""
            return "列表[" + " | ".join(variants) + tail + "]"
        return type(node).__name__

    text = shape(value, 0)
    if len(text) <= _STRUCTURE_MAX_CHARS:
        return text
    return text[:_STRUCTURE_MAX_CHARS] + "…（已截断）"


#: `game/player/binding` 的响应里，明日方舟这一项的 `appCode`。
#: **四条参考实现都以它过滤**（`07-skland-api.md` §15）——不是猜的。
BINDING_APP_ARKNIGHTS: Final[str] = "arknights"


def binding_app_codes(payload: Any) -> list[str]:
    """响应里出现过哪些 `appCode`（按出现顺序，去重）。

    `appCode` 是**协议常量**（`arknights` / `endfield` …），不是个人信息，可以进日志。
    它的用处是把两种失败**分开说**：「结构没认出来」与「认出来了，但这个森空岛账号
    没绑明日方舟」。这两种情况的下一步动作完全不同（后者该去森空岛 App 里绑定）。
    """
    if not isinstance(payload, Mapping):
        return []
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    apps = data.get("list")
    if not isinstance(apps, list):
        return []
    codes: list[str] = []
    for app in apps:
        if not isinstance(app, Mapping):
            continue
        code = app.get("appCode")
        if isinstance(code, str) and code and code not in codes:
            codes.append(code)
    return codes


def _identifier_text(value: Any) -> str:
    """把 uid 这类标识统一成字符串；取不到返回空串。

    接受数字是**协议现实**而不是结构猜测：uid 走 JSON 出来，Go 侧用字符串还是数字
    取决于字段声明，两种都当作合法标识。`bool` 先挡掉（它是 `int` 的子类）。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def arknights_uid(payload: Any) -> str | None:
    """从 `game/player/binding` 的响应里取**明日方舟**的 uid；取不到返回 `None`。

    结构（**四条参考实现一致**：`Morizero1125` MIT、`Siq5005` MIT、
    `qihang518887`、`Azincc`；逐条出处见 `07-skland-api.md` §15）::

        data.list[]          app 列表
          appCode            "arknights"
          defaultUid         app 级默认角色（可能缺）
          bindingList[]      该 app 下的角色
            uid

    先看 `defaultUid`、再看 `bindingList[0].uid`——与 `Morizero1125` 的取法一致。

    取不到返回 `None`，**不返回空字符串**：空串会让上层以为拿到了 uid。
    """
    if not isinstance(payload, Mapping):
        return None
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    apps = data.get("list")
    if not isinstance(apps, list):
        return None
    for app in apps:
        if not isinstance(app, Mapping) or app.get("appCode") != BINDING_APP_ARKNIGHTS:
            continue
        default_uid = _identifier_text(app.get("defaultUid"))
        if default_uid:
            return default_uid
        bindings = app.get("bindingList")
        if not isinstance(bindings, list):
            continue
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            uid = _identifier_text(binding.get("uid"))
            if uid:
                return uid
    return None


@dataclass(frozen=True)
class RawResult:
    """`_call_raw` 的结果：**响应**加上**我们实际发出的请求形状**。

    两样一起带回，是为了让上层（第 3 步那样的多步流程）在失败时能同时说出
    「服务端说了什么」与「我们发了什么形状」——只报一样，排查就会被引向错误方向。
    """

    payload: Any
    http_status: int
    request_shape: str


@dataclass(frozen=True)
class RawCall:
    """一次未签名调用的原始结果——给扫码链路做排查用。

    只带**服务端回了什么**与**请求的*形状***，不带任何请求里的**值**：第 3 步的请求体
    里就是 `scanCode`，绝不能让任何"方便排查"的设计把它带出去。`path` 也刻意**不含
    query**——轮询的 query 里就是 `scanId`，同样算登录票据。

    `request_shape` 之所以可以留下：键名、类型、长度都不是凭据；而它恰恰能证明
    「我们发了非空的值」还是「我们什么都没发」——那是纯响应摘要永远给不出的信息。
    """

    payload: Any
    http_status: int
    method: str
    path: str
    request_shape: str = "请求体=无"


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
        return self._call_raw(method, url, body=body, signed=signed).payload

    def _call_raw(
        self, method: str, url: str, *, body: Any = None, signed: bool = True
    ) -> RawResult:
        """发一次请求，返回 :class:`RawResult`（响应 + HTTP 状态 + 请求形状）。

        为什么要单独把状态码带出来：扫码链路的失败只报「响应里没有 data」，
        **没有 HTTP 状态就无法区分「服务端拒绝」与「网关改写了响应」**。
        为什么还要带**请求形状**：只报服务端说了什么，会把「我们没发」与
        「发了但没被当 JSON 读」混成同一句话——第 3 步那次就是这个混同。
        """
        self.cache.check_cooldown(urllib.parse.urlsplit(url).path)

        payload_bytes = (
            None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )
        headers: dict[str, str] = {"User-Agent": HONEST_USER_AGENT}
        if payload_bytes is not None or signed:
            # 两条并列的理由，**都不能省**：
            #
            # ① 带 body 的请求必须声明 JSON。这里踩过一个只有真机才现形的坑：
            #    `urllib` 在带 body 却没有显式 `Content-Type` 时会补上
            #    `application/x-www-form-urlencoded`（实测，见 tmp/s6_probe_content_type.py），
            #    于是服务端按**表单**绑定，body 里明明是正确的 JSON 也一个字段都读不到。
            #    表现是第 3 步 400：`Field validation for 'scanCode' failed on the
            #    'required' tag` —— 字段名对、值却是空的，因为那份 body 从未被
            #    当作 JSON 解析过。**原先这个头只设在签名分支里，未签名分支没有**，
            #    两条路径的这次漂移就是本 bug 的成因。
            # ② 签名请求（含不带 body 的 POST，例如签到）也声明 JSON：那是本客户端
            #    一贯的协议立场，改掉它属于顺手改行为，不在本次修复范围内。
            headers["Content-Type"] = "application/json"
        if signed:
            if not self.has_credentials:
                raise SklandUnauthorized("还没有授权，先扫码")
            parsed = urllib.parse.urlsplit(url)
            # 签**完整的** `path`，含 `/api/v1` 前缀。**不要**剥掉前缀：
            # 四个参考实现都签完整路径，而剥掉前缀会让签名整体错位，
            # 真机上只表现为「凭据无效」（10000），极难从错误码反推。
            # 逐字节比对见 docs/project-plan/07-skland-api.md §14。
            path = parsed.path
            # GET 签原始 query；POST 签**实际发出的** JSON 文本。
            signed_part = (
                parsed.query if method == "GET" else (payload_bytes or b"").decode("utf-8")
            )
            headers.update(
                sign_headers(
                    cred=self._cred, token=self._token, path=path, query_or_body=signed_part
                )
            )
            # 刻意不在这里再设 Content-Type：上面按「有没有 body」统一设过一次。
            # 同一件事只留一处实现，免得两条路径再次漂移——那个漂移就是本 bug 的成因。

        # 形状在这里定稿：**必须在 `_transport` 之前、且在签名头补完之后**取，
        # 否则记下的是"我们打算发的"，而不是"实际交出去的"——两者之差就是本 bug。
        request_shape = describe_request_shape(body, content_type=headers.get("Content-Type"))
        response = self._transport(method, url, headers, payload_bytes)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SklandTransportError(
                f"响应不是合法 JSON（HTTP {response.status}）：{exc}",
                http_status=response.status,
            ) from exc

        code = _extract_code(payload)
        if code is None:
            # 通行证侧（hypergryph）用 status 字段而不是 code，所以只有在带了 code
            # 的接口上才要求它存在。这里不猜——交给调用方按各自协议判。
            return RawResult(payload, response.status, request_shape)
        if code == CODE_SUCCESS:
            return RawResult(payload, response.status, request_shape)
        message = _message_of(payload, f"接口返回 code={code}")
        detail = f"（HTTP {response.status}，code={code}）"
        if code == CODE_BAD_CRED:
            raise SklandUnauthorized(
                f"凭据无效，需要重新授权{detail}：{message}",
                code=code,
                http_status=response.status,
                payload=payload,
            )
        if code == CODE_NOT_LOGGED_IN:
            raise SklandLoginExpired(
                f"登录已失效，需要重新授权{detail}：{message}",
                code=code,
                http_status=response.status,
                payload=payload,
            )
        if code == CODE_BAD_PARAM:
            raise SklandParamError(
                f"接口参数错误（不会重试）{detail}：{message}",
                code=code,
                http_status=response.status,
                payload=payload,
            )
        raise SklandError(
            f"接口返回 code={code}{detail}：{message}",
            code=code,
            http_status=response.status,
            payload=payload,
        )

    # --- 有实测依据的接口 ---------------------------------------------------

    def call_unauthenticated(self, method: str, url: str, body: Any = None) -> Any:
        """通行证侧的调用（**不需要凭据，也不签名**）。

        扫码链路的前四步都在 `as.hypergryph.com`，它们用的是 `status` 字段而不是
        `code`，所以这里**不做 `code` 判定**——把原始 payload 交给 `login.py` 的解析器，
        由那一层按各自的响应形态处理。顺手过了同一个冷却，免得把通行证侧打爆。
        """
        return self._call(method, url, body=body, signed=False)

    def call_unauthenticated_detailed(self, method: str, url: str, body: Any = None) -> RawCall:
        """与 :meth:`call_unauthenticated` 同一条路，但**把 HTTP 状态码也带回来**。

        扫码链路改用它：只报「响应里没有 data」而不报状态码，等于把最便宜的一条线索
        丢掉。返回值里**不含请求体**——见 :class:`RawCall`。
        """
        result = self._call_raw(method, url, body=body, signed=False)
        return RawCall(
            payload=result.payload,
            http_status=result.http_status,
            method=method,
            path=urllib.parse.urlsplit(url).path,
            request_shape=result.request_shape,
        )

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
        """从绑定信息里取**明日方舟**的 uid；取不到就抛——**不返回空字符串假装成功**。

        解析本身在纯函数 :func:`arknights_uid` 里（可脱离网络单测）。

        失败时会**带上响应结构与看到过的 `appCode`**。原先只说了「没有 uid」，
        而那句话把两件事混成了一件：**我们猜错了结构**，还是**这个账号没绑方舟**？
        分不开就只能再扫一次码去试——一次真实授权要用户亲手操作，不该花在这种地方。
        """
        payload = self.health_check()
        uid = arknights_uid(payload)
        if uid:
            return uid
        codes = binding_app_codes(payload)
        seen = "、".join(codes) if codes else "没有看到 appCode"
        raise SklandError(
            f"绑定信息里没有明日方舟的 uid（看到的应用：{seen}）"
            f"｜响应结构：{describe_structure(payload)}"
        )
