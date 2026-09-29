# 发布前审查（R1 · 面向插件市场）

> **审查对象**：即将提交到 AstrBot 插件市场的 `astrbot_plugin_arknights_toolbox`（HEAD = `49379b0`）。
> **审查视角**：**陌生人使用者**——他没有参与开发、不知道这个项目的任何内部概念、装完就要能用。
> **审查立场**：只找「会让陌生用户卡住或误解」的地方。**本报告不改任何代码**，改动需单独授权。
> **审查方式**：全仓静态核查 + 运行项目自己的发布门禁 + 对 git 历史与 tag 做差异比对。
> **本地环境限制**：本工作区**没有 AstrBot 运行时**，因此凡涉及「面板怎么渲染」「真机链路」的项，本报告一律标注为**未能验证**，不写成结论。

---

## 0. 结论摘要（先说能不能发）

**现在的状态：建议先修完第 1、2 节再发。**

门禁本身是**干净的**：`verify_constitution` / `check_astrbot_load_form` / `check_room_colours` / `ruff check` / `ruff format --check` / `pytest -q` **全部 exit 0**，1050 条测试通过；打包 0.59 MB（上限 16 MB）；**零运行时依赖**；**没有任何打包进来的图片/音频素材**；**没有密码、密钥、真实 IP、真实 QQ 号**；许可面（MIT 全文、AGPL 回避记录、fixture 自撰声明）**经得起看**。

**但有 2 条会直接影响陌生人的，必须先处理**：一是**版本号与 tag 对不上**（同一个 `0.4.0` 指向两套不同产物）；二是**两个默认关闭的模块的开关入口从未被验证过是否存在**。另有 7 条应当修，都属于「成本很低、但陌生人一定会撞上」的类型。

---

## 1. 阻断发布

### B1. 版本号没升，而且 `v0.4.0` 这个 tag 指向的是**另一套产品**

**位置**：`metadata.yaml:32`（`version: 0.4.0`）、git tag `v0.4.0`、`CHANGELOG.md:8-9`（`[Unreleased]` 为空）、`CHANGELOG.md:10-41`（0.4.0 条目）

**事实（本次实测）**：

```
v0.4.0 里 modules/skland/module.py : 不存在
v0.4.0 里 modules/maa/module.py    : 不存在
HEAD   里 modules/skland/module.py : 存在
HEAD   里 modules/maa/module.py    : 存在
v0.4.0 的 metadata.yaml            : version: 0.4.0
工作区的 metadata.yaml             : version: 0.4.0
v0.4.0 之后的提交数                  : 37
```

**为什么会让陌生人受害**：他装到的是 HEAD（含森空岛与 MAA 两个新模块），但版本号写着 `0.4.0`——而 `v0.4.0` 这个 tag 里**既没有森空岛也没有 MAA**。**同一个版本号对应两个功能集完全不同的产物**，任何按版本号比对的地方（市场的更新判断、用户自己 `checkout` tag 复核、以后写的问题报告）都会得出错误结论。而且 `CHANGELOG.md:8` 的 `[Unreleased]` 是**空的**，也就是说这两个模块**从未被对外记录过**——用户升级后没有任何东西告诉他多了什么。

**这一条同时违反项目自己的清单**：`docs/implementation/release-checklist.md:16-18` 明确要求「`version` 已升」+「`CHANGELOG.md` 有对应条目」+「git tag 已打并推送」。

**建议**：升到 `0.5.0`（两个新模块属新增能力，按 `tech-stack.md` §6.1 的语义化版本规则是次版本），补一条 CHANGELOG 条目覆盖 skland、maa、以及班次默认值/顺序的变更，然后重新打 tag。**注意班次默认值改动（`第 1/2/3 班` + 12/6/6）属于用户可见的行为变更**，照 §6.1 也要写进 CHANGELOG。

### B2. 「默认关闭的模块怎么开」这件事，全仓没有任何一次验收记录

> **✅ 2026-09-29 已验收（所有者实测）**：`modules` 段在真实面板上**渲染正常**，
> `skland` 与 `maa` 两个开关**都看得到、能勾选保存**。因此**不需要**按
> `plan.md:85` 的备选方案改扁平键名，`_conf_schema.json` 的结构**保持不动**。
> 本条从"阻断"降为"已确认"，下面的分析保留作判断依据。

**位置**：`_conf_schema.json:2-32`（`modules` 段）、`_conf_schema.json:19-24`（skland 默认 `false`）、`:25-30`（maa 默认 `false`）、`docs/implementation/plan.md:85`、`pages/shift-reminder/index.html`（全文）

**问题**：`skland` 与 `maa` **默认关闭**，而它们的**唯一开启入口**是 `_conf_schema.json` 里那个**双层嵌套**的 `modules` 对象：

```json
"modules": { "type": "object", "items": { "skland": {"type": "bool", ...}, "maa": {...} } }
```

而**插件页面（`pages/shift-reminder/index.html`）里没有任何模块开关**——我把整个页面读完了，它只有：概览、排班表上传、班次时间轴、最近发送。所以「双层 `object`/`items` 能不能被 WebUI 配置页正常渲染」这件事**就是这两个功能的生死线**。

**项目自己早就把它列为待办，但从未关闭**：`docs/implementation/plan.md:85` 写着——「`_conf_schema.json` 的双层 `object`/`items` 嵌套能否被 WebUI 正常渲染」……「本地无法验证，S6 部署时看真实配置页。**渲染不出来就改成扁平键名**」。我全仓搜索过，**没有任何一处记录了这次验证的结果**。

**为什么会让陌生人卡住**：他照着 `_conf_schema.json:22` 的提示想开森空岛，打开配置页却**找不到那个开关**（或者看到一团渲染不出来的东西）。此时他没有任何出路——README 没写、页面里没有、schema 的 hint 只告诉他要开，没告诉他开不了怎么办。**两个功能对他等于不存在。**

**为什么标为阻断**：这**不是一次改动，是一次确认**（在面板上看一眼），成本两分钟；而它一旦不成立，就是「两个模块永远开不了」这种不能事后补救的事。

**缓解证据（不足以替代实测）**：`docs/project-plan/01-astrbot-4281-api-facts.md:51` 记录 `astrbot_plugin_angel_heart` 的 `_conf_schema.json`「演示了 `string/bool/float/object` 嵌套配置」——**嵌套 object 有先例**。但那**不是我们这个 `modules` 形状**，也不能替代本项目自己的一次确认。

