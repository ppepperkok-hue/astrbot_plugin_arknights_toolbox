"""`_conf_schema.json` 与 `metadata.yaml` 的默认值必须是一份**合法的**三班配置。

纯逻辑测试：只读 JSON/YAML 文本并复用 `modules.shift_reminder.schedule`，
**不依赖 AstrBot 运行时**，因此可以在开发机上直接跑。

配置结构（总监裁决）：模块参数收在以模块名命名的 `shift_reminder` 段里、子键扁平化，
嵌套不超过两层；`modules` 开关段留到 S3 装配模块时再加。

不引入 PyYAML 来解析 `metadata.yaml`：技术栈规定运行时零第三方依赖
（见 docs/tech-stack.md §7），这里用必要的关键行断言代替。
"""

import json
import re
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
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"

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
    """参数收在 `shift_reminder` 段里，子键全部扁平（班次字段 + 提前量）。"""
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


def test_schema_declares_modules_section_with_default_on(schema: dict[str, Any]) -> None:
    """`modules` 开关段已落地（裁决 D1/D3），且 `shift_reminder` 默认开启。

    这条原本是 D2 的护栏（断言「不存在 modules 段」）。D3 生效后护栏跟着掉头：
    它现在保证「开关段存在、默认值是开发者想要的那个」，而不是被悄悄删掉或改默认值。
    """
    assert "modules" in schema
    section = schema["modules"]
    assert section["type"] == "object"
    assert section["items"]["shift_reminder"]["type"] == "bool"
    assert section["items"]["shift_reminder"]["default"] is True


# --- 默认配置必须合法（这是本包最要紧的一条） ------------------------------


def test_default_shifts_form_a_valid_table(module_defaults: dict[str, Any]) -> None:
    """默认三班必须正好 24 小时且首尾相接，否则插件一装上去就启动失败。"""
    shifts = _to_shifts(module_defaults)
    table = validate(shifts)

    assert sum(s.duration_minutes for s in shifts) == MINUTES_PER_DAY
    assert len(table.shifts) == 3


def test_default_shifts_match_the_documented_plan(module_defaults: dict[str, Any]) -> None:
    """默认三班：第 1 班 08:00 / 12h、第 2 班 20:00 / 6h、第 3 班 02:00 / 6h。

    **名字用位置（「第 N 班」）而不是「早班/晚班/夜班」**：排班表的 ``plans`` 是按
    位置对应 ``shift_i`` 的，人为标签会随用户作息改变而与实际对不上（见
    `docs/implementation/implementation.md` §2.6 的本条裁决）。
    时长顺序 12/6/6 也要一致——那是 riic.autos 导出与 MAA 使用的顺序。
    """
    assert module_defaults["shift_1_name"] == "第 1 班"
    assert module_defaults["shift_1_start"] == "08:00"
    assert module_defaults["shift_1_hours"] == 12

    assert module_defaults["shift_2_name"] == "第 2 班"
    assert module_defaults["shift_2_start"] == "20:00"
    assert module_defaults["shift_2_hours"] == 6

    assert module_defaults["shift_3_name"] == "第 3 班"
    assert module_defaults["shift_3_start"] == "02:00"
    assert module_defaults["shift_3_hours"] == 6


def test_default_shift_names_are_positional_not_human_labels(
    module_defaults: dict[str, Any],
) -> None:
    """默认名里**不许**再出现「早班/晚班/夜班」这类人为标签。

    这条是给后人的护栏：标签看着更亲切，很容易被"顺手改回去"，而它正是
    所有者这次点名要拿掉的东西——用户一改作息，标签就和实际对不上了。
    """
    labels = {"早班", "晚班", "夜班"}
    names = [module_defaults[f"shift_{slot}_name"] for slot in (1, 2, 3)]

    assert not labels.intersection(names), f"默认名又用回了人为标签：{names}"
    assert names == ["第 1 班", "第 2 班", "第 3 班"]


def test_default_durations_follow_the_schedule_table_order(module_defaults: dict[str, Any]) -> None:
    """默认时长顺序必须是 12/6/6——与排班表（`Shift 1 · 12h` / `6h` / `6h`）一致。

    顺序不一致的后果不是"显示难看"，而是第 i 班在两边指的不是同一班，
    提醒里会取到另一班的干员（见 `roster.describe_duration_alignment`）。
    """
    hours = [module_defaults[f"shift_{slot}_hours"] for slot in (1, 2, 3)]

    assert hours == [12, 6, 6]


