# Changelog

本文件记录**对外可见的变化**（新模块、行为变更、破坏性变更）。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 约束（见 [docs/tech-stack.md](docs/tech-stack.md) §6.1）：**破坏性变更必须显式记在这里并升版本**，不许悄悄改语义。

## [Unreleased]

## [0.2.0] - 2026-09-29

### Added

- 宿主骨架：可插拔模块基类（契约冻结）、模块注册表、`modules/` 目录**自动发现**（加新模块不改宿主代码）
- 换班提醒模块 `shift_reminder`（V1 首个模块）：
  - 从配置读取三班定义与提前量，**配置非法即抛错，不静默降级**
  - 用 `cron_manager.add_basic_job` 为每班注册定时提醒；取不到 `cron_manager` 时显式报错
  - 到点推送提醒到已绑定会话；**幂等**（同一班次的同一次换班只发一次，重载不重复）
  - **失败熔断**（连续失败达阈值后暂停推送）、发送记录持久化（JSONL）、未绑定时不推送并告警
  - 启动时**先清理自己遗留的 cron 任务**再注册，避免重载后任务堆积
- 指令：`/ak bind`、`/ak status`、`/ak test`
- 存储层：原子写 JSON 状态 + JSONL 发送记录（纯逻辑，可单测）
- 调度辅助：cron 表达式生成（含跨天取模）、幂等键、失败熔断器（纯逻辑，可单测）
- 单元测试 **167 条**，全部不依赖 AstrBot 运行时

### Changed

- **权限模型修订**：原「只有已绑定的那个会话可操作」改为「**私聊放开、群聊仅限管理员**」。原模型存在**改绑死锁**（改绑的前提正是换一个会话，而门禁只认原会话），且**防错了对象**（私聊里 `bind` 只影响发起者自己；真正该防的是群聊，那里绑定会把提醒推给全体成员）

### Fixed

- **指令处理完后未阻止事件传播**，消息继续流入 LLM 管线：用户每条指令收到两份回复（插件一份 + 人格一份），且每次白烧一次 LLM 调用。改为宿主统一调用 `event.stop_event()`
- **每次重载/重启都新增 3 条 cron 任务且从不清理**（`cron_jobs` 表累积到 9 条）。改为注册前按 `ak_toolbox:shift_reminder:` 前缀清理；实测连续两次重启后稳定在 3 条

### Deployed

- 已在 `bt-he1k`（AstrBot 4.28.1）实测通过：加载零报错、到点真实推送 QQ 消息、指令通道与权限门禁生效

## [0.1.0] - 2026-09-29

### Added

- 项目立项、市场调研、集成边界调研、技术栈与架构文档
- 项目宪法（`AGENTS.md`）与强制读取校验脚本（`scripts/verify_constitution.py`）
- 纯逻辑层：三班模型与校验、换班时刻计算、判定策略、提醒渲染（含 45 个单元测试）
- 工具链：ruff（含"纯逻辑禁止 import astrbot"的禁入规则）、pytest、GitHub Actions

### Changed

- 项目定位由「换班提醒插件」修正为「明日方舟能力工具箱」，换班提醒是第一个模块
- 仓库由 `astrbot_plugin_arknights_shift` 更名为 `astrbot_plugin_arknights_toolbox`

### Fixed

- `parse_hhmm` 对 `8:00` 这类不补零写法从"报错"改为"接受"（配置是手填的）
