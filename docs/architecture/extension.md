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

## 2. 模块基类契约（冻结；2026-09-29 第四次修订）

```python
class Module(ABC):
    name: str        # 模块 id，等于目录名，等于配置开关名
    config_key: str  # _conf_schema.json 里该模块参数段的键

    @abstractmethod
    async def initialize(self, ctx, config: Mapping[str, Any]) -> None: ...
    @abstractmethod
    async def terminate(self) -> None: ...

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """插件配置被改动后由宿主调用；默认什么都不做。"""
        return None

    @property
    def unavailable_reason(self) -> str | None:
        """已装载但干不了活时的原因；可用返回 None。默认 None。"""
        return None

    async def handle_command(self, command: str, event) -> bool:
        """处理 /ak <command>；未处理返回 False。"""
        return False
```

**第四次修订说明（为什么加 `unavailable_reason`）**：2026-09-29 服务器实测抓到一个
「日志说谎」的缺陷——故意删掉 `recruit` 的数据文件后重启，`recruit` 自己记了 ERROR
「本模块将不可用」，而宿主那行汇总**仍然打**「已装载模块：shift_reminder、recruit」，
`/ak` 兜底也把不可用的模块列成可用。用户据此判断「哪个功能能用」就会被误导。

根因是宿主只有一个判据：「`initialize` 有没有抛异常」。而有的模块**装载成功却干不了活**，
并且它**仍然是活着的**（指令答得上，还能告诉用户文件该放哪儿）。把这种状态硬塞进
「成功」或「失败」都会说假话，所以契约需要一个出口。

**状态三分（本机制的核心）**：

| 状态 | 怎么产生 | 宿主怎么处置 |
| --- | --- | --- |
| 可用 | `initialize` 成功且 `unavailable_reason` 为 `None` | 计入「可用模块」 |
| **已装载但不可用** | `initialize` 成功，但 `unavailable_reason` 非 `None` | 单列原因；**仍会调用 `terminate`**（它分配过资源）；指令照常转发给它 |
| 启动失败 | `initialize` 抛异常 | 单列原因；不调 `terminate`（它没起来） |

**为什么不选「让模块把数据缺失当成 `initialize` 失败」那条更省事的路**：那会把它报成
「启动失败」，而它其实活着、能应答、能指导用户自己修（把数据文件放回去）；同时失败
模块不进 `_started`，回收语义也会跟着错。两种状态的处置不同，就不该合并。

**判据是「能不能干活」，不是「有没有降级」**：只影响观感的降级**不算**不可用。例如
`shift_reminder` 的头像映射缺失时页面退回中文首字色块，但它照常提醒——把它报成不可用
会反过来让用户以为核心功能坏了，那是另一种假话。所以 `shift_reminder` **刻意不**实现
这个属性，而 `recruit` 实现。

**约定**：`None` 表示可用，**其它任何值（含空字符串）都表示不可用**（空值会被宿主换成
一句「未给出原因」的占位文案）。宿主在 `initialize` 成功返回后查询一次并记录；查询本身
抛异常时按「读取状态失败」归入不可用——**读不出来就不敢说它可用**。

**第三次修订说明（为什么加 `apply_config`）**：已核实 AstrBot 4.28.1 源码——`AstrBotConfig.save_config`（`core/config/astrbot_config.py:262`）只保证**内存与磁盘**更新，**不会**让已经注册的定时任务跟着变。而模块是在 `initialize()` 里一次性读配置的，所以「页面改了班次 → 保存成功」会得到**面板显示新值、提醒仍按旧时刻跑**这种最难查的错。加这个钩子，让需要热更新的模块能在配置变更后重建运行状态。

**职责分界（重要）**：`initialize()` 负责**首次**读配置与分配资源；`apply_config()` 只负责**让已有状态跟上新配置**，**可能被反复调用**，**不得**在这里做首次分配。默认实现是 no-op——不需要热更新的模块不必关心它。

**流程说明（如实记录）**：这次修订由子代理在实现 WebUI 编辑器时提出并落地，**它越出了任务包给它划的范围**（`core/module.py` 本被明确禁止修改）。改动经总监复核后接受（理由充分、默认实现无行为变化、职责分界写清），并在此补记契约与说明——**但越界本身记为流程问题**：改契约属于动地基，应先回报、由总监裁决后再动手。

**修订说明（为什么删掉 `commands()` / `jobs()`）**：AstrBot 的指令是用装饰器在**插件类**上静态注册的，模块无法自行注册指令——那两个方法是天生的死接口（全仓无调用点）。改为「宿主统一注册 `/ak`，逐模块转发 `handle_command`」；定时任务由模块在 `initialize()` 里自己用 `ctx.cron_manager.add_basic_job` 注册，并在 `terminate()` 里按 `ak_toolbox:<name>:` 前缀清理，不需要基类代为聚合。

**契约不变项**：任何模块都不许从 `main.py` 取值、不许 import 别的模块、不许自己起调度循环（定时一律走 `context.cron_manager.add_basic_job`）；`config` 是**本模块自己那一段**，模块不得看到整份插件配置。

## 3. 接入检查单

- [ ] 纯逻辑文件**没有** `import astrbot`（`ruff` 的 `TID` 禁入会拦）
- [ ] 模块名在 `_conf_schema.json` 里有独立开关，默认值明确
- [ ] `terminate()` 清理自己注册的 job（按 `ak_toolbox:<module>:` 前缀）
- [ ] 测试覆盖该模块的边界条件（参照 [rules.md](rules.md) §6 的必测清单）
- [ ] 提醒/输出类模块：失败路径可见（不静默吞错）
- [ ] **装载成功却干不了活时，用 `unavailable_reason` 如实报告**（数据文件缺失、外部凭据失效等）——**只打一行 ERROR 不算交代**：宿主汇总会照样把它算进「已装载」，用户看到的就是假话（2026-09-29 实测缺陷）
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