> ⚠️ 顺带提醒：**本项目有过一次直接相关的线上事故**（`docs/implementation/plan.md:115` 的 L2）——插件已加载但注册表为空，`/ak bind` 完全没反应。根因正是当时 schema 里没有 `modules` 段。**入口不可见这类问题在这个项目上已经发生过一次**，不是理论风险。

---

## 2. 应当修（发布前）

### S1. README 只描述了四个模块里的一个，而漏掉的其中一个是**对游戏账号的写操作**

**位置**：`README.md:3`、`README.md:66-73`（指令一览）、`README.md:75-89`（只讲排班表）

README 的开篇（`:3`）是「**到点提醒你去《明日方舟》基建换班**」，指令一览只有 4 条（`bind` / `status` / `test` / `import`）。而插件实际提供 **14 个子命令**：

| 模块 | 未出现在 README 的子命令 |
| --- | --- |
| `recruit` | `/ak recruit <标签…>` |
| `skland` | `/ak skland`、`login`、`logout`、`check`、`signin` |
| `maa` | `/ak maa`、`run`、`skip`、`cancel` |

**为什么严重**：`skland` 模块能**替用户在游戏里签到**（`_conf_schema.json:147-156`，`signin_enabled` + `signin_cron`）。这是一个**写操作**，而面向普通用户的 README **一个字都没提它存在**。一个陌生人读完 README 会认为「这个插件只是提醒我换班」，然后某天发现它还会用他的账号签到——**即使默认关闭，「没告诉用户一件事」本身就不该发生**。

`docs/implementation/implementation.md:249`（P1.3）把 README 的目标写为「面向**想装个换班提醒**的陌生人」——那是**工具箱定位之前**的验收标准，现在已经落后于产品了（项目定位见 `docs/project-plan/04-positioning-revision.md:27`）。这是这一整类问题的**根因**。

**建议**：README 的指令一览补全三个模块，并给森空岛与 MAA 各写一句「这是什么、默认关闭、开了会发生什么、在哪儿开」。MAA 已有现成的用户向文档（`docs/guides/maa-remote-control.md`），README 只需给入口链接。

### S2. `README.md:11` 已经是**假保证**

**位置**：`README.md:11`

原文：

> 它**不需要**登录你的游戏账号，也**不需要**联动 MAA——只是按时刻提醒你该去换班了。

这句话在写作时是真的（那时只有提醒模块）。**现在它是假的**：插件内置森空岛扫码授权（账号级访问）与 MAA 远程控制。这句话说的是「本插件」，读者会理解成**整个插件**。

**为什么严重**：这是**陌生人判断「能不能把这个东西接到我的账号上」的第一句话**，而且它给的答案是错的——**方向偏向让他更放心**。这属于本报告要重点找的那一类：**主动做出的、让人误以为安全的承诺**（当晚同类问题已发现两处：`/ak maa skip` 的「班次索引不会前进」、以及 skland 回执曾声称「已脱敏」却打了完整 uid）。

**建议**：改成准确的表述，例如「**提醒本身**不需要登录你的游戏账号，也不需要 MAA——即使你没有装任何辅助工具、游戏没在运行，提醒照样到。（插件里另有可选模块会用到账号或 MAA，**都默认关闭**，见下文）」。

### S3. 市场元数据只覆盖换班，搜不到另外三个模块

**位置**：`metadata.yaml:3`（`short_desc`）、`metadata.yaml:4-31`（`desc`）、`metadata.yaml:36-40`（`tags`）

`short_desc` 是「基建换班到点提醒，三班时刻自己填」；`desc` 的五条能力全是提醒；`tags` 是「明日方舟 / 基建 / 换班 / 提醒 / arknights」。

**为什么严重**：`desc` 是**市场详情页的正文**，`tags` 决定**分类与搜索**。现在的结果是：一个想找「明日方舟 公招 计算」或者「森空岛 签到」或者「MAA 远程」的用户**搜不到这个插件**，即使搜到了，详情页也告诉他这个插件只会提醒换班。**这四个模块里三个在市场上是不可见的。**

**建议**：`desc` 补三个模块各一段（各写「能做什么 / 默认是否开启 / 需要额外做什么」）；`tags` 补「公开招募」「计算」「森空岛」「签到」「MAA」。**`support_platforms` 继续不写**（`release-checklist.md:26` 的既有裁决，是对的）。

### S4. MAA 用户指南里留着一条**已经从代码里删掉的假保证**

**位置**：`docs/guides/maa-remote-control.md:37`

指南的能力表里写着：

> ✅ 选择「我自己换」 | 发 `/ak maa skip`，不派任务，**班次索引不动**

**而代码已经不再这么说了**：`modules/maa/relay.py:120-145` 的 `skipped_text` 现在是**有条件**的两种措辞（未取走时才能说「不会前进」），而 `modules/maa/module.py:351-352` 的注释明确记着这件事的由来：

> 「`skip` 的旧文案就是这么错的：它**无条件保证「索引不会前进」**」

**为什么严重**：这正是「说了但做不到」的假保证，而且它留在**发出去给用户看的指南**里。用户在任务已被取走时读到这句，会以为班次没动——**而它实际上前进了**，于是他下次手动换班的基准就错了。

**建议**：把这一行改成与 `relay.skipped_text` 一致的条件表述，并在表里把「已取走时可能拦不住」写出来。

### S5. README 完全没提**插件页面**，于是把用户引到了更差的那条路

**位置**：`README.md:75-89`（尤其 `:81-83`）对比 `pages/shift-reminder/index.html:59-73`

README 教用户的是：

> 1. 把导出的 `.json` 文件**放进插件的数据目录**（`/ak import` 用错文件名时会告诉你确切路径）
> 2. 私聊机器人发 `/ak import <文件名>.json`

**而插件自带一个页面，上面就有「选择文件 → 上传并导入」**（`pages/shift-reminder/index.html:59-67`）。更要紧的是：README 在 `:46` 花了一整段解释「班次顺序与排班表对不上会有什么后果」，而那个问题的**一键解法就在页面上**——`:70-73` 那块建议区和「按排班表填入班次时长」按钮（`pages/shift-reminder/index.html:72`）。

