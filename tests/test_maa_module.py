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
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from modules.maa import module as maa_module
from modules.maa import protocol

#: 刻意可辨认的假身份值（见 test_maa_protocol.py 的同一说明）。
FAKE_USER = "ZZQQ-USER-AAAA-1111-BBBB"
FAKE_DEVICE = "ZZDEV-DEVI-CCCC-2222-DDDD"

EXPECTED_ROUTES = {
    f"{maa_module.WEB_ROUTE_PREFIX}{protocol.GET_TASK_SUBPATH}",
    f"{maa_module.WEB_ROUTE_PREFIX}{protocol.REPORT_SUBPATH}",
}


class _FakeJob:
    def __init__(self, name: str, job_id: str) -> None:
        self.name = name
        self.job_id = job_id


class _FakeCronManager:
    """最小的 cron 替身：只记名字、能给 id、能删。"""

    def __init__(self) -> None:
        self.jobs: list[_FakeJob] = []
        self.deleted: list[str] = []
        self.added: list[dict] = []
        self._next = 0

    async def add_basic_job(self, **kwargs):  # noqa: ANN003 - 框架签名就是 kwargs
        self._next += 1
        self.added.append(kwargs)
        job = _FakeJob(str(kwargs.get("name", "")), f"job-{self._next}")
        self.jobs.append(job)
        return job

    async def list_jobs(self):
        return list(self.jobs)

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self.jobs = [job for job in self.jobs if job.job_id != job_id]


def _boot(
    monkeypatch,
    *,
    config: dict | None = None,
    cron_manager: object | None = None,
) -> tuple[maa_module.MaaModule, list[tuple], list[tuple]]:
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
        cron_manager=cron_manager,
    )
    instance = maa_module.MaaModule()
    asyncio.run(instance.initialize(ctx, config if config is not None else {}))
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


def _event(
    *, private: bool = True, admin: bool = True, message: str = "/ak maa"
) -> SimpleNamespace:
    return SimpleNamespace(
        unified_msg_origin="napcat2:FriendMessage:10001",
        is_private_chat=lambda: private,
        is_admin=lambda: admin,
        message_str=message,
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
    assert "队列：空" in text
    assert "还没派过任务" in text


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
    assert "不能当「班换对了」的凭证" in text


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
    assert any("处理子命令" in message for message in messages)


# --- 接入：加模块不改宿主 ---------------------------------------------------


def test_discovery_finds_maa_and_keeps_the_other_modules() -> None:
    """新模块必须被自动发现，且不能把已有模块挤掉。"""
    from core.registry import discover_modules, known_module_names

    found = discover_modules()

    assert "maa" in found
    assert {"shift_reminder", "recruit", "skland"} <= set(known_module_names())


# --- 确认制：没有确认，队列永远是空的 ---------------------------------------


def _text_of(chain: object) -> str:
    """从消息链里取出纯文本（`conftest` 的 MessageChain 把文本段放在 `parts`）。"""
    return "".join(part for part in chain.parts if isinstance(part, str))


def _last_text(sent: list[tuple]) -> str:
    assert sent, "应当发过至少一条消息"
    return _text_of(sent[-1][1])


def test_get_task_stays_empty_until_the_user_confirms(monkeypatch) -> None:
    """**本模块的安全底线**：没有用户确认，端点永远回空任务表。"""
    instance, _registered, _sent = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    responses = [asyncio.run(instance._web_get_task()) for _ in range(5)]

    assert responses == [{"tasks": []}] * 5


def test_confirming_once_queues_exactly_one_task(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)

    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})
    response = asyncio.run(instance._web_get_task())

    assert len(response["tasks"]) == 1
    assert response["tasks"][0]["type"] == "LinkStart", "所有者要的是「跑完全套流程」"


def test_confirming_twice_still_yields_exactly_one_task(monkeypatch) -> None:
    """重复确认既不能变成「多跑一次」，也不该被当成出错。"""
    instance, _registered, sent = _boot(monkeypatch)

    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})

    assert len(asyncio.run(instance._web_get_task())["tasks"]) == 1
    assert "没有再派第二个" in _last_text(sent)


def test_polling_a_hundred_times_reuses_the_same_task_id(monkeypatch) -> None:
    """MAA 一秒轮询一次。一百次里必须始终是同一个 id——协议说同 id 不会重复执行，
    所以「重复下发」是安全的，而「换一个 id」是致命的。"""
    instance, _registered, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})

    ids = {asyncio.run(instance._web_get_task())["tasks"][0]["id"] for _ in range(100)}

    assert len(ids) == 1


