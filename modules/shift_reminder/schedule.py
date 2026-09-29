"""班次模型的**壳**：把 `core/shifts.py` 的名字转出来给本模块的纯逻辑用。

为什么需要这个文件（而不是让纯逻辑直接 import core）
--------------------------------------------------
班次模型是跨模块共享的，**实现只有一份**，住在 `core/shifts.py`（见那里的 docstring）。
但本模块的纯逻辑文件（`strategy` / `notify` / `scheduler` / `plan_name` / `webapi`）
**不许 import `core`**：AstrBot 把插件当**包**加载，顶层没有 `core`，绝对导入会
`ModuleNotFoundError` → `discover_modules()` 抛错 → **整个插件装不起来**
（2026-09-29 实测；护栏见 `scripts/check_astrbot_load_form.py`）。

取用方式因此与本项目**其它装配层完全一致**：先试绝对导入，失败再回退父级相对导入。
两支各对应一个真实场景——

* 从仓库根跑 pytest 时仓库根在 `sys.path` 上，`core` 就是顶层包 → 走绝对那支；
* AstrBot 以包的形式加载插件时顶层没有 `core` → 走相对那支
  （本文件的包是 `<插件>.modules.shift_reminder`，`...` 正好升到插件根）。

**代价是 import 清单写两遍**。这是该模式的固有成本，也换来一个好处：改名字时两处
并排、一眼可见，不会漏掉一支。

**这一层薄壳只做转出，不含任何实现**：`git grep` 得到的每一处时刻计算都只有
`core/shifts.py` 一份。判据是「壳里只有 import 与 `__all__`，没有任何计算」；对象级
证据见 `tests/test_core_shifts.py::test_module_schedule_is_the_same_objects_as_core`
（它断言两边的类与函数**是同一批对象**，不是长得像的两批）。
"""

from __future__ import annotations

try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.shifts import (
        MINUTES_PER_DAY,
        SHIFT_SLOTS,
        ConfigError,
        Shift,
        ShiftTable,
        boundaries_between,
        current_shift,
        format_hhmm,
        parse_hhmm,
        parse_shift_table,
        reminders_between,
        validate,
    )
except ImportError:  # pragma: no cover
    from ...core.shifts import (
        MINUTES_PER_DAY,
        SHIFT_SLOTS,
        ConfigError,
        Shift,
        ShiftTable,
        boundaries_between,
        current_shift,
        format_hhmm,
        parse_hhmm,
        parse_shift_table,
        reminders_between,
        validate,
    )

__all__ = [
    "MINUTES_PER_DAY",
    "SHIFT_SLOTS",
    "ConfigError",
    "Shift",
    "ShiftTable",
    "boundaries_between",
    "current_shift",
    "format_hhmm",
    "parse_hhmm",
    "parse_shift_table",
    "reminders_between",
    "validate",
]
