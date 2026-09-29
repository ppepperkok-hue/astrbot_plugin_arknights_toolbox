"""装配层测试：自动发现、未授权时的行为、失败隔离、指令与权限。

异步部分用 `asyncio.run` 直接驱动，**不引 pytest-asyncio**（技术栈规定依赖尽量为零，
同仓库其它模块测试也是这个写法）。本文件会 import `modules/skland/module.py`
（它 import astrbot），由 `conftest.py` 注入的最小 stub 顶着。
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import modules.recruit.module as recruit_module
import modules.shift_reminder.module as shift_module
import modules.skland.module as skland_module
from core.registry import build_registry, discover_modules, known_module_names
from core.storage import JsonStateStore
from modules.skland import api
from modules.skland.credentials import CREDENTIALS_FILENAME, CredentialState
from modules.skland.module import (
    JOB_PREFIX,
    PLUGIN_NAME,
    SIGNIN_JOB_NAME,
    SklandModule,
    SklandSetupError,
)


class _FakeJob:
    def __init__(self, job_id: str, name: str = "") -> None:
        self.job_id = job_id
        self.name = name


class _FakeCronManager:
    """够用的假 cron：记录注册与删除，够断言「注册了几个、清了哪些」。"""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._jobs: list[_FakeJob] = []

    def seed(self, job_id: str, name: str) -> None:
        """预置一个历史任务，用于验证「注册前按前缀清一遍」。"""
        self._jobs.append(_FakeJob(job_id, name))

    async def list_jobs(self) -> list[_FakeJob]:
        return list(self._jobs)

    async def add_basic_job(self, **kwargs: Any) -> _FakeJob:
        self.jobs.append(kwargs)
        job = _FakeJob(f"job-{len(self.jobs)}", str(kwargs.get("name", "")))
        self._jobs.append(job)
        return job

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)
        self._jobs = [job for job in self._jobs if job.job_id != job_id]


class _FakeCtx:
    """够用的假 ctx：只记**真正送出去**的文本，并可选择返回 False 或抛异常。"""

    def __init__(self, *, ok: bool = True, raises: bool = False) -> None:
        self.sent: list[str] = []
        self.chains: list[Any] = []
        self.attempts = 0
        self.cron_manager = _FakeCronManager()
        self._ok = ok
        self._raises = raises

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.attempts += 1
        if self._raises:
            raise RuntimeError("平台已离线")
        if self._ok:
            self.chains.append(chain)
            # 图片组件不是字符串，用占位符表示——断言「发过一张图」即可，不必解码。
            self.sent.append(
                "".join(part if isinstance(part, str) else "[图片]" for part in chain.parts)
            )
        return self._ok


class _Event:
    """够用的假事件：只提供装配层实际用到的四个成员。"""

    def __init__(self, *, group: bool = False, admin: bool = False) -> None:
        self.message_str = ""
        self.unified_msg_origin = "test:FriendMessage:1"
        self._group = group
        self._admin = admin

    def is_private_chat(self) -> bool:
        return not self._group

    def is_admin(self) -> bool:
        return self._admin


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把三个模块的数据目录都指到 tmp_path，避免读到/写坏真实目录。"""
    for mod in (skland_module, shift_module, recruit_module):
        if hasattr(mod, "get_astrbot_plugin_data_path"):
            monkeypatch.setattr(mod, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    return tmp_path


def _init(
    module: SklandModule,
    ctx: _FakeCtx | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    asyncio.run(module.initialize(ctx or _FakeCtx(), config or {}))


#: `shift_reminder` 装载所需的真实配置段（`start_all` 按 `config_key` 注入，
#: 缺了它那个模块会因「班次名不能为空」而失败，与隔离能力无关）。
_SHIFT_CONFIG: dict[str, Any] = {
    "shift_reminder": {
        "shift_1_name": "早班",
        "shift_1_start": "08:00",
        "shift_1_hours": 12,
        "shift_2_name": "晚班",
        "shift_2_start": "20:00",
        "shift_2_hours": 6,
        "shift_3_name": "夜班",
        "shift_3_start": "02:00",
        "shift_3_hours": 6,
        "lead_minutes": 10,
        "timezone": "Asia/Shanghai",
    }
}


def _say(module: SklandModule, event: _Event, command: str = "skland") -> bool:
    return asyncio.run(module.handle_command(command, event))


# --- 自动发现（「加模块不改宿主」的验收） -----------------------------------


def test_discovery_finds_skland_with_matching_name() -> None:
    discovered = discover_modules()

    assert "skland" in discovered
    module = discovered["skland"]()
    assert module.name == "skland"
    assert module.config_key == "skland", "config_key 必须与模块名一致（宿主按它取配置段）"


def test_discovery_still_finds_the_other_modules() -> None:
    """新模块不许挤掉旧模块——这是模块化架构的核心验收点。"""
    discover_modules()
    names = set(known_module_names())
    assert {"shift_reminder", "recruit", "skland"} <= names


def test_skland_imports_no_other_module_and_not_main() -> None:
    """契约不变项：模块之间不许互相 import，也不许 import 宿主入口。

    用 AST 判而不是字符串匹配——注释里提到别的模块名是正常的（本文件就提了），
    字符串匹配会误报。这是本项目「选择器匹配不上却静默通过」那类错的镜像：
    判据要盯着**语义**，不是**字面**。
    """
    tree = ast.parse(Path(skland_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    offenders = sorted(name for name in imported if name.startswith("modules.") or name == "main")
    assert offenders == [], f"装配层 import 了不该 import 的模块：{offenders}"


# --- 未授权：正常状态，不许崩 -----------------------------------------------


def test_initialize_without_credentials_succeeds(data_root: Path) -> None:
    """未授权是**正常状态**，不是失败：装载必须成功。"""
    module = SklandModule()
    _init(module)  # 不抛异常即为通过

    assert module._store is not None
    assert module._store.status().state is CredentialState.MISSING


def test_status_command_explains_missing_credentials(data_root: Path) -> None:
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    assert _say(module, _Event()) is True
    text = ctx.sent[-1]
    assert "未授权" in text
    # 阶段二起未授权是可以自解的：必须给出具体动作，而不是一句「以后再说」。
    assert "/ak skland login" in text
    # 用户最需要知道的那一句：核心功能没被牵连
    assert "换班提醒" in text


def test_status_command_reports_corrupt_store_with_a_way_out(data_root: Path) -> None:
    """坏档要显式报出，并给出可操作的动作，而不是让它长得像「未授权」。"""
    (data_root / PLUGIN_NAME).mkdir(parents=True, exist_ok=True)
    (data_root / PLUGIN_NAME / CREDENTIALS_FILENAME).write_text("{ broken", encoding="utf-8")

    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)
    _say(module, _Event())

    text = ctx.sent[-1]
    assert text.startswith("【森空岛】凭据存档损坏"), "第一句就要说清是坏档，不是未授权"
    assert CREDENTIALS_FILENAME in text, "要告诉用户删哪个文件"


def test_initialize_creates_no_credential_file(data_root: Path) -> None:
    """装载不应顺手造出一份空凭据——那会让状态永远显示「已授权」。"""
    _init(SklandModule())
    assert not (data_root / PLUGIN_NAME / CREDENTIALS_FILENAME).exists()


# --- 真失败：抛出，交给宿主隔离 ---------------------------------------------


def test_initialize_raises_when_data_dir_cannot_be_prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据目录建不出来是**真失败**，必须抛——吞掉会让宿主汇总说谎。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(skland_module, "get_astrbot_plugin_data_path", lambda: str(blocker))

    module = SklandModule()
    with pytest.raises(SklandSetupError) as excinfo:
        asyncio.run(module.initialize(_FakeCtx(), {}))

    assert "数据目录" in str(excinfo.value)
    # 抛错时不该留下「没建起来的东西」，但要留下 ctx 以便如实回话（见模块 docstring）
    assert module._store is None
    assert module._ctx is not None


def test_isolated_module_can_still_explain_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """被隔离后仍被问到，回执必须如实说「没起来」——这正是保留 ctx 的理由。"""
    blocker = tmp_path / "blocked-explain"
    blocker.write_text("文件挡路", encoding="utf-8")
    monkeypatch.setattr(skland_module, "get_astrbot_plugin_data_path", lambda: str(blocker))

    module, ctx = SklandModule(), _FakeCtx()
    with pytest.raises(SklandSetupError):
        asyncio.run(module.initialize(ctx, {}))

    assert _say(module, _Event()) is True
    assert "没有起来" in ctx.sent[-1]


def test_a_broken_skland_does_not_stop_the_other_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**铁律的验收**：skland 抛错时，换班提醒与公招必须照常起来。

    关键在「**只有 skland** 的数据目录坏掉」——所以只把它自己的路径指向一个文件，
    另外两个模块照常拿到可写目录。否则两个模块同时坏掉，证明不了隔离。
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("文件挡路", encoding="utf-8")
    good = tmp_path / "good"
    good.mkdir()

    monkeypatch.setattr(skland_module, "get_astrbot_plugin_data_path", lambda: str(blocker))
    # recruit 不用数据目录（它只读包内数据），所以只有 shift_reminder 需要给好路径
    monkeypatch.setattr(shift_module, "get_astrbot_plugin_data_path", lambda: str(good))

    registry = build_registry({"shift_reminder": True, "recruit": True, "skland": True})
    # 要给出**真实的分段配置**：start_all 按 config_key 分段注入，空配置会让
    # shift_reminder 因「班次名不能为空」而失败——那样测的就不是隔离能力了。
    asyncio.run(registry.start_all(_FakeCtx(), _SHIFT_CONFIG))

    failed = dict(registry.failed_modules)
    assert "skland" in failed, "skland 的失败必须被宿主记下来"
    assert "shift_reminder" not in failed, "换班提醒不许被 skland 拖累"
    assert "recruit" not in failed
    assert {"shift_reminder", "recruit"} <= set(registry.started_names)


def test_registry_records_the_reason_not_just_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败要带原因：用户问「为什么」时日志可能已经滚掉了。"""
    blocker = tmp_path / "blocked2"
    blocker.write_text("文件挡路", encoding="utf-8")
    monkeypatch.setattr(skland_module, "get_astrbot_plugin_data_path", lambda: str(blocker))

    registry = build_registry({"skland": True})
    asyncio.run(registry.start_all(_FakeCtx(), {}))

    reasons = dict(registry.failed_modules)
    assert "数据目录" in reasons["skland"], f"原因要可读，实际是：{reasons['skland']!r}"


# --- 指令分发与权限 ---------------------------------------------------------


def test_unknown_command_is_declined(data_root: Path) -> None:
    """不认识就返回 False，宿主会继续问下一个模块。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    assert _say(module, _Event(), command="status") is False
    assert ctx.attempts == 0, "不归自己管的指令不该发消息"


def test_group_non_admin_is_rejected_with_the_shared_permission_wording(data_root: Path) -> None:
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    _say(module, _Event(group=True, admin=False))
    text = ctx.sent[-1]
    assert "管理员" in text
    # 走的是共享实现（core/permission.py）而不是本地副本
    assert "私聊" in text


def test_group_admin_is_allowed(data_root: Path) -> None:
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    _say(module, _Event(group=True, admin=True))
    assert "森空岛" in ctx.sent[-1]


# --- 回执失败不得冒泡 -------------------------------------------------------


def test_reply_returning_false_does_not_raise(data_root: Path) -> None:
    module = SklandModule()
    _init(module, _FakeCtx(ok=False))
    assert _say(module, _Event()) is True


def test_reply_raising_does_not_raise(data_root: Path) -> None:
    """平台离线时 send_message 会抛异常——它绝不许冒泡出去（2026-09-29 事故路径）。"""
    module = SklandModule()
    _init(module, _FakeCtx(raises=True))
    assert _say(module, _Event()) is True


# --- terminate 与「不注册定时任务」 -----------------------------------------


def test_terminate_is_idempotent_and_clears_references(data_root: Path) -> None:
    module = SklandModule()
    _init(module)
    assert module._store is not None

    asyncio.run(module.terminate())
    asyncio.run(module.terminate())

    assert module._store is None
    assert module._ctx is None


def test_signin_job_is_absent_unless_enabled(data_root: Path) -> None:
    """签到默认**关闭**：它是写操作，要用户明确同意才该自动跑。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)
    assert ctx.cron_manager.jobs == []


def test_enabling_signin_registers_one_prefixed_job_and_purges_stale_ones(data_root: Path) -> None:
    """开启后注册一个任务，且**注册前按前缀清掉历史任务**（重载会漏 terminate）。"""
    module, ctx = SklandModule(), _FakeCtx()
    ctx.cron_manager.seed("stale-1", f"{JOB_PREFIX}signin")
    ctx.cron_manager.seed("other", "ak_toolbox:shift_reminder:早班")

    _init(module, ctx, config={"signin_enabled": True, "signin_cron": "30 7 * * *"})

    assert [job["name"] for job in ctx.cron_manager.jobs] == [SIGNIN_JOB_NAME]
    assert ctx.cron_manager.jobs[0]["cron_expression"] == "30 7 * * *"
    # 只清自己的前缀，别人的任务一根汗毛都不许碰
    assert ctx.cron_manager.deleted == ["stale-1"]


def test_disabling_signin_removes_the_job(data_root: Path) -> None:
    """把开关关掉要**撤掉**任务，而不是留着它继续跑。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx, config={"signin_enabled": True})
    assert len(ctx.cron_manager.jobs) == 1

    asyncio.run(module.apply_config({"signin_enabled": False}))
    assert ctx.cron_manager.deleted, "关闭签到后应当删掉已注册的任务"
    assert ctx.cron_manager._jobs == []


def test_terminate_cancels_the_login_poll_and_cleans_jobs(data_root: Path) -> None:
    """`terminate` 要取消扫码轮询并清掉自己的任务——重复调用安全。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx, config={"signin_enabled": True})

    async def _never() -> None:
        await asyncio.sleep(3600)

    async def _run() -> None:
        module._login_task = asyncio.create_task(_never())
        await asyncio.sleep(0)
        await module.terminate()
        await module.terminate()

    asyncio.run(_run())
    assert ctx.cron_manager.deleted, "terminate 应当清掉自己注册的任务"
    assert module._login_task is None


def test_authorised_state_is_reported_without_leaking_secrets(data_root: Path) -> None:
    """已授权时回执只报状态，**不许把凭据内容打出来**。"""
    plugin_dir = data_root / PLUGIN_NAME
    store = JsonStateStore(plugin_dir / CREDENTIALS_FILENAME)
    store.set("cred", "SECRET-CRED-VALUE")
    store.set("token", "SECRET-TOKEN-VALUE")
    store.set("user_id", "u-42")

    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)
    _say(module, _Event())

    joined = "\n".join(ctx.sent)
    assert "SECRET-CRED-VALUE" not in joined
    assert "SECRET-TOKEN-VALUE" not in joined
    assert "已授权" in joined


def test_stored_credential_is_valid_json_on_disk(data_root: Path) -> None:
    """落盘的是合法 JSON（原子替换由 core.storage 保证，这里验结果形状）。"""
    from core.storage import JsonStateStore
    from modules.skland.credentials import SklandCredential, SklandCredentialStore

    plugin_dir = data_root / PLUGIN_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # 装配层注入 store；测试照做（见 StoreLike 的说明）。
    store = SklandCredentialStore(JsonStateStore(plugin_dir / CREDENTIALS_FILENAME))
    store.save(SklandCredential(cred="c", token="t", user_id="u"))

    payload = json.loads((plugin_dir / CREDENTIALS_FILENAME).read_text(encoding="utf-8"))
    assert payload == {"cred": "c", "token": "t", "user_id": "u"}


# --- 授权链路的端到端（假传输，不发真实请求） --------------------------------


class _FakeApi:
    """按 URL 给响应的假传输：把五步链路的形状照实测结果摆好。

    这样能在**不联网**的前提下验证「五个请求确实按顺序发出去、并各自取对了字段」——
    真机验证只剩「服务端认不认这个签名」这一项（见 api.py 的说明）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, url: str, headers: Any, body: bytes | None) -> api.HttpResponse:
        self.calls.append((method, url, body))
        if "gen_scan/login" in url:
            return _ok({"scanId": "S-1", "scanUrl": "hypergryph://scan_login?scanId=S-1"})
        if "scan_status" in url:
            return _ok({"msg": "已扫码", "status": 0, "data": {"scanCode": _b64("scan")}})
        if "token_by_scan_code" in url:
            return _ok({"token": "PASSPORT-TOKEN"})
        if "oauth2/v2/grant" in url:
            return _ok({"code": "GRANT-CODE"})
        if "generate_cred_by_code" in url:
            return _cred_ok()
        if "player/binding" in url:
            return _cred_ok({"uid": "u-9"})
        raise AssertionError(f"没预备这个地址：{url}")


def _ok(data: dict[str, Any]) -> api.HttpResponse:
    return api.HttpResponse(status=200, body=json.dumps({"status": 0, "data": data}).encode())


def _cred_ok(data: dict[str, Any] | None = None) -> api.HttpResponse:
    payload = {"code": 0, "data": data or {"cred": "CRED-1", "token": "TOKEN-1"}}
    return api.HttpResponse(status=200, body=json.dumps(payload).encode())


def _b64(text: str) -> str:
    import base64 as _base64

    return _base64.b64encode(text.encode()).decode()


def test_full_scan_login_chain_persists_the_credential(data_root: Path) -> None:
    """五步链路端到端：取码 → 轮询到已扫码 → 换 token → grant → cred → 落盘 → 验证。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    transport = _FakeApi()
    module._client = api.SklandClient(transport=transport)

    asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))

    urls = [url for _, url, _ in transport.calls]
    assert any("token_by_scan_code" in url for url in urls)
    assert any("oauth2/v2/grant" in url for url in urls)
    assert any("generate_cred_by_code" in url for url in urls)

    # 凭据真的落盘了，而且**回执里不许出现它的内容**
    stored = json.loads(
        (data_root / PLUGIN_NAME / CREDENTIALS_FILENAME).read_text(encoding="utf-8")
    )
    assert stored["cred"] == "CRED-1"
    joined = "\n".join(ctx.sent)
    assert "CRED-1" not in joined
    assert "TOKEN-1" not in joined
    assert "授权成功" in joined

    # 授权后立刻验一次连接（签名算法唯一的端到端探针）
    assert "连接验证" in joined
    assert module._client.has_credentials is True


def test_login_chain_failure_is_reported_without_leaking_anything(data_root: Path) -> None:
    """链路中途失败要说清「没拿到凭据」，且**不回显任何服务端字段值**。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    def transport(method: str, url: str, headers: Any, body: bytes | None) -> api.HttpResponse:
        if "token_by_scan_code" in url:
            return api.HttpResponse(status=200, body=json.dumps({"status": 0, "data": {}}).encode())
        return _ok({})

    module._client = api.SklandClient(transport=transport)
    asyncio.run(module._complete_login("test:FriendMessage:1", _b64("scan")))

    joined = "\n".join(ctx.sent)
    assert "失败" in joined
    assert not (data_root / PLUGIN_NAME / CREDENTIALS_FILENAME).exists()


def test_scan_login_sends_a_qr_code_image(data_root: Path) -> None:
    """`/ak skland login` 要真的把二维码当图片发出去（不是只发链接）。"""
    module, ctx = SklandModule(), _FakeCtx()
    _init(module, ctx)

    transport = _FakeApi()
    module._client = api.SklandClient(transport=transport)

    asyncio.run(module._cmd_login(_Event()))

    assert ctx.chains, "应当发了一条消息"
    parts = ctx.chains[0].parts
    kinds = [type(part).__name__ for part in parts]
    assert "Image" in kinds, f"消息里应当有图片组件，实际 {kinds}"
    image = next(part for part in parts if type(part).__name__ == "Image")
    assert image.file.startswith("base64://"), "图片应按 base64 传递（Image.fromBytes 的真实行为）"

    # 轮询任务要被记下来，`terminate` 才取消得掉；这里立刻收尾免得留后台任务
    assert module._login_task is not None
    module._login_task.cancel()
    asyncio.run(module.terminate())