**为什么严重**：陌生人被迫走「找到数据目录 → 手动放文件 → 记文件名 → 发指令」这条路，而**产品里已经有一条点点鼠标就完成的路，README 从没告诉他页面存在**。这会让第一印象差一个档次，而且放错目录时他还得先失败一次才知道路径。

**建议**：README 的「使用」一节开头加一句「所有设置都可以在 AstrBot 面板的**插件页面**里做（拖时间轴改班次、上传排班表、看倒计时与发送记录）」，并给出打开方式；`/ak import` 保留为备用路径。

### S6. `/ak` 的兜底文案把内部标识抛给用户，并指向一个不存在的入口

**位置**：`main.py:183-185`（兜底分支）、`main.py:157-159`（无模块分支）

兜底输出是：

```
未知子命令：maa
已装载的模块：shift_reminder、recruit、skland
可用子命令由各模块提供，见各模块文档。
```

（这段文案是今天在服务器上实测到的原样输出。）

**三个问题**：

1. **`shift_reminder` 是内部模块 id**，不是用户会认识的名字。用户看到的是「模块」列表，而他想知道的只是「我能发什么指令」。
2. **「见各模块文档」是死指针**：README（`README.md:66-73`）里根本没有这些模块的文档入口，用户无处可去。
3. **无法区分「功能没开」与「没这个功能」**。用户读 `docs/guides/maa-remote-control.md` 之后发 `/ak maa`，如果忘了先开模块，得到的是「未知子命令：maa」——而 `maa` **确实存在于这个插件里，只是没开**。他没有任何线索往「去配置里打开」这个方向想。

**建议**：兜底文案里列出**面向用户的功能名与指令样例**（而不是模块 id），并在「子命令匹配到某个存在但未启用的模块」时明说「`maa` 这个模块存在但当前没开启，去插件配置里打开 `modules.maa`」。**注意**：**宿主目前拿不到「哪些模块存在但被关掉」**，但 `main.py:87` 已经调了 `discover_modules()`（磁盘上的模块包），这条路是现成的。

### S7. 公招计算在群聊里被限成管理员专属，而它没有任何需要保护的东西

**位置**：`modules/recruit/module.py:153-156`、`core/permission.py:41-45`

`recruit` 走的是同一条会话门禁：

```python
allowed, reason = session_allowed(is_group=..., is_admin=..., action="查公开招募")
```

拒绝文案是 `core/permission.py:44`：「群聊里只有 AstrBot 管理员能查公开招募：在群里发指令会把结果发给所有人，可能打扰其他成员。」

**为什么这条理由不成立**：`core/permission.py:16-20` 把群聊限制的**理由**写得很清楚——「群里发指令会把结果发给所有人」是**针对绑定操作**的（`bind` 会把换班提醒推给全体成员）。而 `recruit` 是**只读、无状态、不碰绑定**的纯计算：用户主动问、答案回到他问的那个群，**没有任何东西被改动**。把 bind 的理由套到 recruit 上属于**过度限制**。

**为什么会让陌生人卡住**：最常见的第一次接触就是把机器人拉进群试试。他发 `/ak recruit 输出 近战位`，得到的是「只有管理员能查公开招募」——**他会认为这个功能坏了或者这个插件很官僚**，而实际上换个私聊就能用。

**建议**：这是所有者的策略选择，但**建议把「只读、不改状态」的命令（`recruit`、`status`）在群里放开**，只对「会改状态或会影响他人」的命令（`bind`、`import`、`skland` 的授权/签到、`maa` 的派任务）保留群聊管理员限制。若坚持现状，至少要在这条拒绝文案里**明确说出「私聊里面任何人都能用」**——目前 `core/permission.py:45` 的文案其实已经写了这句，**但 `recruit` 的拒绝理由里读者不一定读到最后一句**，属于措辞问题而非逻辑问题。

---

## 3. 可以留到下一版

### L1. 提示里说的按钮名，和页面上的按钮名不一样

**位置**：`modules/shift_reminder/roster.py:394` 与 `pages/shift-reminder/index.html:72`

提示说「可在页面上点**「按排班表填入」**对齐」，页面上的按钮实际写的是**「按排班表填入班次时长」**。同一处还有 `roster.py:344`：「想用它在页面上点「按排班表填入」即可」。用户照着提示找按钮时，标签对不上会多花几秒（好在是子串包含，找得到）。**建议**：把文案里的按钮名改成与页面标签逐字一致。

### L2. `/ak test` 发送失败时，用户在会话里看不到任何东西

**位置**：`modules/shift_reminder/module.py:605-615`

`/ak test` 把测试提醒直接发到当前会话，**失败时只写日志**（`:612` exception、`:615` warning），不回复任何文字。由于 `handle_command` 返回 `True` 后宿主调了 `stop_event()`（`main.py:164`），用户既没收到提醒、也没收到错误。

**为什么只算低**：`README.md:95-96` 已经把这个形状**主动转成了分诊信号**——「能收到，说明推送链路没问题；收不到，那就是第 1、2 条」。所以它不算「静默失败」，只是**诊断体验不够好**。**建议**：发送失败时补一句回执（「测试提醒没能发出去，多半是机器人掉线了」）。

### L3. `astrbot_version: ">=4.17.0"` 只被 `cron_manager` 这一项验证过

**位置**：`metadata.yaml:35`、`docs/tech-stack.md:66`、`main.py:212`、`modules/maa/module.py:426-428`

下限 `>=4.17.0` 的依据是「`cron_manager` 自 4.17 起才有」（`docs/tech-stack.md:66`，有源码核实）。但插件现在还依赖另外两个较新的能力：

- **页面与配置写回**：`main.py:212` `getattr(context, "register_web_api", None)` —— 拿不到就降级（页面与配置写回不可用，提醒不受影响，**有 warning 日志**）；
- **MAA 两个端点**：`modules/maa/module.py:426-428` —— 同样依赖 `register_web_api`，拿不到时 `unavailable_reason` 会明说「两个端点没注册上」。

仓库里**没有任何关于 `register_web_api` 是哪个版本引入的事实记录**（`docs/project-plan/01-astrbot-4281-api-facts.md` 只记了 4.28.1 的表现）。若某个 4.17–4.27 的版本没有它，用户会得到一个「提醒能用、页面不存在」的插件——**好在降级是显式的（有日志、有 `unavailable_reason`），不是静默失败**。**建议**：核实一次 `register_web_api` 的引入版本；若高于 4.17，要么抬高下限，要么在 README 里注明页面需要较新版本。