def test_response_stays_independent_of_the_request_even_with_a_queued_task(
    monkeypatch,
) -> None:
    """**本文件最重要的一条。**

    端点匿名可达，所以「下发什么」只能来自我们自己的队列；请求里自报的
    `user` / `device` 概不采信——那是谁都能编的字段。将来若有人按请求内容拼任务，
    这条会立刻红。
    """
    instance, _registered, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    bodies: list[object] = [
        None,
        {},
        {"user": FAKE_USER, "device": FAKE_DEVICE},
        {"tasks": [{"id": "attacker", "type": "LinkStart"}]},
        {"device": "attacker"},
        "not even an object",
    ]

    responses = []
    for body in bodies:
        _patch_request(monkeypatch, body=body)
        responses.append(asyncio.run(instance._web_get_task()))

    assert responses[0]["tasks"], "队列里那个任务应当被下发"
    assert responses == [responses[0]] * len(bodies)


# --- 「我自己换」 -----------------------------------------------------------


def test_skip_queues_nothing_and_is_recorded(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))

    asyncio.run(instance.handle_command("maa", _event(message="/ak maa skip")))

    _patch_request(monkeypatch, body={})
    assert asyncio.run(instance._web_get_task()) == {"tasks": []}
    assert instance._last_manual_at is not None, "要记下「这次没让 MAA 跑」供排查"
    assert "不会前进" in _last_text(sent)


def test_cancel_clears_the_queue(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))

    asyncio.run(instance.handle_command("maa", _event(message="/ak maa cancel")))

    _patch_request(monkeypatch, body={})
    assert asyncio.run(instance._web_get_task()) == {"tasks": []}


def test_unknown_subcommand_lists_the_usage(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)

    asyncio.run(instance.handle_command("maa", _event(message="/ak maa frobnicate")))

    text = _last_text(sent)
    assert "未知的 maa 子命令" in text
    assert "/ak maa run" in text


# --- 回报转达：诚实措辞 ------------------------------------------------------


def test_report_is_relayed_without_claiming_success(monkeypatch) -> None:
    """端到端的那条路径：确认 → 取走 → 回报 → 回一句话给用户。"""
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})
    task_id = asyncio.run(instance._web_get_task())["tasks"][0]["id"]
    sent.clear()

    _patch_request(
        monkeypatch,
        body={
            "user": FAKE_USER,
            "device": FAKE_DEVICE,
            "task": task_id,
            "status": "SUCCESS",
            "payload": "",
        },
    )
    assert asyncio.run(instance._web_report_status()) == {"ok": True}

    text = _last_text(sent)
    assert "已执行" in text
    assert "成功" not in text
    assert instance._queue.pending is None


def test_a_duplicate_report_does_not_notify_twice(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})
    task_id = asyncio.run(instance._web_get_task())["tasks"][0]["id"]
    body = {"task": task_id, "status": "SUCCESS"}
    _patch_request(monkeypatch, body=body)
    asyncio.run(instance._web_report_status())
    sent.clear()

    asyncio.run(instance._web_report_status())

    assert sent == [], "重复汇报不该再刷一条一样的消息"


def test_an_unknown_report_is_told_apart(monkeypatch) -> None:
    """插件重启后队列会丢，而 MAA 照跑照汇报——如实说对不上号。"""
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa")))
    sent.clear()

    _patch_request(monkeypatch, body={"task": "n" * 36, "status": "SUCCESS"})
    asyncio.run(instance._web_report_status())

    assert "没派过" in _last_text(sent)


# --- 超时告知：不许静默 ------------------------------------------------------


def test_sweep_tells_the_user_when_nobody_fetched_the_task(monkeypatch) -> None:
    """电脑没开时，MAA 一直不来取——这件事必须让用户知道，而且要说清班次没动。"""
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    sent.clear()
    # 模块自己建队列，所以直接把创建时间推回两小时——比注入时钟更直白，
    # 也不会为了可测性在生产代码上开一个只有测试用的口子。
    assert instance._queue.pending is not None
    instance._queue.pending.created_at = datetime.now() - timedelta(hours=2)

    asyncio.run(instance._sweep())

    text = _last_text(sent)
    assert "没来取" in text
    assert "没有前进" in text
    assert instance._queue.pending is None


def test_sweep_stays_quiet_while_the_task_is_still_fresh(monkeypatch) -> None:
    instance, _registered, sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    sent.clear()

    asyncio.run(instance._sweep())

    assert sent == []
    assert instance._queue.pending is not None


def test_a_notice_is_logged_when_there_is_nowhere_to_send_it(monkeypatch) -> None:
    """没有通知目标时不能静默：内容必须落在日志里（重启后就会这样）。"""
    messages = _collect_logs(monkeypatch)
    instance, _registered, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    instance._umo = ""
    assert instance._queue.pending is not None
    instance._queue.pending.created_at = datetime.now() - timedelta(hours=2)

    asyncio.run(instance._sweep())

    assert any("没有通知目标" in message for message in messages)


# --- 定时任务：注册、清理、不碰别人的 ----------------------------------------


