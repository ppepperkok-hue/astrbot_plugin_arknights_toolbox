# AstrBot 4.28.1 实测 API 事实表（服务器 bt-he1k 容器 `astrbot` 内核实）

> 采集时间：2026-09-29 01:40 ／ 方式：SSH 只读探测（probe 1–5），**未改动服务器任何文件**
> 目的：官方文档对应的是最新版，服务器上跑的是 4.28.1。**下面每一条都是在部署实例里 grep / sed 出来的，不是从文档抄的。**
> 证据路径均相对于容器内 `/AstrBot`。

## 1. 版本与环境

| 项 | 实测值 |
| --- | --- |
| AstrBot 版本 | `4.28.1`（`pyproject.toml:3`） |
| Python | 3.12.14 |
| apscheduler | 3.11.3（已装） |
| httpx | 0.28.1（已装） |
| aiohttp | 3.14.3（已装） |
| quart | 已装 |
| 已装插件 | 36 个（`data/plugins/`），其中 **8 个带 `pages/`**，**15+ 个带 `_conf_schema.json`** |

## 2. 可用的插件 API（逐条实测）

| 能力 | 结论 | 证据 |
| --- | --- | --- |
| 主动发消息 | ✅ 可用 | `astrbot/core/star/context.py:614` `async def send_message(self, session: str \| MessageSesion, message_chain: MessageChain) -> bool` |
| 简化版主动发消息 | ✅ 可用 | `astrbot/core/star/star_tools.py:35` `StarTools.send_message(session, message_chain)` 类方法，**不需要 self.context** |
| 注册 Web API | ✅ 可用 | `core/star/context.py:705` `def register_web_api(route, view_handler, methods, desc)`，同路由重复注册会替换 |
| Web API 请求/响应对象 | ✅ 可用 | `astrbot/api/web.py`：`json_response`(:342)、`error_response`(:365)、`file_response`(:390)、`stream_response`(:416)、`PluginUploadFile`(:95)、`PluginRequest`(:165) |
| 插件 Pages | ✅ 可用 | Dashboard 侧：`astrbot/dashboard/api/plugins.py` 的 `/api/plugin/page/entry`(:1244)、`/bridge-sdk.js`(:1457)、`/content/{plugin_id}/{page_name}/`(:1469)；服务实现 `dashboard/services/plugin_page_service.py` |
| 配置表单 | ✅ 可用 | `core/star/star_manager.py:212` `self.conf_schema_fname = "_conf_schema.json"` |
| 插件生命周期钩子 | ✅ **官方姿势** | `core/star/base.py:107` `async def initialize()`（"当插件被激活时会调用"）／`:110` `async def terminate()`（"当插件被禁用、重载插件时会调用"） |
| **注册后台任务**（旧接口） | ⚠️ **已弃用** | `core/star/context.py:884` `register_task` 带 `@deprecated`，理由"This method is deprecated. Start background tasks in the plugin's initialize() method instead." → **必须改用 `initialize()` 里 `asyncio.create_task`** |
| 插件实例互取 | ✅ 可用 | `core/star/context.py:346/352` `get_registered_star(name)` / `get_all_stars()` 返回 `StarMetadata`；`core/star/star.py:42` `star_cls: Star \| None`（插件实例）、`:37` `star_cls_type`、`:59` `activated`、`:62` `config` |
| 内置 cron 管理器 | ✅ **确认可用（4.17+）** | `core/star/context.py:167` `self.cron_manager`；`core/cron/manager.py:148` `async def add_basic_job(*, name, cron_expression, handler, description, timezone, payload, enabled, persistent) -> CronJob`，落库 `db.create_cron_job`，重启 `sync_from_db` 自动恢复；另有 `add_active_job`(:175)、`list_jobs`(:220)、`get_next_run_time`(:292)、`run_job_now`(:310) |
| 生命周期钩子（`initialize`/`terminate` 之外） | ✅ | `@filter.on_astrbot_loaded()`、`on_plugin_loaded()`、`on_plugin_unloaded()`、`on_platform_loaded()`、`on_plugin_error()`、`on_waiting_llm_request()`、`on_llm_request/response()`、`on_agent_begin/done()`、`on_using_llm_tool()`、`on_decorating_result()`、`after_message_sent()`；定义在 `core/star/register/star_handler.py:337-744`，由 `astrbot/api/event/filter/__init__.py` 导出 |
| `Context.event_bus`（跨插件事件总线） | ❌ **不存在** | `core/event_bus.py` 是"平台消息 → PipelineScheduler"的内部分发队列，`context.py` 中 grep 不到 `event_bus` |
| 文转图 | ✅ 可用 | `core/star/base.py:77` `text_to_image()`、`:92` `html_render()` |
| 平台实例 | ✅ 可用 | `core/star/context.py:738` `get_platform(type)`、`:761` `get_platform_inst(platform_id)` |

