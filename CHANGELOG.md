# Changelog

本文件记录**对外可见的变化**（新模块、行为变更、破坏性变更）。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 约束（见 [docs/tech-stack.md](docs/tech-stack.md) §6.1）：**破坏性变更必须显式记在这里并升版本**，不许悄悄改语义。

## [Unreleased]

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
