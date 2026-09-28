# 调研报告：AstrBot 插件「明日方舟基建换班提醒」——集成边界与可行性评估

- **时间**：2026-09-29
- **作者**：agent
- **状态**：调研完成，待用户决策
- **上游**：[00 市场调研](00-market-scan.md)、[01a–01d 立项四步](01a-core-value.md)、[01-astrbot-4281-api-facts.md](01-astrbot-4281-api-facts.md)
- **触发**：用户要求「扩大边界，考虑集成或化用已有插件」并「彻底调研后给一份书面报告」

---

## 0. 结论摘要

**结论一：需求前提需要修正，这是本次调研最重要的发现。**

实测用户本机的 MAA 后发现：MAA **已经在用自定义三班排班**（`Infrast.DefaultInfrast = user_defined`），**排班文件就是从 riic.autos 导出的**（任务配置里的 `Filename` 指向 `arknights-infra-schedule-maa.json`），而且 MAA 里**已经配了两个定时自动启动（04:00 / 23:00）**。也就是说「到点自动换班」这件事在用户机器上早已具备条件。

→ **在做任何东西之前，必须先回答「这个提醒到底解决什么场景」**，否则可能是在造一个用户已经有的东西（详见 §2）。

**结论二：集成可行，而且不是一条路，是三条互相独立的路。**

| 通路 | 提供什么 | 可行性 | 本项目建议 |
| --- | --- | --- | --- |
| **riic.autos 排班表**（导出 JSON） | 「这一班每个房间放谁」 | ✅ 拿到文件后 100% 可离线解析 | V1 可做（用户需要手动导出一次） |
| **MAA**（本机已装） | 「真的去换班」这个动作 | ✅ 官方远程控制协议可用，但只能下发"跑一次"，带不了班次参数 | V2 可选增强 |
| **森空岛**（Skland API） | 「干员现在累不累」真实心情值 | ✅ `ap/360000` 就是心情，另有 `tiredChars` 已耗尽名单 | V3，能实现真·疲劳驱动 |

**结论三：AstrBot 4.28.1 的能力底座足够，且有三个现成范本可抄**（详见 §3）。用户记忆中「插件可以集成别的插件」确有官方依据——`get_all_stars()` 拿到的 `StarMetadata` 带「插件类实例」；但这条通道没有任何接口契约，只能当软依赖。

**结论四：不建议把 MAA 搬到服务器上跑**（§6.4）。服务器是无头 Linux、4C4G，要跑明日方舟得先立 redroid 安卓容器 + ARM 转译层，内存与内核风险都不划算。**正确姿势是服务器管调度与提醒、Windows 侧 MAA 管执行。**

---

## 1. 调研方法与证据分级

| 线 | 做法 | 产出 |
| --- | --- | --- |
| A. 插件生态 | 官方插件市场全量索引（1,332 个）关键词扫描 + GitHub 检索 | 见 §4 |
| B. MAA 集成 | MAA 官方文档（基建排班协议 / 远程控制协议 / 集成文档 / Linux 设备）+ 社区方案源码 | 见 §6 |
| C. 森空岛 | 逆向接口文档、`nonebot-plugin-skland` / `skland-kit` 字段定义、真实 dump 解析 | 见 §7 |
| D. riic.autos | 前端仓库源码逐文件核对 + **直接调用站点生产接口拿到真实导出数据** | 见 §5 |
| E. AstrBot 底座 | **在服务器容器内实测 4.28.1 源码**（7 轮只读探测） | 见 §3、[实测记录](01-astrbot-4281-api-facts.md) |

**证据分级**（本报告全篇遵守）：

- **【实测】**——在本项目自己的服务器 / 本机上亲自取到的证据（最高）
- **【官方】**——厂商官方文档或源码
- **【三方】**——第三方仓库 / 社区实证，附许可证
- **【未验证】**——检索未果或需真实凭据才能确认的，明确标注

---

## 2. ⚠️ 需求前提的修正（本机实测）

### 2.1 实测到的事实

**【实测】** 用户本机明日方舟 MAA 的基建任务配置：

```jsonc
{
  "$type": "InfrastTask",
  "Mode": "Custom",                        // 自定义排班模式
  "CustomFileType": "user_defined",
  "Filename": "…\\Downloads\\arknights-infra-schedule-maa.json",  // ← riic.autos 的导出文件名
  "PlanSelect": 2,                         // 当前选中第 3 个 plan
  "FiammettaTarget1": "清流", "FiammettaTarget2": "可露希尔", "FiammettaTarget3": "但书",
  "UsesOfDrones": "SyntheticJade",
  "RoomList": [ Power, Office, Control, Mfg, Trade, Reception, Dorm, Processing, Training ]
}
```

以及全局设置：

```jsonc
"Infrast.DefaultInfrast": "user_defined",   // 已启用自定义基建排班
"Timer.Timer1": "True", "Timer.Timer1Hour": "4",  "Timer.Timer1Min": "0",   // 已开定时 04:00
"Timer.Timer5": "True", "Timer.Timer5Hour": "23", "Timer.Timer5Min": "0",   // 已开定时 23:00
"Timer.ForceScheduledStart": "True", "Timer.ShowWindowBeforeForceScheduledStart": "True"
```

**【实测】** 用户本机装的东西：

