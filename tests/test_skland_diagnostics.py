"""授权失败的可诊断性：**定位到步骤**、**凭据零外泄**、**措辞能指导行动**。

这个文件的存在理由是一次真实的线上失败：用户扫码后收到

    授权流程失败（没有拿到凭据）：响应里没有 data 对象：字段有 msg

而服务器日志里**什么都没有**——既不知道哪一步，也不知道服务端回了什么。判据是对的，
缺的是定位。本文件把「下次失败必须能定位」钉成护栏。

三条本文件独有的纪律：

1. **凭据外泄是硬红线**：`scanCode` / `token` / `cred` / 授权 `code`（以及 `scanId`、
   `scanUrl` 这两个登录票据）不许以**任何形态**出现在日志与回执里——包括截断、
   包括前后四位、包括服务端消息里的回显。测试用「子串扫描」来钉这件事，而不是只查
   整串：只查整串会漏掉截断与包裹的泄露。
2. **假 logger**：`conftest` 的假 logger 把一切吞掉，那样"日志里没有凭据"这条
   最重要的断言根本无从下手。这里换成会记录的实现。
3. **不发真实请求**：全部走注入的假传输。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import modules.skland.module as skland_module
from modules.skland import api, login
from modules.skland.module import SklandModule


class _FakeCtx:
    """最小假 ctx：只记**真正送出去**的文本。

    刻意不 import `test_skland_module.py` 里的同名类——`tests/` 不是包（没有
    `__init__.py`），跨文件 import 本来就不成立；而且两个测试文件互相依赖会让
    任一文件的内部改动波及另一份。
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.chains: list[Any] = []

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.chains.append(chain)
        self.sent.append(
            "".join(part if isinstance(part, str) else "[图片]" for part in chain.parts)
        )
        return True


class _Event:
    """最小假事件：只提供装配层实际用到的成员。"""

    def __init__(self) -> None:
        self.message_str = ""
        self.unified_msg_origin = "test:FriendMessage:1"

    def is_private_chat(self) -> bool:
        return True

    def is_admin(self) -> bool:
        return False


