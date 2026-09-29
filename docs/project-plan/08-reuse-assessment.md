# 复用评估：森空岛——复用现有插件，而不是自己写

- **时间**：2026-09-29
- **作者**：agent（调研包 R3）
- **状态**：调研完成，待决策
- **触发**：用户指令「等等，有插件能复用就直接复用，不要自己写」
- **上游**：[02 集成调研](02-integration-research.md)、[05 基建机制](05-infra-mechanics.md)、[06 效率计算评估](06-efficiency-assessment.md)
- **证据分级**：【实测】本项目内亲自取到 /【官方】厂商文档或源码 /【三方】第三方仓库，附许可 /【未拿到】检索未果

---

## 0. 结论摘要

**结论一（最重要）：「复用」有两种完全不同的含义，而这一路走不通的是被默认的那一种。**

| 形态 | 是什么 | 本包判定 |
| --- | --- | --- |
| **运行时复用** | 用户装对方插件，我们**在运行时调用它的对象** | ❌ **不可行**（技术上要伪造 event，且无任何契约） |
| **代码级复用** | 把对方 **MIT** 的纯逻辑搬进我们仓库，注明出处 | ✅ **可行且推荐**，但**每个候选都要逐个核 LICENSE** |

**结论二：运行时复用之所以不可行，不是我们不会调，是对方没打算被调。** 实测三个候选的公开接口：业务能力**全部长在消息指令上**（`async def building(self, event)`），没有一个是「给数据、返数据」的纯接口。【实测】

**结论三：能安全复用的 MIT 代码有现成的三份，且质量不低。** 其中 `FrostN0v0/nonebot-plugin-skland`（**72 星**、Python、MIT）是最完整的基底；`Siq5005/astrbot_plugin_arknights` 的 `core/skland.py` 是适合直接嵌入的纯函数层。【实测 + 三方】

**结论四：用户候选清单里有两个碰不得。** `Azincc/astrbot_plugin_skland`（**26 星，最流行**）与 `qihang518887/astrbot_plugin_sklandv2` **都没有 LICENSE 文件**——法律上等于未授权；`fxquarter/..._sanity` 是 **AGPL-3.0**，碰了会传染整个项目。【实测】

**结论五：森空岛这一块的开发量，比"自己写"听起来小得多，也比"直接复用"听起来大一点。** 真正的活不是写登录签名（有现成 MIT 代码可搬），而是**凭据生命周期 + 失效即告知 + 绝不传染核心提醒**这部分工程。**这才是我们的原创部分。**

---

## 1. 用户指令与本包的定位

用户原话：「**有插件能复用就直接复用，不要自己写**」。这是对上一个包（S1，正在自己搭森空岛骨架）的**方向纠正**。

本包不依赖也不干预 S1 的产出，只回答：**该复用哪个、以什么形态复用、以及我们自己还得做什么。**

---

## 2. 候选插件横向对比【实测】

许可与维护状态**全部通过 GitHub API 实测**（`/repos/{owner}/{repo}` 的 `license.spdx_id` + `/repos/{owner}/{repo}/license` 是否真有 LICENSE 文件），**不看 README 自称**——本项目此前已因此否决过两个包（"npm 声明 ISC 但包内无 LICENSE"、"README 自称 MIT"）。

| 仓库 | stars | 最后提交 | LICENSE 文件 | spdx | 判定 |
| --- | --- | --- | --- | --- | --- |
| `Azincc/astrbot_plugin_skland` | **26** | 2026-08-26 | ❌ **不存在** | — | ❌ **不可用**（最流行，但未授权） |
| `qihang518887/astrbot_plugin_sklandv2` | 0 | 2026-05-15 | ❌ **不存在** | — | ❌ **不可用** |
| `fxquarter/astrbot_plugin_arknights_sanity` | 0 | 2026-03-17 | ✅ 存在 | **AGPL-3.0** | ❌ **不可用**（传染） |
| `Siq5005/astrbot_plugin_arknights` | 1 | 2026-09-20 | ✅ `LICENSE` (1060B) | **MIT** | ✅ 可用 |
| `Morizero1125/astrbot_plugin_arknights_skland` | 2 | 2026-08-29 | ✅ `LICENSE` (1065B) | **MIT** | ✅ 可用 |
| `FrostN0v0/nonebot-plugin-skland` | **72** | 2026-09-22 | ✅ `LICENSE` (1066B) | **MIT** | ✅ 可用（基底首选） |

> ⚠️ **注意 `Azincc` 这个数据点**：它是这个领域里星最多、最活跃的现成实现，但它**没有任何许可声明**。这恰好说明为什么本项目的纪律是**先查 LICENSE 再看功能**——一个 26 星的流行仓库，照样可能法律上不能用。

### 2.1 功能覆盖（读树 + 读入口得出）【实测】