| 路径 | 是什么 |
| --- | --- |
| `D:\mrfzmma` | **明日方舟 MAA 本体**（.NET GUI 版，2026-09-23 更新，含 `MaaCore.dll`、`resource/`、`Python/`） |
| `E:\Arknights Game` | **明日方舟 PC 官方客户端**（`Arknights.exe`、`Arknights_Data`） |
| `D:\maazmd` | **MaaEnd**——MAA 的**终末地**小助手（`interface.json` 控制窗口 `window_regex: "Endfield"`），**与明日方舟无关，别搞混** |
| `C:\Program Files\MuMu*Vbox` | MuMu 模拟器组件 |

**【实测】** 该 MAA 的 `resource/custom_infrast/` 下备有 `153/243/252/333` 各布局的「一天 3 换 / 4 换」排班文件；其中 `252_layout_3_times_a_day.json` 头部自述为「换班时间 12H-6H-6H」，三个班次命名为 `第1班/第2班/第3班`，描述分别为 `12H` / `6H` / `6H`。

### 2.2 由此推出的结论

**「到点自动换班」在用户机器上早已具备条件。** 因此原立项中「提醒我换班」这个需求，必须先澄清它到底补的是哪块缺口。四种可能，指向完全不同的方案：

| 假设 | 若成立，项目应该做什么 |
| --- | --- |
| H1：MAA 的定时不好用/不可靠，想要更灵活的提醒 | 做提醒即可，不必碰 MAA |
| H2：不想让电脑一直开着跑 MAA，想在手机上手动换 | 做纯提醒（消息带班次与干员），不碰 MAA |
| H3：MAA 在跑但**班次切换不对**（`PlanSelect` 需要人工改） | 价值最大：提醒 + 自动/一键切班次 |
| H4：其实想要的是「疲劳才换」而不是「到点就换」 | 方向变成接入森空岛（§7），周期提醒只是兜底 |

> **这是立项第 1 步「真问题」的回退点。** 在 H1–H4 里没定之前，任何功能设计都是猜。

---

## 3. AstrBot 4.28.1 能力底座【实测】

在服务器容器内逐条 grep 核实（完整证据见 [实测记录](01-astrbot-4281-api-facts.md)）。**官网文档对应最新版，服务器跑的是 4.28.1，下表全部以部署实例为准。**

| 能力 | 4.28.1 可用性 | 证据 |
| --- | --- | --- |
| 主动发消息 | ✅ | `core/star/context.py:614` `send_message(session, message_chain)`；另有 `StarTools.send_message()`（不需要 `self.context`） |
| 注册 Web API（V2 界面用） | ✅ | `core/star/context.py:705` `register_web_api(route, handler, methods, desc)` |
| 插件 Pages（V2 界面用） | ✅ | Dashboard 已在服务 `/api/plugin/page/entry`、`/bridge-sdk.js`、`/content/{plugin}/{page}/`；服务器上 8 个插件在用 |
| 文件上传（V3 导入排班表用） | ✅ | `astrbot/api/web.py` 的 `PluginUploadFile`、`PluginRequest`、`json_response`、`file_response`、`stream_response` |
| 配置表单（`_conf_schema.json`） | ✅ | `core/star/star_manager.py:212`；服务器上 15+ 插件在用 |
| 后台任务 | ⚠️ **接口变了** | `context.register_task` 在 4.28.1 **已 `@deprecated`**，官方要求改在插件的 `initialize()` 里 `asyncio.create_task`；`Star.initialize()` / `terminate()` 是官方生命周期钩子（`core/star/base.py:107/110`） |
| **跨插件调用** | ✅ 但脆弱 | `StarMetadata.star_cls` 即插件实例；`get_registered_star(name)` / `get_all_stars()`。**无事件总线、无接口契约**（全树无 `Bus`/`Broker`） |
| 数据持久化 | ✅ | 官方要求存 `get_astrbot_plugin_data_path()`（`<data>/plugin_data`），**不可存插件目录** |
| 会话级插件开关 | ✅ | `core/star/session_plugin_manager.py`，状态存 `session_plugin_config`（umo 粒度） |
| 内置 cron 管理器 | ✅ **确认可用**（4.17+） | `context.cron_manager.add_basic_job(*, name, cron_expression, handler, …, persistent)` → `CronJob`，落库并 `sync_from_db` 重启自恢复 |
| 生命周期钩子（`initialize`/`terminate` 之外） | ✅ | `@filter.on_astrbot_loaded()`、`on_plugin_loaded()`、`on_plugin_unloaded()`、`on_platform_loaded()`、`after_message_sent()` 等 |
| `Context.event_bus` | ❌ **不存在** | `core/event_bus.py` 是"平台消息 → Pipeline"的内部分发队列，不对外；跨插件协作官方推荐走 LLM 工具共享 |
| 运行时环境 | Python 3.12.14；`apscheduler 3.11.3`、`httpx`、`aiohttp` 均已装 | — |

### 3.1 可照抄的三个现成范本【实测】

| 范本 | 抄什么 |
| --- | --- |
| `astrbot_plugin_daily_sharing` | **定时推送的完整骨架**：`initialize()` 起 `AsyncIOScheduler`、`terminate()` 里 `shutdown` + 逐个 `task.cancel()`、`_is_terminated` 防僵尸实例 |
| `astrbot_plugin_angel_heart` | **Pages + Web API 的完整骨架**：`pages/chat-config/`、`web_api/__init__.py`、`_conf_schema.json`（含嵌套 object 与 `_special: select_provider`）、甚至带 Web API 的单测 |
| `astrbot_plugin_qzone` | 被 `daily_sharing` 用 `star_cls` 直接调用其方法——**插件互调的真实先例** |

