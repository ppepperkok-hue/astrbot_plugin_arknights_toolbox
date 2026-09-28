# 最小骨架（03c）

> 对应 skill 第 3 步第 3 小步：目录立起来、最小链路真实跑通、验证命令可执行。
> **当前状态**：结构与验证命令已定；代码骨架正在按本文件搭建（本文档随搭建进度更新状态列）。

## 1. 目标目录树

```
astrbot_plugin_arknights_toolbox/
├── main.py                      # 插件入口：读配置 → 装载模块 → 转发指令/定时
├── metadata.yaml                # 插件元数据（含 astrbot_version 约束）
├── _conf_schema.json            # WebUI 渲染的配置表单
├── requirements.txt             # 运行时依赖：空（零第三方依赖）
├── requirements-dev.txt         # 开发依赖：ruff / pytest（钉版本）
├── ruff.toml                    # lint + 格式化规则（含禁入规则）
├── pyproject.toml               # pytest 配置
├── README.md
├── .github/workflows/ci.yml     # 与本地同一条命令
├── core/
│   ├── __init__.py
│   ├── module.py                # 模块基类（S1 插槽）
│   ├── registry.py              # 模块注册表：装载 / 启停 / 未知模块报错
│   ├── config.py                # 配置读取与校验（纯逻辑部分不 import astrbot）
│   └── storage.py               # plugin_data 读写 + 官方 KV 封装
├── modules/
│   ├── __init__.py
│   └── shift_reminder/          # 【V1 唯一模块】
│       ├── __init__.py
│       ├── module.py            # 装配：唯一碰 astrbot 的文件
│       ├── schedule.py          # 三班模型 + 校验 + 时刻计算（纯函数）
│       ├── strategy.py          # 判定策略（S2 插槽，V1 只有周期策略）
│       └── notify.py            # 消息渲染（纯函数，S3 插槽）
└── tests/
    ├── test_config.py
    ├── test_schedule.py
    ├── test_strategy.py
    └── test_notify.py
```

**分层铁律**：`core/` 与 `modules/*/module.py` 可以碰 astrbot；`schedule.py` / `notify.py` / `strategy.py` / `core/config.py` 的纯逻辑部分**不许碰**——由 `ruff.toml` 的 `TID` 禁入规则强制。

## 2. 最小横切链路

| 能力 | 位置 | M1 状态 |
| --- | --- | --- |
| 配置加载与校验 | `core/config.py` + `_conf_schema.json` | 待搭 |
| 日志 | AstrBot `logger` | 待搭 |
| 错误处理 | 配置非法 → 抛带文案的错并拒绝注册定时任务 | 待搭 |
| 存储 | `core/storage.py`（KV 存绑定目标；JSONL 存发送记录） | 待搭 |

## 3. 端到端最小链路（M1 的验收对象）

```
QQ 私聊 /ak test
   → main.py 收到指令
   → registry 找到 shift_reminder 模块
   → 读配置（三班 + 提前量）
   → strategy 判定"现在是第几班、下一班几点"
   → notify 渲染一条消息
   → context.send_message(umo, chain)
   → QQ 收到
```

**这条链路上，除首尾两端，中间全是纯函数**——所以它能被 pytest 覆盖，而不必每次推到服务器看运气。

## 4. 验证命令（本地与 CI 完全一致）

```bash
ruff check . && ruff format --check . && pytest -q
```

M1 之前这条命令必须能跑、且通过。

## 5. 手动验证清单（框架胶水层，无法单测）

| # | 步骤 | 期望 |
| --- | --- | --- |
| 1 | 插件目录放到 `/opt/astrbot/data/plugins/`，WebUI 重载插件 | 日志无报错，插件卡片出现 |
| 2 | QQ 私聊发 `/ak bind` | 收到绑定成功提示；重载插件后绑定**仍在** |
| 3 | 发 `/ak test` | 收到一条**主动**消息，内容为渲染后的提醒 |
| 4 | 发 `/ak status` | 打印下一班时刻、各模块开关、最近发送记录 |
| 5 | 反复重载插件 | 同一班次**不重复**推送；日志里无残留 job 警告 |
| 6 | 停用插件 | `terminate()` 清理干净，无孤儿定时任务 |

## 6. 骨架内**不得出现**的东西

按 [scope.md](scope.md) §3「留接口 ≠ 写空实现」：V1 的骨架里不许出现为 V2 准备的分支代码、空实现、`TODO: 将来`注释。S1/S2/S3 三处插槽**只体现为函数签名与基类形状**。

## 7. 待办

| # | 项 | 状态 |
| --- | --- | --- |
| 1 | `ruff.toml`（含 `TID` 禁入 astrbot / logging） | 待搭 |
| 2 | `metadata.yaml` / `_conf_schema.json` | 待搭 |
| 3 | `core/`（module / registry / config / storage） | 待搭 |
| 4 | `modules/shift_reminder/` 纯逻辑三件套 + 测试 | 待搭 |
| 5 | `main.py` 装配 | 待搭 |
| 6 | CI workflow | 待搭 |
| 7 | 本机跑通 `ruff + pytest` | 待验证 |
| 8 | 服务器 M1 手动清单 | 待验证 |