class _RecordingLogger:
    """把日志记下来。

    本文件要断言的核心之一是「**日志里不许出现凭据**」，而 `conftest` 的假 logger
    什么都吞掉——那样这条护栏就变成了"写了但测不到"。记录下来的实现让它可断言。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, level: str, *args: Any, **kwargs: Any) -> None:
        if not args:
            self.lines.append(f"{level}:")
            return
        template = str(args[0])
        rest = tuple(args[1:])
        if rest:
            try:
                template = template % rest
            except (TypeError, ValueError):
                template = " ".join([template, *(str(a) for a in rest)])
        self.lines.append(f"{level}: {template}")

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self._record("debug", *args, **kwargs)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._record("info", *args, **kwargs)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._record("warning", *args, **kwargs)

    def error(self, *args: Any, **kwargs: Any) -> None:
        self._record("error", *args, **kwargs)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._record("exception", *args, **kwargs)

    def critical(self, *args: Any, **kwargs: Any) -> None:
        self._record("critical", *args, **kwargs)

    @property
    def joined(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    monkeypatch.setattr(skland_module, "logger", recorder)
    return recorder


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录指到 tmp_path（沿用 test_skland_module.py 的做法）。"""
    monkeypatch.setattr(skland_module, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    return tmp_path


#: 看起来像真凭据的假值。**故意含大写字母**：这样它们的 4 字符片段不可能与
#: 小写十六进制摘要、或中文文案偶然撞上，于是"子串扫描"不会误报。
SCAN_CODE = "Sk9QmZ2xR7tL4pWvB8nC1dFgH6jK"
PASSPORT_TOKEN = "Pt5Xr8Yb3Nq7Lm2Wv9ZcK4JhD1sG"
GRANT_CODE = "Gc7Vb4Nm9Qx2Rt5Wz8LkP3JdH6s"
CRED_VALUE = "Cd3Mk8Wq5Zx9Rt4Nb7Jv2LpS6g"
FRESH_TOKEN = "Tk9Wq4Zb7Nm2Rt5Xv8Lc3JdP6h"
INVITE_TICKET = "Sc8Nb5Mq2Wx7Rt4Zv9Lk3JdP6h"

#: 响应里可能藏凭据的每一个位置，逐一验证。
_CREDENTIAL_POSITIONS: list[tuple[str, dict[str, Any]]] = [
    ("data.scanCode", {"status": 0, "data": {"scanCode": SCAN_CODE}}),
    ("data.token", {"status": 0, "data": {"token": PASSPORT_TOKEN}}),
    ("data.cred", {"code": 0, "data": {"cred": CRED_VALUE}}),
    ("data.code", {"status": 0, "data": {"code": GRANT_CODE}}),
    (
        "data 的多键混合",
        {"status": 0, "data": {"cred": CRED_VALUE, "token": FRESH_TOKEN, "scanId": INVITE_TICKET}},
    ),
    ("顶层 scanCode", {"status": 0, "scanCode": SCAN_CODE}),
]


def _substrings(value: str, size: int = 4) -> set[str]:
    return {value[i : i + size] for i in range(len(value) - size + 1)}


def _assert_no_leak(*texts: str) -> None:
    """**本文件最重要的一条断言**：凭据的任何 4 字符以上片段都不许出现。

    为什么按片段查而不是整串：截断（`prefix[:8]`）、包裹（`token=XXX`）、
    服务端回显（"invalid scanCode XXX"）都能躲过整串比对，而**片段查能抓住它们全部**。
    """
    haystack = "\n".join(texts)
    for name, secret in (
        ("scanCode", SCAN_CODE),
        ("passport token", PASSPORT_TOKEN),
        ("grant code", GRANT_CODE),
        ("cred", CRED_VALUE),
        ("fresh token", FRESH_TOKEN),
        ("scanId", INVITE_TICKET),
    ):
        leaked = sorted(s for s in _substrings(secret) if s in haystack)
        assert not leaked, f"{name} 的值泄露到输出里了（片段：{leaked[:6]}）"


def _response(payload: Any, status: int = 200) -> api.HttpResponse:
    return api.HttpResponse(status=status, body=json.dumps(payload).encode())


def _ok(data: dict[str, Any]) -> api.HttpResponse:
    return _response({"status": 0, "data": data})


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class _StepTransport:
    """一个只在**指定步骤**失败、其余步骤正常的假传输。

    这样每个断言都能把失败**钉在某一步**上——否则"报错里出现了第 3 步"这句话
    无法排除"其实是第 4 步报的"。
    """

    def __init__(self, broken_step: login.Step, payload: Any, status: int = 200) -> None:
        self.broken_step = broken_step
        self.payload = payload
        self.status = status
        self.calls: list[str] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> api.HttpResponse:
        self.calls.append(url)
        step = self._step_of(url)
        if step is self.broken_step:
            return _response(self.payload, self.status)
        return self._healthy(url)

    @staticmethod
    def _step_of(url: str) -> login.Step:
        if "gen_scan/login" in url:
            return login.STEP_SCAN_TICKET
        if "scan_status" in url:
            return login.STEP_POLL
        if "token_by_scan_code" in url:
            return login.STEP_TOKEN
        if "oauth2/v2/grant" in url:
            return login.STEP_GRANT
        return login.STEP_CRED

    @staticmethod
    def _healthy(url: str) -> api.HttpResponse:
        if "gen_scan/login" in url:
            return _ok({"scanId": INVITE_TICKET, "scanUrl": "hypergryph://scan_login?scanId=x"})
        if "scan_status" in url:
            return _ok({"scanCode": SCAN_CODE})
        if "token_by_scan_code" in url:
            return _ok({"token": PASSPORT_TOKEN})
        if "oauth2/v2/grant" in url:
            return _ok({"code": GRANT_CODE})
        if "player/binding" in url:
            return _response({"code": 0, "data": {"uid": "u-9"}})
        return _response({"code": 0, "data": {"cred": CRED_VALUE, "token": FRESH_TOKEN}})


def _module_with(transport: Any, data_root: Path) -> tuple[SklandModule, _FakeCtx]:
    """建一个已初始化、且传输层被换掉的模块。

    冷却**设为 0**：本文件要连着发好几次请求。生产里轮询间隔 2 秒、冷却 1 秒，不会撞；
    但测试把间隔压到 0 之后，第二次请求会立刻撞上冷却并抛 `SklandRateLimited`——
    那看起来像「第 2 步失败」，会掩盖真正要断言的东西。这类"脚手架自己造出来的
    故障"正是本项目栽过的那一类，所以这里显式说明而不是默默绕过。
    """
    module, ctx = SklandModule(), _FakeCtx()
    asyncio.run(module.initialize(ctx, {}))
    module._client = api.SklandClient(
        transport=transport,
        cache=api.CachePolicy(cooldown_seconds=0),
    )
    return module, ctx


# --- 一、每一步的失败都要能定位 ---------------------------------------------


@pytest.mark.parametrize(
    ("step", "payload", "http_status"),
    [
        # 第 1 步：通行证侧的错误形态（带 msg、没有 data）
        (login.STEP_SCAN_TICKET, {"msg": "参数不合法", "status": 1}, 400),
        # 第 3 步：**线上实际遇到的形态**——只有一个 msg
        (login.STEP_TOKEN, {"msg": "scanCode 无效"}, 200),
        # 第 4 步：通行证侧的登录过期形态（msg + status + type，HTTP 401）
        (login.STEP_GRANT, {"msg": "登录已过期", "status": 3, "type": "A"}, 401),
        # 第 5 步：森空岛侧的参数错误形态
        (login.STEP_CRED, {"code": 10001, "msg": "参数错误"}, 400),
    ],
)
def test_each_step_failure_names_the_step_and_the_server_response(
    data_root: Path,
    recorded: _RecordingLogger,
    step: login.Step,
    payload: dict[str, Any],
    http_status: int,
) -> None:
    """失败的日志与回执里必须有：**第几步**、**HTTP 状态**、**服务端字段**。"""
    transport = _StepTransport(step, payload, http_status)
    module, ctx = _module_with(transport, data_root)

    if step is login.STEP_SCAN_TICKET:
        asyncio.run(module._cmd_login(_Event()))
    else:
        asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))

    output = recorded.joined + "\n" + "\n".join(ctx.sent)

    assert f"第 {step.index} 步" in output, f"没有指出是哪一步：{output}"
    assert step.title in output, f"没有给出该步的名字：{output}"
    assert f"HTTP {http_status}" in output, f"没有 HTTP 状态：{output}"

    # 服务端回了什么：判据字段与消息原文（脱敏后）都要在
    for key in ("msg", "code", "status"):
        if key in payload:
            assert key in output, f"少了服务端字段 {key}：{output}"
    if isinstance(payload.get("msg"), str):
        assert payload["msg"] in output, f"少了服务端消息原文：{output}"


