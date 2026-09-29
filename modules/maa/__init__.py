"""MAA 远程控制模块（最小可验证端点）。

当前职责**只有一个**：让 MAA 能连上我们，并让「它连上了」成为一件可见的事实。
它不执行任何任务、不认识任何班次、也不往 QQ 推任何东西。

拆成两半（照 `docs/architecture/extension.md` §2）：

- `protocol.py`：协议形状、判据与日志渲染——**纯逻辑**，不 import astrbot。
- `module.py`：装配层，注册两个端点并记录到达。
"""

from __future__ import annotations

__all__ = ["module", "protocol"]
