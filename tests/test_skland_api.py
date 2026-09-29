"""`modules/skland/api.py` 的测试：签名、错误码语义、限流、健康检查。

## 关于签名「正确性」的边界（必须说清）

这里能证明的是**字节序与签名输入的形状**（金标把 `path+body+ts+json` 的拼接钉死，
调用点那条钉子住「`path` 必须含 `/api/v1`」），**不能证明服务端认这个签名**——
那需要一次真实授权，而实测发现服务端对未登录请求根本不校验签名（`07-skland-api.md` §3）。
真正验证它的地方是 `/ak skland check`：签名错了会回 10001/10000 而不是 10002。
这条边界写进测试而不是藏着。

签名输入与参考实现的逐字节比对见 `07-skland-api.md` §14。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse

import pytest

from modules.skland import api


def _client(handler, **kwargs) -> api.SklandClient:  # noqa: ANN001
    """构造一个把网络换成 `handler` 的客户端（**不发真实请求**）。

    默认**关掉冷却**：这个文件大多在测签名与语义，不该被限流干扰；
    限流本身有专门用例，那里显式传自己的 `CachePolicy`。
    """
    calls: list[tuple[str, str, dict, bytes | None]] = []

    def transport(method: str, url: str, headers, body):  # noqa: ANN001
        calls.append((method, url, dict(headers), body))
        return handler(method, url, headers, body)

    kwargs.setdefault("cache", api.CachePolicy(cooldown_seconds=0.0))
    client = api.SklandClient(transport=transport, **kwargs)
    client.calls = calls  # type: ignore[attr-defined]
    return client


def _json_response(payload: object, status: int = 200) -> api.HttpResponse:
    return api.HttpResponse(status=status, body=json.dumps(payload).encode("utf-8"))


# --- 签名 -------------------------------------------------------------------


def test_signature_matches_frozen_golden() -> None:
    """字节序金标：`path + query/body + ts + json(hdr)` 的顺序不许悄悄变。

    ⚠️ `path` 是**完整路径**（含 `/api/v1`）。这条金标值随 §14 那次修正更新过：
    旧值 `3dd33f97…` 对应剥掉前缀的 `"/game/player/binding"`，是**错的**——
    真机上只回「凭据无效」，见 `docs/project-plan/07-skland-api.md` §14。
    """
    headers = api.sign_headers(
        cred="CRED-X",
        token="TOKEN-Y",
        path="/api/v1/game/player/binding",
        query_or_body="uid=42",
        timestamp=1700000000,
    )
    assert headers["sign"] == "c9fc6b9185c96cae0682bbd7f48aec84"
    assert headers["cred"] == "CRED-X"
    assert headers["timestamp"] == "1700000000"
    # 三个身份字段要**存在且为空串**，不是省略——实测如此
    assert headers["platform"] == ""
    assert headers["dId"] == ""
    assert headers["vName"] == ""
    assert headers["User-Agent"] == api.HONEST_USER_AGENT


def test_signature_changes_with_the_inputs_that_feed_it() -> None:
    """签名输入是 `token + path + query/body + ts + hdr`。

    ⚠️ `cred` **不在**签名输入里——它只进请求头。所以改 cred 不该改签名；
    这条断言是刻意的，免得后人「顺手」把它塞进签名。
    """
    base = {"cred": "c", "token": "t", "path": "/p", "query_or_body": "q", "timestamp": 1700000000}
    original = api.sign_headers(**base)["sign"]
    for key, value in (
        ("token", "t2"),
        ("path", "/p2"),
        ("query_or_body", "q2"),
        ("timestamp", 1700000001),
    ):
        assert api.sign_headers(**{**base, key: value})["sign"] != original, key

    same = api.sign_headers(**{**base, "cred": "another-cred"})
    assert same["sign"] == original
    assert same["cred"] == "another-cred"


def test_signature_defaults_to_now_minus_one() -> None:
    """实测做法是 `ts = now - 1`（规避时钟偏差），默认必须保留这个行为。"""
    import time

    before = int(time.time()) - 1
    headers = api.sign_headers(cred="c", token="t", path="/p")
    assert before <= int(headers["timestamp"]) <= before + 1


def test_signature_is_bound_to_the_actual_request_bytes() -> None:
    """**签名输入必须与实际发出的请求一致**——这是最容易错、也最难查的一处。

    做法：把请求里带的 `timestamp` 取出来，按同一 path/query/body 重算一遍，
    断言与真实发出的 `sign` 相同。改动拼接顺序或漏掉 query 都会让这条红。
    """
    seen: list[tuple[str, str, dict, bytes | None]] = []

    def handler(method, url, headers, body):  # noqa: ANN001
        seen.append((method, url, dict(headers), body))
        return _json_response({"code": 0, "data": {}})

    client = _client(handler, cred="cred", token="tok")
    client.player_info("42")  # GET，带 query
    client.sign_in()  # POST，无 body

    assert [item[0] for item in seen] == ["GET", "POST"]

    method, url, headers, body = seen[0]
    assert body is None
    parsed = urllib.parse.urlsplit(url)
    assert parsed.query == "uid=42", "GET 必须带 query，否则签名与实际不符"
    expected = api.sign_headers(
        cred="cred",
        token="tok",
        path=parsed.path,  # 完整路径（含 /api/v1）——见 07-skland-api.md §14
        query_or_body=parsed.query,
        timestamp=int(headers["timestamp"]),
    )
    assert headers["sign"] == expected["sign"]

    # POST：没有 body 时签的是空串（**这一步未经真机验证**，见 api.sign_in 的说明）
    _, url, headers, body = seen[1]
    assert body is None
    expected = api.sign_headers(
        cred="cred",
        token="tok",
        path=urllib.parse.urlsplit(url).path,  # 完整路径，不剥 /api/v1
        query_or_body="",
        timestamp=int(headers["timestamp"]),
    )
    assert headers["sign"] == expected["sign"]


def test_signed_path_keeps_the_api_v1_prefix() -> None:
    """签名里必须带 `/api/v1` 前缀——这条钉的是**调用点**，不是 `sign_headers`。

    缺陷就长在调用点：`sign_headers` 一直是好的，是它拿到了一条被剥掉前缀的
    `path`（`/game/player/binding`），于是签名整体错位。真机表现只是
    「凭据无效（10000）」，错误码完全指不到真因，所以这里用**逐字节重算**
    钉住实际参与签名的那个串。
    """
    seen: list[tuple[str, dict, bytes | None]] = []

    def handler(method, url, headers, body):  # noqa: ANN001
        seen.append((url, dict(headers), body))
        return _json_response({"code": 0, "data": {"list": []}})

    client = _client(handler, cred="CRED-X", token="TOKEN-Y")
    client.health_check()  # 打的就是 /api/v1/game/player/binding

    url, headers, _ = seen[0]
    assert url == "https://zonai.skland.com/api/v1/game/player/binding"

    ts = int(headers["timestamp"])
    # 与参考实现逐字节一致：完整路径 + 空 query + ts + 紧凑 JSON（空身份字段）。
    signed = (
        "/api/v1/game/player/binding"
        + ""
        + str(ts)
        + '{"platform":"","timestamp":"'
        + str(ts)
        + '","dId":"","vName":""}'
    )
    digest = hmac.new(b"TOKEN-Y", signed.encode(), hashlib.sha256).hexdigest()
    assert headers["sign"] == hashlib.md5(digest.encode()).hexdigest()

    # 反过来钉一次：若有人把前缀剥掉，重算值必然不同（防止"改回去也通过"）。
    stripped = (
        "/game/player/binding"
        + ""
        + str(ts)
        + '{"platform":"","timestamp":"'
        + str(ts)
        + '","dId":"","vName":""}'
    )
    stripped_digest = hmac.new(b"TOKEN-Y", stripped.encode(), hashlib.sha256).hexdigest()
    assert headers["sign"] != hashlib.md5(stripped_digest.encode()).hexdigest()


def test_post_with_a_body_signs_the_exact_json_text() -> None:
    """POST 的签名输入是**实际发出的那段 JSON 文本**（紧凑分隔符）。"""
    seen: list[tuple[str, dict, bytes | None]] = []

    def handler(method, url, headers, body):  # noqa: ANN001
        seen.append((url, dict(headers), body))
        return _json_response({"status": 0, "data": {}})

    client = _client(handler)
    client.call_unauthenticated("POST", f"{api.HYPERGRYPH_BASE}/x", {"b": 2, "a": 1})

    _, headers, body = seen[0]
    # 通行证侧不签名，但请求体本身要紧凑（签名侧要一致，这里顺手锁住形状）
    assert body == b'{"b":2,"a":1}'
    assert "sign" not in headers


def test_unauthenticated_call_sends_no_credentials_and_no_signature() -> None:
    """通行证侧（扫码前四步）**不该**带 cred/sign，也不该因为没凭据就抛。"""

    def handler(method, url, headers, body):  # noqa: ANN001
        assert "cred" not in headers
        assert "sign" not in headers
        return _json_response({"status": 0, "data": {"scanId": "x"}})

    client = _client(handler)
    assert client.has_credentials is False
    payload = client.call_unauthenticated(
        "POST", f"{api.HYPERGRYPH_BASE}/general/v1/gen_scan/login", {}
    )
    assert payload["data"]["scanId"] == "x"


def test_signed_call_without_credentials_raises_before_touching_the_network() -> None:
    client = _client(lambda *a, **k: pytest.fail("不该发出请求"))
    with pytest.raises(api.SklandUnauthorized):
        client.health_check()


# --- 错误码语义 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (api.CODE_BAD_CRED, api.SklandUnauthorized),
        (api.CODE_NOT_LOGGED_IN, api.SklandLoginExpired),
        (api.CODE_BAD_PARAM, api.SklandParamError),
        (99999, api.SklandError),
    ],
)
@pytest.mark.parametrize("http_status", [400, 500])
def test_code_decides_not_http_status(
    code: int, expected: type[Exception], http_status: int
) -> None:
    """**同一个码在不同 HTTP 状态下含义相同**——实测 10001 出现过 400 与 500。

    把 5xx 当「服务器故障、重试」会让客户端永久重试一个永久性错误。
    """
    client = _client(
        lambda *a, **k: _json_response({"code": code, "message": "boom"}, http_status),
        cred="c",
        token="t",
    )
    with pytest.raises(expected) as info:
        client.health_check()
    assert info.value.code == code
    assert "boom" in str(info.value)


def test_success_returns_the_payload() -> None:
    client = _client(
        lambda *a, **k: _json_response({"code": 0, "data": {"uid": "u"}}), cred="c", token="t"
    )
    assert client.health_check()["data"] == {"uid": "u"}


def test_boolean_code_is_not_treated_as_an_integer_code() -> None:
    """`True` 是 `int` 的子类，但 `"code": true` 不是错误码——别把它当成 1。"""
    client = _client(
        lambda *a, **k: _json_response({"code": True, "data": {}}), cred="c", token="t"
    )
    assert client.health_check()["code"] is True


def test_non_json_body_is_a_transport_error() -> None:
    client = _client(
        lambda *a, **k: api.HttpResponse(status=200, body=b"<html>nope"), cred="c", token="t"
    )
    with pytest.raises(api.SklandTransportError, match="JSON"):
        client.health_check()


def test_transport_failure_is_reported_as_transport_error() -> None:
    def boom(*_a, **_k):
        raise api.SklandTransportError("连接被拒")

    client = api.SklandClient(transport=boom, cred="c", token="t")
    with pytest.raises(api.SklandTransportError, match="连接被拒"):
        client.health_check()


# --- 健康检查的选型（这条是设计决定，不是实现细节） ------------------------


def test_health_check_never_uses_auth_refresh() -> None:
    """实测 `auth/refresh` 对**无效凭据**也返回成功，拿它做健康检查等于自欺。"""
    urls: list[str] = []

    def handler(method, url, headers, body):  # noqa: ANN001
        urls.append(url)
        return _json_response({"code": 0, "data": {"uid": "1"}})

    client = _client(handler, cred="c", token="t")
    client.health_check()
    client.binding_uid()

    assert urls, "应当确实发了请求"
    assert all("auth/refresh" not in url for url in urls)
    assert all("game/player/binding" in url for url in urls)


def test_binding_uid_reads_flat_and_nested_shapes() -> None:
    for payload in (
        {"code": 0, "data": {"uid": "u-1"}},
        {"code": 0, "data": {"bindingList": [{"uid": "u-1"}]}},
        {"code": 0, "data": {"list": [{"uid": "u-1"}]}},
    ):
        client = _client(lambda *a, _p=payload, **k: _json_response(_p), cred="c", token="t")
        assert client.binding_uid() == "u-1"


def test_binding_uid_raises_instead_of_returning_empty() -> None:
    """取不到就抛——返回空字符串会让上层以为拿到了 uid。"""
    client = _client(lambda *a, **k: _json_response({"code": 0, "data": {}}), cred="c", token="t")
    with pytest.raises(api.SklandError, match="uid"):
        client.binding_uid()


# --- 限流与缓存 -------------------------------------------------------------


def test_cooldown_blocks_rapid_fire_and_then_lets_through() -> None:
    now = [0.0]
    cache = api.CachePolicy(cooldown_seconds=1.0, clock=lambda: now[0])
    client = _client(lambda *a, **k: _json_response({"code": 0}), cred="c", token="t", cache=cache)

    client.health_check()
    with pytest.raises(api.SklandRateLimited):
        client.health_check()

    now[0] = 1.5
    client.health_check()  # 冷却过后放行


def test_cooldown_is_per_endpoint_so_a_multi_step_login_can_complete() -> None:
    """扫码授权是**连着几步**的顺序调用，全局冷却会让它永远失败。"""
    now = [0.0]
    cache = api.CachePolicy(cooldown_seconds=5.0, clock=lambda: now[0])
    client = _client(
        lambda *a, **k: _json_response({"code": 0, "data": {}}), cred="c", token="t", cache=cache
    )

    client.health_check()  # /game/player/binding
    client.sign_in()  # /game/attendance —— 同一秒内，必须放行
    assert [call[1].split("/api/v1")[-1] for call in client.calls] == [  # type: ignore[attr-defined]
        "/game/player/binding",
        "/game/attendance",
    ]


def test_cache_policy_expires_and_clears() -> None:
    now = [0.0]
    cache = api.CachePolicy(ttl_seconds=120.0, clock=lambda: now[0])
    cache.put("k", "v")
    assert cache.get("k") == "v"

    now[0] = 121.0
    assert cache.get("k") is None

    cache.put("k", "v")
    cache.clear()
    assert cache.get("k") is None


def test_updating_credentials_clears_the_cache() -> None:
    """换号必须清缓存，否则会把旧账号的数据当成新账号的。"""
    client = _client(lambda *a, **k: _json_response({"code": 0}), cred="old", token="t")
    client.cache.put("uid", "old-uid")
    client.update_credentials("new", "t2")
    assert client.cache.get("uid") is None
    assert client.has_credentials is True


def test_signed_request_carries_content_type_for_post() -> None:
    seen: list[dict] = []

    def handler(method, url, headers, body):  # noqa: ANN001
        seen.append(dict(headers))
        return _json_response({"code": 0, "data": {}})

    client = _client(handler, cred="c", token="t")
    client.sign_in()
    assert seen[0]["Content-Type"] == "application/json"