---

## 4. 许可与合规核查

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 仓库根 `LICENSE` 为 MIT 且**完整** | ✅ | `LICENSE:1-21`，标准 MIT 全文，`Copyright (c) 2026 ppepperkok-hue`，无删节 |
| `metadata.yaml` 声明许可与之一致 | ⚠️ **无许可字段** | `metadata.yaml` 只有 `name/display_name/short_desc/desc/version/author/repo/astrbot_version/tags`。**未验证** AstrBot 元数据规范是否支持许可字段（本项目的市场调研 `docs/project-plan/00-market-scan.md:51` 列出的字段里也没有它）。**若市场支持该字段，建议补上以与 LICENSE 一致** |
| 引入了 AGPL 代码 | ✅ 未发现 | 全仓 `AGPL` 命中**全部是回避记录**，例如 `docs/tech-stack.md:16,71,109,199-204`、`docs/project-plan/02-integration-research.md:160-165,234`、`docs/project-plan/03-requirements-clarification.md:84-101`。MAA 集成是**按官方协议自己实现**（`docs/implementation/v2-integration-plan.md:34`；`docs/project-plan/09-maa-trigger-assessment.md:4`、`10-maa-shift-switching.md:7` 各自声明未复制 MAA 代码或资源） |
| 引入了无 LICENSE 项目的代码 | ✅ 未发现 | 无 LICENSE 的三个候选被明确否决并记档：`docs/project-plan/08-reuse-assessment.md:25,168`、`docs/project-plan/03-requirements-clarification.md:96`、`docs/project-plan/07-skland-api.md:164` |
| fixture 是否裁剪自 AGPL 样本 | ✅ 自撰且有声明 | `tests/fixtures/infrast_three_shifts.json:2` 与 `tests/fixtures/infrast_four_shifts_with_skip.json:2` 的 `_comment` 都写明「结构与 MAA 同源，**内容自撰，不是从任何 AGPL 项目复制的文件**」；原因记于 `tests/test_schedule_file.py:8-10` |
| 打包了游戏图片/音频 | ✅ **一个都没有** | `git ls-files` 里**没有任何** `.png/.jpg/.gif/.svg/.mp3/.wav/.ico/.ttf/.zip` 文件（命令输出为空） |
| 数据文件的外链政策 | ✅ 自洽且已声明 | `modules/shift_reminder/data/avatar_map.json:2` 写明「只登记文件名，不登记图片本身……头像一律外链引用，绝不打包进仓库」，并注明来源（arkntools，MIT，LICENSE 已核实）与**游戏资源版权属鹰角**；`modules/shift_reminder/avatars.py:13-14` 复述同一政策；`modules/recruit/data/recruit_pool.json:2` 注明来源（PRTS 公开 Cargo API）与**只取干员名/星级/标签三项事实** |
| 额外依赖 | ✅ 零运行时依赖 | `requirements.txt:1-5` 明确「Runtime dependencies: intentionally EMPTY」；开发依赖另在 `requirements-dev.txt`（ruff/pytest/pytest-cov，不会被插件安装）；`skland` 的二维码是自研标准库编码器（`modules/skland/qr.py`），未引入 `qrcode`/`Pillow` |
| 打包体积 | ✅ 0.59 MB | `git archive` 实测 615,805 字节（市场上限 16 MB） |

---

## 5. 密钥与个人信息扫描

**结论：没有发现任何凭据、真实 IP、真实 QQ 号。** 以下是全部命中，**只报位置与类别，不复述值**。

### 5.1 看起来像密钥、实为测试占位（无需处理）

| 位置 | 内容类别 | 为什么安全 |
| --- | --- | --- |
| `tests/test_skland_api.py:446` | 变量名形如 `secret`，值形如 `SUPER-SECRET-TOKEN-<数字>` | 签名算法的单元测试输入，**明显是占位符**，不是任何真实凭据 |
| `tests/test_maa_protocol.py:130,136` | IPv4 字面量 | 属 **RFC 5737 文档专用段**（`203.0.113.0/24`），不可路由 |
| `tests/test_maa_module.py:97,285` | IPv4 字面量 | 同上，文档专用段 |
| `docs/guides/maa-remote-control.md:84,143,176` | IPv4 字面量 | `127.0.0.1`（回环）与 `192.0.2.0/24`（RFC 5737 文档段） |

### 5.2 `/etc/passwd` 类的命中（无需处理）

`modules/shift_reminder/roster.py:108-109`、`tests/test_roster.py:116,121`、`docs/implementation/plan.md:51`、`docs/implementation/session-report.md:40` —— 全部是**路径穿越防护的测试用例与记录**，不是凭据。

### 5.3 服务器指纹（**低风险，但确实带进了仓库**）

这些是**开发文档里的环境标识**，随仓库一起发给市场用户：

| 位置 | 类别 |
| --- | --- |
| `CHANGELOG.md:98`、`docs/tech-stack.md:154`、`docs/project-plan/01-astrbot-4281-api-facts.md:1`、`docs/project-plan/09-maa-trigger-assessment.md:126` | 服务器主机名 |
| `docs/implementation/plan.md:60`、`docs/implementation/session-report.md:27` | 内部测试平台实例名与端口 |
| `docs/project-plan/09-maa-trigger-assessment.md:236` | 运维脚本路径 |

**判定**：**都不是凭据**——没有 IP、没有账号、没有密钥，主机名单独出现无法用于攻击。**不建议为此改历史文档**（这些是开发过程的真实记录，属于`docs/project-plan/` 的立项材料性质）。但若所有者在意，可在下一版顺手把 `docs/implementation/` 里的运维细节留在运维工作区、不随插件发布。**本报告不建议在发布前处理这一条**。

### 5.4 明确**没有**发现的东西（也是核查项）

- **真实服务器 IP**：全仓 IPv4 扫描只命中 5.1 里那些文档专用段与回环地址。
- **真实 QQ 号**：按已知的两个机器人号与所有者账号号搜索，**零命中**；测试里用的是 `10001` 这类明显的假号（`tests/test_maa_module.py:156`、`tests/test_target.py:261+`）。
- **API Key / token 值**：无命中（`abk_` 前缀、`Bearer <值>`、`api_key=<值>` 形状均未出现在仓库内）。
- **密码字段**：无。

