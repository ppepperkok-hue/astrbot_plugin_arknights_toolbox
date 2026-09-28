# 需求与边界（活文档）

> 本文件只做**指针与边界摘要**；需求的完整定义以立项文档为准，不在这里重复维护（避免两份事实）。

## 1. 需求来源（按优先级）

| 文件 | 内容 |
| --- | --- |
| [project-checklist.md](../project-checklist.md) | 立项清单（唯一汇总） |
| [04-positioning-revision.md](../project-plan/04-positioning-revision.md) | **定位**：明日方舟工具箱，换班提醒只是第一个模块 |
| [03-requirements-clarification.md](../project-plan/03-requirements-clarification.md) | 执行端可选；服务器常开、电脑不常开；集成许可证三档 |
| [01b-project-goals.md](../project-plan/01b-project-goals.md) | 核心目标 G1–G5（可测量） |

## 2. 边界摘要

**是什么**：AstrBot 明日方舟工具箱（宿主 + 可插拔模块）。

**V1 做什么**：`shift_reminder` 模块——三班可配置、到点经 QQ 主动推送、幂等、失败可见。

**V1 不做什么**：Pages 界面、森空岛、MAA 触发、排班表导入、多模块并存。

**三条不可破的约束**：

1. **保底路径零外部依赖**——没有任何外部插件/API 时，提醒必须照常工作。
2. **不复制 AGPL / 无 LICENSE 仓库的代码**——保留自由选择许可证的空间。
3. **不直接操作游戏**——不注入、不代打、不碰账号密码。

## 3. 变更纪律

聊天里临时冒出的想法**不算正式变更**；先回写到 [implementation.md](implementation.md) 才算。
涉及底层架构（技术栈 / 目录结构 / 核心数据模型 / 权限）的改动，先停下出影响评估。
