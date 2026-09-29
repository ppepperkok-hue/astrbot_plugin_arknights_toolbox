"""MAA 远程控制协议的形状与判据（**纯逻辑**，不 import astrbot）。

协议依据是官方文档（2026-09-29 抓取，**逐条读原文，不是推测**）：

    https://docs.maa.plus/zh-tw/protocol/remote-control-schema.html

要点：

1. **两个端点都必须接受 `Content-Type: application/json` 的 POST**，且**可匿名存取**
   ——文档原文说 MAA 只能填两个 URL，请求体里只有 `user` 与 `device`。
2. **方向是 MAA 主动轮询我们**（默认 1 秒一次），不是我们去连用户的电脑。
   这条推翻了立项文档里「服务器→手机→本机、链路最长」的描述。
3. 领任务端点必须回 `{"tasks": [...]}`。文档原文：**「若不存在 tasks 則視為連線無效」**
   ——所以「没有任务」必须写成**空数组** `{"tasks": []}`，不能省略字段，也不能回非 JSON。
4. 汇报端点收到 `{"user","device","task","status","payload"}`；文档原文：
   **「該端點的回傳內容不限，MAA 不會讀取回傳內容，也不校驗狀態碼」**。
5. `status` 只有 `SUCCESS` / `FAILED`，且文档明确：**「通常不論成敗皆彙報 SUCCESS」**。
   ⇒ **`SUCCESS` 不等于任务成功**，它更像「指令我处理完了」。将来任何把它转述成
   「换班成功」的文案都是过度解读。

本文件只处理**形状与渲染**，不碰网络、不碰框架。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "GET_TASK_KIND",
    "GET_TASK_SUBPATH",
    "IDENTITY_KEYS",
    "REPORT_KIND",
    "REPORT_SUBPATH",
    "STATUS_VALUES",
    "describe_payload",
    "describe_request",
    "empty_tasks_response",
    "extract_status",
    "redact_text",
    "report_ack_response",
    "shape_of",
]

#: 两个端点在本模块路由前缀下的相对路径。名字照官方文档，不自行发挥。
GET_TASK_SUBPATH = "/getTask"
REPORT_SUBPATH = "/reportStatus"

#: 日志里用来区分两类到达。用常量而非字面量：拼错会当场 AttributeError，
#: 而不是悄悄落进某个 `else` 分支（本项目最怕的「静默匹配不上」）。
GET_TASK_KIND = "get_task"
REPORT_KIND = "report_status"

#: **身份标识**：MAA 每个请求都带这两个字段。它们的**值绝不落盘**——
#: 写进日志等于把用户标识按秒刷满磁盘，而「连上了没有」与「是谁」无关。
IDENTITY_KEYS: frozenset[str] = frozenset({"user", "device"})

#: 协议定义的 `status` 取值。
STATUS_VALUES: frozenset[str] = frozenset({"SUCCESS", "FAILED"})

#: **唯一允许渲染值的键**。`status` 是协议枚举，是这一行日志里最有用的一位信息；
#: 其它一律只给类型与长度——因为响应里可能出现 `payload`（截图 Base64，可达数十 MB）。
VALUE_KEYS: frozenset[str] = frozenset({"status"})

#: 嵌套展开的最大深度。协议的请求体是平铺的，深挖没有收益，还容易被构造的深层
#: JSON 拖慢。
MAX_DEPTH = 3

#: 单层最多渲染多少个键。
MAX_FIELDS = 32


def empty_tasks_response() -> dict[str, Any]:
    """领任务端点的响应：**一个常量**。

    它是常量这件事本身是安全设计的一部分——见 `module.MaaModule._web_get_task`：
    端点的响应不来自请求里的任何字段，所以这里**结构上不可能**被用来执行外部指令。
    """
    return {"tasks": []}


def report_ack_response() -> dict[str, Any]:
    """汇报端点的响应。MAA 不读它（文档原文），但我们要回一个合法 JSON。"""
    return {"ok": True}


def redact_text(value: str) -> str:
    """把**值**渲染成不含内容的标记。"""
    return f"<len={len(value)}>"


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _size_of(value: object) -> str:
    """容器/文本的规模；标量给 `-`（它们没有规模可言）。"""
    if isinstance(value, (str, bytes, bytearray)):
        return str(len(value))
    if isinstance(value, Mapping):
        return str(len(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return str(len(value))
    return "-"


def shape_of(value: object, *, depth: int = 0) -> str:
    """把一个值渲染成**形状**（类型 / 规模 / 键名），**永不含值**。

    这是整个模块里唯一描述请求体的函数，所以「不泄露」这条规矩只需要在这一个
    地方守对——而不是靠每个调用点自觉。
    """
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            return f"map(len={len(value)})"
        keys = list(value.keys())[:MAX_FIELDS]
        inner = ", ".join(f"{key}: {shape_of(value[key], depth=depth + 1)}" for key in keys)
        if len(value) > MAX_FIELDS:
            inner += f", …共 {len(value)} 个键"
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            return f"list(len={len(value)})"
        items = list(value)[:MAX_FIELDS]
        inner = " | ".join(shape_of(item, depth=depth + 1) for item in items)
        if len(value) > MAX_FIELDS:
            inner += f" | …共 {len(value)} 项"
        return "[" + inner + "]"
    if isinstance(value, (str, bytes, bytearray)):
        return f"{_type_name(value)}(len={len(value)})"
    return _type_name(value)


def _render_field(key: str, value: object) -> str:
    if key in IDENTITY_KEYS:
        # 身份标识：**只给类型与规模**。故意放在最前面，让「这两个键要特殊对待」
        # 在读代码时先撞到眼睛。
        return f"{key}=<{_type_name(value)}, len={_size_of(value)}>"
    if key in VALUE_KEYS and value in STATUS_VALUES:
        return f"{key}={value}"
    if isinstance(value, bool):
        return f"{key}={value}"
    if isinstance(value, (int, float)):
        return f"{key}={value}"
    if value is None:
        return f"{key}=null"
    if isinstance(value, str):
        return f"{key}={redact_text(value)}"
    return f"{key}={shape_of(value)}"


def describe_payload(payload: object) -> str:
    """把请求体渲染成**一行形状**：键名 + 类型/长度，**永不含值**。

    Args:
        payload: 已解析的请求体（MAA 发来的是平铺的 JSON 对象）。

    Returns:
        供日志用的形状串。非对象输入也给出可读结果，不抛异常——**日志渲染器
        自己崩掉会把「请求到底来了没有」这条证据一起弄丢**。
    """
    if not isinstance(payload, Mapping):
        return shape_of(payload)
    if not payload:
        return "{}"
    keys = list(payload.keys())[:MAX_FIELDS]
    fields = [_render_field(str(key), payload[key]) for key in keys]
    if len(payload) > MAX_FIELDS:
        fields.append(f"…共 {len(payload)} 个键")
    return "{" + ", ".join(fields) + "}"


def extract_status(payload: object) -> str:
    """从汇报体里取 `status`；**取不到或不是协议枚举就返回空串**。

    刻意不猜：拿不准就留空，让日志显示「没给状态」，而不是把垃圾值当状态写下去。
    """
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get("status")
    return value if isinstance(value, str) and value in STATUS_VALUES else ""


def describe_request(
    *,
    kind: str,
    method: str,
    path: str,
    client_host: str,
    user_agent: str,
) -> str:
    """拼一条到达记录的正文。

    **不包含任何请求头**（除了 User-Agent）：Dashboard 的 API key 可以走
    `Authorization` / `X-API-Key` 头，把整个头表写进日志等于把它抄下来。
    """
    kind_label = "领任务" if kind == GET_TASK_KIND else "汇报状态"
    return (
        f"{kind_label} {method} {path}；来源 {client_host or '未知'}；UA {user_agent or '（无）'}"
    )
