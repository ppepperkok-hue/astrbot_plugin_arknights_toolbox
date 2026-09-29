"""配置**空值**容错：一次全新安装实测出来的真问题（2026-09-29）。

## 现场

做了一次全新安装（代码、配置、用户数据全部清空）。默认配置由宿主生成，结果是：

```
modules        = {...}   ✓
shift_1_start  = 08:00   ✓
...
maa.task_type  = ''      ← 空串，而 schema 的**段级** default 写的是 "LinkStart"
```

后果已经发生过一次：``maa`` 模块**启动失败**，日志为
``TaskTypeError: 任务类型配置不合法：''``——**提示还在怪用户填错**，而他什么都没填。

## 真因

宿主按 ``_conf_schema.json`` 的 **``items`` 逐项**生成配置。``task_type`` 那一项
**漏写了 ``default``**（段级的 ``default`` 不生效），于是全新安装后它就是空串。

⇒ 这是**一类**问题，不是一处：任何一个 ``items`` 条目漏写 ``default``，都会让那一项
在全新安装后变成空串。所以本文件有两层护栏：**行为层**（空值回退）与**模式层**
（schema 里每个标量条目都必须声明 ``default``）。

## 边界（同样是本文件要钉的东西）

**空 ≠ 填错。** 空是"没设置"⇒ 回退默认值并记 WARN；真正的填错（``"NotATask"``、
``0``、``"30"``）**必须继续当场报错**——放宽这一条就是放宽整个校验。
本文件与 ``test_maa_module.py::test_invalid_config_fails_loudly`` 一起守住这条分界：
**那边钉"错的仍然报错"，这边钉"空的回退且不失败"。**
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import config as config_reader
from core.shifts import ConfigError, parse_shift_table
from modules.maa import module as maa_module
from modules.maa import tasks
from modules.shift_reminder import module as reminder_module

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "_conf_schema.json"

#: 标量类型的 schema 条目必须有 `default`——字面量、列表、对象没法这么判。
SCALAR_TYPES = frozenset({"string", "int", "float", "bool"})


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# --- 1. 「什么算没设置」的唯一定义 -------------------------------------------


@pytest.mark.parametrize("value", [None, "", " ", "\t\n", "   "])
def test_unset_covers_none_and_blank(value) -> None:
    assert config_reader.is_unset(value) is True


@pytest.mark.parametrize("value", [0, False, "0", "false", "x", 1, [], {}])
def test_unset_does_not_swallow_meaningful_values(value) -> None:
    """``0`` 与 ``False`` **是有意义的取值**，不是"缺失"。

    ``bool`` 是 ``int`` 的子类，所以用真假值判断"有没有设置"会把用户明确填的
    ``0`` 吞掉——而 ``0`` 在 ``maa`` 的 TTL 里是**必须报错的越界值**，
    不该被静默换成默认值。
    """
    assert config_reader.is_unset(value) is False


def test_setting_falls_back_only_for_unset() -> None:
    config = {"blank": "", "space": "  ", "none": None, "zero": 0, "text": "x"}

    assert config_reader.setting(config, "blank", "D") == "D"
    assert config_reader.setting(config, "space", "D") == "D"
    assert config_reader.setting(config, "none", "D") == "D"
    assert config_reader.setting(config, "missing", "D") == "D"
    # 设了值就原样返回，**不做类型校验**——校验留给调用方，非法值才报得出来。
    assert config_reader.setting(config, "zero", 7) == 0
    assert config_reader.setting(config, "text", "D") == "x"


def test_unset_keys_names_them_for_the_warning() -> None:
    config = {"a": "", "b": "x", "c": None}

    assert config_reader.unset_keys(config, ("a", "b", "c", "d")) == ("a", "c", "d")
    assert config_reader.unset_keys(config, ("b",)) == ()


def test_core_config_is_pure_logic() -> None:
    """`core/config.py` 必须是纯逻辑：不依赖框架、不依赖 core 自身、不用 logging。

    用源码扫描而不是靠自觉——本项目已因"纯逻辑偷偷依赖"栽过
    （`credentials.py` 的绝对导入掀翻了整个插件的加载）。
    """
    source = REPO / "core" / "config.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert imported, "至少应该 import 一些标准库——空的说明扫描写错了"
    for name in imported:
        assert not name.startswith("astrbot"), f"纯逻辑层不该依赖 astrbot：{name}"
        assert name != "core" and not name.startswith("core."), f"不该依赖 core 自身：{name}"
        assert name != "logging", f"不许用标准库 logging：{name}"


# --- 2. maa：空值回退，且**不启动失败** --------------------------------------


class _FakeCronManager:
    """最小 cron 替身：够 `_register_sweep_job` 跑完就行。"""

    async def list_jobs(self) -> list:
        return []

    async def delete_job(self, job_id: str) -> None:  # noqa: ARG002 - 契约签名
        return None

    async def add_basic_job(self, **kwargs):  # noqa: ANN003 - 框架签名就是 kwargs
        return SimpleNamespace(name=str(kwargs.get("name", "")), job_id="job-1")


def _boot_maa(config: dict) -> maa_module.MaaModule:
    """把 maa 模块跑起来（真的要过 `initialize`，因为本包验的就是"不启动失败"）。"""

    async def _send(umo, chain):  # noqa: ANN001, ANN202 - 契约签名
        return True

    ctx = SimpleNamespace(
        register_web_api=lambda *args: None,
        send_message=_send,
        cron_manager=_FakeCronManager(),
    )
    instance = maa_module.MaaModule()
    asyncio.run(instance.initialize(ctx, config))
    return instance


def _collect_logs(monkeypatch, target) -> list[str]:
    """收集日志并**按 `%` 模板渲染**（渲染过的才是用户真正看到的那句话）。"""
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
            target.logger, level, lambda *args, _m=messages, **kwargs: _m.append(_render(args))
        )
    return messages


@pytest.mark.parametrize(
    "config",
    [
        {},  # 键整个缺失
        {"task_type": "", "task_ttl_minutes": "", "fetched_ttl_minutes": ""},  # 空串
        {"task_type": None, "task_ttl_minutes": None, "fetched_ttl_minutes": None},  # None
        {"task_type": "   "},  # 只有空白
    ],
)
def test_maa_empty_values_fall_back_to_defaults(config) -> None:
    """空值 ⇒ 用默认值，**且模块照常启动**。

    这一条就是线上那次启动失败的回归护栏：全新安装的默认配置里 `task_type` 是空串，
    而模块当时直接抛 `TaskTypeError`，整个 maa 功能对用户等于不存在。
    """
    instance = _boot_maa(config)

    assert instance._task_type == tasks.DEFAULT_TASK_TYPE
    assert instance._queue._task_ttl == timedelta(minutes=maa_module.DEFAULT_TASK_TTL_MINUTES)
    assert instance._queue._fetched_ttl == timedelta(minutes=maa_module.DEFAULT_FETCHED_TTL_MINUTES)
    assert instance.unavailable_reason is None


def test_maa_empty_values_are_reported_not_silent(monkeypatch) -> None:
    """回退**必须留痕**：静默用默认值会让用户以为自己的配置生效了。"""
    messages = _collect_logs(monkeypatch, maa_module)

    _boot_maa({"task_type": "", "task_ttl_minutes": "", "fetched_ttl_minutes": ""})

    warnings = [message for message in messages if "task_type" in message or "ttl" in message]
    assert any("task_type" in message for message in warnings), warnings
    assert any("task_ttl_minutes" in message for message in warnings), warnings
    assert any("fetched_ttl_minutes" in message for message in warnings), warnings


@pytest.mark.parametrize(
    "config",
    [
        {"task_type": "NotATask"},
        {"task_type": "LinkStart-Combat"},
        {"task_ttl_minutes": 0},
        {"task_ttl_minutes": "30"},
        {"fetched_ttl_minutes": -5},
        {"fetched_ttl_minutes": True},
    ],
)
def test_maa_typos_still_fail_loudly(config) -> None:
    """**回退不许顺手把校验也放宽**：这些是真填错了，必须当场抛。

    与 `test_maa_module.py::test_invalid_config_fails_loudly` 是同一个边界的两侧：
    那边在它的清单里钉住同一批值，这边紧挨着"空值回退"钉一遍——
    放在一起，改坏任何一侧都很显眼。
    """
    with pytest.raises(ValueError):
        _boot_maa(config)


# --- 3. shift_reminder：独立的字段回退，互相依赖的字段保持严格 ---------------


@pytest.mark.parametrize("blank", ["", " ", None])
def test_lead_minutes_blank_falls_back(blank) -> None:
    """提前量是**独立**字段：空 ⇒ 用默认值，不该让整个提醒模块起不来。"""
    assert reminder_module.parse_lead_minutes({"lead_minutes": blank}) == (
        reminder_module.DEFAULT_LEAD_MINUTES
    )
    assert reminder_module.parse_lead_minutes({}) == reminder_module.DEFAULT_LEAD_MINUTES


@pytest.mark.parametrize("bad", [-1, "10", True, 1.5])
def test_lead_minutes_typos_still_fail_loudly(bad) -> None:
    with pytest.raises(ConfigError):
        reminder_module.parse_lead_minutes({"lead_minutes": bad})


@pytest.mark.parametrize("blank", ["", "  ", None])
@pytest.mark.parametrize("suffix", ["start", "hours", "name"])
def test_shift_slots_stay_strict_on_blank(blank, suffix) -> None:
    """**刻意的例外**：班次槽位空值仍然报错，不回退。

    理由不是"偷懒"，而是这些字段**互相依赖**：三班必须合计 24 小时且首尾相接。
    静默补一个默认值会得到一张**合法但不是用户想要**的表——提醒会在错误的时刻响，
    **而且不会报错**。这正是本项目最怕的"静默错位"（见 `core/config.py` 的边界说明）。

    而且它们**不需要**回退：每个槽位字段在 schema 里都有 `items` 级默认值，
    全新安装不会产生空串（这一点由本文件第 4 节的护栏保证）。
    """
    config = {
        "shift_1_name": "第 1 班",
        "shift_1_start": "08:00",
        "shift_1_hours": 12,
        "shift_2_name": "第 2 班",
        "shift_2_start": "20:00",
        "shift_2_hours": 6,
        "shift_3_name": "第 3 班",
        "shift_3_start": "02:00",
        "shift_3_hours": 6,
    }
    config[f"shift_1_{suffix}"] = blank

    with pytest.raises(ConfigError):
        parse_shift_table(config)


# --- 4. 模式层护栏：这一类问题不许再出现 --------------------------------------


def test_every_scalar_schema_item_declares_a_default(schema: dict) -> None:
    """**本包最重要的一条护栏**：每个标量 `items` 条目都必须声明 `default`。

    宿主按 `items` 逐项生成配置，漏写 `default` 的那一项在全新安装后就是空串——
    `maa.task_type` 就是这么出事的。行为层的回退（第 2 节）能兜住后果，
    但**根因是 schema 写漏了**，所以这里直接把"写漏"变成红。

    只查标量类型：`object` / `list` 的默认值形态不同，不该被这条规则绑住。
    """
    missing: list[str] = []
    for section_name, section in schema.items():
        if not isinstance(section, dict):
            continue
        for item_name, spec in (section.get("items") or {}).items():
            if not isinstance(spec, dict):
                continue
            if spec.get("type") in SCALAR_TYPES and "default" not in spec:
                missing.append(f"{section_name}.{item_name}")

    assert not missing, f"这些 schema 条目没写 default，宿主会把它们生成成空串：{missing}"


def test_code_defaults_match_the_schema(schema: dict) -> None:
    """代码里的默认常量与 schema 必须一致——**默认值不许有两份不同的说法**。

    本项目为"同一个事实两份实现"栽过多次（权限判定、`Content-Type` 只设在签名
    分支），而两份默认值一旦分叉，**不会报错**，只会让回退后的行为与用户看到的面板
    对不上。所以这里把它们钉在一起。
    """
    maa_items = schema["maa"]["items"]
    assert tasks.DEFAULT_TASK_TYPE == maa_items["task_type"]["default"]
    assert maa_module.DEFAULT_TASK_TTL_MINUTES == maa_items["task_ttl_minutes"]["default"]
    assert maa_module.DEFAULT_FETCHED_TTL_MINUTES == maa_items["fetched_ttl_minutes"]["default"]

    reminder_items = schema["shift_reminder"]["items"]
    assert reminder_module.DEFAULT_LEAD_MINUTES == reminder_items["lead_minutes"]["default"]
    assert reminder_module.DEFAULT_TIMEZONE == reminder_items["timezone"]["default"]
