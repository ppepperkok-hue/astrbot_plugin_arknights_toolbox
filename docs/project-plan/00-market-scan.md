# 调研：AstrBot 生态里有没有现成的「基建换班提醒」插件

> 时间：2026-09-29 ／ 作者：agent
> 目的：用户要求「先上 GitHub 找一找相关插件，有就不重复造轮子」。本文是这次检索的完整结论，作为立项清单的输入。

## 1. 检索范围与方法

| 渠道 | 具体做法 |
| --- | --- |
| AstrBot 官方插件市场索引 | 拉取 [`AstrBotDevs/AstrBot_Plugins_Collection`](https://github.com/AstrBotDevs/AstrBot_Plugins_Collection) 的 `plugins.json`（493 KB，**1,332 个插件**），对插件名 + 描述全文做中文/英文关键词匹配 |
| GitHub 仓库检索 | `astrbot arknights`（21 个结果）等组合查询 |
| AstrBot 官方文档 | [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 及其 12 篇子指南 |

关键词集合：`基建`、`换班`、`排班`、`arknights`、`明日方舟`、`方舟`、`riic`、`森空岛`、`skland`、`infrast`、`maa`、`理智`、`schedule`、`cron`、`reminder`。

## 2. 结论

> **1,332 个上架插件里，「基建」「换班」「排班」「infrast」四个关键词命中数为 0。没有现成的明日方舟基建换班提醒插件。**

与需求沾边的插件共 6 类，逐条说明为什么都不能直接用：

| 插件 | 它做什么 | 为什么不能直接用 |
| --- | --- | --- |
| [`astrbot_plugin_arknights`](https://github.com/liuwanwan1/astrbot_plugin_arknights) | 基于森空岛公开 API 查询玩家数据 | 只查询、不提醒；但**森空岛封装可以直接借鉴**（V3 用到） |
| [`astrbot_plugin_skland`](https://github.com/Azincc/astrbot_plugin_skland) | 森空岛签到，支持定时 | 签到不是换班；同上可借鉴登录与 API 封装 |
| [`astrbot_plugin_maa`](https://github.com/Hakuin123/astrbot_plugin_maa) | 远程遥控 MAA | 是「替你操作」而不是「提醒你」，且与 01a 的价值边界冲突 |
| `astrbot_plugin_mrfz` / `_mrfzccl` / `_mrfz_haunting_query` / `ark_info_search` / `gameinfo` | 干员语音、立绘猜谜、抽卡、Wiki 查询 | 全是内容查询，与换班时刻无关 |
| [`astrbot_plugin_arknights_sanity`](https://github.com/fxquarter/astrbot_plugin_arknights_sanity) | 明日方舟**理智**提醒 | 最接近的一类，但提醒对象是理智回复，不涉及基建三班与换班时刻 |
| `astrbot_plugin_scheduler` / `_reminder` / `_better_reminder` / `_daily_reminder` / `_nyscheduler` | 通用定时任务与提醒（Cron、间隔、可视化配置） | 能定时发消息，但**不知道什么是「三班 12h/6h/6h」**，也没有干员/排班概念；可以借鉴调度实现 |

补充：社区里也**没有**任何插件以 riic.autos 排班表导入或基建换班为卖点。

## 3. 结论：要自己做，但有三块现成的可以借

1. **调度实现**：`astrbot_plugin_scheduler`（Cron + 可视化配置）和 `astrbot_plugin_reminder` 的定时写法可以直接读源码借鉴，不必从零摸索 AstrBot 的定时姿势。
2. **森空岛 API 封装**：`astrbot_plugin_arknights` / `astrbot_plugin_skland` 已经把登录、token 刷新、数据拉取趟过一遍，V3 阶段读游戏状态时优先参考（注意各自的开源许可）。
3. **插件市场规范**：官方 `plugin-publish` 与 `plugin-market` 文档给出了上架要求，如果将来开源发布，按它走。

## 4. 技术可行性：AstrBot 提供了什么

用户要求「有开发潜力」——即现在只做提醒，以后要能长出 WebUI 配置、排班表导入、森空岛状态读取。逐条核对官方能力：

| 需要的扩展点 | AstrBot 的对应能力 | 文档 |
| --- | --- | --- |
| 主动定时推送 | `await self.context.send_message(unified_msg_origin, chain)`，配合存储的 umo 即可在任意时刻发消息 | [消息的发送](https://docs.astrbot.app/dev/star/guides/send-message.html) |
| WebUI 配置表单 | 插件目录放 `_conf_schema.json`，AstrBot 自动在 WebUI 渲染配置项 | [插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html) |
| WebUI 自定义页面 | `pages/<name>/index.html` + `context.register_web_api()`，可做复杂表单、状态面板、SSE | [插件 Pages](https://docs.astrbot.app/dev/star/guides/plugin-pages.html) |
| 排班表文件上传 | `await request.files()` 取 `PluginUploadFile`，`upload.save(path)` 落盘 | 同上 |
| 持久化 | 官方存储接口，数据存 `data` 目录（**不要**存插件自身目录，避免更新覆盖） | [存储](https://docs.astrbot.app/dev/star/guides/storage.html) |
| 调用协议端 API | `event.bot.api.call_action(...)`（如查好友、发群消息） | [杂项](https://docs.astrbot.app/dev/star/guides/other.html) |
| 元数据与版本约束 | `metadata.yaml` 支持 `display_name`、`short_desc`、`support_platforms`、`astrbot_version` | [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) |

**插件形态相对「独立服务」省掉的一件事**：不需要再给 NapCat 加 HTTP 端点、也不需要重启 NapCat 容器。AstrBot 自己已经连着两个 QQ 号，插件直接复用它的发送通道。原立项里的风险 R2（改 NapCat 配置要重启容器）随之消失。

## 5. 需要验证的未知项

| # | 待验证 | 怎么验 |
| --- | --- | --- |
| U-1 | 插件里做长期定时任务的推荐姿势（自建 `asyncio` 任务 vs 依赖 AstrBot 的调度设施） | 读 `astrbot_plugin_scheduler` 源码 + 本地起一个最小插件实测 |
| U-2 | `umo` 怎么最自然地拿到（用户私聊一次机器人 vs 配置里手填） | 最小插件里打印 `event.unified_msg_origin` 观察 |
| U-3 | 插件热重载后定时任务是否残留（会不会重复推送） | 反复重载插件，观察任务数 |
| U-4 | 机器人号与收件人是否好友（决定私聊能否送达） | 装好骨架后直接发一条试 |

## 6. 对本项目立项的影响

- **形态变更**：从「独立小服务 + 直推 OneBot」改为「**AstrBot 插件**」。原形态下的大部分运维改动（改 NapCat 配置、独立部署、独立进程）全部不需要了。
- **仓库**：仍是独立仓库（插件本来就该独立成仓），命名建议 `astrbot_plugin_ak_shift_reminder` 之类，遵守官方 `astrbot_plugin_` 前缀约定。
- **边界变更**：01a 里「不碰游戏账号」这条在 V3（森空岛）会被触碰，需要重新判定，本期不放松。
- **范围**：本期仍然只做「到点提醒」，但插件骨架必须给 WebUI / 排班导入 / 森空岛留出扩展位。