---

## 4. 插件生态：可集成与可化用

### 4.1 全量扫描结果

**【实测】** 拉取官方插件市场索引 `plugins.json`（**1,332 个插件**）做关键词匹配：

| 关键词 | 命中 |
| --- | --- |
| `基建` / `换班` / `排班` / `infrast` | **0** |
| `明日方舟` / `arknights` | 十余个，全部是内容查询类 |
| `森空岛` / `skland` | 3–4 个，签到与数据查询 |
| `maa` | 2 个（遥控类） |
| `reminder` / `schedule` / `cron` | 十余个通用定时/提醒 |

→ **没有现成的「基建换班提醒」插件，必须自研。**

### 4.2 候选清单与取舍

| 插件 | 它有什么 | 对我们的价值 | 许可证 | 结论 |
| --- | --- | --- | --- | --- |
| `astrbot_plugin_scheduler` | 通用定时调度（Cron / 间隔 / 条件），可视化配置 | 调度实现参考 | 待核 | **化用**（读源码，不依赖） |
| `astrbot_plugin_reminder` | 定时发消息到群/私聊，Cron + 富媒体 | 主动推送写法参考 | 待核 | **化用** |
| `astrbot_plugin_daily_sharing`（已装） | `initialize` 起调度 + `terminate` 清理 | **定时骨架直接抄** | 待核 | **化用** |
| `astrbot_plugin_angel_heart`（已装） | Pages + Web API + 配置 schema | **V2 骨架直接抄** | 待核 | **化用** |
| `astrbot_plugin_maa` | 把 MAA 官方远程控制协议包成 AstrBot 插件（起 HTTP 服务 + `/maa start 基建`） | **V2 执行端可复用** | **AGPL-3.0** | ⚠️ **不能整段抄**，但可另实现同一协议 |
| `astrbot_plugin_arknights` / `_skland` / `_sklandv2` | 森空岛登录 / 签到 / 数据查询 | V3 参考 | 仓库 LICENSE 缺失（README 自称 MIT 不构成授权） | ⚠️ 只读思路 |
| `astrbot_plugin_endfield` | 终末地森空岛数据 | 与本项目无关 | **AGPL-3.0** | ❌ 避开 |
| `astrbot_plugin_maa`（列表之外）`fxquarter/..._sanity` | 明日方舟**理智**提醒 | 最接近的提醒类，但对象不同 | 待核 | 仅参考 |

> ⚠️ **许可证是本项目必须盯的一条线**：AGPL-3.0 有传染性，一旦复制其代码，整个插件就得按 AGPL 开源；「仓库里没有 LICENSE 文件」的权威解读是**未授予任何权利**，README 里写「MIT」不算数。集成策略应以「自己实现同协议」为主，而不是复制代码。

### 4.3 「集成」到底怎么集成

**【实测】** AstrBot 提供了插件互取的通道，但没有解耦层：

```python
md = self.context.get_registered_star("astrbot_plugin_maa")
if md and md.activated and md.star_cls is not None:
    ...  # md.star_cls 就是对方插件实例，可调用其方法
```

工程判定：

- ✅ **可用于增强**：对方在 → 多一个能力；对方不在 → 静默降级。
- ❌ **不可用于核心链路**：没有版本协商、没有 capability 声明、对方重构内部方法我们就会静默碎掉。
- 结论：**核心的「到点提醒」必须零外部插件依赖**；跨插件能力一律做成可选。

---

### 4.4 生态深挖补充（第二轮：GitHub 全站检索 + 逐仓库读源码）

第二轮把排查从"官方索引"扩到 GitHub 全站，得到几条**修正甚至推翻第一轮判断**的结论：

**（1）定时：不要抄 `astrbot_plugin_scheduler`。** 它自己写了个 30 秒轮询循环 + 自研 CronParser（**没有用 APScheduler**），3 star、最后更新 2025-07、且是 AGPL-3.0。而 AstrBot 本体已经内建 APScheduler——**【实测】** `context.cron_manager.add_basic_job(*, name, cron_expression, handler, description, timezone, payload, enabled, persistent) -> CronJob`，任务落库（`db.create_cron_job`），重启时 `sync_from_db` 自动恢复。**三班制直接建 3 个 basic job 就行，插件不必自己起循环。**

**（2）主动推送**：官方唯一形态就是 `event.unified_msg_origin`（umo）→ 持久化 → `await self.context.send_message(umo, chain)`。可参考 `Foolllll-J/astrbot_plugin_reminder`（把 origin 落 JSON）与 `Morizero1125/astrbot_plugin_arknights_skland`（umo → 私聊推送 + 失败计数）。

**（3）WebUI 可抄骨架**（除服务器上已装的 `angel_heart` 外，公开仓库里还有三个成熟的）：`AstraSolis/astrbot_proactive_reply`（`pages/webui/index.html` + `web_api.py`）、`Foolllll-J/astrbot_plugin_reminder`（`webui/api.py` 注册 + 上传）、`Justice-ocr/astrbot_plugin_mimo_tts_clone`（`pages_api.py` + 文件上传范式）。

