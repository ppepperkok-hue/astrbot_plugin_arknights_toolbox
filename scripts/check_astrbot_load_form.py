#!/usr/bin/env python3
"""在**模拟 AstrBot 加载形态**下导入整个插件，拦住「本地绿、线上挂」这类环境差异。

为什么需要它
------------
本项目为同一个病栽过两次，而两次都是**本地测试全绿、部署后才炸**：

1. `.gitignore` 里裸写 `data/`，把 `modules/recruit/data/recruit_pool.json` 一起
   忽略了。本地文件在，测试自然绿；部署后模块因缺数据不可用。
2. `modules/skland/credentials.py` 写了绝对导入 `from core.storage import ...`。
   pytest 从仓库根跑时 `core` 可导入 → 694 条测试全绿；而 AstrBot 把插件当**包**
   加载（服务器实测的包名是 ``data.plugins.<插件目录名>``），顶层根本没有 `core`
   → ``ModuleNotFoundError`` → ``discover_modules()`` 抛错 → **整个插件加载失败**，
   三个模块全被卸载，``/ak`` 直接流进 LLM 人格。

共同点：**本地环境与真实加载环境的差异，既有测试完全看不见。**

本脚本做的事
------------
在一个**干净的子进程**里（调用方负责起子进程，避免 `sys.modules` 已被污染的
进程内模拟不出真实情形）：

1. 把仓库根从 ``sys.path`` 摘掉——这一步是关键，否则 ``import core`` 会**碰巧**
   成功，模拟就假了；
2. 装上最小 `astrbot` stub（直接复用 `tests/conftest.py` 里那一份，不复制）；
3. 按真实形态构造包链 ``data`` → ``data.plugins`` → ``data.plugins.<插件>` ，
   其 ``__path__`` 指向仓库根；
4. 导入 ``<插件>.core.registry``、调用 ``discover_modules()``，断言**磁盘上的每个
   模块都被发现**（含默认关闭的：发现与开关无关，少装一个却报成功最危险）；
5. 导入插件入口 ``<插件>.main``；
6. 逐个导入每个模块的**纯逻辑文件**（除 ``module.py`` / ``__init__.py``）。

**这个护栏能证明什么、不能证明什么（不许过度声称）**
- 能：这些文件在「插件被当包加载、顶层无 `core`」的形态下**导入得动**，且模块能被
  自动发现。第二次事故正是这一类。
- 不能：**不保证部署一定成功**。真实环境还有别的差异（AstrBot 版本行为、依赖库、
  `_conf_schema.json` 的渲染、网络与凭据），那些本地模拟不出来。
- 也不能覆盖 `.gitignore` 那一类**打包/发布**差异——那个要靠「按 git 内容打包再验」
  的做法，跟本脚本不是同一件事。

退出码：0 = 全部导入得动；1 = 有导入失败（逐条给文件、原因、以及为什么在 AstrBot 里会挂）。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 服务器实测的包名形态：`data.plugins.astrbot_plugin_arknights_toolbox.core.registry`
# 用真实形态而不是自造的简名，才能覆盖 `__package__` 的层级推导（见 core/registry.py:19）。
PLUGIN_PKG = f"data.plugins.{REPO.name}"

#: 装配层：允许 import astrbot 与父级相对导入，因此不参与「纯逻辑」检查。
ASSEMBLY_FILES = frozenset({"__init__.py", "module.py"})


def _scrub_repo_root_from_sys_path() -> list[str]:
    """把仓库根从 `sys.path` 上摘掉，返回被摘掉的条目（供报告）。

    这是整个模拟的地基：只要仓库根还在路径上，`from core.x import y` 就会**碰巧**
    成功，于是护栏什么也拦不住——那种「模拟得不像」的护栏比没有更糟，因为它会
    让人以为验过了。
    """
    removed: list[str] = []
    kept: list[str] = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except (OSError, ValueError):
            kept.append(entry)
            continue
        if resolved == REPO:
            removed.append(entry)
            continue
        kept.append(entry)
    sys.path[:] = kept
    return removed


def _install_astrbot_stub() -> None:
    """复用 pytest 用的那份 stub（单一份来源，不复制第二份）。"""
    conftest = REPO / "tests" / "conftest.py"
    if not conftest.is_file():
        raise RuntimeError(f"找不到 stub 来源：{conftest}")
    name = "_ak_toolbox_conftest_stub"
    spec = importlib.util.spec_from_file_location(name, conftest)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件存在则不该发生
        raise RuntimeError(f"无法加载 stub：{conftest}")
    module = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules：dataclass 之类会回查 `sys.modules[cls.__module__]`。
    sys.modules[name] = module
    spec.loader.exec_module(module)


def _build_plugin_package() -> None:
    """按真实形态构造包链，让仓库根成为插件包的 `__path__`。"""
    for name in ("data", "data.plugins"):
        holder = types.ModuleType(name)
        holder.__path__ = []  # 命名空间包：子包全由我们手工挂上
        sys.modules[name] = holder

    plugin = types.ModuleType(PLUGIN_PKG)
    plugin.__path__ = [str(REPO)]
    plugin.__package__ = PLUGIN_PKG
    sys.modules[PLUGIN_PKG] = plugin

    sys.modules["data"].plugins = sys.modules["data.plugins"]
    setattr(sys.modules["data.plugins"], REPO.name, plugin)


def _module_dirs() -> list[Path]:
    """`modules/` 下带 `module.py` 入口的子包。"""
    root = REPO / "modules"
    if not root.is_dir():
        return []
    return sorted(
        entry for entry in root.iterdir() if entry.is_dir() and (entry / "module.py").is_file()
    )


def _pure_logic_files(module_dir: Path) -> list[Path]:
    """模块里的纯逻辑文件（排除装配层 `module.py` 与包初始化）。"""
    return sorted(path for path in module_dir.glob("*.py") if path.name not in ASSEMBLY_FILES)


def _diagnose(exc: BaseException, target: str) -> str:
    """把导入失败翻译成「哪个文件、为什么、在 AstrBot 里会怎样」。"""
    text = f"{type(exc).__name__}: {exc}"
    if "No module named 'core'" in text or 'No module named "core"' in text:
        return (
            f"{target}\n"
            "    用了绝对导入 `from core... import ...`。\n"
            "    AstrBot 把插件当包加载，顶层**没有** `core`（它只是插件包内的子包），\n"
            "    因此这个文件在线上会 ModuleNotFoundError → 发现的整个插件都装不起来。\n"
            "    纯逻辑文件请改成「声明所需形状 + 由装配层注入」（见 modules/shift_reminder/\n"
            "    webapi.py 的 Protocol 写法）；装配层则用 `try: 绝对 / except: 相对` 回退链。"
        )
    if "attempted relative import beyond top-level package" in text:
        return (
            f"{target}\n"
            "    相对导入层级越界：从当前包往上跳得太远。\n"
            "    AstrBot 下的包层级是 `data.plugins.<插件>.<子包>...`，`...` 只到插件根。"
        )
    if "No module named 'modules'" in text or 'No module named "modules"' in text:
        return (
            f"{target}\n"
            "    按顶层 `modules` 导入。AstrBot 里模块包是 `<插件>.modules`，"
            "顶层没有 `modules`；请走 `core/registry.py` 的 `MODULES_PACKAGE` 推导。"
        )
    return f"{target}\n    {text}"


def main() -> int:
    print(f"插件包名（模拟）：{PLUGIN_PKG}")
    removed = _scrub_repo_root_from_sys_path()
    print(f"从 sys.path 摘掉的仓库根条目：{removed or '（本来就不在）'}")

    # 模拟得像不像，取决于这一条：此时 `import core` 必须失败。
    try:
        importlib.import_module("core")
    except ImportError:
        print("模拟成立：顶层 `core` 不可导入（与 AstrBot 现场一致）")
    else:
        print("FAIL：顶层 `core` 仍可导入，模拟无效——本护栏会假绿，请先修 sys.path 处理")
        return 1

    _install_astrbot_stub()
    _build_plugin_package()

    failures: list[str] = []
    module_dirs = _module_dirs()
    on_disk = sorted(entry.name for entry in module_dirs)

    # --- 1. 注册表与模块自动发现 -------------------------------------------
    discovered: list[str] = []
    try:
        registry = importlib.import_module(f"{PLUGIN_PKG}.core.registry")
        discovered = sorted(registry.discover_modules())
    except Exception as exc:  # noqa: BLE001 - 收集后统一报告
        failures.append(_diagnose(exc, "core/registry.py::discover_modules()"))
        print("\n--- 发现阶段失败，原始回溯 ---")
        traceback.print_exc()
    else:
        print(f"发现模块：{discovered}")
        missing = sorted(set(on_disk) - set(discovered))
        if missing:
            failures.append(
                f"磁盘上有模块目录但没被发现：{missing}\n"
                "    AstrBot 会少装这些模块却报加载成功——最难查的那种 bug。"
            )

    # --- 2. 插件入口 -------------------------------------------------------
    try:
        importlib.import_module(f"{PLUGIN_PKG}.main")
        print("插件入口 main.py 导入成功")
    except Exception as exc:  # noqa: BLE001 - 收集后统一报告
        failures.append(_diagnose(exc, "main.py（插件入口）"))
        print("\n--- 入口导入失败，原始回溯 ---")
        traceback.print_exc()

    # --- 3. 每个模块的纯逻辑文件 -------------------------------------------
    checked = 0
    for module_dir in module_dirs:
        for path in _pure_logic_files(module_dir):
            rel = path.relative_to(REPO).as_posix()
            try:
                importlib.import_module(f"{PLUGIN_PKG}.modules.{module_dir.name}.{path.stem}")
            except Exception as exc:  # noqa: BLE001 - 收集后统一报告
                failures.append(_diagnose(exc, rel))
                print(f"\n--- {rel} 导入失败，原始回溯 ---")
                traceback.print_exc()
            else:
                checked += 1

    print(f"\n纯逻辑文件检查：{checked} 个导入成功")

    if failures:
        print("\n" + "=" * 72)
        print("FAIL：以下内容在 AstrBot 的加载形态下会出问题（本地 pytest 可能看不出来）")
        for item in failures:
            print("  - " + item)
        return 1

    print("=" * 72)
    print("OK：全部模块入口、插件入口与纯逻辑文件在 AstrBot 加载形态下都能导入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
