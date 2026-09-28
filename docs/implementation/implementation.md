# 实施真元文档 · V1：三班可配置的换班提醒

> 本文件是**当前大阶段（V1）的单一可信来源（SSOT）**：我照着它写，您照着它验收。
> 上游：[立项清单](../project-checklist.md) · [定位修订 04](../project-plan/04-positioning-revision.md) · [技术栈](../tech-stack.md) · [架构规则](../architecture/rules.md)
> 行为规则见项目根 [AGENTS.md](../../AGENTS.md)。本文件只管「这一段具体做什么」。

## 1. 大阶段目标与「完成」的定义

**目标**：插件在服务器 AstrBot 上加载成功，能绑定提醒目标、能手动触发收到真实 QQ 消息、能到点自动推送。

**完成定义（可验证，不靠感觉）**：

| # | 判据 | 验证方式 |
| --- | --- | --- |
| 1 | 验证命令全绿 | `python scripts/verify_constitution.py && ruff check . && ruff format --check . && pytest -q` |
| 2 | 插件在服务器 AstrBot 上加载无报错 | AstrBot 日志 + WebUI 插件卡片 |
| 3 | 手动触发收到真实 QQ 消息 | QQ 实收截图/文本 |
| 4 | 到点自动推送至少成功一次 | AstrBot 日志 + QQ 实收 |
| 5 | 重载插件不重复推送 | 连续重载 2 次，同一班次只收到一条 |

## 2. 子阶段与步骤

### S1 宿主骨架 —— 让 AstrBot 能加载，且宿主不认识「三班」

| 步骤 | 做什么 | 做到什么程度 | 验收标准 | 验证方式 | 暂时不做什么 |
| --- | --- | --- | --- | --- | --- |
| S1.1 | `metadata.yaml` + `_conf_schema.json` | 元数据含 `astrbot_version: ">=4.17.0"`；配置含**三班 + 提前量**（**模块开关按 D1 形式在 S3 加入**，见下方裁决表） | 文件存在且被 AstrBot 接受；默认三班能通过 `validate` | 服务器加载插件 + 单测 | 不做 Pages 相关字段；**本阶段不放 `modules` 段** |
| S1.2 | `core/module.py` 模块基类 | 冻结 `name`/`config_key`/`initialize`/`terminate`/`commands`/`jobs` 签名 | 基类可被继承；契约与 [extension.md](../architecture/extension.md) §2 一致 | 单测（假模块继承）+ review | 不写任何具体模块逻辑 |
| S1.3 | `core/registry.py` 注册表 | 按配置开关装载模块；未知模块名报错 | 给未知模块名 → 明确报错，不静默跳过 | 单测 | 不做插件间调用 |
| S1.4 | `main.py` 入口 | 读配置 → 建注册表 → `initialize()` → 注册 `/ak` 指令；`terminate()` 全清 | 服务器上加载后有日志、无异常 | 服务器加载 + 日志 | 不做 Web API |

**本子阶段结束时**：插件能在服务器上加载，`/ak` 指令能回一句"还没实现功能"。

### S1 配置结构裁决（D1–D5）

> 由 S1B 发现的并行包集成冲突触发，2026-09-29 裁定。**本表是配置结构的唯一事实**（原先只在 `plan.md`，现搬入 SSOT）。

| # | 决定 | 理由 |
| --- | --- | --- |
| D1 | **模块开关放 `modules.<模块名>`，模块参数放以模块名命名的段，嵌套不超过两层** | 将来加 `skland` / `maa` 时能区分「模块开关」与「模块参数」；官方文档称 `object`「理论上无限嵌套，但不建议过多嵌套」 |
| D2 | **S1 阶段不放 `modules` 段** | 当前无任何模块实现、`_KNOWN_MODULES` 为空，该段存在与否都无意义。**（修正：原先写「该段存在会抛 `UnknownModuleError`」，经核实不成立——`_config_schema_to_default_config` 对 `object` 忽略顶层 `default`，只用 `items` 递归生成）** |
| D3 | 模块开关默认值在 **S3** 落定时设为 `true` | S1 没有可开的模块，默认值无意义 |
| D4 | **保留**「未知模块名无论开关真假都抛错」 | 显式失败优于静默跳过（宪法 §2 第 2 条） |
| D5 | 三班用固定 `object` 段，**不用 `template_list`** | `template_list` 允许任意班次数，属提前造扩展点（宪法 §5 第 4 条） |

