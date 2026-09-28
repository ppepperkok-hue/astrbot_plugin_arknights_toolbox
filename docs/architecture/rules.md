# 通用规则（03b）

> 对应 skill 第 3 步第 2 小步：**一次性定死技术约定**，以后每个功能按同一套走。
> 范围限定：本文件只定**技术约定**；行为纪律（验证、失败显式化、权限边界等）归 Agent 宪法（第 4 步）。
> **每条规则都必须绑定检查手段**——没有检查手段的规则属于"凭感觉遵守"，不许写进来。

## 1. 目录与命名

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 目录 | `core/`（宿主与基础能力）、`modules/<module_name>/`（功能模块）、`tests/`（测试）、`docs/`（文档） | 目录存在性由骨架固定 |
| 模块命名 | 小写下划线；模块目录名 = 模块 id = 配置里的开关名 | 注册表装载时校验（未知模块名报错） |
| 文件命名 | 小写下划线（Python 惯例）；文档用 kebab-case | 人工 + review |
| 类/函数 | 类 PascalCase，函数与变量 snake_case | ruff（`N` 规则未开，暂靠 review） |
| 测试文件 | `tests/test_<被测模块>.py` | pytest 收集规则 |

## 2. 依赖方向（最容易腐化的一条，单独强约束）

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| **纯逻辑不得依赖框架** | `modules/*/schedule.py`、`notify.py`、配置校验等**不许 import astrbot** | **ruff 的 `flake8-tidy-imports`（`TID`）禁入规则**，CI 必过 |
| 模块不得依赖宿主入口 | `modules/` 下不得 import `main.py` | 同上（禁止 import `main`） |
| 禁止循环依赖 | 模块之间不互相 import；跨模块协作走 `core/module.py` 定义的口子 | review + 目录结构约束 |
| 模块间耦合唯一出口 | 判定策略接口（V1 只有周期策略） | 接口签名在 `core/module.py` 冻结 |

## 3. 错误处理与日志

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 日志 | 只用 AstrBot 的 `logger`（`from astrbot.api import logger`），**不用 `logging`** | ruff 禁 `import logging`（`TID` 禁入） |
| 配置非法必须显式失败 | 三班时长和 ≠ 24h、时刻格式错、缺项 → 抛出带明确文案的错误并**不注册定时任务** | 单测覆盖每种非法配置 |
| 推送失败不得静默 | `context.send_message` **返回 `False` 即视为失败**，计失败数、写 WARNING/ERROR | 单测（mock 返回 False）+ 熔断计数断言 |
| 禁止裸 `except` | 至少 `except Exception` 并记录，不许 `except: pass` | ruff `E722`（bare except） |
| 熔断 | 同一平台连续失败达阈值后暂停推送，成功后解除 | 单测覆盖阈值与恢复 |

## 4. 配置与密钥

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 配置声明 | 所有开关与参数写在 `_conf_schema.json`，**不在代码里硬编码可调值** | review + 单测读默认值 |
| 默认值 | 三班默认 `08:00 / 20:00 / 02:00`、提前量 10 分钟；**模块开关的默认值在 S3 装配模块时才落定**（裁决 D3，见 [implementation.md](../implementation/implementation.md)） | 单测断言默认配置可解析 |
| 时区 | 固定 `Asia/Shanghai`，可配置但不默认跟随宿主 | 单测 |
| 密钥 | V1 无凭据。V2 的 cred/token 只落 `plugin_data/`，**不进日志、不进仓库** | `.gitignore` 覆盖 + review |
| 数据位置 | 一律 `plugin_data/<plugin_name>/`，**禁止写插件自身目录** | review（官方硬约束） |

## 5. 接口契约

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 模块基类 | `core/module.py` 冻结：`name` / `config_key` / `initialize(ctx, config)` / `terminate` / `commands` / `jobs`。**C3 裁决**：`config` 是**该模块自己那一段**配置，由宿主按 `config_key` 取出后传入——模块不得看到整份插件配置 | 新增模块必须通过基类校验 |
| 判定策略 | `Strategy.judge(now) -> Decision`；V1 只有周期策略 | 单测（固定时间点断言判定结果） |
| 定时任务 | 统一走 `context.cron_manager.add_basic_job`，job 名前缀 `ak_toolbox:` | 单测断言 job 名；`terminate` 按前缀清理 |
| 指令前缀 | 统一 `/ak <子命令>`（如 `/ak status`、`/ak test`） | review |
| Web API（V2） | 路由必须带插件名前缀 `/<plugin_name>/...` | review |

## 6. 测试规则

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 布局 | `tests/` 与被测代码分离；**测试不得 import astrbot**（被测的纯逻辑也不依赖它） | CI 跑 pytest |
| 最低覆盖 | **纯逻辑层全覆盖**：三班校验、时刻计算（含跨天/月末）、消息渲染、策略判定、失败计数 | `pytest --cov` 报告（先看数字，不设硬门禁） |
| 必测边界 | ① 时长和 ≠ 24h ② 跨天（提前量跨 00:00）③ 月末/年末 ④ 重复触发幂等 ⑤ 推送返回 False ⑥ 空/错配置 | 测试用例清单 |
| 验证命令 | 本地与 CI **完全一致**：`ruff check . && ruff format --check . && pytest -q` | CI workflow 与 README 写同一行 |
| 框架胶水层 | 不做单测，用手动验证脚本 + 真实 QQ 消息验收 | M1 验收清单 |

## 7. 代码风格与提交

| 规则 | 内容 | 检查手段 |
| --- | --- | --- |
| 格式化 | ruff format，行宽 100 | `ruff format --check .` |
| Lint | ruff check，规则集在 `ruff.toml` 显式声明 | `ruff check .` |
| 注释与文档字符串 | 公共函数必须有 docstring；说明"为什么"而非"做了什么" | review |
| 提交信息 | 英文，`type: 描述`（feat/fix/docs/refactor/test/chore） | review；CI 不强制 |
| 依赖 | 运行时零第三方依赖；开发依赖钉版本在 `requirements-dev.txt` | `requirements.txt` 为空即通过 review |
| 变更记录 | 架构或规则变更 → 先改文档再改代码 | review |

## 8. 规则的检查手段汇总（缺一不可）

| 手段 | 覆盖的规则域 |
| --- | --- |
| `ruff check` + `ruff format --check` | 风格、依赖方向（禁 import astrbot / logging）、bare except |
| `pytest` | 配置校验、时刻计算、渲染、策略、失败与熔断 |
| CI（GitHub Actions） | 上面两条的复现 |
| 手动验证清单 | 框架胶水层与真实推送 |
| review | 命名、密钥、数据位置、契约一致性 |

> 规则变更走「先改本文档 → 再改代码 → 提交信息说明原因」，不许悄悄改。