def test_validate_rejects_a_five_hour_shift(module_defaults: dict[str, Any]) -> None:
    """反例：把某班改成 5 小时，校验必须抛 ConfigError，证明这条校验真的在起作用。"""
    broken = dict(module_defaults)
    broken["shift_2_hours"] = 5

    with pytest.raises(ConfigError):
        validate(_to_shifts(broken))


# --- 其余默认值 ------------------------------------------------------------


def test_lead_minutes_default_is_ten(module_defaults: dict[str, Any]) -> None:
    assert module_defaults["lead_minutes"] == 10


def test_timezone_default_is_shanghai(module_defaults: dict[str, Any]) -> None:
    """P1.4：时区默认值必须保持 `Asia/Shanghai`。

    老用户的配置文件里没有这个键，靠这个默认值维持原有行为（向后兼容）。
    """
    assert module_defaults["timezone"] == "Asia/Shanghai"


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
    """`astrbot_version` 是必填元数据。

    已核实该 specifier 只在**安装 / 更新**路径被校验
    （`core/star/star_manager.py:671` `_validate_astrbot_version_specifier`，
    调用点 `:2086`），**加载路径不校验**；写错会显式报错而不是静默放行。
    """
    if not METADATA_PATH.is_file():
        pytest.fail(f"缺少元数据文件：{METADATA_PATH}")

    text = METADATA_PATH.read_text(encoding="utf-8")
    assert "name: astrbot_plugin_arknights_toolbox" in text
    assert 'astrbot_version: ">=4.17.0"' in text
    assert re.search(r"^version: \d+\.\d+\.\d+$", text, re.MULTILINE), "缺少合法的 version 行"


def test_metadata_declares_version_exactly_once() -> None:
    """子串断言挡不住"文件后面又冒出一行 version: 9.9.9"，所以数出现次数。

    只断言"恰好一条"且"形如 X.Y.Z"，**不硬编码具体版本号**——否则每次发版
    都要回来改测试，而升版本本身不该让测试变红。
    """
    if not METADATA_PATH.is_file():
        pytest.fail(f"缺少元数据文件：{METADATA_PATH}")

    version_lines = [
        line.strip()
        for line in METADATA_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("version:")
    ]
    assert len(version_lines) == 1, f"version: 应恰好出现一次，实际为 {version_lines}"
    assert re.fullmatch(r"version: \d+\.\d+\.\d+", version_lines[0]), (
        f"version 必须形如 X.Y.Z，实际为 {version_lines[0]!r}"
    )


def test_metadata_version_has_a_matching_changelog_entry() -> None:
    """`metadata.yaml` 的版本号必须在 CHANGELOG 里有对应条目。

    为什么加这条（2026-09-29 发布前审查的阻断项 B1）：当时工作区的版本号是
    `0.4.0`，而 `v0.4.0` 这个 tag 里**连森空岛与 MAA 两个模块都不存在**——
    **同一个版本号指向两套功能完全不同的产物**，中间隔着 37 个提交。而这类错误
    **不会让任何检查变红**：版本号本身合法、测试全绿、打包正常。

    唯一能机器化的一半是「**升版本时必须同时写变更记录**」——那就把它变成断言。
    另一半（「加了功能却忘了升版本」）机器判断不了，只能靠纪律。

    同样**不硬编码版本号**：只要求两边一致，发版时改两处即可。
    """
    if not METADATA_PATH.is_file():
        pytest.fail(f"缺少元数据文件：{METADATA_PATH}")
    if not CHANGELOG_PATH.is_file():
        pytest.fail(f"缺少变更记录：{CHANGELOG_PATH}")

    match = re.search(
        r"^version: (\d+\.\d+\.\d+)$", METADATA_PATH.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, "metadata.yaml 里没有合法的 version 行"
    version = match.group(1)

    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE), (
        f"CHANGELOG.md 里找不到 [{version}] 这一条——升版本必须同时写变更记录，"
        "否则用户升级后没有任何东西告诉他多了什么"
    )