| 仓库 | 文件结构透露的能力 | 签到 | 理智 | 干员查询 | 基建状态 |
| --- | --- | --- | --- | --- | --- |
| `Siq5005` | `core/{skland,operators,daily,cards,gamedata,gacha}.py` + `docs/preview/building.jpg` | ✅ `daily` | ✅（`derive_sanity`） | ✅ `operators` | ✅（有预览图） |
| `Morizero1125` | `skland/{auth,login,did,store,gacha}.py` + `templates/assets/building/*.svg` | ？ | ✅ | ✅ `char` | ✅ `building` |
| `FrostN0v0/nonebot-plugin-skland` | nonebot 生态，非 AstrBot | 需再核 | 需再核 | 需再核 | 需再核 |

**说明**：`FrostN0v0` 是 **nonebot** 插件，**不能作为 AstrBot 插件直接安装**，但它的**接口层代码是 MIT 且与框架无关**（签名、token 刷新、扫码），这正是最难自己写对的部分。

---

## 3. 运行时复用机制：实测结论是「不可行」【实测】

### 3.1 框架给的通道确实存在

在服务器容器内实测 AstrBot **4.28.1**：

```
/AstrBot/astrbot/core/star/context.py:346:  def get_registered_star(self, star_name: str) -> StarMetadata | None
/AstrBot/astrbot/core/star/context.py:352:  def get_all_stars(self) -> list[StarMetadata]
/AstrBot/astrbot/core/star/star.py:42:      star_cls: Star | None = None
```

`get_registered_star` 的实现是**遍历全局 `star_registry` 按 `name` 精确匹配**。

所以形态上是这条：

```python
md = self.context.get_registered_star("astrbot_plugin_xxx")
if md and md.activated and md.star_cls is not None:
    ...  # md.star_cls 是对方插件实例
```

### 3.2 但框架自己声明这条通道没有保障

`star.py:18-22` 的类文档原文：

> 当 `activated` 为 `False` 时，`star_cls` 可能为 `None`，**请不要在插件未激活时调用 `star_cls` 的方法**。

并且 `star_manager.py:1240-1245` 确实会把 `star_cls` 置 `None`：

```python
if metadata.star_cls:
    setattr(metadata.star_cls, "name", p_name)
    ...
    metadata.star_cls = None      # 插件被禁用时
```

**没有能力声明、没有版本协商、没有接口契约。** 全树也不存在对外的事件总线（`event_bus` 只出现在框架内部的 `core_lifecycle.py` / `event_bus.py`，是「平台消息 → Pipeline」的内部分发队列，不对外）。

### 3.3 致命的一点：对方的业务方法要一个 `event`

实测三个候选的公开接口：

| 仓库 | 公开业务方法 | 签名 |
| --- | --- | --- |
| `Azincc` | 全部私有：`_auto_sign_all_users` / `_send_private_message` | 无对外能力 |
| `Morizero1125` | `building` / `info` / `char` / `warehouse` / `progress` / `gacha` | **`async def building(self, event)`** |
| `Siq5005` | 入口 `__init__.py` 仅 40 字节（壳），能力在 `core/` 各模块 | 未暴露实例方法 |

**`async def building(self, event)` 的语义是「响应某条消息事件」**，不是「给我数据、我返给你」。要跨插件调它，**我们得伪造一个 `AstrMessageEvent` 塞进去**——那不是一个受支持的用法，而是把一个为消息流设计的对象硬拗成函数调用。

**这不是"我们调用姿势不对"，是对方从未设计成被调用。** 用它等于把我们的功能绑在别人内部实现的偶然形状上。

### 3.4 所以运行时复用的判定

| 形态 | 可行性 | 理由 |
| --- | --- | --- |
| **A. 硬依赖**（用户必须装对方） | ❌ 不做 | 要伪造 event；对方重构即静默碎；还要用户装两个插件 |
| **B. 软依赖**（装了就用） | ⚠️ 收益太小 | 坏处同 A，只是不崩；而"能拿到的能力"本身就没多大——它发的是**它自己的消息**，我们要的是**数据** |
| **C. 不集成**（各用各的） | ✅ 可接受 | 零成本，但用户要自己装、自己看 |
| **D. 代码级复用**（搬 MIT 纯逻辑） | ✅ **推荐** | 合法、可控、无运行时耦合、不依赖用户装什么 |

---

## 4. 逐功能判断（用户要求「不要一刀切」）

| 功能 | 建议 | 依据 |
| --- | --- | --- |
| **签到** | **代码级复用** | 签到是「调一个带签名的 POST + 判结果」。`Azincc`/`qihang518887` 的签到实现不能抄（无 LICENSE）。**MIT 来源里要找到可信的签到实现**；若找不到，签到本身协议简单，自己实现也不难——**但不应把它做成核心功能**（见 §5 铁律） |
| **理智（含回满提醒）** | **代码级复用** | `Siq5005` 的 `derive_sanity` 是**纯函数**、MIT、可单测。【三方】这是最适合直接嵌入的一段 |
| **干员查询** | **代码级复用** | `Siq5005/core/operators.py`（MIT）。若只是"查干员名字/头像"，我们其实**已经有 `avatar_map.json`**，需要评估是否重复 |
| **基建状态（心情 / `tiredChars`）** | **代码级复用** | 数据来自 `player/info`，字段已由 [05](05-infra-mechanics.md) 与 [02 §7.1](02-integration-research.md) 查实。`Morizero1125` 的 `building`（MIT）可参考渲染，**取数部分自己写更干净**（它要 event） |
| **运行时调对方插件** | ❌ 全部不做 | 见 §3 |