**（4）MAA 遥控的真相：方向是反的。** `astrbot_plugin_maa` **不是去控制 MAA**——它自己是个 **HTTP 服务端**（aiohttp，默认 2828 端口，路由 `/maa/getTask` 与 `/maa/reportStatus`），由 MAA 客户端「设置 → 远程控制」**主动轮询它**领取任务、再回报状态；插件拿到结果后 `context.send_message(umo, chain)` 通知用户。任务别名表里原生就有 `Base` / `基建` / `基建换班`。

→ **「到点确认后自动触发 MAA 换班」完全可行**：我们只需往它的任务队列里放一条 `{"type":"Base"}`，**不必碰 MAA 本体**。代价两条：AGPL-3.0（不能抄代码，但可以自己实现同一协议或直接依赖）；它同样需要 umo 绑定（device → sender → umo 持久化在 data 目录）。

**（5）跨插件机制的真面目。** `Context.event_bus` **不存在**——**【实测】** `core/event_bus.py` 只是"平台消息 → PipelineScheduler"的内部分发队列，不对外暴露。跨插件只有"读全局注册表"这一级。**官方推荐的跨插件协作方式是 LLM 工具共享**（`context.add_llm_tools` / `activate_llm_tool_async`），而不是直接调对象。另**【实测】**除 `initialize()` / `terminate()` 外，还有一批生命周期钩子可用：`@filter.on_astrbot_loaded()`、`on_plugin_loaded()`、`on_plugin_unloaded()`、`on_platform_loaded()`、`on_plugin_error()`、`after_message_sent()` 等。

**（6）两个 MIT 的森空岛现成实现——本轮最有价值的收获：**

| 仓库 | 许可证 | 有什么 | 对我们的意义 |
| --- | --- | --- | --- |
| `Morizero1125/astrbot_plugin_arknights_skland` | **MIT** | AmiyaBot 官方森空岛插件移植；**已有 `amiya building` 基建查询**（走 `player/info`）；理智回满私聊提醒（间隔轮询 + umo 推送）；cred 自动刷新（10001/10002 重登）；SQLite 存 token | **V3「读取森空岛状态」可直接化用它**，不必从零移植 |
| `Siq5005/astrbot_plugin_arknights` | **MIT** | `core/skland.py` 的 `get_player_info` 与 `derive_sanity` 理智外推，纯函数、更工程化 | 便于抽取复用 |

**（7）原创性确认**：把「基建 / 换班 / riic」放到 GitHub 全站检索，**零命中**。这块确实没人做过，「三班制数据模型」必须我们自己定义。

> **综合判断**：本项目真正需要自研的只有「三班制时刻模型 + 提醒文案 + 边界胶水」三块；**数据获取、定时调度、WebUI、MAA 触发都有现成可复用件**——前提是逐个核对 LICENSE 文件（见 §4.2 的红线）。

---

### 4.5 实现选型速查（生态排查的落地结论）

| 层 | 选定方案 | 明确不选 / 坑在哪 |
| --- | --- | --- |
| **定时** | `context.cron_manager.add_basic_job()` × 3（三班各一个） | ❌ 自建 `AsyncIOScheduler`——重载后任务要自己重建、幂等自己做；❌ 抄 `astrbot_plugin_scheduler`（AGPL + 自研 30s 轮询） |
| **定时·版本兼容** | `getattr(self.context, "cron_manager", None)`，取不到再退 APScheduler；并声明 `astrbot_version: ">=4.17.0"` | 生态里 `Luna-channel/astrbot_plugin_Conversa` 就是这么降级的 |
| **任务类型** | **`add_basic_job`**（直接回调我们的 handler） | ❌ `add_active_job`——它构造 `CronMessageEvent` **唤醒主 Agent**，会消耗 LLM 调用，是给"让 AI 自己写提醒"用的 |
| **重载清理** | `terminate()` 里按 job 名前缀 `delete_job` | ⚠️ 框架**只在插件定义了 `terminate()` 时**才 await 它（`star_manager.py:1962`）；只定义 `__del__` 则被丢进线程池，不可靠 |
| **主动推送** | `await self.context.send_message(umo, chain)`——**必须检查返回值** | ⚠️ 找不到平台时只返回 `False` + 一条 warning，**不抛异常**，不检查就会静默漏提醒；`qq_official` 平台不支持此方法 |
| **umo 持久化** | 官方 KV（`Star.put_kv_data` / `get_kv_data`，需 ≥4.9.2）存目标列表；排班表等大对象存 `data/plugin_data/<plugin_name>/` | ❌ 存插件自身目录（插件更新即丢） |
| **失败处理** | 发送失败计数 + 阈值熔断（借 `Conversa` 的 offline protection 思路），恢复后自动解除 | QQ 掉线时定时任务会持续失败刷日志 |
| **「等你确认」交互** | `astrbot.core.utils.session_waiter` 的 `@session_waiter` + `SessionController` | 这是"到点问一句、你回话后再触发 MAA"的**官方工具**，不必自己造状态机 |
| **WebUI** | `pages/<name>/index.html` + `register_web_api("/<plugin_name>/...")`；后端用 `astrbot.api.web`，前端用 `window.AstrBotPluginPage` | 静态资源写相对路径即可——AstrBot 会重写并追加短期 `asset_token`；**不要手拼** `/api/plugin/page/content/...`，也不要自己加 token |
| **文件上传** | multipart + base64 **双通道**都提供（小文件贴文本、大文件走 multipart） | 受限 iframe 拿不到本地文件路径，只能上传 |
| **森空岛取数** | 复制 `Siq5005/astrbot_plugin_arknights` 的 **MIT** `core/skland.py`（`get_player_info` + `derive_sanity`，纯函数） | ❌ 用 `Azincc/astrbot_plugin_skland`——**仓库无 LICENSE 文件**，README 自称 MIT 不构成授权 |
| **MAA 触发** | 自己实现 `/maa/getTask` + `/maa/reportStatus` 两个 aiohttp 端点，任务类型 `Base` | ❌ fork `astrbot_plugin_maa`（AGPL 传染）；❌ 调它的私有 `_add_task()`（私有方法、无契约） |

