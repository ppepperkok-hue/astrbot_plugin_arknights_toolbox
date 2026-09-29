"""`modules/maa/protocol.py` 的单测：形状、判据、以及**日志渲染绝不泄露值**。

这一层存在的理由是：**描述请求体的代码只有一处**，所以「不把值写进日志」这条
规矩只需要在一个地方守对，而不是靠每个调用点自觉。相应地，这里必须有一条
**能真的拦住泄露**的断言（见文件末尾的「可证伪」一组）。
"""

from __future__ import annotations

import pytest

from modules.maa import protocol

# 一组**刻意可辨认**的假身份值：大写字母 + 连字符，使它们的任意 4 字符片段
# 都不可能与数字、十六进制摘要或中文文案偶然撞上（避免误报）。
FAKE_USER = "ZZQQ-USER-AAAA-1111-BBBB"
FAKE_DEVICE = "ZZDEV-DEVI-CCCC-2222-DDDD"


def _logged_leak(haystack: str, secret: str) -> list[str]:
    """`secret` 的任何 4 字符以上片段出现在 `haystack` 里，就算泄露。

    用**子串扫描**而不是整串比对：整串比对会漏掉截断、包裹、以及服务端回显。
    """
    hits: list[str] = []
    for start in range(len(secret) - 3):
        fragment = secret[start : start + 4]
        if fragment in haystack:
            hits.append(fragment)
    return hits


# --- 响应形状（照官方文档原文） ---------------------------------------------


def test_empty_tasks_response_is_an_empty_array_not_a_missing_key() -> None:
    """「没有任务」必须写成**空数组**。

    文档原文：「若不存在 tasks 則視為連線無效」——省略字段会被 MAA 当成连接无效，
    那时我们会以为是网络或证书的问题，而其实是我们自己把响应写错了。
    """
    payload = protocol.empty_tasks_response()

    assert payload == {"tasks": []}
    assert isinstance(payload["tasks"], list)


def test_report_ack_response_is_json_serialisable() -> None:
    """MAA 不读回执（文档原文），但它必须是合法 JSON，便于人工用 curl 试。"""
    import json

    assert json.loads(json.dumps(protocol.report_ack_response())) == {"ok": True}


# --- 形状渲染 ---------------------------------------------------------------


def test_describe_payload_renders_keys_as_names_and_sizes() -> None:
    rendered = protocol.describe_payload({"user": FAKE_USER, "device": FAKE_DEVICE})

    assert "user=" in rendered
    assert "device=" in rendered
    assert f"len={len(FAKE_USER)}" in rendered


def test_describe_payload_renders_the_protocol_status_enum() -> None:
    """`status` 是唯一允许渲染值的键——它是这一行日志里最有用的一位信息。"""
    rendered = protocol.describe_payload({"task": "x" * 36, "status": "SUCCESS"})

    assert "status=SUCCESS" in rendered
    assert f"task=<len={36}>" in rendered


def test_describe_payload_never_renders_a_large_payload_body() -> None:
    """截图任务的 `payload` 是 Base64，可达数十 MB——一个字符都不该进日志。"""
    blob = "A" * 5000

    rendered = protocol.describe_payload({"status": "SUCCESS", "payload": blob})

    assert "payload=<len=5000>" in rendered
    assert len(rendered) < 200


def test_describe_payload_handles_nested_structures() -> None:
    """嵌套对象也要展开成形状——但**里面的值同样不许出现**。

    注意 `shape_of` 用 `key: 类型` 的写法（它是通用形状渲染器），与顶层字段的
    `key=...` 区分开；这里断言的是「键名出现了、规模出现了、值没出现」。
    """
    rendered = protocol.describe_payload({"outer": {"user": FAKE_USER, "n": 3}})

    assert "outer=" in rendered
    assert "user: str" in rendered
    assert f"len={len(FAKE_USER)}" in rendered
    assert _logged_leak(rendered, FAKE_USER) == []


@pytest.mark.parametrize("value", [None, "a string", 42, ["x"], b"bytes"])
def test_describe_payload_never_raises_on_odd_inputs(value: object) -> None:
    """渲染器自己崩掉会把「请求到底来了没有」这条证据一起弄丢，所以它不许抛。"""
    assert isinstance(protocol.describe_payload(value), str)


def test_shape_of_stops_at_the_depth_limit() -> None:
    """深层 JSON 不该让我们无限展开（构造出来的深结构会拖慢日志路径）。"""
    node: dict[str, object] = {"leaf": "x"}
    for _ in range(10):
        node = {"child": node}

    rendered = protocol.shape_of(node)

    assert "…" not in rendered or "len=" in rendered
    assert len(rendered) < 400


def test_extract_status_refuses_to_guess() -> None:
    """拿不准就留空——不把垃圾值当状态写下去。"""
    assert protocol.extract_status({"status": "SUCCESS"}) == "SUCCESS"
    assert protocol.extract_status({"status": "FAILED"}) == "FAILED"
    assert protocol.extract_status({"status": "MAYBE"}) == ""
    assert protocol.extract_status({"task": "x"}) == ""
    assert protocol.extract_status("not a mapping") == ""


def test_describe_request_mentions_method_path_and_source() -> None:
    rendered = protocol.describe_request(
        kind=protocol.GET_TASK_KIND,
        method="POST",
        path="/api/v1/plugins/extensions/x/maa/getTask",
        client_host="203.0.113.7",
        user_agent="MAA/6.18.0",
    )

    assert "POST" in rendered
    assert "getTask" in rendered
    assert "203.0.113.7" in rendered
    assert "MAA/6.18.0" in rendered


# --- 可证伪：这些断言必须真的能拦住泄露 -------------------------------------


def test_identity_values_never_appear_in_the_rendered_shape() -> None:
    """身份标识的值一个字都不许出现（含 4 字符以上片段）。

    这条断言用**子串扫描**，所以截断、包裹、以及将来有人改成 `value[:8]`
    都会被抓到。
    """
    rendered = protocol.describe_payload(
        {"user": FAKE_USER, "device": FAKE_DEVICE, "task": "t" * 36}
    )

    assert _logged_leak(rendered, FAKE_USER) == []
    assert _logged_leak(rendered, FAKE_DEVICE) == []