### 已核实的框架事实（本机 AstrBot 4.28.1 源码）

| 事实 | 出处 | 影响 |
| --- | --- | --- |
| `initialize()` 失败时框架**不会**调用 `terminate()` | `core/star/star_manager.py:1421/1436/1452`；`terminate()` 仅见于 `:1963` 停用/重载路径 | 宿主必须自行回滚已启动的模块 |
| schema 未声明的配置键会被**删除** | `core/config/astrbot_config.py:172` `check_config_integrity`；`:245` 打印 `Config key removed` | `modules` 段在 S1 写进去也会被抹掉 |

### 模块如何取得自己的配置（C3 裁决）

**决定：`initialize(self, ctx, config)`——宿主把「该模块自己那一段」显式传入。**

```python
async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None: ...
```

理由：① 显式传参，不给 `Module` 加可变状态；② 模块只看到自己那段（`config[module.config_key]`），**不知道别的模块段存在**；③ 假模块在测试里直接收 dict 即可。宿主侧 `start_all(ctx, config)` 按 `config_key` 取段后传入。

### S5 硬性完成判据

**S5 在任何子命令落地之前必须先有指令权限过滤**——`/ak status` 会输出排班与绑定目标，`/ak bind` 会改掉推送目标。默认开放一旦成为事实标准就难收回。这是完成判据，不是待办。

### 待 S3/S4 明确

`core/module.py` 的 `commands()` / `jobs()` 目前无调用点。**S3/S4 必须明确谁聚合它们**（宿主统一注册 vs 模块自注册），否则 S4 的「按前缀清理定时任务」没有落点。

### S2 纯逻辑层 —— 已完成（2026-09-29）

| 步骤 | 状态 | 交付物 | 验证证据 |
| --- | --- | --- | --- |
| S2.1 三班模型与校验 | ✅ | `modules/shift_reminder/schedule.py` | 24h 校验、首尾相接、跨天、月末/跨年 —— 单测覆盖 |
| S2.2 判定策略 | ✅ | `modules/shift_reminder/strategy.py` | 一天 24 个整点快照一致性测试 |
| S2.3 消息渲染 | ✅ | `modules/shift_reminder/notify.py` | 含/不含 `extra`、倒计时、无历史三种情形 |

**验证证据**：`pytest -q` → 45 passed；`ruff check .` → All checks passed。

> 说明：这三件在补写本文档**之前**就写完了，属于流程越界（已向用户报告）。此处补录，使其纳入 SSOT。

### S3 绑定与持久化

| 步骤 | 做什么 | 做到什么程度 | 验收标准 | 验证方式 | 暂时不做什么 |
| --- | --- | --- | --- | --- | --- |
| S3.1 | `core/storage.py` | 封装官方 KV（绑定的 umo）与 JSONL（发送记录，保留最近 50 条） | 写入后可读回；文件落在 `plugin_data/<plugin>/` | 单测（临时目录） | 不引数据库 |
| S3.2 | `/ak bind` 指令 | 记录当前会话的 `unified_msg_origin` | 绑定后重载插件，绑定仍在 | 服务器手动验证 | 不做多目标/群聊订阅 |
| S3.3 | 发送记录 | 每次推送记一行 JSONL（时间、班次、成功与否） | `/ak status` 能读出来 | 单测 + 手动 | 不做统计聚合 |

### S4 调度与推送