def test_the_sweep_job_is_registered_under_this_module_s_prefix(monkeypatch) -> None:
    cron = _FakeCronManager()

    _boot(monkeypatch, cron_manager=cron)

    assert [kwargs["name"] for kwargs in cron.added] == [maa_module.SWEEP_JOB_NAME]
    assert maa_module.SWEEP_JOB_NAME.startswith(maa_module.JOB_PREFIX)


def test_terminate_removes_its_own_jobs(monkeypatch) -> None:
    cron = _FakeCronManager()
    instance, _registered, _sent = _boot(monkeypatch, cron_manager=cron)

    asyncio.run(instance.terminate())

    assert cron.deleted == ["job-1"]
    assert cron.jobs == []


def test_initialize_clears_stale_own_jobs_but_touches_no_one_else_s(monkeypatch) -> None:
    """清理只按自己的前缀。**别人的任务一个都不许动**——那会拆掉换班提醒。"""
    cron = _FakeCronManager()
    cron.jobs.append(_FakeJob(f"{maa_module.JOB_PREFIX}stale", "stale-1"))
    cron.jobs.append(_FakeJob("ak_toolbox:shift_reminder:早班", "foreign-1"))

    _boot(monkeypatch, cron_manager=cron)

    assert "stale-1" in cron.deleted
    assert "foreign-1" not in cron.deleted
    surviving = {job.job_id for job in cron.jobs}
    assert "foreign-1" in surviving, "别人的任务必须原样留着"
    assert "stale-1" not in surviving


def test_a_missing_cron_manager_degrades_loudly_without_killing_the_module(
    monkeypatch,
) -> None:
    """少一个巡检只意味着「超时不会主动告知」，派任务本身照常。"""
    messages = _collect_logs(monkeypatch)

    instance, _registered, _sent = _boot(monkeypatch)

    assert any("过期巡检未注册" in message for message in messages)
    assert instance.unavailable_reason is None


# --- 配置：非法值必须当场报错 -----------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"task_type": "NotATask"},
        {"task_type": ""},
        {"task_type": "LinkStart-Combat"},
        {"task_ttl_minutes": 0},
        {"task_ttl_minutes": "30"},
        {"fetched_ttl_minutes": -5},
    ],
)
def test_invalid_config_fails_loudly(monkeypatch, config) -> None:
    """**不静默回落到默认值**：用户改了个错值却以为生效了，比直接报错糟得多。"""
    instance = maa_module.MaaModule()

    with pytest.raises(ValueError):
        asyncio.run(instance.initialize(SimpleNamespace(), config))


def test_apply_config_switches_the_task_type(monkeypatch) -> None:
    instance, _registered, _sent = _boot(monkeypatch)

    asyncio.run(instance.apply_config({"task_type": "LinkStart-Base"}))
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})

    assert asyncio.run(instance._web_get_task())["tasks"][0]["type"] == "LinkStart-Base"


def test_apply_config_keeps_a_task_maa_may_already_have_taken(monkeypatch) -> None:
    """重建队列会把 MAA 可能已经取走的任务丢掉，用户就会看到「派了但查不到」。"""
    instance, _registered, _sent = _boot(monkeypatch)
    asyncio.run(instance.handle_command("maa", _event(message="/ak maa run")))
    _patch_request(monkeypatch, body={})
    task_id = asyncio.run(instance._web_get_task())["tasks"][0]["id"]

    asyncio.run(instance.apply_config({"task_ttl_minutes": 5}))

    assert instance._queue.pending is not None
    assert instance._queue.pending.task_id == task_id


# --- 铁律：maa 起不来不许拖垮邻居 -------------------------------------------


class _Sibling:
    """最小兄弟模块，用来验证「一个模块倒下不拖垮别的」。"""

    name = "buddy"
    config_key = "buddy"
    unavailable_reason = None

    def __init__(self) -> None:
        self.started = False

    async def initialize(self, ctx, config):  # noqa: ANN001, ANN201 - 契约签名
        self.started = True

    async def terminate(self) -> None:
        return None

    async def handle_command(self, command: str, event: object) -> bool:
        return False


def test_a_broken_maa_module_does_not_stop_a_sibling() -> None:
    """「森空岛失效绝不许拖垮核心功能」的宿主层形态，用真模块跑一遍。

    这里用局部注册表（`ModuleRegistry.add`）而不是全局的 `register_module`，
    免得把发现出来的工厂改掉、影响别的用例。
    """
    from core.registry import ModuleRegistry

    registry = ModuleRegistry()
    registry.add(maa_module.MaaModule())
    buddy = _Sibling()
    registry.add(buddy)

    asyncio.run(
        registry.start_all(
            ctx=SimpleNamespace(),
            config={"maa": {"task_type": "NotATask"}, "buddy": {}},
        )
    )

    assert buddy.started is True
    assert "buddy" in registry.started_names
    assert [name for name, _reason in registry.failed_modules] == ["maa"]
    assert "NotATask" in registry.failed_modules[0][1], "失败原因要说清是哪个值"