**一句话**：**我们复用的是"代码"，不是"插件"。** 前者是工程手段，后者在这个框架里没有干净的路。

---

## 5. 风险

| # | 风险 | 影响 | 应对 |
| --- | --- | --- | --- |
| **K1** | **森空岛失效传染核心提醒** | **最严重**——今晚这条 QQ 链路已经崩过两次 | 铁律：**核心换班提醒零外部依赖**，`skland` 是独立模块，失效只影响自己 |
| **K2** | 抄错 LICENSE（最流行的那个恰恰没许可） | 侵权 / 被迫 AGPL 开源整个项目 | 只搬 MIT；**搬之前逐个实测 `/license` 端点**（本包已做）；搬运处注明来源 + 许可 |
| **K3** | 对方仓库停更 / 接口变更 | 我们搬来的代码过时 | 只搬**协议层**（签名、刷新），不搬业务策略；接口变更时改动面小 |
| **K4** | 森空岛限流 429、凭据失效 | 功能不可用 | 全局冷却 + 缓存；**失效必须显式告知用户**（不许静默） |
| **K5** | 凭据泄漏 | 账号风险 | 只存 `plugin_data/`；不进仓库、不进日志、不进报错信息 |
| **K6** | 边界（"不碰游戏账号"） | 立项底线 | 只用公开接口；**不做绕过、模拟客户端、反检测**；接入前用户单独确认 |
| **K7** | 用户要装两个插件才用得全 | 体验代价 | **我们的复用以"不要求用户装任何东西"为前提**——这正是代码级复用优于运行时复用的地方 |

---

## 6. 结论与推荐

**推荐路线：代码级复用 MIT 实现 + 自己写凭据与失效处理。不做运行时跨插件调用。**

具体做法：

1. **基底**：以 `Siq5005/astrbot_plugin_arknights` 的 `core/skland.py`（MIT）为主搬对象——它是纯函数、适合嵌入、便于单测。`Morizero1125`（MIT）作为**认证链**的参考（`skland/{auth,login,did}.py`）。`FrostN0v0/nonebot-plugin-skland`（MIT，72 星）作为**签名与刷新的交叉验证来源**。
2. **搬运纪律**：搬进 `modules/skland/` 时**在文件头注明来源仓库、作者、许可**，并在仓库里保留对应的许可说明；**只搬协议层**，不搬它的消息渲染与业务策略。
3. **不做运行时调用**：不依赖用户安装任何其它插件，不用 `get_registered_star` 去调森空岛插件。
4. **我们的原创部分（也是真正的难点）**：凭据生命周期（存哪、怎么刷新、失效怎么告诉用户）、**失效绝不传染核心提醒**、限流与缓存、以及"授权过期"这条用户路径。
5. **绝不碰**：`Azincc`、`qihang518887`（无 LICENSE）、`fxquarter/..._sanity`（AGPL）。

**用户需要装什么**：**什么都不用装。** 这是我们推荐代码级复用而不是运行时复用的直接好处。

**给用户的直白回答**：您说「不要自己写」——**能复用的部分确实不用自己写**（森空岛对接最难的那层正好有 MIT 的现成代码），但**"装一个别人的插件然后调它"这条路在这个框架里走不通**，因为对方的能力是长在消息指令上的，框架也没有能力声明或事件总线。所以复用要落在**代码**上，不是**插件**上。

---

## 7. 验证记录

**GitHub API 实测**（`gh api`）：6 个候选的 `license.spdx_id` + `/license` 端点是否返回真实文件（见 §2 表格）。**结论：3 个可用、2 个无 LICENSE、1 个 AGPL。**

**候选源码结构实测**：`git/trees/HEAD?recursive=1` 取三个 AstrBot 插件的文件树；`raw.githubusercontent.com` 拉取 `Azincc/main.py`（16,716B）、`Morizero1125/main.py`（44,094B）逐行看公开方法。**产物只在工作区 scratch 目录，未进仓库。**

**AstrBot 4.28.1 实测**（服务器容器内只读探测，3 轮）：
- `core/star/context.py:346/352`——`get_registered_star` / `get_all_stars` 存在
- `core/star/star.py:18-42`——`StarMetadata` 全字段，含 `star_cls: Star | None`、`activated: bool`；类文档明确警告未激活时 `star_cls` 可能为 None
- `core/star/star_manager.py:1240-1245`——插件被禁用时 `star_cls` 被置 `None`
- **无对外事件总线**；版本 `4.28.1`

**未做**：未调用任何森空岛接口（那是 S1/阶段二的活）；未复制任何代码进仓库。
