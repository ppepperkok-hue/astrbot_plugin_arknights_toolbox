# 发布清单（AstrBot 插件市场）

> 每次发版照这份清单走一遍。依据：官方[发布插件](https://docs.astrbot.app/dev/star/plugin-publish.html)文档 + 本项目 `docs/tech-stack.md` §6.1 版本策略。
> **最后一步（提交市场）只能由仓库所有者完成**——需要 AstrBot Cloud 账号。

## 1. 代码就绪

- [ ] `python scripts/verify_constitution.py` 通过
- [ ] `ruff check .` 无问题
- [ ] `ruff format --check .` 无待格式化文件
- [ ] `pytest -q` 全绿
- [ ] 纯逻辑文件**零 `astrbot` import**（`git grep -n "^from astrbot\|^import astrbot"` 只应命中 `main.py` 与 `modules/*/module.py`）

## 2. 版本与变更记录

- [ ] `metadata.yaml` 的 `version` 已升（语义化版本：破坏性变更 → 主版本；新增能力 → 次版本；修 bug → 修订号）
- [ ] `CHANGELOG.md` 有对应条目，**破坏性变更显式写明**（见 `tech-stack.md` §6.1）
- [ ] git tag 已打并推送：`git tag -a vX.Y.Z -m "..."` + `git push --tags`

## 3. 市场元数据

- [ ] `name` 为 `astrbot_plugin_` 前缀 + 全小写 + 无空格
- [ ] `display_name` / `desc` / `version` / `author` / `repo` 齐全
- [ ] `desc` 是**用户向**描述（支持 Markdown），**不出现架构术语**
- [ ] `tags` 已填（影响市场分类与搜索）
- [ ] `support_platforms` **不写**（本插件推送走通用 `send_message`，任何平台都支持；写了反而把用户挡在外面）

## 4. 体积与仓库卫生

- [ ] 插件 zip **< 16MB**（超限会被 CI 自动拒）
- [ ] 仓库内**没有** `.git`、`__pycache__`、`.pytest_cache`、`.ruff_cache`、`node_modules`（`.gitignore` 已覆盖，`git status --ignored` 复核）
- [ ] 无密钥、无个人路径、无真实 QQ 号/IP 写进代码或文档

## 5. 许可

- [ ] 仓库根有 `LICENSE`（本项目为 MIT）
- [ ] 新增代码**未引入 AGPL / 无 LICENSE 仓库**的代码（立项调研 R5；本项目曾因此拒绝把 MAA 样本复制进 fixture）
- [ ] 引用他人思路时，在 commit message 或文档中说明来源

## 6. 上线实测（发布前必须真跑一遍）

- [ ] 部署到真实 AstrBot：加载**零报错**
- [ ] 核心链路**真实到达**（本项目：到点推送真的发出去了）
- [ ] **重载/重启后状态稳定**（本项目踩过两次：cron 任务堆积、指令漏进 LLM——**这两类只在上线后暴露**）
- [ ] 关键配置项改了之后**真的生效**（如时区）
- [ ] 未绑定 / 配置非法 / 依赖缺失等**失败路径**都有明确报错，不静默

## 7. 文档

- [ ] `README.md` 面向**普通用户**：怎么装、填什么、发什么指令、不生效怎么查
- [ ] 开发者文档（架构、契约）在 `docs/` 下，README 里有入口链接

## 8. 提交市场（仓库所有者执行）

- [ ] 登录 <https://cloud.astrbot.app/publish> 提交仓库地址与版本
- [ ] 提交后回来在 `CHANGELOG.md` 记一笔「已发布到市场」

## 附：本项目已知的、发布前必须盯住的坑

| 坑 | 什么时候会踩 | 已否解决 |
| --- | --- | --- |
| 指令处理完仍流入 LLM → 用户收到两份回复 + 白烧一次模型调用 | 上线后才暴露 | ✅ 已修（`event.stop_event()`） |
| 每次重载新增 3 条 cron 任务且不清理 → 表无限增长 | 上线后才暴露 | ✅ 已修（注册前按前缀清理） |
| 改绑死锁：门禁只认原会话，导致永远改不回去 | 第二个会话操作时才暴露 | ✅ 已修（私聊放开、群聊限管理员） |
| 时区配置空转（容器无 tzdata 时） | 部署到精简镜像才暴露 | ✅ 容器实测有时区数据 |
| 多用户只能有一个提醒目标 | 第二个用户绑定时 | ⚠️ **已知限制**，v1 不做多绑定（仅留门） |
