# 推进计划与验收记录（活文档）

> 对应 [implementation.md](implementation.md) 的子阶段。每推完一个子阶段就停下验收，问题记在这里。
> 验收三问（每次汇报都要答）：**对照文档哪些做完了 / 改了哪些地方有没有越界 / 怎么验证的结果是什么**。

## 子阶段状态

| 子阶段 | 内容 | 状态 | 验收记录 |
| --- | --- | --- | --- |
| **S1** | 宿主骨架（metadata / 配置 / 基类 / 注册表 / 入口） | ⬜ 未开始 | — |
| **S2** | 纯逻辑层（三班模型 / 策略 / 渲染） | ✅ 已完成 | 见下 |
| **S3** | 绑定与持久化 | ⬜ 未开始 | — |
| **S4** | 调度与推送（cron_manager / 幂等 / 熔断） | ⬜ 未开始 | — |
| **S5** | 指令（`/ak test` / `/ak status`） | ⬜ 未开始 | — |
| **S6** | 部署与端到端验收 | ⬜ 未开始 | — |

## S2 验收记录（2026-09-29）

**对照文档**：S2.1 / S2.2 / S2.3 三步全部完成。

**改了哪些地方**：

- `modules/shift_reminder/schedule.py`（三班模型、校验、时刻计算）
- `modules/shift_reminder/strategy.py`（判定策略）
- `modules/shift_reminder/notify.py`（消息渲染）
- `tests/test_schedule.py` · `test_notify.py` · `test_strategy.py`
- 工具链：`ruff.toml` · `pyproject.toml` · `requirements*.txt`

**越界说明（如实报告）**：本子阶段是在补写 [implementation.md](implementation.md) **之前**完成的，属于流程越界——应当先有实施真元文档再写码。代码本身未超范围（只做了纯逻辑三个文件），已补录进 SSOT。

**验证证据**：

```
python -m ruff check .          → All checks passed!
python -m ruff format --check . → 9 files already formatted
python -m pytest -q             → 45 passed
```

**过程中发现并修掉的真问题**：

1. `parse_hhmm("8:00")` 原本被接受还是拒绝？——测试期望拒绝，实现却放过（`"8".isdigit()` 为真）。**决定：接受**（配置是手填的，不该为不补零报错），实现与测试同步修正。
2. 原本的"班次断开"用例会先触发"总时长≠24h"，**测不到目标分支**。重写为"总时长正好 24h 但两班重叠"才真正打到。

**下一步**：S1 宿主骨架（按 §2 的 S1.1–S1.4 顺序，每步一片，写一片跑一次测试再提交）。

## 待办与风险

| # | 项 | 备注 |
| --- | --- | --- |
| 1 | AstrBot 4.28.1 上 `cron_manager` 的实际行为（job 落库、重载清理） | S4 开工前先在服务器实测一次 |
| 2 | 服务器上 AstrBot 插件热重载的真实表现（是否残留任务） | S4.5 验收项 |
| 3 | 部署时机与影响 | 部署会产生一次 AstrBot 重载；需与用户约定低峰时段 |
