# astrbot_plugin_arknights_shift

> 《明日方舟》基建换班提醒 —— 一个 AstrBot 插件。

**当前状态：立项阶段（Phase 0）**，尚未开始写实现代码。

## 这个插件要做什么

在每次该换班的时候，通过 QQ 把「这是第几班、该换人了、下一班几点开始」送到手上；如果那台跑 MAA 的电脑正好在线，还可以顺手把换班任务派给它。

- **服务器常开、电脑不常开** —— 所以调度与提醒必须长在服务器上，执行端是"能连上就用 MAA、连不上就手动"。
- **三班制** —— 12h / 6h / 6h 覆盖 24 小时（具体分界时刻见立项清单）。
- **疲劳驱动是终态** —— 通过森空岛读取干员真实心情，实现"累了才换"，而不是死板按钟点。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/project-checklist.md](docs/project-checklist.md) | 立项清单（唯一汇总） |
| [docs/project-plan/01a-core-value.md](docs/project-plan/01a-core-value.md) | 核心价值：为谁解决什么问题 |
| [docs/project-plan/01b-project-goals.md](docs/project-plan/01b-project-goals.md) | 核心目标：可测量的成功标准 |
| [docs/project-plan/01c-feature-breakdown.md](docs/project-plan/01c-feature-breakdown.md) | 功能拆解：做什么、不做什么 |
| [docs/project-plan/01d-executable-units.md](docs/project-plan/01d-executable-units.md) | 可执行单元与里程碑 |
| [docs/project-plan/00-market-scan.md](docs/project-plan/00-market-scan.md) | 市场调研：生态里有没有现成的 |
| [docs/project-plan/01-astrbot-4281-api-facts.md](docs/project-plan/01-astrbot-4281-api-facts.md) | AstrBot 4.28.1 插件 API 逐条实测 |
| [docs/project-plan/02-integration-research.md](docs/project-plan/02-integration-research.md) | 集成边界与可行性调研（五条线） |

## 开发环境

- 目标运行环境：AstrBot **4.28.1**（部署于个人服务器）
- 插件开发指南：[官方文档 · 从这里开始](https://docs.astrbot.app/dev/star/plugin-new.html)
- 插件命名：`astrbot_plugin_` 前缀 + 全小写 + 无空格

## 许可证

待定（见立项清单的许可证边界一节；本插件为原创实现，不复制任何 AGPL / 无许可证仓库的代码）。
