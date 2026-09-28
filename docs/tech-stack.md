# 技术栈（Tech Stack）

- **时间**：2026-09-29
- **上游**：[立项清单](project-checklist.md) · [定位修订 04](project-plan/04-positioning-revision.md) · [集成调研 02](project-plan/02-integration-research.md)
- **项目定位**：**AstrBot 明日方舟能力工具箱**（宿主 + 可插拔模块），换班提醒是第一个模块

---

## 0. 选型总原则

按 skill 的「项目与 AI 适配」最高原则，本项目在此之上再加三条硬约束（来自立项）：

| # | 约束 | 来源 |
| --- | --- | --- |
| C1 | **保底路径零外部依赖**——换班提醒在没有任何外部插件 / API / 账号时也必须工作 | 立项 G1、定位修订 |
| C2 | **不引入 AGPL 与无 LICENSE 的代码**——保留自由选择许可证的空间 | 集成调研 §4.2 |
| C3 | **最少依赖**——每个依赖都要有理由；能少装就少装 | skill 02 原则 |

由此推导出一条统领全篇的架构决定：

> **纯逻辑必须与 AstrBot 框架解耦。** 三班的时刻计算、消息渲染、配置校验等写成不 `import astrbot` 的纯函数；框架只做装载、定时与推送。这样它们既能在开发机上用 pytest 单测（AI 可闭环验证），又能在框架升级时不跟着碎。

## 1. 前端技术栈 —— **本层 V1 不适用**

| 候选 | 结论 |
| --- | --- |
| React / Vue + Vite | ❌ 否决：V1 根本没有前端；为 V2 的一个表单引入构建链，违反 C3 |
| 原生 HTML + ES module（无构建） | ✅ **V2 的默认方向**（待 V2 立项时确认） |

**理由**：V1 是纯后端插件，界面由 AstrBot WebUI 自带的插件配置页承担（`_conf_schema.json` 渲染）。V2 才可能需要自定义页面（改三班、看倒计时、翻发送记录、上传排班表），那时的形态是插件目录下 `pages/<name>/index.html`，由 Dashboard 以受限 iframe 加载。生态里既有"手写单文件 JS"（`astrbot_proactive_reply`）也有"Vite 构建产物"（`angel_heart`），本项目倾向**前者**——页面规模小，不值得一条构建链。

> ⚠️ V2 若真做页面，本层需重新走一次选型流程。

## 2. UI 组件库 —— **本层不适用**

无独立前端，因此无组件库、无设计系统、无主题层。AstrBot 的 Pages bridge 会注入自己的样式上下文，页面跟随 Dashboard 主题即可。

## 3. 后端语言 —— **Python 3.12**

| 候选 | 结论 |
| --- | --- |
| Python 3.12 | ✅ **唯一可行**：AstrBot 4.28.1 容器内即 Python 3.12.14，插件必须与宿主同进程同解释器 |
| 其他语言 | ❌ 不适用：AstrBot 插件机制是 Python 进程内注入 |

**决定**：

- 运行基线对齐部署环境 **Python 3.12.14**；
- 类型注解使用现代语法（`str | None`、`list[str]`），不引入 `from __future__ import annotations` 之外的兼容垫片；
- 不使用 3.13+ 独有特性，避免本地开发环境与容器不一致。

**AI 适配**：Python 是训练覆盖最广的语言之一；`dataclass` + 类型注解让数据结构显式可读。

## 4. 开发框架 —— **AstrBot 插件框架（`Star` 基类）**

| 候选 | 结论 |
| --- | --- |
| AstrBot `Star` 基类 + `filter` 装饰器 | ✅ 采用（唯一形态） |
| 额外引入 Web 框架 / 工作流框架 | ❌ 否决：AstrBot 已自带 Quart、aiohttp、APScheduler，重复引入违反 C3 |

**关键决定（均已在本机 4.28.1 源码核实，见 [实测记录](project-plan/01-astrbot-4281-api-facts.md)）**：