**许可证底线（本项目若开源，必须守）**

- ✅ **可安全复制的 MIT**：`Siq5005/astrbot_plugin_arknights`、`Morizero1125/astrbot_plugin_arknights_skland`、`Justice-ocr/astrbot_plugin_mimo_tts_clone`、`anka-afk/astrbot_plugin_meme_manager`、`OMSociety/astrbot_plugin_schedule_assistant`、`Wyccotccy/astrbot_plugin_qzone_tools`
- ⚠️ **只能看思路、代码一行不能碰（AGPL-3.0）**：`astrbot_plugin_scheduler`、`astrbot_plugin_maa`、`astrbot_plugin_reminder`、`astrbot_proactive_reply`、`astrbot_plugin_nyscheduler`、`astrbot_plugin_endfield`、`astrbot_plugin_ssh`；**GPL-3.0**：`astrbot_plugin_screenctrl`
- ❌ **无 LICENSE 文件 = 法律上默认不可复制**：`Azincc/astrbot_plugin_skland`、`qihang518887/astrbot_plugin_sklandv2`、`yanyuhanyue/astrbot_plugin_electricity_monitor`、`thnf/get_api`、`Luna-channel/astrbot_plugin_Conversa`

**一条反面教材**：`KitsuneiMomo/astrbot_plugin_instant_memo` 里用 `self.context.star_map` 去遍历别的插件——`star_map` 是模块级变量，**`Context` 上根本没有这个属性**，那段代码永远走 fallback。取插件实例只能用 `get_all_stars()` / `get_registered_star()`。

---

## 5. 排班数据源：riic.autos 导出格式【实测 + 三方】

调研方式：读前端仓库源码逐文件核对，并**直接调用站点生产接口**（匿名示例 Box 通道）拿到真实导出数据。

### 5.1 关键结论：导出文件里没有时间轴

计算器路径导出的 MAA 文件（文件名固定 `arknights-infra-schedule-maa.json`）结构：

```jsonc
{
  "title": "可露希尔基建终端 · 243",
  "planTimes": "3班",
  "plans": [
    { "name": "Shift 1 · 12h", "Fiammetta": {...}, "drones": {...},
      "rooms": { "trading": [{"operators": ["但书","推进之王","摩根"], "sort": true, ...}],
                 "manufacture": [...], "control": [...], "power": [...],
                 "meeting": [...], "hire": [...], "processing": [...], "dormitory": [...] } },
    { "name": "Shift 2 · 6h", ... },
    { "name": "Shift 3 · 6h", ... }
  ]
}
```

| 字段 | 有没有 | 说明 |
| --- | --- | --- |
| `plans[].rooms.<组>[].operators[]` | ✅ | **班次 → 房间 → 干员**直接可解析，元素是干员名字符串 |
| `plans[].name` | ✅ | 形如 `Shift 1 · 12h`，**时长只能从这里正则抠** |
| `planTimes` | ✅ | `"3班"`（字符串） |
| `plans[].period` | ❌ **不存在** | 计算器导出链路完全不写 |
| `plans[].duration` | ❌ **不存在** | 同上 |
| `plans[].groups` | ❌ | 站点自身从不生成 |

**结论：班次时刻必须由我们自己维护**（用户声明 12/6/6 + 起始锚点，或用 `name` 里的 `· Nh` 做兜底校验）。这与 §6.2 发现的 MAA 侧行为完全一致——**两边都没有时间轴，时间是你的责任**。

### 5.2 离线可解析性

- **生成/导出阶段必须联网 + 登录**（求解在服务端跑）；
- **拿到导出的 JSON 之后，解析「班次 + 房间 + 干员」100% 可离线完成**；
- 更省事的一条路：站点把完整结果（含 `rotation.shifts[].duration_hours` 与 `period`）存在浏览器 localStorage 的 `arknights-infra-calc-session-v5` 里，让用户顺手贴这个比解析 MAA 文件还直接。

### 5.3 附带资产：干员与基建技能数据表【三方】

前端仓库打包了两份可离线复用的数据（用于 V3 起显示干员名/技能）：

- `operator-catalog.json`——429 名干员，含 `buildingSkills[]`（技能 id + 精英/等级解锁条件）
- `building-skill-catalog.json`——755 个基建技能，以技能 id 为键，带 `icon` 字段

