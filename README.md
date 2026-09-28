# astrbot_plugin_arknights_toolbox

> 把我的明日方舟日常收进一个 AstrBot 插件。

**当前状态：立项阶段（Phase 0）**，尚未开始写实现代码。

## 这是什么

一个 **AstrBot 明日方舟工具箱**：每个能力是一个**可单独开关的模块**，服务器常开就一直在。

**换班提醒只是它的第一个功能，不是它的全部。** 加新模块时不需要改动已有模块的代码——这是这个项目存在的理由。

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| `shift_reminder` 基建换班提醒 | **V1 必做** | 三班可配置、到点经 QQ 主动推送、保底路径零外部依赖 |
| `skland` 森空岛状态 | V2 | 基建一览 / 干员心情 / 理智；也是"疲劳驱动"的数据源 |
| `maa` 本机 MAA 触发 | V2 | 电脑在线时替我们跑一次换班（MAA 官方远程控制协议） |
| `schedule_import` 排班表导入 | V2 | 吃 [riic.autos](https://riic.autos/) 导出的 MAA JSON，让提醒带上干员名单 |

## 核心约束

- **服务器常开、电脑不常开** —— 所以调度与提醒长在服务器上；执行端是可选分支（用 MAA，或自己手动）。
- **保底路径零外部依赖** —— MAA 不在线、森空岛没接、排班表没导入时，提醒必须照常工作。
- **不复制 AGPL / 无 LICENSE 仓库的代码** —— 需要其能力时按公开协议自己实现。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/project-checklist.md](docs/project-checklist.md) | 立项清单（唯一汇总） |
| [docs/project-plan/04-positioning-revision.md](docs/project-plan/04-positioning-revision.md) | **定位修订：为什么是工具箱而不是提醒插件** |
| [docs/project-plan/03-requirements-clarification.md](docs/project-plan/03-requirements-clarification.md) | 需求修订：执行端可选、服务器常开、集成三档 |
| [docs/project-plan/01a-core-value.md](docs/project-plan/01a-core-value.md) | 核心价值（§1–§6 已被 04 取代） |
| [docs/project-plan/01b-project-goals.md](docs/project-plan/01b-project-goals.md) | 核心目标：可测量的成功标准 |
| [docs/project-plan/01c-feature-breakdown.md](docs/project-plan/01c-feature-breakdown.md) | 功能拆解（§1–§4 已被 04 取代） |
| [docs/project-plan/01d-executable-units.md](docs/project-plan/01d-executable-units.md) | 可执行单元与里程碑 |
| [docs/project-plan/00-market-scan.md](docs/project-plan/00-market-scan.md) | 市场调研：生态里有没有现成的 |
| [docs/project-plan/01-astrbot-4281-api-facts.md](docs/project-plan/01-astrbot-4281-api-facts.md) | AstrBot 4.28.1 插件 API 逐条实测 |
| [docs/project-plan/02-integration-research.md](docs/project-plan/02-integration-research.md) | 集成边界与可行性调研（五条线） |

## 开发环境

- 目标运行环境：AstrBot **4.28.1**
- 插件开发指南：[官方文档 · 从这里开始](https://docs.astrbot.app/dev/star/plugin-new.html)
- 命名约定：`astrbot_plugin_` 前缀 + 全小写 + 无空格

## 许可证

待定。本项目为原创实现，**不复制任何 AGPL / 无 LICENSE 仓库的代码**，以保留自由选择许可证的空间。
