"""逐次轮询不得刷屏，而"它还连着吗"仍要能回答。

MAA 按协议**每秒**来一次。原先每一次到达都写一条 INFO，就是一天 86400 条——
后果不只是吵：「首次收到请求」「任务被取走」「回报到达」「超时作废」这些真正
需要看见的事全被埋在同一秒一条的心跳里，而服务器上的容器日志没有轮转保证。

这里的测试钉住三件事：
1. 正常轮询**不再**逐条写 INFO（条数有上界，与次数无关）；
2. 首次到达与周期性摘要**仍在** INFO（可观测性没丢，只是换了节奏）；
3. 回报、异常这些**一条都不许被节流吃掉**。

时钟是**注入**的（`instance._now`），所以没有任何 sleep——节流是时间行为，
用睡眠去测它既慢又不稳。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from modules.maa import module as maa_module
from modules.maa import protocol

FAKE_USER = "probe-user"
FAKE_DEVICE = "e" * 32
START = datetime(2026, 9, 29, 14, 0, 0)


def _render(args: tuple[object, ...]) -> str:
    if not args:
        return ""
    template = str(args[0])
    if len(args) > 1:
        try:
            return template % tuple(args[1:])
        except Exception:  # noqa: BLE001 - 与另一个测试文件同一取舍
            pass
    return " ".join(str(arg) for arg in args)


def _collect(monkeypatch) -> tuple[list[str], list[str]]:
    """收日志，INFO 档与 DEBUG 档**分开**。

    混在一起的话，"INFO 条数有上界"这条断言就没有意义了——而它正是本包的主题。
    """
    info: list[str] = []
    debug: list[str] = []
    for level in ("info", "warning", "error", "exception"):
        monkeypatch.setattr(
            maa_module.logger, level, lambda *args, _m=info, **kw: _m.append(_render(args))
        )
    monkeypatch.setattr(
        maa_module.logger, "debug", lambda *args, _m=debug, **kw: _m.append(_render(args))
    )
    return info, debug


def _boot(monkeypatch) -> maa_module.MaaModule:
    registered: list[tuple] = []

    async def send_message(umo, chain):
        return True

    ctx = SimpleNamespace(
        register_web_api=lambda route, handler, methods, desc: registered.append(route),
        send_message=send_message,
        cron_manager=None,
    )
    instance = maa_module.MaaModule()
    # 时钟注入：不 sleep 也能测一小时的节流。
    clock = {"now": START}
    instance._now = lambda: clock["now"]
    instance._clock = clock  # type: ignore[attr-defined] - 测试自己推进它
    asyncio.run(instance.initialize(ctx, {}))
    return instance


def _patch_request(monkeypatch, body: object, *, explode: bool = False) -> None:
    async def _json(default=None):
        if explode:
            raise RuntimeError("模拟读取请求体失败")
        return body

    monkeypatch.setattr(
        maa_module,
        "request",
        SimpleNamespace(
            method="POST",
            path=f"{maa_module.WEB_ROUTE_PREFIX}{protocol.GET_TASK_SUBPATH}",
            client_host="203.0.113.7",
            headers={"user-agent": "MaaWpfGui/v6.18.0"},
            json=_json,
        ),
    )


def _poll(instance: maa_module.MaaModule, times: int) -> None:
    """按秒推进时钟并记一次到达。

    **直接驱动记录层，不走 HTTP 端点**。模拟三个周期要一万多次调用，而每次都
    建一个事件循环、走一遍路由——测试的成本会比被测代码高三个数量级，而且
    第一版就是这么写的：跑了五分钟没结束。端点接线另有一条专门的用例覆盖。
    """
    for _ in range(times):
        instance._clock["now"] += timedelta(seconds=1)  # type: ignore[attr-defined]
        instance._record(protocol.GET_TASK_KIND, {})


def _poll_via_endpoint(instance: maa_module.MaaModule, times: int) -> None:
    """走真实端点的少量调用，用来证明节流确实挂在请求路径上。"""
    for _ in range(times):
        instance._clock["now"] += timedelta(seconds=1)  # type: ignore[attr-defined]
        asyncio.run(instance._web_get_task())


# --- 一、正常轮询不再刷屏 ---------------------------------------------------


def test_a_thousand_polls_do_not_produce_a_thousand_lines(monkeypatch) -> None:
    """**本文件最重要的一条**：INFO 条数与轮询次数**不成正比**。

    1000 次轮询（约 17 分钟）原先会产出 1000 条；现在应当只有「首次」这一条，
    因为一个小时的摘要周期还没到。

    只看**装载之后**的增量：`initialize` 本身会记几条（配置回退的 WARN、
    模块已装载），把它们算进来会让断言数错对象——第一版就是这么写的。
    """
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    baseline = len(info)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll(instance, 1000)

    after_load = info[baseline:]
    poll_lines = [m for m in after_load if "收到请求" in m]
    assert poll_lines == [], f"逐次轮询仍在写 INFO：{poll_lines[:3]}"
    assert len(after_load) == 1, f"只该留「首次」这一条：{after_load}"
    assert any("首次收到 MAA 请求" in m for m in after_load)
    assert instance._arrivals == 1000, "节流不能影响计数——/ak maa 仍要报真实次数"


def test_first_arrival_is_still_logged(monkeypatch) -> None:
    """首次那条是「公网可达 + 证书被接受 + 轮询已开始」的唯一证据，不许丢。"""
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    asyncio.run(instance._web_get_task())

    assert any("首次收到 MAA 请求" in m for m in info), info
    assert any("证书被接受" in m for m in info), "首次那条要保留可读的理由说明"


def test_per_poll_detail_survives_at_debug(monkeypatch) -> None:
    """降级不是删除：排查时打开 DEBUG 还看得到每一次的形状。"""
    _info, debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)

    _poll(instance, 3)

    assert len(debug) == 3, f"DEBUG 应当逐次记录：{debug}"
    assert all("收到请求" in m for m in debug)


def test_the_throttle_is_on_the_request_path(monkeypatch) -> None:
    """走真实端点也要安静——节流挂在**请求路径**上，不只是记录层。

    只测记录层会让"接线"没人管：把 `_record` 里的节流拆掉、但保留端点不调用它，
    测试照样绿。本项目为这种假绿栽过一次（改 `cancel` 时只测了零件没测接线）。
    """
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll_via_endpoint(instance, 20)

    assert not [m for m in info if "收到请求" in m], info
    assert instance._arrivals == 20


# --- 二、周期性摘要：还连着吗 -----------------------------------------------


def test_no_heartbeat_before_the_interval(monkeypatch) -> None:
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    # 差一秒不到一个周期
    _poll(instance, maa_module.HEARTBEAT_INTERVAL_SECONDS - 1)

    assert not any("持续连接中" in m for m in info), info


def test_heartbeat_appears_after_the_interval(monkeypatch) -> None:
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll(instance, maa_module.HEARTBEAT_INTERVAL_SECONDS + 1)

    beats = [m for m in info if "持续连接中" in m]
    assert len(beats) == 1, f"一个周期应当恰好一条摘要：{beats}"
    # 摘要要说人话：多少分钟、多少次——只说"还在连"没有信息量。
    assert "分钟" in beats[0] and "次" in beats[0], beats[0]


def test_heartbeat_repeats_once_per_interval_not_once_per_poll(monkeypatch) -> None:
    """跑三个周期：应当恰好三条摘要，而不是三千条。"""
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll(instance, maa_module.HEARTBEAT_INTERVAL_SECONDS * 3 + 5)

    beats = [m for m in info if "持续连接中" in m]
    assert len(beats) == 3, f"三个周期该有三条摘要，实际 {len(beats)} 条"


def test_heartbeat_counter_resets_so_each_summary_covers_its_own_window(monkeypatch) -> None:
    """每条摘要只报**它自己那一窗**的请求数，不是累计——否则数字只增不减，看不出节奏。"""
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll(instance, maa_module.HEARTBEAT_INTERVAL_SECONDS + 1)

    beat = next(m for m in info if "持续连接中" in m)
    # 第二窗只有 1 次请求（最后那一下），若误用累计值就会是个大数。
    assert str(maa_module.HEARTBEAT_INTERVAL_SECONDS) in beat, beat
    assert instance._heartbeat_arrivals == 0, "摘要之后计数应当归零"


def test_a_restart_logs_first_arrival_again(monkeypatch) -> None:
    """重启后 `_heartbeat_at` 是 None：新实例的第一条仍是「首次」。

    这是**有意**的——重启之后确实还没有任何到达记录，而"这次重启后它连上了吗"
    正是这个模块最常被问的问题。
    """
    info, _debug = _collect(monkeypatch)
    _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})
    fresh = maa_module.MaaModule()
    fresh._now = lambda: START  # type: ignore[assignment]

    asyncio.run(fresh.initialize(SimpleNamespace(register_web_api=lambda *a: None), {}))
    asyncio.run(fresh._web_get_task())

    assert any("首次收到 MAA 请求" in m for m in info), info


# --- 三、节流不许吃掉事件 ---------------------------------------------------


def test_reports_are_never_throttled(monkeypatch) -> None:
    """汇报是真正的事件：即使在一秒一条的轮询中间，也必须每次留痕。

    这里**真的排一个任务再回报它**，因为"队列里本来就有任务"不是默认状态——
    第一版直接回报一个不存在的 id，于是得到的是「对不上号」，而不是
    「结清」，断言自然落空。
    """
    info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    instance._now = datetime.now
    result = instance._queue.enqueue(instance._manual_slot(), task_type=instance._task_type)
    task_id = result.task_id

    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})
    asyncio.run(instance._web_get_task())

    baseline = len(info)
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
    asyncio.run(instance._web_report_status())

    reported = info[baseline:]
    assert any("结清" in m for m in reported), reported
    assert any("status=SUCCESS" in m for m in reported), reported


def test_errors_are_never_throttled(monkeypatch) -> None:
    """异常一条都不许被节流。丢错误日志比刷屏严重得多。"""
    errors: list[str] = []
    monkeypatch.setattr(
        maa_module.logger, "error", lambda *args, **kw: errors.append(_render(args))
    )
    for level in ("info", "warning", "exception"):
        monkeypatch.setattr(maa_module.logger, level, lambda *args, **kw: None)
    monkeypatch.setattr(maa_module.logger, "debug", lambda *args, **kw: None)

    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE}, explode=True)

    for _ in range(50):
        asyncio.run(instance._web_get_task())

    assert len(errors) == 50, f"每一次处理失败都该记 ERROR，实际 {len(errors)}"
    assert instance._failures == 50


def test_status_text_still_answers_whether_maa_is_connected(monkeypatch) -> None:
    """节流之后，「MAA 还连着吗」这个问题**必须仍然能回答**。

    这是本包的核心约束：安静不等于失明。轮询被降级，但统计口径一个字没动。
    """
    _info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    _poll(instance, 7)

    text = instance._status_text()
    assert "已收到 7 次请求" in text, text
    assert "最近一次" in text, text


@pytest.mark.parametrize("kind", [protocol.GET_TASK_KIND, protocol.REPORT_KIND])
def test_throttle_does_not_drop_the_arrival_counters(monkeypatch, kind: str) -> None:
    """计数与节流是两件事：降级只动日志，不动 `/ak maa` 看到的事实。"""
    _info, _debug = _collect(monkeypatch)
    instance = _boot(monkeypatch)
    _patch_request(monkeypatch, body={"user": FAKE_USER, "device": FAKE_DEVICE})

    for _ in range(5):
        instance._record(kind, {})

    assert instance._arrivals == 5
    if kind == protocol.GET_TASK_KIND:
        assert instance._get_task_hits == 5
    else:
        assert instance._report_hits == 5