| 决定 | 内容 | 理由 |
| --- | --- | --- |
| 生命周期 | 用 `initialize()` / `terminate()` | 官方钩子；`context.register_task` 在 4.28.1 **已弃用** |
| 定时 | `context.cron_manager.add_basic_job()` | 落库 + 重启 `sync_from_db` 自恢复；**不用** `add_active_job`（会唤醒主 Agent 耗 LLM） |
| 版本兼容 | `getattr(self.context, "cron_manager", None)`，取不到时**抛明确错误**，不静默降级——维护两套调度不值当；版本需求已由 `astrbot_version` 限定 | `cron_manager` 自 4.17 起才有；同时 `metadata.yaml` 声明 `astrbot_version: ">=4.17.0"` |
| 主动推送 | `await self.context.send_message(umo, chain)`，**必须检查返回值** | 找不到平台时只返回 `False` + warning，不抛异常，不检查就会静默漏提醒 |
| 状态清理 | `terminate()` 里按 job 名前缀 `delete_job` + 取消后台任务 | 框架只在插件**定义了 `terminate()`** 时才 await 它 |
| 模块宿主 | `core/module.py` 定义模块基类，`core/registry.py` 装载 | 定位修订 04 的架构要求：加模块不改老代码 |

**否决项**：抄 `astrbot_plugin_scheduler` 的调度实现（AGPL-3.0 + 自研 30s 轮询，无必要）。

## 5. 数据与存储 —— **文件 + 官方 KV，不引入数据库**

| 数据 | 方案 | 存放位置 |
| --- | --- | --- |
| 模块配置（三班、提前量、模块开关） | **`_conf_schema.json`**，AstrBot 托管并在 WebUI 渲染 | 插件配置系统 |
| 小状态（绑定的提醒目标 umo、上次发送的班次） | **官方 KV**（`Star.put_kv_data` / `get_kv_data`） | AstrBot 数据库 |
| 发送记录（追加型日志） | **JSONL 文件**（一行一条，便于追加与截断） | `data/plugin_data/<plugin_name>/` |
| V2 森空岛凭据（cred / token） | 加密后落盘，**绝不进日志、绝不入仓库** | 同上 |

| 候选 | 结论 |
| --- | --- |
| SQLite | ❌ 否决：数据量以"条"计，引入建表与迁移成本不划算 |
| Redis / 缓存层 | ❌ 否决：无并发、无性能诉求 |
| 存插件自身目录 | ❌ **明令禁止**：插件更新会覆盖数据（官方原则） |

**AI 适配**：JSON / JSONL 是人类与 AI 都最容易核对与构造的格式，出问题时能直接用 read 工具看。

### 5.1 备份与恢复（02e 验收项）

| 项 | 方案 |
| --- | --- |
| 备份对象 | `data/plugin_data/astrbot_plugin_arknights_toolbox/`（绑定目标 + 发送记录）；配置在 AstrBot 的配置系统里，随 AstrBot 数据目录一起备份 |
| 备份方式 | 复用运维工作区既有的 AstrBot 数据备份路径（`/opt/astrbot/data` 整体归档）；本插件**不自建备份机制**——数据量以 KB 计，独立备份链路的维护成本高于收益 |
| 恢复方式 | 把 `plugin_data/<plugin>/` 目录放回原处 → 重载插件。**恢复后必须验证**：`/ak status` 能读出绑定目标与最近发送记录 |
| 丢失后果评估 | 丢失 = 需要重新执行一次 `/ak bind`（一条指令）。**这是可接受的**，所以不做实时备份 |
| 与 AstrBot 升级的关系 | 插件升级（git pull）不动 `plugin_data/`，数据不丢；插件停用也不删数据 |

> 结论：**备份不做自动机制，恢复路径文档化并实测一次**（S6.3 回滚验证涵盖）。这是"按数据形态选方案"的直接结果。

## 6. API 与通信契约 —— **四个边界，各用官方规格**

| 边界 | 契约 | 说明 |
| --- | --- | --- |
| 插件 ↔ AstrBot | 官方 Python API：`context.send_message` / `register_web_api` / `cron_manager` | 不碰内部私有字段 |
| 插件 ↔ QQ | **OneBot v11**，由 AstrBot 的 `aiocqhttp` 适配器承担 | 我们**不直连** NapCat，不碰端口与 token |
| 插件 ↔ Pages（V2） | `window.AstrBotPluginPage` bridge + `register_web_api` | 路由必须带插件名前缀；静态资源用相对路径，由框架重写 |
| 插件 ↔ MAA（V2） | **MAA 官方远程控制协议**：MAA 客户端轮询 `POST getTask`、回报 `reportStatus` | 按公开规格自己实现，**不读 AGPL 插件代码** |
| 插件 ↔ 森空岛（V2） | 复用 MIT 实现的函数签名（`get_player_info`） | 不自己发明接口形状 |