## 3. 关键格式

| 项 | 值 | 证据 |
| --- | --- | --- |
| `unified_msg_origin`（umo）格式 | `platform_name:message_type:session_id` | `core/platform/astr_message_event.py:106-108` |
| 插件数据目录（官方要求数据存这里） | `get_astrbot_plugin_data_path()` → `<data>/plugin_data` | `core/utils/astrbot_path.py:60` |
| 插件代码目录 | `<data>/plugins` | `core/utils/astrbot_path.py` |

## 4. 服务器上现成的参考实现（可直接照抄骨架）

| 插件 | 为什么值得抄 |
| --- | --- |
| `astrbot_plugin_daily_sharing` | **定时推送的完整范本**：`main.py:29` 自建 `AsyncIOScheduler`；`:139` `initialize()` 里 `asyncio.create_task(self._delayed_init())` 并用 `_bg_tasks` 集合跟踪；`:145` `terminate()` 里 `scheduler.shutdown(wait=False)` + 逐个 `task.cancel()`；自带 `_is_terminated` 防僵尸实例（重载后旧任务不复活） |
| `astrbot_plugin_angel_heart` | **Pages + Web API 的完整范本**：`pages/chat-config/{index.html,assets/}`；`web_api/__init__.py:372` 调 `context.register_web_api(path, handler, methods, description)`；`_conf_schema.json` 演示了 `string/bool/float/object` 嵌套配置与 `_special: select_provider`；还有 `tests/test_web_api_quart.py` 示范怎么给 Web API 写单测 |
| `astrbot_plugin_qzone` | 被 `daily_sharing` 用 `star_cls` 直接调用其 `service.publish_post(...)` —— **"集成别的插件"的真实先例** |

## 5. 由此确定的技术路线（本地证据支撑）

1. **后台定时**：在 `initialize()` 里起 `asyncio.create_task`（官方推荐），或自建 `AsyncIOScheduler`（`daily_sharing` 已跑通）；`terminate()` 里必须 shutdown + cancel，防止重载后重复触发。
2. **主动推送**：`StarTools.send_message(umo, chain)` 或 `self.context.send_message(umo, chain)`；umo 通过与用户的一次交互取得并存到 `plugin_data`。
3. **V2 的 WebUI 页**：`pages/<name>/index.html` + `context.register_web_api()`，4.28.1 已在服务这些路由，且有 `angel_heart` 可抄。
4. **V3 的排班表上传**：`astrbot/api/web.py` 的 `PluginUploadFile` + `request.files()` 在 4.28.1 **确实存在**——**但它在插件 Pages 的 bridge 上根本递不过去**。

   > ⚠️ **2026-09-29 线上实测修正（务必先读这条，否则会照着上面那句再走一遍弯路）**
   >
   > 插件 Pages 的 bridge 用 `postMessage` 与父页面通信，而 **`FormData` 不能被结构化克隆**，在真实浏览器里直接抛
   > `Failed to execute 'postMessage' on 'Window': FormData object could not be cloned.`
   > 文件**永远到不了后端**。
   >
   > **可用做法**：前端用 `FileReader.readAsDataURL` 把文件读成 base64（切掉 `data:` 前缀），走**普通 JSON POST**（`bridge.apiPost(endpoint, { filename, content_b64 })`），后端解码后再校验。
   > 代价是体积约 ×1.37；我们实测排班表 15~30KB，完全可接受。
   >
   > **为什么可以确信这条能走**：配置写回走的是**同一座 bridge 的同一套 `apiPost(纯对象)`**，而那条路已在页面上成功使用过。唯一不可用的 `FormData` 已经拿掉。
   >
   > 体积上限要**按解码后的字节算**，且解码前先按 base64 长度做一次廉价拒绝——否则「防大文件」的校验逻辑自己就会把大字符串吃进内存。
