"""装配层测试：自动发现、未授权时的行为、失败隔离、指令与权限。

异步部分用 `asyncio.run` 直接驱动，**不引 pytest-asyncio**（技术栈规定依赖尽量为零，
同仓库其它模块测试也是这个写法）。本文件会 import `modules/skland/module.py`
（它 import astrbot），由 `conftest.py` 注入的最小 stub 顶着。
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import modules.recruit.module as recruit_module
import modules.shift_reminder.module as shift_module
import modules.skland.module as skland_module
from core.registry import build_registry, discover_modules, known_module_names
from core.storage import JsonStateStore
from modules.skland.credentials import CREDENTIALS_FILENAME, CredentialState
from modules.skland.module import PLUGIN_NAME, SklandModule, SklandSetupError


class _FakeJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class _FakeCronManager:
    """够用的假 cron：`shift_reminder` 装载时要用它，本模块阶段一不用。"""

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    async def list_jobs(self) -> list[Any]:
        return []

    async def add_basic_job(self, **kwargs: Any) -> _FakeJob:
        self.jobs.append(kwargs)
        return _FakeJob(f"job-{len(self.jobs)}")

    async def delete_job(self, job_id: str) -> None:
        return None


class _FakeCtx:
    """够用的假 ctx：只记**真正送出去**的文本，并可选择返回 False 或抛异常。"""

    def __init__(self, *, ok: bool = True, raises: bool = False) -> None:
        self.sent: list[str] = []
        self.attempts = 0
        self.cron_manager = _FakeCronManager()
        self._ok = ok
        self._raises = raises

    async def send_message(self, umo: str, chain: Any) -> bool:
        self.attempts += 1
        if self._raises:
            raise RuntimeError("平台已离线")
        if self._ok:
            self.sent.append("".join(chain.parts))
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


def _init(module: SklandModule, ctx: _FakeCtx | None = None) -> None:
    asyncio.run(module.initialize(ctx or _FakeCtx(), {}))


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
    assert "阶段二" in text
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


def test_module_registers_no_cron_jobs(data_root: Path) -> None:
    """阶段一刻意不做定时任务。这里从源码判定，防止有人顺手加一个空清理分支。"""
    source = Path(skland_module.__file__).read_text(encoding="utf-8")
    assert "cron_manager" not in source
    assert "add_basic_job" not in source


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
    from modules.skland.credentials import SklandCredential, SklandCredentialStore

    plugin_dir = data_root / PLUGIN_NAME
    plugin_dir.mkdir(parents=True, exist_ok=True)
    SklandCredentialStore(plugin_dir).save(SklandCredential(cred="c", token="t", user_id="u"))

    payload = json.loads((plugin_dir / CREDENTIALS_FILENAME).read_text(encoding="utf-8"))
    assert payload == {"cred": "c", "token": "t", "user_id": "u"}