**决定**：**V1 只涉及前两个边界**，第三、四个是 V2 的预留位——V1 的模块骨架要给它们留插槽，但不实现。

### 6.1 版本化策略（02f 验收项）

| 边界 | 版本化方式 | 破坏性变更怎么处理 |
| --- | --- | --- |
| 插件 ↔ AstrBot | `metadata.yaml` 的 `astrbot_version` 声明支持范围（V1：`>=4.17.0`） | 上游破坏性升级 → 改范围并在 CHANGELOG 说明；**不静默兼容** |
| 插件 ↔ QQ | OneBot v11 由 AstrBot 适配器承担，插件不直接依赖协议版本 | 不处理（属 AstrBot） |
| 插件指令 `/ak <cmd>` | 指令名与语义**向后兼容** | 语义必须变时：加新子命令，旧命令保留并标注弃用；不许原地改语义 |
| 插件 ↔ Pages（V2） | Web API 路由带插件名前缀；响应用 `api_version` 字段自述 | 新增字段允许；删改字段须升 `api_version` |
| 插件 ↔ MAA（V2） | 跟 MAA 官方远程控制协议的版本 | 协议变更跟随上游，自己不改协议 |
| 持久化数据格式 | JSONL 每行带 `v` 字段；KV 键名带 `v1_` 前缀 | 读旧格式按 `v` 兼容；**无法兼容时明确报错，不静默丢弃数据** |

> 原则：**能不破就不破**；必须破时显式升版本 + 写 CHANGELOG，不允许"悄悄改语义"。

## 7. 测试与质量工具链 —— **ruff + pytest + GitHub Actions**

| 项 | 选择 | 理由 |
| --- | --- | --- |
| Lint / 格式化 | **ruff**（check + format） | AstrBot 官方开发原则明确要求；生态主流；单工具替代 flake8+isort+black |
| 单元测试 | **pytest** | 生态主流（`angel_heart` 等在用）；纯函数可脱离 AstrBot 运行 |
| 类型检查 | **不开 mypy/pyright 作为门禁** | V1 体量小，类型注解已足够；引入严格类型检查的收益不抵配置成本（记入否决项） |
| 包管理与依赖 | 运行时**零第三方依赖**（只用标准库 + `astrbot`）；开发依赖走 `requirements-dev.txt` | C3 |
| CI | **GitHub Actions** | 仓库已在 GitHub，公开库 Actions 免费；本地与 CI 跑同一套命令 |
| lock | `requirements.txt`（AstrBot 插件协议要求）+ `requirements-dev.txt`（版本钉死） | 可复现 |

**统一命令**（本地与 CI 完全一致，写进 README）：

```bash
ruff check . && ruff format --check . && pytest -q
```

**AI 适配**：一条命令闭环、配置显式存在仓库里（`ruff.toml` / `pyproject.toml`）、不依赖编辑器私有配置。

> 📌 **可测性的前提**：`schedule.py`（三班模型与时刻计算）、`notify.py`（消息渲染）、配置校验必须是**不 import astrbot 的纯函数**。TDD 的测试全部落在这一层；框架胶水层用手动验证脚本 + 真实 QQ 消息验收。

## 8. 部署、可观测性与安全 —— **插件目录落盘 + AstrBot 原生日志**

### 8.1 部署

| 项 | 方案 |
| --- | --- |
| 目标 | 服务器 `bt-he1k` 上的 AstrBot 容器（`/opt/astrbot`），插件目录 `/opt/astrbot/data/plugins/` |
| 发布 | `git clone` / `git pull` 到插件目录 → WebUI「重载插件」 |
| 启用 | AstrBot WebUI → 插件管理 → 启用 |
| 回滚 | WebUI 停用 → 删除插件目录。**数据在 `plugin_data/`，停用不丢**；彻底回滚再删数据目录 |
| 版本约束 | `metadata.yaml` 的 `astrbot_version: ">=4.17.0"`，不满足时 AstrBot 会阻止加载 |

