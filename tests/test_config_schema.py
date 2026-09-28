"""`_conf_schema.json` 与 `metadata.yaml` 的默认值必须是一份**合法的**三班配置。

纯逻辑测试：只读 JSON/YAML 文本并复用 `modules.shift_reminder.schedule`，
**不依赖 AstrBot 运行时**，因此可以在开发机上直接跑。

配置结构（总监裁决）：模块参数收在以模块名命名的 `shift_reminder` 段里、子键扁平化，
嵌套不超过两层；`modules` 开关段留到 S3 装配模块时再加。

不引入 PyYAML 来解析 `metadata.yaml`：技术栈规定运行时零第三方依赖
（见 docs/tech-stack.md §7），这里用必要的关键行断言代替。
"""

import json
from pathlib import Path
from typing import Any

import pytest

from modules.shift_reminder.schedule import (
    MINUTES_PER_DAY,
    ConfigError,
    Shift,
    parse_hhmm,
    validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"
METADATA_PATH = REPO_ROOT / "metadata.yaml"

MODULE_KEY = "shift_reminder"
SHIFT_INDICES = (1, 2, 3)


def _field_names(index: int) -> tuple[str, str, str]:
    """扁平键名：`shift_<n>_name` / `shift_<n>_start` / `shift_<n>_hours`。"""
    return (
        f"shift_{index}_name",
        f"shift_{index}_start",
        f"shift_{index}_hours",
    )


def _read_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.is_file():
        pytest.fail(f"缺少配置文件：{SCHEMA_PATH}")
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        pytest.fail(f"_conf_schema.json 不是合法 JSON：{exc}")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return _read_schema()


@pytest.fixture(scope="module")
def module_defaults(schema: dict[str, Any]) -> dict[str, Any]:
    return schema[MODULE_KEY]["default"]


def _to_shifts(defaults: dict[str, Any]) -> list[Shift]:
    """把扁平键的默认值组装成 `Shift` 列表。"""
    shifts: list[Shift] = []
    for index in SHIFT_INDICES:
        name_key, start_key, hours_key = _field_names(index)
        shifts.append(
            Shift(
                name=defaults[name_key],
                start_minute=parse_hhmm(defaults[start_key]),
                duration_minutes=defaults[hours_key] * 60,
            )
        )
    return shifts


# --- 结构：扁平化，且不残留旧段 --------------------------------------------


def test_schema_uses_a_flat_module_section(schema: dict[str, Any]) -> None:
    """参数收在 `shift_reminder` 段里，十个子键全部扁平。"""
    assert MODULE_KEY in schema
    assert schema[MODULE_KEY]["type"] == "object"

    defaults = schema[MODULE_KEY]["default"]
    for index in SHIFT_INDICES:
        for key in _field_names(index):
            assert key in defaults, f"缺少扁平键 {key}"
    assert "lead_minutes" in defaults


def test_schema_drops_the_old_nested_shifts_section(schema: dict[str, Any]) -> None:
    """旧的顶层 `shifts`（object 套 object 共两层）必须已被清掉。"""
    assert "shifts" not in schema


def test_schema_has_no_modules_section_yet(schema: dict[str, Any]) -> None:
    """`modules` 开关段留到 S3 装配模块时再加：现在加会让注册表因未知模块名抛错。"""
    assert "modules" not in schema


# --- 默认配置必须合法（这是本包最要紧的一条） ------------------------------


def test_default_shifts_form_a_valid_table(module_defaults: dict[str, Any]) -> None:
    """默认三班必须正好 24 小时且首尾相接，否则插件一装上去就启动失败。"""
    shifts = _to_shifts(module_defaults)
    table = validate(shifts)

    assert sum(s.duration_minutes for s in shifts) == MINUTES_PER_DAY
    assert len(table.shifts) == 3


def test_default_shifts_match_the_documented_plan(module_defaults: dict[str, Any]) -> None:
    """默认三班：早班 08:00 / 12h，晚班 20:00 / 6h，夜班 02:00 / 6h。"""
    assert module_defaults["shift_1_name"] == "早班"
    assert module_defaults["shift_1_start"] == "08:00"
    assert module_defaults["shift_1_hours"] == 12

    assert module_defaults["shift_2_name"] == "晚班"
    assert module_defaults["shift_2_start"] == "20:00"
    assert module_defaults["shift_2_hours"] == 6

    assert module_defaults["shift_3_name"] == "夜班"
    assert module_defaults["shift_3_start"] == "02:00"
    assert module_defaults["shift_3_hours"] == 6


def test_validate_rejects_a_five_hour_shift(module_defaults: dict[str, Any]) -> None:
    """反例：把某班改成 5 小时，校验必须抛 ConfigError，证明这条校验真的在起作用。"""
    broken = dict(module_defaults)
    broken["shift_2_hours"] = 5

    with pytest.raises(ConfigError):
        validate(_to_shifts(broken))


# --- 其余默认值 ------------------------------------------------------------


def test_lead_minutes_default_is_ten(module_defaults: dict[str, Any]) -> None:
    assert module_defaults["lead_minutes"] == 10


def test_items_defaults_match_top_level_default(schema: dict[str, Any]) -> None:
    """两处默认值必须一致，否则 WebUI 显示的和实际生效的会对不上。"""
    defaults = schema[MODULE_KEY]["default"]
    items = schema[MODULE_KEY]["items"]

    for key, value in defaults.items():
        assert key in items, f"items 缺少 {key}"
        assert items[key]["default"] == value, f"{key} 的 items 默认值与 default 不一致"


def test_every_declared_field_has_type_description_and_hint(schema: dict[str, Any]) -> None:
    """`type` 是官方必填项；`description` 与 `hint` 按项目规则也不许空。"""
    section = schema[MODULE_KEY]
    nodes: list[tuple[str, dict[str, Any]]] = [(MODULE_KEY, section)]
    nodes.extend((key, node) for key, node in section["items"].items())

    for path, node in nodes:
        assert node.get("type"), f"{path} 缺少 type"
        assert node.get("description"), f"{path} 缺少 description"
        assert node.get("hint"), f"{path} 缺少 hint"


# --- 插件元数据 ------------------------------------------------------------


def test_metadata_declares_name_and_astrbot_version() -> None:
    """`astrbot_version` 是硬要求：版本不满足时 AstrBot 会拒绝加载而不是静默出错。"""
    if not METADATA_PATH.is_file():
        pytest.fail(f"缺少元数据文件：{METADATA_PATH}")

    text = METADATA_PATH.read_text(encoding="utf-8")
    assert "name: astrbot_plugin_arknights_toolbox" in text
    assert 'astrbot_version: ">=4.17.0"' in text
    assert "version: 0.1.0" in text