---

## 6. 全新安装可行性核查

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 磁盘上的文件是否全部进了 git | ✅ **完全一致** | 排除缓存目录后磁盘 127 个文件 = `git ls-files` 127 个；**「在盘不在库」为空集**（本项目曾因裸 `data/` 规则把 `recruit_pool.json` 漏掉：本地全绿、部署即缺文件，CHANGELOG 0.4.0 已记） |
| 数据文件是否都随代码发布 | ✅ | `modules/recruit/data/recruit_pool.json`、`modules/shift_reminder/data/avatar_map.json` 均在库内；`tests/test_recruit_dataset.py:197` 有一条断言专门钉住「招募数据文件是随代码发布的资源，必须存在」 |
| `plugin_data/` 不存在时会怎样 | ✅ 自动创建 | `modules/shift_reminder/module.py:235-236` 与 `modules/skland/module.py:151-152` 都是 `mkdir(parents=True, exist_ok=True)`；存储层 `core/storage.py:131,171` 也各自补建父目录 |
| 默认配置是否自洽 | ✅ | `_conf_schema.json:37-49` 默认三班 `08:00/12h + 20:00/6h + 02:00/6h`：合计 24 小时、首尾相接（08→20→02→08），能通过 `validate` |
| 有没有空字符串导致的启动失败 | ✅ 本版本无 | `test_config_schema.py` 有断言钉住默认值。**但要记一条今晚的线上实例**：`modules.maa.task_type` 曾是空串（开关先于该配置项被打开，AstrBot 不给已存在的配置段补默认值），导致 `TaskTypeError: 任务类型配置不合法：''`、模块启动失败。**这是升级路径的坑，不是全新安装的坑**——全新安装会拿到 schema 默认值 |
| 各模块默认开关是否合理 | ✅ | `shift_reminder=true`、`recruit=true`（纯本地计算、零配置即可用）、`skland=false`（需账号授权）、`maa=false`（需本机配置）。**理由都写在各自的 `hint` 里**（`_conf_schema.json:10-11,16,22,28`） |
| 关掉的模块会不会被用户误以为不存在 | ❌ **会**，见 S6 | |
| 纯逻辑层有没有漏进框架依赖 | ✅ | `^(from|import) astrbot` 只命中 `main.py` 与四个 `modules/*/module.py`，与 `release-checklist.md:12` 的预期完全一致 |
| 插件能否在 AstrBot 的加载形态下导入 | ✅ | `scripts/check_astrbot_load_form.py` exit 0（21 个纯逻辑文件在「顶层没有 `core`」的子进程里全部导入成功） |

---

## 7. 陌生用户路径走查

> 逐步走一遍「装 → 开 → 配 → 用」。每步标注：他**看到什么**、需不需要额外知识、卡住时有没有出路。
> 打 ❌ 的步骤就是第 1、2 节里的发现。

### 第 1 步 · 在市场里找到它

他搜「明日方舟」→ 能找到。搜「公开招募 / 公招 / 森空岛 / 签到 / MAA」→ **找不到**（S3）。看到详情页：`short_desc` 说「基建换班到点提醒」，`desc` 五条全是提醒。**他的判断：这是个换班提醒插件。**

**❌ S3**：插件实际有四个模块，市场页只描述了一个。**卡不卡**：不卡，但**三个功能对他不可见**，等于没发布。

### 第 2 步 · 安装

市场一键安装，或按 README `:23-28` 手动 clone。**这一步没问题**：`metadata.yaml` 的 `name` 有 `astrbot_plugin_` 前缀、全小写、无空格；`astrbot_version` 能过 AstrBot 的校验（`tests/test_config_schema.py:221-233` 有断言）。

### 第 3 步 · 第一次看日志

`shift_reminder` 与 `recruit` 默认开，日志是 `[ak_toolbox] 已装载模块：shift_reminder、recruit`；`recruit` 还会打「已装载招募数据：160 位干员、29 个标签」并附来源 URL。**他看到的东西是可信的、有信息量的**——这一处做得很好。

### 第 4 步 · 读 README 找用法

README `:56-64` 告诉他私聊发 `/ak bind`。他照做，绑定成功。**这一步是顺的**：`/ak` 无参数默认 `status`（`main.py:197`），`/ak status` 会报当前班次、下一班倒计时、最近发送、绑定目标——**信息密度合适**。

**❌ S1**：README 只讲到这里。他不会知道有 `/ak recruit`、`/ak skland`、`/ak maa`。

### 第 5 步 · 读 README 安排班表

**❌ S5**：README `:81-83` 让他把 `.json` 放进插件数据目录、再发 `/ak import <文件名>.json`。他不知道那个目录在哪——**README 说「用错文件名时会告诉你确切路径」**，所以他要**先失败一次**才知道。而插件页面上就有上传按钮，README 没提。

**❌ S2**：他在 README `:11` 读到「不需要登录你的游戏账号，也不需要联动 MAA」，于是放下了对账号的顾虑。

### 第 6 步 · 想改班次

**❌ S5 的连带**：README 的配置章节（`:32-52`）教他去 WebUI 的插件配置页填六个数（`shift_1_name` / `shift_1_start` / `shift_1_hours` × 3），并强调「三段时长之和必须正好 24 小时、必须首尾相接」——**这是一条容易出错的手工约束**。而插件页面上是**拖拽时间轴**（`pages/shift-reminder/index.html:86-89`：「三段永远首尾相接、合计 24 小时，所以拖不出非法配置」）。**他再次被引向更差的路径**。

### 第 7 步 · 没收到提醒

README `:93-104` 给了五步分诊表，质量**很高**（先 `/ak test` 分诊、检查绑定、检查机器人是否掉线、检查时刻是否合法、检查提前量、检查是否已发过），最后给了日志搜索关键词 `ak_toolbox`。**这一步做得比多数插件好。**

**L2 的关联**：`/ak test` 失败时用户端没有回执，只能靠「没反应」推断——但 README 已经把「收不到 = 第 1、2 条」写清楚了，所以**他能自己走下去**。

### 第 8 步 · 想用公招计算，在群里试