**这条比"独立服务"省事得多**：不需要新端口、不需要改 NapCat/nginx/安全组、不需要 systemd 单元，回滚就是删目录。

### 8.2 可观测性

| 项 | 方案 |
| --- | --- |
| 日志 | AstrBot 的 `logger`（不要用 `logging` 模块），容器已是 json-file 驱动、20 MB × 3 轮转 |
| 健康检查 | 指令 `/ak status`：输出「下一班时刻 / 最近 N 条发送记录 / 各模块开关」 |
| 失败可见 | 发送失败计数 + 阈值熔断（QQ 掉线时不刷屏），恢复后自动解除；失败写 ERROR 级日志 |
| 告警 | **不做外部告警**（个人自用，日志足够）——记入否决项 |

### 8.3 安全与密钥

| 项 | 方案 |
| --- | --- |
| V1 凭据 | **没有**。只用一个提醒目标 umo，不含 token |
| V2 森空岛凭据 | 加密存 `plugin_data/`；**不进日志、不进仓库**；仓库 `.gitignore` 已含 `secrets/`、`.env`、`data/` |
| 权限 | 不申请超出所需的权限；不碰游戏账号密码 |

---

## 9. 版本锁定

| 组件 | 版本 | 锁定方式 |
| --- | --- | --- |
| AstrBot | 部署 4.28.1；声明 `>=4.17.0` | `metadata.yaml` 的 `astrbot_version` |
| Python | 3.12（部署环境 3.12.14） | 代码不越界使用新特性 |
| 运行时依赖 | **无** | `requirements.txt` 留空或仅注释 |
| ruff | 钉版本 | `requirements-dev.txt` |
| pytest | 钉版本 | `requirements-dev.txt` |
| CI | GitHub Actions，固定 action 主版本 | `.github/workflows/ci.yml` |

## 10. 否决项记录

| 否决 | 理由 |
| --- | --- |
| 前端框架 + 构建链（React/Vue/Vite） | V1 无前端；V2 页面简单，违反 C3 |
| SQLite | 数据量以条计，不值当 |
| 自建 APScheduler 调度 | 官方 `cron_manager` 已够用且自带持久化 |
| 抄 `astrbot_plugin_scheduler` 的实现 | AGPL-3.0 + 自研轮询，无必要 |
| fork `astrbot_plugin_maa` | AGPL-3.0 传染；按 MAA 官方协议自己实现 |
| mypy / pyright 作为 CI 门禁 | 收益不抵配置成本（V1 体量小） |
| 外部告警（邮件/webhook/APM） | 个人自用，日志足够 |
| 独立服务 / 容器 / systemd 单元 | 插件形态全部免除（见 8.1） |
| 任何 AGPL / 无 LICENSE 依赖 | C2 许可证红线 |

## 11. AI 适配检查（逐层）

- ✅ **主流与训练覆盖**：Python、pytest、ruff、GitHub Actions 都是训练覆盖最广的一档
- ✅ **显式与可预测**：配置走 `_conf_schema.json` 与 `ruff.toml`，不用隐式魔法；纯逻辑与框架解耦
- ✅ **可验证**：一条命令跑完 lint + 测试；真实链路有 `/ak status` 与手动触发指令
- ✅ **错误可读**：AstrBot `logger` 结构化输出；配置校验失败直接说明哪一段不合法
- ✅ **契约与版本锁定**：`astrbot_version` 声明 + `requirements-dev.txt` 钉版本
- ✅ **长期稳定**：无第三方运行时依赖，AstrBot 升级面被限定在公开 API 之内

## 12. 验收门

- [x] 八层逐层有决定，跳过层写明理由（层 1、层 2 不适用）
- [x] 每个选型有理由与后果，否决项集中在 §10
- [x] 每层过了 AI 适配检查（§11）
- [x] 版本锁定（§9）、许可无风险（C2：零 AGPL、零无证依赖）
- [ ] **用户确认**

> 确认后进入 Phase 0 第 3 步「项目架构」。