def test_the_exact_online_failure_shape_is_now_fully_diagnosable(
    data_root: Path, recorded: _RecordingLogger
) -> None:
    """复刻线上那次：「响应里没有 data 对象：字段有 msg」——现在必须说得更清楚。

    它曾经只说"没有 data"，既不知道哪一步也不知道 msg 是什么。
    """
    transport = _StepTransport(login.STEP_TOKEN, {"msg": "scanCode 已过期"}, 200)
    module, ctx = _module_with(transport, data_root)

    asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))

    output = recorded.joined + "\n" + "\n".join(ctx.sent)
    assert "第 3 步" in output
    assert "没有 data 对象" in output, "原本的判据要保留（不许为了好排查放宽）"
    assert "scanCode 已过期" in output, "服务端的 msg 必须带出来"
    assert "HTTP 200" in output
    # 而且日志里确实留下了这一条（线上失败时日志是空的）
    assert recorded.lines, "失败必须写日志——线上那次就是日志里什么都没有"


def test_poll_failure_is_attributed_to_step_two(
    data_root: Path, recorded: _RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skland_module, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(skland_module, "POLL_ATTEMPTS", 2)
    transport = _StepTransport(login.STEP_POLL, {"status": 5, "msg": "未知状态"}, 200)
    module, _ = _module_with(transport, data_root)

    asyncio.run(module._poll_scan(_Event(), INVITE_TICKET, "test:FriendMessage:1"))

    output = recorded.joined
    assert "第 2 步" in output
    assert "未知状态" in output


def test_poll_logs_every_new_state_shape(
    data_root: Path, recorded: _RecordingLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """轮询过程的**状态变化要留痕**——线上那次日志只有「已发送二维码」一行。"""
    monkeypatch.setattr(skland_module, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(skland_module, "POLL_ATTEMPTS", 2)

    seen: list[str] = []

    def transport(method: str, url: str, headers: Any, body: bytes | None) -> Any:
        if "scan_status" in url:
            seen.append(url)
            # 第一次未扫码、第二次已扫码但未确认——两种形态都该被记一笔
            if len(seen) == 1:
                return _response({"msg": "未扫码", "status": 100, "type": "A"})
            return _response({"status": 0, "type": "A"})
        return _response({"code": 0, "data": {}})

    module, _ = _module_with(transport, data_root)
    asyncio.run(module._poll_scan(_Event(), INVITE_TICKET, "test:FriendMessage:1"))

    changes = [line for line in recorded.lines if "扫码状态变化" in line]
    assert len(changes) >= 2, f"应当记下多种状态形态，实际 {changes}"
    assert "waiting" in recorded.joined


# --- 二、凭据绝不外泄（本文件的硬红线） -------------------------------------


@pytest.mark.parametrize(("where", "payload"), _CREDENTIAL_POSITIONS)
def test_describe_response_never_renders_a_credential(where: str, payload: dict[str, Any]) -> None:
    """`describe_response` 是唯一允许渲染响应的出口，它必须守死凭据。"""
    rendered = login.describe_response(payload, http_status=200)
    _assert_no_leak(rendered)
    # 但它仍要给出可诊断的信息：键名在了，长度与摘要也在
    assert "data" in rendered
    for key in payload.get("data") or {}:
        assert key in rendered, f"键名 {key} 应当保留（那是诊断信息）"


def test_step_failure_message_never_renders_a_credential() -> None:
    """连异常消息本身也不许带凭据——它会被直接写进日志。"""
    for _, payload in _CREDENTIAL_POSITIONS:
        for step in (login.STEP_SCAN_TICKET, login.STEP_POLL, login.STEP_TOKEN):
            exc = login.step_failure(step, "测试用失败", payload, http_status=200)
            _assert_no_leak(str(exc), exc.detail)


def test_a_credential_echoed_inside_a_server_message_is_scrubbed() -> None:
    """服务端**可能**把我们的 scanCode 回显在 msg 里（第 3 步的验证器就会）。

    这时没有键名可依，只能靠"长 token 形状"兜底抹掉。这条断言就是那个兜底。
    """
    payload = {"msg": f"invalid scanCode {SCAN_CODE} rejected", "status": 3}
    rendered = login.describe_response(payload, http_status=400)
    _assert_no_leak(rendered)
    assert "invalid scanCode" in rendered, "消息的可读部分要保留（否则日志没用了）"
    assert "已隐去" in rendered, "被抹掉的部分要留下痕迹，否则看不出它曾存在"


def test_module_logs_and_replies_carry_no_credential_anywhere(
    data_root: Path, recorded: _RecordingLogger
) -> None:
    """**端到端那条**：装配层写日志与回执时同样不泄露。"""

    # 每一步都回一个塞满凭据的响应，让最容易泄露的那条路径走一遍
    def transport(method: str, url: str, headers: Any, body: bytes | None) -> Any:
        return _response(
            {
                "status": 0,
                "code": 0,
                "msg": f"echo {SCAN_CODE}",
                "scanCode": SCAN_CODE,
                "data": {
                    "scanCode": SCAN_CODE,
                    "token": PASSPORT_TOKEN,
                    "cred": CRED_VALUE,
                    "code": GRANT_CODE,
                    "scanId": INVITE_TICKET,
                },
            }
        )

    module, ctx = _module_with(transport, data_root)
    asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))
    asyncio.run(module._cmd_login(_Event()))

    _assert_no_leak(recorded.joined, *ctx.sent)


def test_success_path_logs_no_credential_either(
    data_root: Path, recorded: _RecordingLogger
) -> None:
    """成功路径也要过一遍这条红线——成功时同样会写日志（"凭据已落盘"那行）。"""
    module, ctx = _module_with(_StepTransport(login.STEP_CRED, {}), data_root)
    # `_StepTransport` 只在 STEP_CRED 用注入的 payload，所以换一个全健康的传输
    module._client = api.SklandClient(transport=_StepTransport(login.STEP_POLL, {}, 200))
    asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))

    _assert_no_leak(recorded.joined, *ctx.sent)