⚠️ **许可证红线**：riic.autos 前端仓库 [RIIC-Web](https://github.com/KnightCodeSquareMatrix/RIIC-Web) 采用 **PolyForm Noncommercial 1.0.0**，**只能读、不能抄代码**。数据表的上游是 `arkntools` 与 `yuanyan3060/ArknightsGameResource`，若要用应回溯到上游的数据许可，而不是从前端仓库搬。

---

## 6. 执行端：MAA【官方 + 三方 + 实测】

### 6.1 MAA 能自动换班，但不管时间

- 基建换班三种模式：`mode=0` 常规 / `mode=10000` 自定义（读 `custom_infrast/*.json`）/ `mode=20000` 队列轮换。
- 自定义排班支持：按心情阈值切编组（`use_operator_groups` + `threshold`）、菲亚梅塔、无人机。
- **`period` 字段 MaaCore 不处理**。官方原文：「若当前时间在该区间内，则自动选择该计划……**core 不处理该字段，若您使用接口集成 maa，请自行实现该逻辑**」。
- `duration` 与 `drones.rule` 是**保留字段、目前无作用**（官方特意注明「以后可能到时间了弹窗提醒该换班了」——正是本需求的位置，但尚未实现）。

→ **「12h/6h/6h 到点换对应班次」的判断责任在外部调度器。**

### 6.2 现状印证

**【实测】** 用户本机的 `arknights-infra-schedule-maa.json` 同样**没有 `period` 也没有 `duration`**，与 §5.1、§6.1 三方一致。用户当前的换班时刻是靠 MAA 的两个定时器（04:00 / 23:00）+ 手工的 `PlanSelect` 在维持。

### 6.3 外部触发的四条路

| 方式 | 能做什么 | 限制 |
| --- | --- | --- |
| **MaaCore 集成 API**（C / Python / Java / Go / Rust / TS 绑定） | 可精确控制 `Infrast` 任务参数、`plan_index`、`filename` | 要自带 MaaCore + `resource/` + ADB 环境 |
| **maa-cli** | 无 GUI 跑任务；`Time`/`Weekday` 等**变体**可指定 `plan_index` | 没有 `maa infrast` 预定义命令，需自写 `type="Infrast"` 任务文件 |
| **官方远程控制协议** ⭐ | MAA 主动轮询你的 `getTask` 端点取任务、`reportStatus` 回传结果（含截图）；官方文档里甚至有「用 QQBot 控制 MAA」的范例 | **只能下发 `LinkStart-Base`，带不了 `filename`/`plan_index`**——跑的是 MAA 客户端里当前那套配置；且客户端侧只在 GUI 实现 |
| HTTP / WS 常驻接口 | — | **官方没有**。社区有 `MAA-Linux-RemoteControl`、`MAA-FnOS` 等替代 |

**关键约束**：远程控制只能"触发一次已经配好的基建任务"。所以若要做到"按班次精确切换"，必须在 **MAA 侧**用其自带的换班时间条件、或改用 maa-cli 的变体能力；AstrBot 侧只负责"什么时候触发"。

### 6.4 Linux 服务器能跑 MAA 吗

**能跑 MAA，但跑不了游戏。** MAA 本体不需要 GUI（官方有 maa-cli，定位就是无头服务器），但它靠**截图识别 + ADB 点击**，必须有"一个跑着明日方舟的安卓实例"。

- 服务器要自建，得走 redroid / Waydroid：需要内核 `binder`/`ashmem`（或 memfd）、`--privileged` 容器、x86 上还要 ARM 转译层（**方舟新引擎已删掉 x86 的 so**，纯 x86 镜像跑不起来），整套约 3 GB 磁盘；
- 这台机器 4C4G、已跑 4 个容器、可用内存约 1.5 GB——**同时扛 redroid + 方舟 + MaaCore + AstrBot 基本不够**；
- OpenCloudOS 9 的内核 binder 支持与 glibc 版本**未验证**（官方库要求 glibc ≥ 2.31，社区实践建议 Ubuntu 24.04 级别）。

→ **不建议**。正确拓扑是「服务器调度 + 本机执行」。

### 6.5 推荐拓扑

| 方案 | 说明 | 评价 |
| --- | --- | --- |
| **A. 纯提醒** | 到点 QQ 提醒，人工处理 | 最稳、零风险增量 |
| **B. 提醒 + 一键触发本机 MAA** ⭐ | 服务器发提醒 → 用户确认 → MAA 跑一次基建 | 执行端用户机器上**已现成**；接法见下 |
| C. 全 Linux 自建 | redroid + MaaCore + 调度器 | ❌ 见 §6.4 |

B 的两种接法：

- **B1（最省事）**：用 MAA 官方远程控制协议——在 AstrBot 插件里暴露 `getTask`/`reportStatus`，MAA GUI 里填端点轮询。优点：官方协议、自带截图回传。缺点：只能触发，带不了班次参数；且端点要被用户本机访问到（需端口放行或用隧道，注意 [ADR-0004](../decisions/ADR-0004-tencent-domain-block.md) 的域名限制）。
- **B2（最灵活）**：本机装 maa-cli，写 `type="Infrast"` 任务 + `Time` 变体指定 `plan_index`，由一个小服务接受服务器调用。能精确按班次切换，代价是本机要多维护一个常驻小服务与凭据。

### 6.6 风险

- MAA 官方[用户协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/dev-v2/terms-of-service.md)：仅供学习交流，用户自行评估在带反作弊的系统上使用的风险，账号安全与数据损失 MAA Team 不负责。
- 鹰角曾[公示封禁](https://ak.hypergryph.com/news/2852)「第三方非法程序或非法修改游戏内数据」账号。MAA 用截图 + ADB 点击、不改游戏文件，未被点名，但**自动化本身违反用户协议，风险自负**。
- 本项目**只做提醒与触发**，不做注入、不修改游戏数据——这是应当守住的边界。

---

## 7. 状态源：森空岛「疲劳驱动」【三方 + 逆向】

### 7.1 结论：能拿到真实心情

**【三方】** `GET https://zonai.skland.com/api/v1/game/player/info` 的 `data.building` 里：

| 数据 | 字段 | 说明 |
| --- | --- | --- |
| 各房间当前进驻干员 | `building.{control,manufactures,tradings,dormitories,...}.chars[]` | 全房间覆盖 |
| **干员心情/疲劳** | 每个进驻干员的 `ap`（整数） | **`心情 = clamp(ap / 360000, 0, 24)`**；满值 `8640000` |
| **已耗尽名单** | `building.tiredChars[]` | 游戏自己算好的"没心情了"，最省事 |
| 制造站产物与进度 | `manufactures[].formulaId`、`weight/capacity/complete/speed` | 可判爆仓 |
| 贸易站订单 | `tradings[].stock[]`、`stockLimit` | — |
| 无人机 | `building.labor.{value,maxValue}` | — |
| 宿舍氛围 / 线索 / 办公室 / 训练室 | `dormitories[].comfort`、`meeting.clue`、`hire`、`training` | 完整 |

**两个必须记住的语义坑**：

1. **`ap` 越大越不疲劳**（它是心情容量，不是消耗量）。宿舍里的干员实测全为 `8640000`，在岗的落在 `1836000–4464000`（心情 5.1–12.4）。把 `tiredChars[].ap = 35` 读成"心情 35"就完全反了。
2. **快照不实时**：`ap` 是上次同步那一刻的值，不能直接做时间外推。稳妥做法是只判「当前心情 ≤ 阈值」与 `tiredChars` 非空两类信号。

### 7.2 认证与限制

- 森空岛**没有公开开发者平台**，接口均来自逆向；认证链为 `通行证 token → oauth grant → cred/token → player/info`，推荐**扫码**授权。
- `token` 约每 20 分钟刷新，`cred` 固定保存 7 天后要重新授权。
- 限流：触顶返回 429，需做**全局冷却**（RIIC-Web 的做法是命中一次冷却 60s）；建议按 uid 缓存结果 ≥120s。
- **数据非实时**：一次请求返回组合快照；游戏侧基建本身约 10 分钟粒度结算。
- 第三方调用是否封号：**官方无公开表态【未验证】**；同接口的签到类工具长期在跑、未见公开封号报告，但属未授权使用未公开接口。

### 7.3 可复用代码与许可证【三方】

| 仓库 | 语言 | 许可证 | 可用性 |
| --- | --- | --- | --- |
| `FrostN0v0/nonebot-plugin-skland` | Python | **MIT** | ✅ **首选基底**（签名 + 刷新 + 扫码 + pydantic schema） |
| `ProbiusOfficial/Skland_API` | 文档 | **MIT** | ✅ 字段文档（作者声明停更，注意过时） |
| `AEtherside/skland-kit` | TS | **MIT** | ✅ 最完整的 `building` 字段定义 |
| `Azincc/astrbot_plugin_skland` | Python | **无 LICENSE 文件** | ⚠️ 只读思路 |
| `astrbot_plugin_endfield`、`erzaozi/skland-plugin` | — | **AGPL-3.0** | ❌ 避开 |
| `KnightCodeSquareMatrix/RIIC-Web` | TS | **PolyForm Noncommercial** | ⚠️ 只能读不能抄 |

---

## 8. 集成边界与分层设计（本报告的核心产出）

把上面五条线拼起来，建议的架构是**四层 + 单向依赖 + 逐层可降级**：

```
┌─────────────────────────────────────────────────────────┐
│ L4 执行层（可选，V2+）                                   │
│   MAA（本机）：官方远程控制协议 / maa-cli 变体            │
│   降级：没有 → 退化为"只提醒"                            │
├─────────────────────────────────────────────────────────┤
│ L3 通知层（V1 核心）                                     │
│   AstrBot 插件：调度 + 消息渲染 + QQ 主动推送             │
│   依赖：AstrBot 4.28.1 本体，零外部插件                   │
├─────────────────────────────────────────────────────────┤
│ L2 判定层（策略可换）                                    │
│   周期策略（V1，12h/6h/6h + 用户声明锚点）                │
│   心情策略（V3，森空岛 ap / tiredChars）                  │
│   两者同接口，调度层不感知实现                            │
├─────────────────────────────────────────────────────────┤
│ L1 数据层（按需接入，每层独立降级）                       │
│   a. 班次时刻：插件配置（V1 必选）                        │
│   b. 班次干员：riic.autos 导出 JSON（V1 可选）            │
│   c. 真实心情：森空岛 API（V3）                           │
└─────────────────────────────────────────────────────────┘
```

**四条铁律**：

1. **核心链路零外部依赖**——「到点提醒」不许依赖任何第三方插件、任何游戏账号、任何外部 API。全部缺失时它必须仍然工作。
2. **跨插件只做增强**——`star_cls` 拿不到、对方没激活、方法改名，一律静默降级（`get_registered_star` + `activated` + `star_cls is not None` 三重判空）。
3. **策略与调度解耦**——周期判定与心情判定是同一个接口的两个实现，这样 V3 不用重写调度（对应 [01c §2.2](01c-feature-breakdown.md) 的扩展点要求）。
4. **不碰游戏账号是 V1 的底线**——V3 一旦接森空岛就触碰这条边界，必须重新判定、单独授权，并明确凭据只落本地、不进日志。

---

## 9. 风险清单（合并五条线）

| # | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| R1 | **需求前提未澄清**（§2：MAA 已能自动换班） | 可能重复建设 | **先回答 H1–H4，再动工** |
| R2 | `context.register_task` 已弃用，照老教程写会在重载时出问题 | 重载后重复推送 | 用 `initialize()` + `terminate()`，照 `daily_sharing` 骨架 |
| R3 | 插件重载后旧调度器/任务残留 | 同一班次推两次 = 骚扰 | `terminate()` 里 `shutdown` + `cancel`，加 `_is_terminated` 标志 |
| R4 | 跨插件 `star_cls` 无契约 | 对方改动即静默失效 | 只做增强，三重判空，绝不进核心链路 |
| R5 | 许可证传染（AGPL / 无 LICENSE 仓库） | 被迫开源或侵权 | 只抄 MIT；同协议自己实现；用前逐个核 LICENSE 文件 |
| R6 | riic.autos 前端是 PolyForm Noncommercial | 抄代码即违规 | 只读思路；数据表回溯上游 |
| R7 | 森空岛限流 429，且凭据需保管 | 提醒失灵 / 凭据泄露 | 全局冷却 + 缓存 ≥120s；凭据加密落 `plugin_data`，绝不进日志 |
| R8 | 游戏账号安全与用户协议（MAA / 森空岛） | 封号风险 | 只提醒不注入；接入第三方前单独向用户确认并记录风险 |
| R9 | 服务器（香港）到用户本机的连通性 | 执行层触发不了 | 优先做拉模式（本机主动轮询），避免给服务器开入站端口 |
| R10 | 排班表靠人工导出，会过期 | 提醒里干员名单不准 | 消息附"排班导出时间"；配置里可关闭干员展示 |

---

## 10. 结论与建议路线

**建议：先做纯提醒，把执行层与状态源留成可插拔的扩展，并且在动工前先把 §2 的 H1–H4 问清楚。**

分期：

| 阶段 | 内容 | 依赖 | 说明 |
| --- | --- | --- | --- |
| **V1** | AstrBot 插件：三班配置 → 到点 QQ 提醒（班次名 + 提示 + 下一班时刻） | 仅 AstrBot 4.28.1 | 零外部依赖，风险最低 |
| **V1.5** | 可选：提醒里带该班各房间干员 | 用户导出一次 riic JSON | 解析器只认 `plans[].rooms[].operators[]`，时长按声明 + `name` 兜底 |
| **V2** | WebUI 页面（改三班、看倒计时、看发送记录） | `pages/` + `register_web_api`（4.28.1 已支持） | 骨架抄 `angel_heart` |
| **V2+** | 一键触发本机 MAA 换班 | 官方远程控制协议（§6.5 B1） | 只能触发不能选班次；**若 H3 成立则价值最大** |
| **V3** | 疲劳驱动（森空岛心情） | 扫码授权；**可直接化用 `Morizero1125/astrbot_plugin_arknights_skland`（MIT，已有基建查询与 umo 推送）** | 真·按需换班；需重新判定"不碰游戏账号"边界 |

**明确不建议**：把 MAA / 安卓实例搬到服务器上（§6.4）；复制 AGPL 或无 LICENSE 仓库的代码（§4.2、§7.3）；从 riic.autos 抓取数据（无公开 API、需登录）。

---

## 11. 待用户拍板

| # | 问题 | 为什么挡着 |
| --- | --- | --- |
| **P1** | §2 的 H1–H4：既然 MAA 已能定时自动换班，你要的「提醒」到底补哪个缺口？ | **整个项目的地基。** 决定是纯提醒、提醒+执行、还是直接转向疲劳驱动 |
| **P2** | 现在生成排班表用 riic.autos，还是一次流（`ark.yituliu.cn`）？ | 决定解析器认哪种导出（本机 MAA 自带文件自述来自一图流，但你在用的 `Filename` 是 riic 的导出名） |
| **P3** | 若走 V2+：接受「服务器提醒 → 你确认 → 本机 MAA 跑一次」这条链路吗？ | 决定要不要做跨机下发，以及用 B1（官方协议）还是 B2（maa-cli） |
| **P4** | 跑 MAA 的那台电脑是否长期开机？ | 决定执行层能不能被依赖 |
| **P5** | V3 接森空岛会碰到「不碰游戏账号」这条边界，是否允许？ | 决定疲劳驱动能不能立项 |

> P1 一答复，我会把立项四小步按新结论重写一版（[01a](01a-core-value.md)–[01d](01d-executable-units.md)）；P2–P5 影响后续阶段的排期。