**❌ S7**：他拉机器人进群，发 `/ak recruit 输出 近战位`，收到「群聊里只有 AstrBot 管理员能查公开招募」。**他会以为这个功能坏了**，而实际上换个私聊就能用。

### 第 9 步 · 在配置页里看到森空岛和 MAA 两个开关

**❌ B2**：这里就是最大的未知。**开关渲染出来** → 他开了森空岛，发 `/ak skland login`，拿到二维码，扫码（这一条链路今天已在服务器上真机跑通，README 之外还有 `_conf_schema.json:145` 的用法说明）。**开关没渲染出来** → 他**没有任何出路**：页面里没有、README 没写、schema 的 hint 只叫他开。

### 第 10 步 · 自己看 MAA 指南

如果他找到了 `docs/guides/maa-remote-control.md`（**README 里没有入口链接**，得自己去仓库里翻），这份指南的质量**很高**：能力边界、前置条件、API Key、两个 URL、路径为什么这么写、证书这条第一风险、排障五档、未验证事项逐条列出——**都写得很实在**。

**❌ S4**：但 `:37` 那条「`skip` 班次索引不动」的假保证与代码不一致。

---

## 8. 本次已验证为干净的项目（不必再查）

- **发布门禁全绿**：`verify_constitution.py` 通过、`check_astrbot_load_form.py` OK、`check_room_colours.py` OK、`ruff check .` All checks passed、`ruff format --check .` 76 files already formatted、`pytest -q` **1050 passed**（exit 0）。
- **打包体积** 615,805 字节，远低于市场 16 MB 上限。
- **零运行时依赖**，且 `skland` 的二维码是自研标准库实现（未引入第三方编码库）。
- **仓库内无任何二进制/图片/音频素材**（`git ls-files` 扫过去为空）。
- **`maa` 默认关闭时不会暴露任何端点**。这一条我专门查了：`_register_web_api` 只在 `initialize` 里调用（`modules/maa/module.py:204,419`），而 `initialize` 只对**启用的模块**执行（`main.py:89-91` → `core/registry.py`）。所以「关掉的模块不会注册任何指令或端点」这条 `_conf_schema.json:5` 的承诺**成立**，用户不会在没主动开启的情况下被暴露一个公网端点。
  > ⚠️ 顺带纠正一条**曾经被当成证据的错误判据**（已记于 `docs/project-plan/09-maa-trigger-assessment.md`）：那条前缀下**任何**路径——包括根本不存在的——都会返回 401，因为鉴权先于路由判定。**「401 所以路由存在」是假的**；本报告改用「读注册调用点」来核实这一条。
- **重复确认的幂等性有测试钉着**，且 `maa` 的任务 id 是 `uuid5(命名空间, 换班时刻)` 的纯函数（`modules/maa/queue.py`），保证同一换班只有一个 id。
- **敏感值不落日志**：`skland` 的扫码状态日志渲染为 `scanCode=<已隐去 len=… sha256=…>`（代码位置 `modules/skland/module.py:583`，今天在服务器上实测到的原样输出即为此形状）；`maa` 的请求只记键名、类型与长度（`modules/maa/protocol.py:228`）。

---

## 9. 未能在本环境验证的项（不写成结论）

| # | 事项 | 为什么没验 | 建议谁来验 |
| --- | --- | --- | --- |
| 1 | **`modules` 双层 `object`/`items` 能否被 WebUI 配置页渲染** | 本工作区**没有 AstrBot 运行时**，无法渲染配置页 | **所有者**，在真实面板上看一眼（**B2**，发布前必做） |
| 2 | `register_web_api` 是哪个 AstrBot 版本引入的 | 本地无 AstrBot 源码；仓库内也没有该事实的记录 | 开发者，或按 **L3** 直接抬高下限 |
| 3 | AstrBot 元数据规范是否支持「许可」字段 | 同上 | 对照官方发布文档确认（**§4** 那一行） |
| 4 | 市场对全新安装的端到端行为（是否会自动装依赖、如何处理 `tests/` 与 `docs/`） | 需要真实提交一次 | 提交后复核，并回写 `CHANGELOG.md`（`release-checklist.md:56` 的既有要求） |
| 5 | 页面在真实面板里的呈现（含 `asset_token` 重写、外链头像取不到时的回退） | 同上 | 所有者打开页面看一眼 |

---

## 10. 发布前必须处理清单

**必须先做（阻断）**

1. **B1** —— 升版本（建议 `0.5.0`）+ 补 CHANGELOG 覆盖 skland、maa、班次默认值变更 + 重新打 tag。**依据**：`release-checklist.md:16-18`；且现状下同一个 `0.4.0` 指向两套不同产物（37 个提交的差距）。
2. **B2** —— 在真实面板上确认 `modules` 段渲染正常，**并且**确认能勾选 `skland` / `maa`。若渲染不出来，按 `docs/implementation/plan.md:85` 的既定方案改扁平键名（那需要单独的任务包）。

**建议发布前做完（成本都很低）**

3. **S1** —— README 补三个模块，**尤其要写明森空岛会做签到这个写操作**。
4. **S2** —— 改掉 `README.md:11` 那句已经不成立的承诺。
5. **S3** —— `metadata.yaml` 的 `desc` 与 `tags` 覆盖四个模块。
6. **S4** —— 删掉 `docs/guides/maa-remote-control.md:37` 的假保证。
7. **S5** —— README 加一句「也可以用插件页面」，并给页面上的上传与对齐按钮一个位置。
8. **S6** —— `/ak` 兜底文案改用面向用户的功能名，并区分「没开」与「没有」。
9. **S7** —— 决定公招在群聊里是否放开（纯策略选择，一行改动）。

**可以留到下一版**

10. **L1** 按钮名对齐、**L2** `/ak test` 失败补回执、**L3** 核实或抬高 `astrbot_version` 下限。

**明确不建议在发布前处理**

- **§5.3 的服务器指纹**：是开发文档的真实记录，不是凭据，改动会弄脏历史文档。
- **`docs/` 整体是否随插件发布**：属产品决策，且当前体积（0.59 MB）没有任何压力。

---

## 附：本报告的取证方式

