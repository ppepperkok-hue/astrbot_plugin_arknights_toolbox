"""`_conf_schema.json` 里 recruit 那一段的形状。

不复用 `test_config_schema.py`：那个文件管的是换班提醒那一段，改它等于动别的
模块的测试。这里只钉新模块自己的部分，包括几条架构约定：

- 模块开关名 = 模块目录名 = `Module.name`（`docs/architecture/extension.md` §1）
- 模块参数收在以模块名命名的段里、子键扁平（裁决 D1）
- 每个声明项都要有 `type` / `description` / `hint`（项目规则）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from modules.recruit.module import (
    MAX_COMBINATIONS_KEY,
    MAX_OPERATORS_KEY,
    RecruitModule,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"

MODULE_NAME = "recruit"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    assert SCHEMA_PATH.is_file(), f"缺少配置文件：{SCHEMA_PATH}"
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_module_switch_exists_and_defaults_on(schema: dict[str, Any]) -> None:
    switch = schema["modules"]["items"][MODULE_NAME]
    assert switch["type"] == "bool"
    assert switch["default"] is True


def test_switch_name_matches_the_module_identity(schema: dict[str, Any]) -> None:
    """开关名、配置段名、`Module.name`、目录名必须是同一个词。

    注册表在装载时会校验「目录名 == `Module.name`」，而配置段名对不上就会
    静默读不到配置——这条把三者钉在一起。
    """
    assert MODULE_NAME in schema["modules"]["items"]
    assert MODULE_NAME in schema
    assert RecruitModule.name == MODULE_NAME
    assert RecruitModule.config_key == MODULE_NAME


def test_parameters_live_in_a_section_named_after_the_module(schema: dict[str, Any]) -> None:
    section = schema[MODULE_NAME]
    assert section["type"] == "object"
    assert set(section["items"]) == {MAX_OPERATORS_KEY, MAX_COMBINATIONS_KEY}


def test_every_declared_field_has_type_description_and_hint(schema: dict[str, Any]) -> None:
    section = schema[MODULE_NAME]
    nodes: list[tuple[str, dict[str, Any]]] = [(MODULE_NAME, section)]
    nodes.extend((key, node) for key, node in section["items"].items())

    for path, node in nodes:
        assert node.get("type"), f"{path} 缺少 type"
        assert node.get("description"), f"{path} 缺少 description"
        assert node.get("hint"), f"{path} 缺少 hint"


def test_section_default_matches_the_declared_item_defaults(schema: dict[str, Any]) -> None:
    """AstrBot 对 `object` 只按 `items` 递归生成默认值，两处不一致迟早出岔子。"""
    section = schema[MODULE_NAME]
    for key, node in section["items"].items():
        assert section["default"][key] == node["default"], f"{key} 的两处默认值不一致"


def test_declared_defaults_are_the_ones_the_module_falls_back_to(schema: dict[str, Any]) -> None:
    """schema 写的默认值必须就是代码里的兜底值，否则「配置缺项」与「默认配置」
    会给出两种行为。"""
    from modules.recruit.module import DEFAULT_MAX_COMBINATIONS, DEFAULT_MAX_OPERATORS

    items = schema[MODULE_NAME]["items"]
    assert items[MAX_OPERATORS_KEY]["default"] == DEFAULT_MAX_OPERATORS
    assert items[MAX_COMBINATIONS_KEY]["default"] == DEFAULT_MAX_COMBINATIONS


def test_shift_reminder_section_is_untouched(schema: dict[str, Any]) -> None:
    """新模块只许**新增**，不许动到老模块那一段（接入检查单的核心验收点）。"""
    shift = schema["shift_reminder"]
    assert shift["items"]["shift_1_start"]["default"] == "08:00"
    assert shift["items"]["lead_minutes"]["default"] == 10
    assert set(schema["modules"]["items"]) >= {"shift_reminder", "recruit"}
