"""`modules/maa/module.py` 的装配层测试。

这一层要证明三件事：

1. **领任务端点结构上不可能执行任何东西**——响应与请求内容无关（同一份常量）；
2. **「MAA 连上了」这件事真的可见**（首次到达有一条醒目日志，`/ak maa` 报出来）；
3. **身份标识的值一个字都不进日志**——而且这条断言是**可证伪**的。

异常路径也在覆盖里：处理器出错必须给 MAA 一个**明确**的响应并留下 ERROR，
否则 MAA 侧看到的是超时，我们会误判成证书或网络问题。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from modules.maa import module as maa_module
from modules.maa import protocol

#: 刻意可辨认的假身份值（见 test_maa_protocol.py 的同一说明）。
FAKE_USER = "ZZQQ-USER-AAAA-1111-BBBB"
FAKE_DEVICE = "ZZDEV-DEVI-CCCC-2222-DDDD"

EXPECTED_ROUTES = {
    f"{maa_module.WEB_ROUTE_PREFIX}{protocol.GET_TASK_SUBPATH}",
    f"{maa_module.WEB_ROUTE_PREFIX}{protocol.REPORT_SUBPATH}",
}


def _boot(monkeypatch) -> tuple[maa_module.MaaModule, list[tuple], list[tuple]]:
    """把模块跑起来，返回 (模块, 已注册路由, 已发送消息)。"""
    registered: list[tuple] = []
    sent: list[tuple] = []

    async def send_message(umo, chain):
        sent.append((umo, chain))
        return True

    ctx = SimpleNamespace(
        register_web_api=lambda route, handler, methods, desc: registered.append(
            (route, handler, methods, desc)
        ),
        send_message=send_message,
    )
    instance = maa_module.MaaModule()
    asyncio.run(instance.initialize(ctx, {}))
    return instance, registered, sent


def _patch_request(
    monkeypatch,
    *,
    body: object = None,
    headers: dict[str, str] | None = None,
    method: str = "POST",
    path: str = "/api/v1/plugins/extensions/x/maa/getTask",
    host: str = "203.0.113.7",
    explode: bool = False,
) -> None:
    """换掉模块看到的 `request`。

    `explode=True` 让读请求体抛错，用来验证异常路径会给 MAA 一个明确响应。
    """

    async def _json(default=None):
        if explode:
            raise RuntimeError("模拟读取请求体失败")
        return default if body is None else body

    monkeypatch.setattr(
        maa_module,
        "request",
        SimpleNamespace(
            method=method,
            path=path,
            client_host=host,
            headers=headers if headers is not None else {},
            json=_json,
        ),
    )


def _collect_logs(monkeypatch) -> list[str]:
    """收集日志，并**按 `%` 模板渲染**。

    为什么要渲染：stub logger 只记录参数，不做格式化。若直接把参数拼起来，
    断言里看到的会是 `处理%s请求时出错` 这个**模板串**，而不是真实文案——
    那样断言就永远对不上，而人只会以为"消息没记"。
    """
    messages: list[str] = []

    def _render(args: tuple[object, ...]) -> str:
        if not args:
            return ""
        template = str(args[0])
        if len(args) > 1:
            try:
                return template % tuple(args[1:])
            except Exception:  # noqa: BLE001 - 格式化失败就退回拼接
                pass
        return " ".join(str(arg) for arg in args)

    for level in ("info", "warning", "error", "exception"):
        monkeypatch.setattr(
            maa_module.logger,
            level,
            lambda *args, _m=messages, **kwargs: _m.append(_render(args)),
        )
    return messages


def _event(*, private: bool = True, admin: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        unified_msg_origin="napcat2:FriendMessage:10001",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        message_str="/ak maa",
    )


def _leaked(haystack: str, secret: str) -> list[str]:
    return [
        secret[start : start + 4]
        for start in range(len(secret) - 3)
        if secret[start : start + 4] in haystack
    ]


# --- 路由注册 ---------------------------------------------------------------


def test_initialize_registers_exactly_the_two_protocol_routes(monkeypatch) -> None:
    """路由必须带插件名前缀（否则 Dashboard 转发匹配不到），且只注册协议要求的两个。"""
    _instance, registered, _sent = _boot(monkeypatch)

    assert {route for route, _h, _m, _d in registered} == EXPECTED_ROUTES
    for _route, _handler, methods, _desc in registered:
        assert methods == ["POST"], "协议要求两个端点都接受 POST"


def test_register_web_api_absent_degrades_loudly(monkeypatch) -> None:
    """取不到 register_web_api 时：抛出去会连累整个插件，所以只降级 + 留痕。"""
    messages = _collect_logs(monkeypatch)
    instance = maa_module.MaaModule()

    asyncio.run(instance.initialize(SimpleNamespace(), {}))

    assert any("注册失败" in message for message in messages)
    assert instance.unavailable_reason is not None


def test_module_is_available_when_routes_registered(monkeypatch) -> None:
    """等 MAA 来连是**正常状态**，不该报成不可用。"""
    instance, _registered, _sent = _boot(monkeypatch)

    assert instance.unavailable_reason is None


# --- 领任务端点：结构上不可能执行任何东西 -----------------------------------


def test_get_task_returns_an_empty_task_list(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    assert asyncio.run(instance._web_get_task()) == {"tasks": []}


def test_get_task_response_does_not_depend_on_the_request(monkeypatch) -> None:
    """**本文件最重要的一条**：响应与请求内容无关。

    这是「端点结构上不可能被用来执行外部指令」的可测形式——它不读任何字段来决定
    下发什么。将来若有人按请求内容拼任务，这条会立刻红。
    """
    instance, _registered, _sent = _boot(monkeypatch)
    bodies: list[object] = [
        None,
        {},
        {"user": FAKE_USER, "device": FAKE_DEVICE},
        {"tasks": [{"id": "attacker", "type": "LinkStart"}]},
        {"user": {"nested": ["deep"]}},
        "not even an object",
        [1, 2, 3],
    ]

    responses = []
    for body in bodies:
        _patch_request(monkeypatch, body=body)
        responses.append(asyncio.run(instance._web_get_task()))

    assert responses == [{"tasks": []}] * len(bodies)


# --- 汇报端点 ---------------------------------------------------------------


def test_report_status_records_and_acknowledges(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(
        monkeypatch,
        body={
            "user": FAKE_USER,
            "device": FAKE_DEVICE,
            "task": "t" * 36,
            "status": "SUCCESS",
            "payload": "",
        },
    )

    assert asyncio.run(instance._web_report_status()) == {"ok": True}
    assert instance._report_hits == 1
    assert instance._last_status == "SUCCESS"


def test_first_arrival_is_logged_prominently(monkeypatch) -> None:
    """第一次到达就是「证书那关过了」的证据，必须显眼。"""
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    asyncio.run(instance._web_get_task())

    joined = "\n".join(messages)
    assert "首次收到 MAA 请求" in joined
    assert "证书" in joined


def test_arrival_is_logged_with_method_path_and_user_agent(monkeypatch) -> None:
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(
        monkeypatch,
        body={"user": FAKE_USER, "device": FAKE_DEVICE},
        headers={"user-agent": "MAA/6.18.0"},
        path="/api/v1/plugins/extensions/x/maa/getTask",
    )

    asyncio.run(instance._web_get_task())

    joined = "\n".join(messages)
    assert "MAA/6.18.0" in joined
    assert "getTask" in joined
    assert "203.0.113.7" in joined


# --- 异常路径：给 MAA 明确响应，不许静默 --------------------------------


def test_get_task_failure_returns_an_explicit_response(monkeypatch) -> None:
    """出错时必须**回答**（哪怕是个错误），不能让 MAA 超时。

    超时会被误判成证书/网络问题——那正是本次调研里列为第一风险的东西。
    """
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, explode=True)

    response = asyncio.run(instance._web_get_task())

    assert isinstance(response, dict)
    assert response.get("status_code") == 500
    assert any("处理领任务请求时出错" in message for message in messages)


def test_report_status_failure_returns_an_explicit_response(monkeypatch) -> None:
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, explode=True)

    response = asyncio.run(instance._web_report_status())

    assert response.get("status_code") == 500
    assert any("处理汇报状态请求时出错" in message for message in messages)


# --- 身份标识绝不落盘（可证伪） ---------------------------------------------


def test_identity_values_never_reach_the_log(monkeypatch) -> None:
    """`user` / `device` 的值一个字都不许出现（含 4 字符以上片段）。

    它们每个请求都带，按秒轮询——写进日志等于把用户标识刷满磁盘，而
    「连上了没有」与「是谁」无关。
    """
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    asyncio.run(instance._web_get_task())

    joined = "\n".join(messages)
    assert _leaked(joined, FAKE_USER) == []
    assert _leaked(joined, FAKE_DEVICE) == []


def test_status_text_never_contains_identity_values(monkeypatch) -> None:
    """`/ak maa` 的输出是回到 QQ 的——泄露面更大，同样不许带值。"""
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    asyncio.run(instance._web_get_task())
    text = instance._status_text()

    assert _leaked(text, FAKE_USER) == []
    assert _leaked(text, FAKE_DEVICE) == []


# --- /ak maa ---------------------------------------------------------------


def test_status_text_before_any_arrival_says_so(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)

    text = instance._status_text()

    assert "还没收到过请求" in text
    assert "不会执行任何操作" in text


def test_status_text_after_arrival_reports_counts_and_the_success_caveat(
    monkeypatch,
) -> None:
    """`SUCCESS` 不等于成功——不解释这一点，用户会把它当成「换班成功了」。"""
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "status": "SUCCESS"})
    asyncio.run(instance._web_report_status())

    text = instance._status_text()

    assert "已收到 1 次请求" in text
    assert "status=SUCCESS" in text
    assert "别当成功凭证" in text


def test_handle_command_replies_and_stops(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)

    handled = asyncio.run(instance.handle_command("maa", _event()))

    assert handled is True
    assert len(sent) == 1


def test_handle_command_ignores_other_subcommands(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)

    assert asyncio.run(instance.handle_command("status", _event())) is False
    assert sent == []


def test_handle_command_denies_group_non_admin(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)

    handled = asyncio.run(instance.handle_command("maa", _event(private=False, admin=False)))

    assert handled is True
    assert len(sent) == 1


def test_handle_command_does_not_throw_at_the_host(monkeypatch) -> None:
    """指令路径的异常必须自己兜住：宿主接住只会打一段栈，用户看不懂。"""
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)

    def _explode() -> str:
        raise RuntimeError("模拟状态组装失败")

    monkeypatch.setattr(instance, "_status_text", _explode)

    handled = asyncio.run(instance.handle_command("maa", _event()))

    assert handled is True
    assert any("处理 /ak maa 时出错" in message for message in messages)


# --- 接入：加模块不改宿主 ---------------------------------------------------


def test_discovery_finds_maa_and_keeps_the_other_modules() -> None:
    """新模块必须被自动发现，且不能把已有模块挤掉。"""
    from core.registry import discover_modules, known_module_names

    found = discover_modules()

    assert "maa" in found
    assert {"shift_reminder", "recruit", "skland"} <= set(known_module_names())