5. **集成其他插件**（用户新提的方向）：`context.get_registered_star("astrbot_plugin_maa").star_cls` 可以拿到实例，但要判 `activated` 且 `star_cls is not None`；`qzone` 被 `daily_sharing` 调用就是先例。**但这是软依赖，对方插件没装/未激活时必须降级。**

## 5.1 跨插件机制专章（应要求回查官方文档确认）

用户记忆中「AstrBot 存在这么一个东西（能让插件之间互相调用）」。已在 4.28.1 **随附文档**（容器内 `/AstrBot/docs/zh/dev/star/plugin.md`）中定位到原文：

```py
#### 载入的所有插件
plugins = self.context.get_all_stars()  # 返回 StarMetadata 包含了插件类实例、配置等等
```

同一节（`### 其他`）还给出：

```py
#### 注册一个异步任务
直接在 __init__() 中使用 asyncio.create_task() 即可。

#### 获取加载的所有平台
platforms = self.context.platform_manager.get_insts()
```

**结论：官方文档明确承认 `StarMetadata` 里带「插件类实例」，跨插件调用属于文档内能力。** 新指南对应页 `guides/other.md:55` 内容一致。

### 这条通道的真实边界（源码级核实）

| 项 | 事实 | 影响 |
| --- | --- | --- |
| 有没有事件总线 / Broker | **没有**。全树 grep `Bus`/`Broker` 只命中 `StarHandlerRegistry`、`PluginManager`、`SessionPluginManager`、`SessionServiceManager`，均为内务管理类，不是消息总线 | 只能"直取对象 + 调方法"，没有解耦层 |
| 有没有接口契约 | **没有**。无一版本协商、无 capability 声明、无依赖注入 | 对方改内部方法名 = 我们静默碎掉 |
| 取不到时怎么办 | `StarMetadata.star_cls` 可为 `None`，注解原文："当 activated 为 False 时，star_cls 可能为 None，请不要在插件未激活时调用 star_cls 的方法" | 必须判空 + 判 `activated` |
| 社区先例 | `astrbot_plugin_daily_sharing` 直接调 `astrbot_plugin_qzone` 的实例方法 | 证明可行，但同样脆弱 |

**工程判定：可用于增强，不可用于核心链路。** 本项目的"到点提醒"必须能在**零外部插件**的情况下完整工作；跨插件能力一律做成可选增强，失败即降级。

### 另外两个可能被记混的机制（一并记录）

| 机制 | 位置 | 能力 |
| --- | --- | --- |
| 会话级插件开关 | `astrbot/core/star/session_plugin_manager.py` `SessionPluginManager`，状态存 `sp` 的 `session_plugin_config` 键，Dashboard 侧 `services/session_management_service.py` | 可按 `unified_msg_origin` 粒度启用/禁用某插件（例如只在私聊开着提醒） |
| 能力注册为 LLM 工具 | `context.register_llm_tool` / `add_llm_tools` / `activate_llm_tool_async` | 把插件的能力暴露给 AI 调用，适合"跟机器人说句话查下一班"这类自然语言入口 |

## 6. 遗留待验（进实现阶段第一件事）

| # | 待验 | 影响 |
| --- | --- | --- |
| L1 | ~~`context.cron_manager.add_basic_job` 对插件是否可直接调用~~ → **已确认可用**（签名见 §2，落库 + `sync_from_db` 重启自恢复） | 结论：定时用官方 `cron_manager`，**不自建 APScheduler** |
| L2 | 插件重载后旧 `AsyncIOScheduler`/task 是否真的被清干净（跑一次实测） | 决定是否需要额外的幂等锁 |
| L3 | `PluginKVStoreMixin` 的实际方法集（Star 继承它，但方法不在 `base.py` 里定义） | 决定绑定关系存哪 |
| L4 | aiocqhttp 适配器下 umo 的实际取值（实测打印一次） | 决定绑定指令怎么写 |