| 步骤 | 做什么 | 做到什么程度 | 验收标准 | 验证方式 | 暂时不做什么 |
| --- | --- | --- | --- | --- | --- |
| S4.1 | 用 `context.cron_manager.add_basic_job` 注册三班提醒 | job 名前缀 `ak_toolbox:shift_reminder:`；取不到 `cron_manager` 时明确降级或报错 | 任务出现在 AstrBot cron 列表，`get_next_run_time` 合理 | 服务器手动验证 | 不用 `add_active_job` |
| S4.2 | 推送 | `await context.send_message(umo, chain)`，**检查返回值** | 返回 `False` 时计失败并写日志 | 单测（mock 返回 False） | 不做备用通道 |
| S4.3 | 幂等 | 同一班次同一时刻只推一次（KV 记 last_sent） | 重载/重复触发不重复推送 | 单测 + 服务器手动重载 | 不做补推 |
| S4.4 | 失败熔断 | 连续失败达阈值暂停推送，成功后解除 | 阈值与恢复行为可单测 | 单测 | 不做外部告警 |
| S4.5 | `terminate()` 清理 | 按前缀 `delete_job` + 取消后台任务 | 停用插件后无孤儿任务 | 服务器手动验证 | — |

### S5 指令

| 步骤 | 做什么 | 做到什么程度 | 验收标准 | 验证方式 | 暂时不做什么 |
| --- | --- | --- | --- | --- | --- |
| S5.1 | `/ak test` | 立刻渲染并发一条测试提醒 | 收到内容正确的消息 | 服务器手动 | 不改任何状态 |
| S5.2 | `/ak status` | 输出当前班次、下一班倒计时、模块开关、最近 5 条发送记录 | 输出与 `notify.render_status` 一致 | 单测 + 手动 | 不做页面 |

### S6 部署与端到端验收

| 步骤 | 做什么 | 做到什么程度 | 验收标准 | 验证方式 | 暂时不做什么 |
| --- | --- | --- | --- | --- | --- |
| S6.1 | 部署到服务器 | 插件目录落在 `/opt/astrbot/data/plugins/`，WebUI 启用 | 加载无报错 | AstrBot 日志 | 不动 NapCat / nginx |
| S6.2 | 手动验收清单 | 跑完 [skeleton.md](../architecture/skeleton.md) §5 的 6 项 | 6 项全过 | 逐项实测 | — |
| S6.3 | 回滚验证 | 停用→删除目录→数据不丢 | 停用后无孤儿任务；重新启用可恢复 | 服务器手动 | — |
| S6.4 | 交付物 | README 更新、`deployment.md` 写明部署与回滚、运维工作区登记服务 | 文档与代码一致 | review | 不做自动部署脚本 |

## 3. 本阶段明确不做（边界）

| 不做 | 何时 |
| --- | --- |
| Pages 界面 | V2 |
| 森空岛状态模块 | V2 |
| MAA 远程控制端点 | V2 |
| 排班表导入 | V2 |
| 多模块并存验证 | V2 |
| 外部告警、备用通道、补推 | 不做 |

## 4. 与原始需求对照

| 需求（立项清单 / 修订） | 本阶段的落点 | 有冲突吗 |
| --- | --- | --- |
| 服务器常开、电脑不常开 → 提醒长在服务器 | S4 全部在插件内，零外部依赖 | 无 |
| 业务规则全可配置 | S1.1 配置 schema + S3.1 存储 | 无 |
| 到点经 QQ 主动推送 | S4.2 | 无 |
| 保底路径零外部依赖 | S3/S4 只用 AstrBot 本体 | 无 |
| 执行端可选（MAA / 手动） | **本阶段不做**，仅保留 S1.2 基类形状 | 无冲突（已声明 V2） |
| 定位：工具箱，换班提醒只是第一个模块 | S1.2–S1.4 的宿主/模块分离 | 无 |

**冲突项：无。**

## 5. 地基声明

本阶段**不动**以下地基：技术栈、目录结构、核心数据模型（`Shift` / `ShiftTable` / `Snapshot` 三个 dataclass 的字段）、权限设计（只用 AstrBot 的指令权限）。若实现中发现必须改，先停下出影响评估，再决定。
