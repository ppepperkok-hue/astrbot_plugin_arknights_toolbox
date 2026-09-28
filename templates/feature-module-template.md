# 功能模块模板（Feature Module Template）

> 新增一个模块时**照本骨架增量实现**，不许自带新架构、不许绕开规则、不许复制粘贴改命名。
> 规则依据：[docs/architecture/rules.md](../docs/architecture/rules.md) · [docs/architecture/extension.md](../docs/architecture/extension.md)

## 1. 复制这四样东西

```
modules/<module_name>/
├── __init__.py        # 一句话说明这个模块是什么、纯逻辑与装配层各在哪
├── <logic>.py         # 纯逻辑：不 import astrbot，可被 pytest 直接测
└── module.py          # 装配层：唯一接触框架的文件
tests/test_<module_name>_<logic>.py
```

命名：`<module_name>` 小写下划线；它同时是目录名、模块 id、`_conf_schema.json` 里的配置键。

## 2. 纯逻辑骨架（`<logic>.py`）

```python
"""<模块>的纯逻辑。

禁止 import astrbot（ruff 的 TID 禁入规则会拦），因此可被 pytest 直接覆盖。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class <Module>Result:
    """这个模块对外产出的数据结构。"""


def <do_something>(...) -> <Module>Result:
    """纯函数：输入确定则输出确定，无 I/O、无全局状态。"""
```

## 3. 装配层骨架（`module.py`）

```python
"""<模块>的装配层：唯一接触 AstrBot 的文件。"""

from astrbot.api import logger

from core.module import Module


class <ModuleName>Module(Module):
    name = "<module_name>"
    config_key = "<module_name>"

    async def initialize(self, ctx) -> None:
        """读配置 → 校验 → 注册指令与定时任务。配置非法时抛错，不静默降级。"""

    async def terminate(self) -> None:
        """按 'ak_toolbox:<module_name>:' 前缀清理自己注册的 job 与后台任务。"""
```

## 4. 测试骨架（`tests/test_<module_name>_<logic>.py`）

必测：正常路径、边界值、非法输入报错、失败路径可见（对应 [rules.md](../docs/architecture/rules.md) §6 的六项必测边界）。

## 5. 交付检查单（每模块过一遍，不依赖记忆）

- [ ] 纯逻辑文件**没有** `import astrbot`（`ruff check .` 会拦）
- [ ] 模块在 `_conf_schema.json` 里有独立开关，默认值明确
- [ ] `terminate()` 清理自己注册的 job（按 `ak_toolbox:<module_name>:` 前缀）
- [ ] 测试覆盖边界；`pytest -q` 全绿
- [ ] 失败路径不静默吞错（返回值 / 日志至少留其一）
- [ ] 文档同步：README 模块表 + 实施真元文档状态
- [ ] 无残留：死代码、调试输出、临时文件
- [ ] **没有改动任何已有模块的文件** ← 模块化架构的核心验收点

## 6. 模板实测要求

模板落地后，用本模板生成一个示例模块并**跑通全部验收命令**，然后把示例归档为参考实现，证明这条接入路径可走通（03d 验收门）。