- **静态核查**：读取 `AGENTS.md`、`README.md`、`metadata.yaml`、`_conf_schema.json`、`LICENSE`、`CHANGELOG.md`、`main.py`、`core/{permission,storage}.py`、四个模块的 `module.py`、`modules/maa/{relay,tasks}.py`、`modules/shift_reminder/{module,roster}.py`、`modules/skland/module.py`、`pages/shift-reminder/index.html`、`docs/implementation/{release-checklist,plan,implementation}.md`、`docs/guides/maa-remote-control.md`、`docs/project-plan/{00,01,02,03,08,09,10}-*.md`、两份 fixture、两份数据文件的 `_comment`。
- **命令核查**：`git ls-files` / `git status --ignored`（磁盘与库的一致性）、`git ls-tree -r v0.4.0` 与 `git rev-list --count v0.4.0..HEAD`（B1 的差异）、`git archive`（体积）、`git tag`、项目自己的发布门禁六条命令、`^(from|import) astrbot` 的纯逻辑扫描。
- **全仓搜索**：凭据形状（`abk_` / `sk-` / `Bearer <值>` / `api_key=` / `password` / `secret`）、IPv4、已知真实 QQ 号与主机名、`AGPL` / `GPL-3` / 「无 LICENSE」、二进制素材扩展名、`TODO` / `未实现` / `暂未`。
- **未做**：没有运行插件、没有连接服务器、没有修改任何文件（本文件是本次审查唯一的产物）。

---

## 11. 修复记录（R2，2026-09-29）

> 本节点在审查报告**之后**追加，用来标注哪些发现已经处理。上面的正文保持原样不动——它是一份**当时的取证记录**，改它会让「当时查到了什么」变得不可考。

| 编号 | 处理 | 说明 |
| --- | --- | --- |
| **B1** | ✅ 已修 | `metadata.yaml` 与 `CHANGELOG.md` 双双升到 **0.5.0**；CHANGELOG 补了一条覆盖森空岛、MAA、班次默认值与判据变更的完整条目。**tag 由总监在提交后打**（本包不许 `git tag`），建议 **`v0.5.0`** |
| **B2** | ✅ 代码侧已尽力 | `README.md` 新增「功能与开关」一节：四个模块的默认状态、**开关在面板的哪一项**、以及「发指令收到未知子命令多半就是没开」。**没有改 `_conf_schema.json` 的结构**（那要看真实渲染结果，属另一个决定）。另外补了一句出路：若面板里看不到「模块开关」这一项，欢迎报 issue——原先这种情况下用户**无路可走** |
| **S1** | ✅ 已修 | README 指令一览补全到 **14 个子命令**（本包重新数过：`bind/status/test/import` 4 个 + `recruit` 1 个 + `skland` 5 个 + `maa` 4 个），并给森空岛、MAA 各写了一节。**森空岛能代你签到**这一点写在开篇、功能表和专节三处 |
| **S2** | ✅ 已修 | `README.md:11` 那句已改成如实陈述：**提醒本身**不需要账号与 MAA；另外几个功能需要你主动授权，**且都默认关闭**。不再为了让人放心而含糊 |
| **S3** | ✅ 已修 | `metadata.yaml` 的 `short_desc`、`desc`、`tags` 覆盖四个模块。tags 补了公开招募、公招、森空岛、签到、MAA——**每个都对应一个真实功能或一种真实称呼**，没有堆砌 |
| **S4** | ✅ 已修，并**多查出两处同类** | `:37` 的「班次索引不动」改成有条件措辞。另外查出：**`:88` 说自签证书「尚未验证」，与 `:157` 的「已实测拒绝」自相矛盾**（同一份文档里两处结论相反）；**`:298` 说轮询间隔与 UA 未验证，而 `:312` 已经实测过**。三处都改到一致了 |
| **S5** | ✅ 已修 | README 新增「先用插件页面（推荐）」一节，把页面放在手动放文件之前，并点出「按排班表填入班次时长」那个按钮 |
| **S6** | ✅ 已修（一半） | `/ak` 兜底改为：能区分「**存在但没开启**」与「压根没有」，说清去配置页打开（给出 `modules.<名字>`），列出未开启的模块，并把死指针「见各模块文档」换成 README |
| **S7** | ✅ 已放宽 | `modules/recruit` 不再做会话门禁（理由写在函数 docstring 里）。**这是一处行为变更**，已在 CHANGELOG 记录 |

**S6 的一处如实交代**：审查建议「列出**面向用户的功能名**」。本包只做到一半——宿主能把「模块名」列出来，但**列不出功能的中文名**，因为模块契约里**没有显示名字段**，而契约是冻结的（改它要单独出影响评估）。所以现在的做法是：把名字**标注清楚它是什么**（「配置页里的开关名，与这里一致」），并把用户真正需要的出路指向 README。**要真正解决，需要给契约加一个显示名字段**——那是另一个决定。

**S7 的一处如实交代**：审查同时建议把 `/ak status` 也放开。本包**没有动它**——它属于 `modules/shift_reminder`，不在本包可写范围内。所以现在的状态是「公招在群里放开，提醒模块那四个命令仍然整组限管理员」，**这个不一致是已知的**，留给下一版决定。

**L1 / L2 / L3 按裁决留到下一版，未动**（L1 的按钮名不一致实际仍在：提示说「按排班表填入」，页面标签是「按排班表填入班次时长」）。

**本包未做**：没有 `git commit` / `git push` / `git tag`；没有改 `_conf_schema.json` 结构、`pages/**`、`modules/{shift_reminder,skland,maa}/**`（除 `recruit`）、`ruff.toml`、`docs/architecture/**`。

---

## 12. 全新安装实测发现（F8，2026-09-29）：空值会把模块打死

> 本节点同样在报告**之后**追加。它记的不是审查发现的，而是**一次全新安装**（代码、配置、用户数据全部清空）**跑出来的**——属于"只有真装一遍才会现形"的那类问题。

### 12.1 现场

全新安装后，宿主按 `_conf_schema.json` 生成的默认配置里：

```
modules        = {"shift_reminder": true, "recruit": true, "skland": false, "maa": false}   ✓
shift_1_name   = 第 1 班   shift_1_start = 08:00   shift_1_hours = 12   ✓
shift_2/3_*    = 第 2/3 班  20:00·6h / 02:00·6h                          ✓
timezone       = Asia/Shanghai     lead_minutes = 10                      ✓
maa.task_type  = ''        ← 空串（schema 里写的是 "LinkStart"）
```

