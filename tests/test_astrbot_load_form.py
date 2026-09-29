"""用 AstrBot 的加载形态导入插件，拦住「本地绿、线上挂」的环境差异。

为什么必须是子进程
------------------
在 pytest 进程里模拟不出真实形态：`tests/conftest.py` 已经装好了 `astrbot` stub，
仓库根也已经在 `sys.path` 上（于是 `import core` 会碰巧成功），`sys.modules` 更是
早被污染。**在污染的进程里做模拟，只会得到一个假绿的护栏。**

所以这里只做一件事：把 `scripts/check_astrbot_load_form.py` 当子进程跑起来，
那个进程从零开始、自己把仓库根从 `sys.path` 摘掉、按包形态构造插件包。

它能证明什么、不能证明什么
--------------------------
能：所有模块入口、插件入口 `main.py`、以及各模块的纯逻辑文件，在「插件被当包加载、
顶层无 `core`」的形态下**导入得动**，且 `discover_modules()` 能发现磁盘上的每个模块。
2026-09-29 第二次事故（`modules/skland/credentials.py` 绝对导入 `core`，694 条测试全绿
而线上整个插件装不起来）正是这一类。

**不能**：不保证部署一定成功——真实环境还有版本行为、依赖、schema 渲染、网络与凭据
等本地模拟不出来的差异；也覆盖不到 `.gitignore` 那一类打包/发布差异。别把它当保证线。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "check_astrbot_load_form.py"


def test_plugin_imports_under_astrbot_load_form() -> None:
    """插件必须在「顶层没有 core」的包形态下导入得动。"""
    assert GUARD.is_file(), f"护栏脚本不见了：{GUARD}"

    proc = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        # 把护栏的诊断原样带出来：失败信息里已经写了「哪个文件、为什么、线上会怎样」。
        raise AssertionError(
            "插件在 AstrBot 的加载形态下装不起来（本地 pytest 看不见这个差异）\n"
            f"--- 护栏输出（退出码 {proc.returncode}）---\n{proc.stdout}\n{proc.stderr}"
        )
