# 功能接入规则（03d）

> 对应 skill 第 3 步第 4 小步：**新功能按模板增量接入，禁止另起炉灶**。
> 本项目的"功能" = 一个模块。

## 1. 新增模块的固定步骤

| # | 步骤 | 产出 |
| --- | --- | --- |
| 1 | 建目录 `modules/<module_name>/` | — |
| 2 | 先写**纯逻辑**（不 import astrbot）：数据模型、计算、渲染 | 可被 pytest 直接测的文件 |
| 3 | 写该模块的测试 | `tests/test_<module_name>_*.py` |
| 4 | 写 `modules/<module_name>/module.py`：继承 `core.module.Module`，装配纯逻辑 | 模块实现 |
| 5 | 在 `_conf_schema.json` 增加该模块的一组配置（含总开关） | 配置片段 |
| 6 | 在 `core/registry.py` 登记模块名 | 注册项 |
| 7 | 跑验证命令 | 全绿 |
| 8 | 更新 README 的模块表 | 文档 |

## 2. 模块基类契约（冻结；2026-09-29 第二次修订）

```python
class Module(ABC):
    name: str        # 模块 id，等于目录名，等于配置开关名
    config_key: str  # _conf_schema.json 里该模块参数段的键

    @abstractmethod
    async def initialize(self, ctx, config: Mapping[str, Any]) -> None: ...
    @abstractmethod
    async def terminate(self) -> None: ...

    async def handle_command(self, command: str, event) -> bool:
        """处理 /ak <command>；未处理返回 False。"""
        return False
```

**修订说明（为什么删掉 `commands()` / `jobs()`）**：AstrBot 的指令是用装饰器在**插件类**上静态注册的，模块无法自行注册指令——那两个方法是天生的死接口（全仓无调用点）。改为「宿主统一注册 `/ak`，逐模块转发 `handle_command`」；定时任务由模块在 `initialize()` 里自己用 `ctx.cron_manager.add_basic_job` 注册，并在 `terminate()` 里按 `ak_toolbox:<name>:` 前缀清理，不需要基类代为聚合。

**契约不变项**：任何模块都不许从 `main.py` 取值、不许 import 别的模块、不许自己起调度循环（定时一律走 `context.cron_manager.add_basic_job`）；`config` 是**本模块自己那一段**，模块不得看到整份插件配置。

## 3. 接入检查单

- [ ] 纯逻辑文件**没有** `import astrbot`（`ruff` 的 `TID` 禁入会拦）
- [ ] 模块名在 `_conf_schema.json` 里有独立开关，默认值明确
- [ ] `terminate()` 清理自己注册的 job（按 `ak_toolbox:<module>:` 前缀）
- [ ] 测试覆盖该模块的边界条件（参照 [rules.md](rules.md) §6 的必测清单）
- [ ] 提醒/输出类模块：失败路径可见（不静默吞错）
- [ ] **没有改动任何已有模块的文件** ← 这一条是模块化架构的核心验收点
- [ ] 若确实必须新增跨模块口子，先在 `core/module.py` 定义接口并更新本文档

## 4. 什么时候**不该**加模块

| 情况 | 处置 |
| --- | --- |
| 只是已有模块的一个参数 | 加配置项，不加模块 |
| 与已有模块共享 80% 逻辑 | 抽到 `core/`，两个模块共用（别复制） |
| 需要新的外部依赖或凭据 | 先回到技术栈流程评估依赖与许可证，再动手 |
| 要引入 AGPL / 无 LICENSE 代码 | **红线，禁止**（见技术栈 C2） |

## 5. 预留的三处插槽由谁填

| 插槽 | 定义位置 | 谁填 |
| --- | --- | --- |
| S1 模块基类 | `core/module.py` | 所有后续模块 |
| S2 判定策略 | `modules/shift_reminder/strategy.py` | V2 的森空岛心情策略（同签名，调度层不感知） |
| S3 提醒附加段 | `modules/shift_reminder/notify.py` 的 `extra` 参数 | V2 的排班表导入模块 |