# --- 三、措辞要能指导行动 ---------------------------------------------------


def test_expired_qr_gets_a_resend_instruction_not_a_dead_end() -> None:
    """「过期了，重发一次」比「没有拿到凭据」有用得多。"""
    action = login.suggest_action(
        step=login.STEP_POLL, payload={"status": 102, "msg": "二维码已失效"}, http_status=200
    )
    assert "过期" in action or "失效" in action
    assert "/ak skland login" in action, "要给出具体命令，不是泛泛的「重试」"


def test_step_three_and_four_suggest_rescanning() -> None:
    """这两步失败最常见的原因是码被用过或确认太慢——那都指向"重扫"。"""
    for step in (login.STEP_TOKEN, login.STEP_GRANT):
        action = login.suggest_action(step=step, payload={"msg": "nope"}, http_status=200)
        assert "/ak skland login" in action


def test_unknown_failure_still_tells_the_user_what_to_keep() -> None:
    """连原因都判不出来时，至少告诉用户"把这句话留给我"。"""
    action = login.suggest_action(step=login.STEP_CRED, payload=None, http_status=500)
    assert action.strip()
    assert "第 N 步" in action or "再试" in action


def test_different_reasons_produce_different_actions() -> None:
    expired = login.suggest_action(
        step=login.STEP_TOKEN, payload={"msg": "登录已过期"}, http_status=401
    )
    plain = login.suggest_action(step=login.STEP_CRED, payload={"msg": "别的错"}, http_status=400)
    assert expired != plain