后果**已经发生过一次**：`maa` 模块启动失败，日志为

```
TaskTypeError: 任务类型配置不合法：''；当前可用：LinkStart、LinkStart-Base
```

**而提示在怪用户填错，他什么都没填。** 这正是下一个打开 `maa` 开关的用户会原样撞上的东西。

### 12.2 真因（比"一处写漏"更值得记）

**宿主是按 `items` 逐项生成配置的。** `maa` 段里：

- `task_ttl_minutes` / `fetched_ttl_minutes` 的 **`items` 级**有 `default` → 生成后正常；
- `task_type` 的 **`items` 级漏写了 `default`**，段级 `default` 里的 `"LinkStart"` **不生效** → 生成后是空串。

⇒ 这是一个**模式**：**任何 `items` 条目漏写 `default`，那一项在全新安装后都会是空串。**
所以本包做了两层护栏（见 12.5），而不是只补一行。

### 12.3 规则与边界

**「用户没动过这一项」与「用户明确填了空」在配置里长得一模一样**，而这两者的正确行为都是**用默认值**——一个空的任务类型、空的提前量都没有意义。

**但空 ≠ 填错。** 边界钉死在两处：

| 情形 | 行为 |
| --- | --- |
| 键缺失 / `None` / 空串 / 只有空白 | **回退默认值**，并记一条 WARN（不静默） |
| 类型不对（`"30"`）、越界（`0`、`-5`）、不在白名单（`"NotATask"`） | **当场报错，模块不启动** |

**放宽第二条就是放宽整个校验**——那是本项目最怕的改法。所以行为层的改动只针对"空"，校验一行没动。

规则的**唯一定义**在新建的 `core/config.py`（`is_unset` / `setting` / `unset_keys`），纯逻辑、叶子、只用标准库。

### 12.4 逐个字段的排查清单

| 模块 | 字段 | 空值原本会怎样 | 处理 |
| --- | --- | --- | --- |
| `maa` | `task_type` | **抛 `TaskTypeError`，模块起不来** | ✅ 回退 + WARN |
| `maa` | `task_ttl_minutes` | **抛 `ValueError`，模块起不来** | ✅ 回退 + WARN |
| `maa` | `fetched_ttl_minutes` | 同上 | ✅ 回退 + WARN |
| `shift_reminder` | `lead_minutes` | **抛 `ConfigError`，模块起不来** | ✅ 回退 + WARN |
| `shift_reminder` | `timezone` | 已经回退（`parse_timezone` 早有这条） | ✔ 原样，未动 |
| `shift_reminder` | `shift_N_name/start/hours` | 抛 `ConfigError` | ⚠️ **刻意保持严格**，理由见下 |
| `recruit` | `max_operators` / `max_combinations` | 已经 WARN + 回退（`read_limit` 早有这条） | ✔ 原样，未动 |
| `skland` | `signin_enabled` | `bool("")` 为 `False`，与默认值相同 | ✔ 语义上无差别，未动 |
| `skland` | `signin_cron` | 已经回退（`_signin_cron` 早有这条） | ✔ 原样，未动 |
| `main.py` | `modules` 开关段 | 缺失即"一个模块都不开"，是**有意义的取值** | ✔ 不动（空开关不是配置错误） |

### 12.5 刻意保留的例外：班次槽位

`shift_N_name/start/hours` 的空值**仍然报错**。这不是偷懒，理由是它们**互相依赖**：

三班必须**合计 24 小时且首尾相接**。静默补一个默认值会得到一张**合法但不是用户想要**的表——提醒会在**错误的时刻**响，而且**不会报错**。那正是本项目最怕的"静默错位"（与 `10-maa-shift-switching.md` 里"触发一次就前进一班"同一族）。

而且它们**不需要**回退：每个槽位字段在 schema 里都有 `items` 级默认值，全新安装不会产生空串——**这一点由 12.5 的模式护栏保证**。

于是判据是：**字段独立、给错默认值最多"不如意" ⇒ 空即默认**；**字段互相依赖、给错默认值会静默错位 ⇒ 空即报错**。

**⚠️ 这条是本包最值得被推翻的判断**，如果上级认为一致性优先于这条风险，改动很小（在 `parse_shift_slots` 里加回退即可），但那要连 `core/shifts.py` 的叶子约束一起处理。

### 12.6 验证

六条门禁全过：`verify_constitution` 0 ／ `check_astrbot_load_form` 0（21 个纯逻辑文件）／ `check_room_colours` 0 ／ `ruff check .` 全过 ／ `ruff format --check .` 79 个文件 ／ `pytest -q` **1102 passed**（F8 前 1058；本包新增 45 条，另移出 1 条把空串当非法值的参数 ⇒ 净 +44）。

**可证伪验证（三处注入 → 红 → 逐字节还原 → 绿）**，脚本在仓库外、全程字节操作：

| 注入 | 打的是哪一层 | 结果 |
| --- | --- | --- |
| `is_unset` 对空白永远返回 `False` | **行为** | 11 failed ✅ 红 |
| `maa` 的读取点不再调用 `setting`（保留 helper 实现） | **接线** | 4 failed ✅ 红 |
| 撤掉 schema 里 `maa.task_type` 的 `default` | **根因** | 2 failed ✅ 红 |

还原全部 `sha256` 一致、复绿。**第二个探针是特意加的**：只测零件不测接线时护栏会假绿（M5 的教训）。

### 12.7 动了什么

- **新增** `core/config.py`（通用规则，纯逻辑叶子）、`tests/test_config_defaults.py`（45 条）。
- **改** `modules/maa/module.py`（读取点 + `initialize` docstring 改成与代码一致）、`modules/shift_reminder/module.py`（`parse_lead_minutes` + 导入链）。
- **改** `_conf_schema.json`：给 `maa.items.task_type` **补上漏写的 `default`**（根因）。
- **改** `tests/test_maa_module.py`：从"非法值"清单里**移出 `{"task_type": ""}`**，并在 docstring 里写明**这是有意的重新分类，不是为了让测试变绿而放宽**；其余五条真填错的值一条没动。

**未做**：没有 `git commit` / `git push` / `git tag`；没有改 `main.py`、`pages/**`、`ruff.toml`、`docs/architecture/**`、`core/shifts.py`。