# --- 四、scanCode 的来源（任务书点名要核实的疑点） ---------------------------


def test_scan_code_comes_from_the_polling_response_under_data() -> None:
    """**核实结论**：scanCode 来自第 2 步轮询响应的 `data.scanCode`，**不是** scanUrl 里的。

    依据有两条，互相独立：① 实测链路记录（`07-skland-api.md` 第 2 步的响应形态）；
    ② MIT 参考实现 `morizero_main.py:530` 取的就是 `(res.get('data') or {}).get('scanCode')`。
    所以这一层**没有取错值**——线上那次失败不在"从哪取值"上。
    """
    reading = login.interpret_scan_status({"status": 0, "data": {"scanCode": SCAN_CODE}})
    assert reading.state is login.ScanState.SCANNED
    assert reading.scan_code == SCAN_CODE


def test_top_level_scan_code_is_still_accepted() -> None:
    """兜底那条路也留着（服务端形态未完全枚举时不至于漏掉）。"""
    reading = login.interpret_scan_status({"status": 0, "scanCode": SCAN_CODE})
    assert reading.scan_code == SCAN_CODE


def test_scan_code_is_passed_through_without_re_encoding() -> None:
    """实测第 3 步的验证器要求 `scanCode` **就是 base64**。

    这里要钉的是：我们把扫到的值**原样**递过去，**不额外编码一次**——再编码一次
    就会变成"对 base64 再做 base64"，服务端只会报参数错误。这条正是"取值/编码方式
    有疑问"那条疑点的答案：**编码方式是对的**。
    """
    body = login.token_request(SCAN_CODE)
    assert body == {"scanCode": SCAN_CODE}


def test_data_url_prefixed_scan_code_is_unwrapped() -> None:
    body = login.token_request(f"data:text/plain;base64,{SCAN_CODE}")
    assert body == {"scanCode": SCAN_CODE}


# --- 五、轮询状态语义的修正（顺带查出的真 bug） -----------------------------


def test_scanned_but_unconfirmed_is_not_a_failure() -> None:
    """`status == 0` 但还没有 `scanCode` = **已扫码、等手机确认**，不是失败。

    此前它落进"未知状态 → FAILED"那一支，会给用户一句「扫码失败」——**用户会以为
    二维码坏了而放弃**，而实际上他只是还没在手机上点确认。参考实现（MIT）明确把
    这个形态当中间态继续等。
    """
    reading = login.interpret_scan_status({"status": 0, "type": "A"})
    assert reading.state is login.ScanState.WAITING
    assert "确认" in reading.message


def test_expired_status_is_its_own_state_not_a_generic_failure() -> None:
    """`status == 102`：二维码失效。单列成一种状态，措辞才能对症。"""
    reading = login.interpret_scan_status({"status": 102, "msg": "二维码已失效"})
    assert reading.state is login.ScanState.EXPIRED
    assert reading.message


def test_unknown_status_without_scan_code_is_still_a_failure() -> None:
    """未知状态**仍然如实报失败**——修上面那条不许把它连坐成"永远在等"。"""
    reading = login.interpret_scan_status({"status": 5, "msg": "说不清"})
    assert reading.state is login.ScanState.FAILED


def test_expired_state_reaches_the_user_with_a_resend_hint(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skland_module, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(skland_module, "POLL_ATTEMPTS", 1)

    def transport(method: str, url: str, headers: Any, body: bytes | None) -> Any:
        return _response({"status": 102, "msg": "二维码已失效"})

    module, ctx = _module_with(transport, data_root)
    asyncio.run(module._poll_scan(_Event(), INVITE_TICKET, "test:FriendMessage:1"))

    joined = "\n".join(ctx.sent)
    assert "失效" in joined
    assert "/ak skland login" in joined
