# 森空岛（Skland）接口调研 · 2026-09-29

- **本文件用途**：为 `modules/skland` 的阶段二（扫码授权 + 取数）提供**实测过的**接口事实。
- **编写原则**：**不靠文档**。每条结论都标注它来自「**实测**」（本机真实请求）还是「**读码**」（读 MIT 参考实现）还是「**未验证**」。
- **上游**：[02-integration-research.md](02-integration-research.md) §7（早前的桌面调研）。**本文件修正了它若干条**，见 §7。

> ⚠️ 一句话结论：**社区文档里的扫码链路已经全面过时**——`gen_scan/login` 只认 POST、状态与换 token 换了路径、凭据刷新路径也搬了家。照文档写必然 404，而且 404 长得像「接口被删」，极易误判。

---

## 1. 认证链路（实测打通到「拿到 cred 之前」）

当前真实链路，**每一步都实测过**：

| # | 步骤 | 请求 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 取二维码 | `POST https://as.hypergryph.com/general/v1/gen_scan/login`，body `{"appCode":"4ca99fa6b56cc2ba"}` | **200**，`data.scanId` + `data.scanUrl`（`hypergryph://scan_login?scanId=…`）+ `data.enableScanAppList` |
| 2 | 轮询扫码状态 | `GET https://as.hypergryph.com/general/v1/scan_status?scanId=<id>` | **200**，`{"msg":"未扫码","status":100,"type":"A"}` |
| 3 | 换取通行证 token | `POST https://as.hypergryph.com/user/auth/v1/token_by_scan_code`，body `{"scanCode":<base64>}` | **400**，验证器报 `TokenByScanCodeReq.scanCode` 必须是 **base64** → 端点存在、字段名与类型已确认 |
| 4 | 换森空岛认证码 | `POST https://as.hypergryph.com/user/oauth2/v2/grant`，body `{"appCode":<code>,"token":<token>,"type":0}` | **401** `{"msg":"登录已过期，请重新登录","status":3,"type":"A"}`（用的是格式合法但已失效的 token） |
| 5 | 换凭据 | `POST https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code`，body `{"code":<code>,"kind":1}` | 缺 `kind` → **400** `code=10001 参数错误`；字段齐全但值非法 → **500** `code=10001 服务器开小差` |

**两条必须记住的实现坑**：

1. **失败以 `status`/`code` 字段表达，不靠 HTTP 状态码**。第 4 步失败是 HTTP 401 + `status:3`；第 5 步失败是 **HTTP 500**（不是 4xx！）+ `code:10001`。**把 5xx 当「服务器故障、重试」会让客户端无限重试一个永久性错误。**
2. **`appCode` 有三个，别用错**（实测来自第 1 步响应的 `enableScanAppList`）：

   | 应用 | appCode | 来源 |
   | --- | --- | --- |
   | 明日方舟 | `7318def77669979d` | 实测 |
   | **森空岛** | **`4ca99fa6b56cc2ba`** | 实测 |
   | 明日方舟：终末地 | `dd7b852d5f1dd9da` | 实测 |
   | 官网通行证（另一条路） | `be36d44aa36bfb5b` | 读码（参考实现 `web_app_code`） |

   我们要的是**森空岛**的 `4ca99fa6b56cc2ba`。

---

## 2. 过时路径对照（照文档写就会踩的坑）

| 社区文档写的 | 实测结果 | 真实路径 |
| --- | --- | --- |
| `GET /general/v1/gen_scan/login` | **404 page not found** | `POST` 同路径（**GET 不存在**） |
| `GET /general/v1/gen_scan/login_status` | **404 page not found** | `GET /general/v1/scan_status` |
| `GET /general/v1/gen_scan/get_token` | 400 `scan type not support` | `POST /user/auth/v1/token_by_scan_code` |
| `POST /api/v1/user/auth/get_session` | **404 page not found** | 未找到（见下方说明） |
| `POST /api/v1/user/auth/refresh` | **404 page not found** | `GET /api/v1/auth/refresh`（**少了 `user/`**） |

**「404 page not found」= 路由不存在**（纯文本）；返回 JSON `{"code":…}` 才是路由存在。这条判据是本文件所有「存在/不存在」结论的依据。

> ⚠️ **一个把我自己骗过的假判据**：`OPTIONS` 对 `/api/v1/` 下**任意**路径都返回 200——包括 `OPTIONS /api/v1/definitely/not/a/route`。所以 **OPTIONS 不能用来判断路由是否存在**。我一开始用它得出「8 个路由都在」，那条结论已作废。

关于 `get_session`：**它在当前路径上不存在**。参考实现里也确实没有这个方法（它用的是 `auth/refresh`）。不排除它搬到别处，但**我没有找到**，标为【未找到】。

---

## 3. 签名机制（读码重建，**未端到端验证**）

Skland 的 `zonai.skland.com/api/v1` 接口需要签名头。算法（自 MIT 参考实现重建，**用标准库即可实现**）：

```
ts     = int(time.time()) - 1                      # 故意减 1，规避时钟偏差
hdr    = {"platform": "", "timestamp": str(ts), "dId": "", "vName": ""}
secret = f"{path}{query_or_body}{ts}{json.dumps(hdr, separators=(",", ":"))}"
sign   = md5(hmac_sha256(key=token, msg=secret).hexdigest()).hexdigest()
headers = {"cred": cred, "sign": sign, "platform": "", "timestamp": str(ts), "dId": "", "vName": ""}
```

要点：

- **GET 签的是原始 query 串**（`urlparse(url).query`，不含 `?`）；**POST 签的是请求体那段 JSON 文本**——即签名与**实际发出的字节**必须一致。
- `platform` / `dId` / `vName` 默认**空字符串**（不是省略）。
- `dId`（设备指纹）走 `https://fp-it.portal101.cn/deviceprofile/v4`，带一段嵌死的数据块。**参考实现里没有任何调用点传 `use_did=True`** → **我们不实现它**，这同时让我们离「模拟客户端」更远。

**⚠️ 未验证的部分（阶段二的第一风险）**：我**没能端到端证明这个签名是对的**。

我设计的验证实验是「用假凭据 + 正确签名访问 `player/info`，若错误码从 `10001 参数错误` 变成 `10002 用户未登录`，则签名结构正确」。**实验失败，原因不是签名错，而是设计有缺陷**：实测发现不带任何签名头时，**只要带了 `uid` 参数**，服务端就直接回 `10002 用户未登录`——也就是说**服务端在对未登录请求上根本不检查签名**，两种情况的错误码无法区分。

真正验证签名需要**一次真实授权**。这条必须留给阶段二，且阶段二第一件事就该是它。

---

## 4. 错误码语义（实测 + 读码一致）

| code | 含义 | 实测来源 |
| --- | --- | --- |
| `0` | 成功 | `auth/refresh` 的 200 响应 |
| `10000` | 凭据无效／未授权 | 读码（参考实现据此抛 `UnauthorizedException`） |
| `10001` | 参数错误 | `GET /game/player/info`（缺 `uid`）→ HTTP 400；`generate_cred_by_code`（坏 code）→ HTTP 500 |
| `10002` | 用户未登录 | `player/info?uid=0`、`player/binding`、`user/teenager`、`user/me`、`game/attendance` → 全部 HTTP 401 |

**HTTP 状态码与 code 无稳定对应关系**（同一个 10001 出现过 400 与 500）。**判据一律用 `code`。**

**校验顺序（实测推出）**：`player/info` 缺 `uid` → 10001；带 `uid` 但无凭据 → 10002。即**先参数、后登录**。

---

## 5. ⚠️ 一个反直觉的实测发现：`auth/refresh` 对无效凭据也返回成功

```
GET https://zonai.skland.com/api/v1/auth/refresh
  header: cred: junk-cred
→ 200 {"code":0,"message":"OK","data":{"token":"18a9d873337cf018b6bcd5ccbc815454"}}
```

**用一个明显无效的凭据，它返回了 `code:0` 和一个 token。** 换 App 的 UA 与我们的 UA 都是同一结果、同一个 token 值。

**含义（对本模块的设计是决定性的）**：**「刷新成功」不能当作「凭据有效」的证据。** 若阶段二拿 `auth/refresh` 做健康检查／授权状态判断，就会得到「一切正常」然后每个取数接口都回 10002——正是本项目最怕的「看起来成功但什么都没发生」。

**正确做法**：健康检查要打**真实取数接口**（例如 `GET /game/player/binding`），以 `code == 0` 为准。

---

## 6. 边界与合规（本项目铁律的实测支撑）

### 6.1 User-Agent 无关 → **我们不必模拟官方客户端**

我们的铁律写着「不做任何绕过、模拟客户端、反检测的行为」。社区实现全都伪造 `Skland/1.32.1 (com.hypergryph.skland; …) Okhttp/4.11.0`，所以这里必须实测。

**实测：五种 UA 结果完全一致**——空 UA、`curl/8.0`、`python-urllib/3.12`、我们自己的 UA、App 的 UA，在 `gen_scan/login` 上都返回 200 与同样的响应结构；在 `player/binding` 上都返回 401/10002；在 `auth/refresh` 上都返回同一个 token。

→ **结论：UA 不是通关门。我们用自己诚实的 UA，边界守得住。**

### 6.2 我们明确**不做**的

- **不实现 `dId` 设备指纹**（`fp-it.portal101.cn`）：它需要提交一段嵌死的指纹数据块去伪造设备身份，且参考实现里无人使用。
- **不做签到以外的任何写操作**，不模拟游戏客户端，不做反检测。
- 只用官方接口；签到是官方提供的正常操作（见 `implementation.md` §2.7 铁律第 4 条）。

### 6.3 未能确认的风险

- **频率限制**：早前调研称触顶返回 429、建议冷却 60s。**本次约 60 次探测请求未触发任何 429**，所以这条**未验证**——不能因为没触发就认为不存在。阶段二仍应按「按 uid 缓存 ≥120s + 全局冷却」设计。
- **封号风险**：官方无公开表态。**未验证**，风险自负（`02-integration-research.md` §9 R8 已记录）。

---

## 7. 对早前调研的修正

| 早前记录（`02-integration-research.md`） | 本次实测 |
| --- | --- |
| §7.2「认证链为 `通行证 token → oauth grant → cred/token → player/info`，推荐扫码」 | ✅ 方向对，但**路径细节全面过时**（见 §2） |
| §7.2「`token` 约每 20 分钟刷新，`cred` 固定保存 7 天后要重新授权」 | ⚠️ **未验证**。且 §5 的发现让「刷新」的语义更可疑：拿 token 不代表凭据有效 |
| §7.3 表格称 `AEtherside/skland-kit` 是 **MIT** | ❌ **错**。GitHub API 查得该仓库 `license` 为 **null（无 LICENSE 文件）** → 按本项目红线**不可复制** |
| §4.2 表格称 `ProbiusOfficial/Skland_API` 是 MIT 字段文档 | ✅ 许可对，但**该仓库已归档**（最后推送 2024-05）——**过时路径的源头就是它** |
| §7.3 首选基底 `FrostN0v0/nonebot-plugin-skland` | ✅ **MIT 复核通过**（GitHub API `license.spdx_id = MIT`），72★，2026-09-22 仍在更新 → **确认为首选参考** |

**许可证复核结果（2026-09-29，GitHub API `license` 字段）**：

| 仓库 | 许可 | 可用性 |
| --- | --- | --- |
| `FrostN0v0/nonebot-plugin-skland` | **MIT** | ✅ 首选参考 |
| `Siq5005/astrbot_plugin_arknights` | **MIT** | ✅ |
| `Morizero1125/astrbot_plugin_arknights_skland` | **MIT** | ✅ |
| `ProbiusOfficial/Skland_API` | MIT（**已归档**） | ⚠️ 路径过时 |
| `AEtherside/skland-kit` | **无 LICENSE** | ❌ 不可复制 |

> **复用策略**：即使对方是 MIT，本项目也**优先自己实现**——签名算法与链路已经实测清楚，用标准库就能写，而复制会引入对方的重依赖（`httpx` + `nonebot`）与本项目「运行时零第三方依赖」冲突。

---

## 8. 阶段二的落地清单（本文件的下游）

1. **第一件事**：用真实授权验证 §3 的签名算法（唯一未验证的关键项）。
2. 用标准库实现签名（`hmac` / `hashlib` / `json` / `urllib`），不引入 `httpx`。
3. 健康检查**必须打真实取数接口**（§5），不能用 `auth/refresh`。
4. 错误判据一律看 `code`，**不看 HTTP 状态**（§4）；`10000` → 提示重新授权，`10002` → 提示登录失效。
5. 请求带**自己的** UA（§6.1）。
6. 按 uid 缓存 ≥120s + 全局冷却（§6.3，未验证但按保守设计）。
7. 凭据落 `plugin_data/`，**不进日志、不进仓库**；展示时一律脱敏。

---

## 9. 阶段二实施记录（2026-09-29）

### 9.1 本包做完的

| 交付 | 位置 | 说明 |
| --- | --- | --- |
| 二维码编码 + PNG 输出 | `modules/skland/qr.py` | **纯标准库**（`zlib` + `struct`），字节模式，版本 1–10，四级纠错 |
| 请求层（签名 / 错误码 / 限流） | `modules/skland/api.py` | stdlib `urllib`；运输层可注入，故全部错误路径都能单测 |
| 扫码流程解析 | `modules/skland/login.py` | 纯逻辑：五步的响应解析与状态判定 |
| 装配（指令 + 定时签到） | `modules/skland/module.py` | `/ak skland [status\|login\|logout\|check\|signin]` |

### 9.2 为什么二维码是自己写的（不是选型偏好，是被逼的）

**`scanUrl` 是 `hypergryph://scan_login?scanId=…`，不是 http(s) 链接**，所以：

- 不能直接交给「按 URL 取图」的接口（那不是合法 http URL）；
- 也不能丢给在线二维码服务——那会把**登录用的 scanId 泄露给第三方**；
- 引 QR 库则与「运行时零第三方依赖」冲突。

所以自己实现。**正确性不靠自己声明**：开发期与参考实现 `qrcode` 做了**强制同掩码、含格式信息**的逐位比对，
**61 载荷 × 4 等级 × 8 掩码 = 1848 项检查，0 处不符**；PNG 侧用 Pillow 逐像素比对。
比对脚本是开发期工具、未进仓库，仓库里留的是**冻结金标**（防回归）+ 上面这次比对的结论。

比对抓到过两个真 bug，记下来给后来的人：

1. **格式信息那 15 位的接线写成了转置**（行/列 8 弄反）——矩阵看起来「大体像」，
   只有格式位错，扫码器直接读不出纠错等级。
2. **PNG 灰度极性写反**：1 位灰度里采样值 `1` 是**白**、`0` 是**黑**，
   按直觉「深色 = 1」写会得到**整张反色**的图。

### 9.3 新增的两条实测/设计约束

- **限流冷却必须按端点记，不能全局。** 扫码授权是**连着三步**的顺序调用，
  全局冷却会把它们互相卡死——这是个会让登录永远失败的 bug，被单测抓出来。
- **签名测试锁的是字节序，不是服务端认可。** `sign_headers` 有金标（`path + query/body + ts + json(hdr)` 的
  拼接顺序），但那**只能证明拼接没变**；服务端认不认它，只有一次真实授权能证——
  这正是 `/ak skland check` 的意义：签名错了会回 10001/10000 而不是 10002。

### 9.4 本包**刻意没做**的（以及为什么）

**干员疲劳 / 基建状态 / 干员查询一律未实现。**

理由不是时间不够，而是**这几条路由连存在性都没实测过**——§1 的五步链路是逐条打过的，
而基建/干员那几条没有任何请求记录。照文档猜路径正是本项目栽过的坑
（`FormData` 那条「照文档抄的 API 事实」在真实环境里根本递不过去）。
**宁可少做一个功能，也不要照文档写一个跑不通的。**

`api.sign_in()` 与 `api.player_info()` 是**唯二有实测记录**的业务端点（无凭据时分别返回
401/10002、缺 `uid` 时返回 10001），所以它们实现了，但成功响应体的字段名在代码里标了
`UNVERIFIED`，并且按**多个候选键**读取、取不到就抛——**不返回空值假装成功**。

### 9.5 仍未验证的（等真机授权）

1. **签名是否被服务端接受**（唯一的关键项）。
2. `attendance` 的成功响应体结构、以及它的请求方法（按 POST 实现）。
3. `player/info` 的成功响应体字段。
4. 凭据有效期（早前「约 20 分钟 / 7 天」的说法本次**未能证实**）与频率限制
   （约 60 次探测未触发 429，但没触发 ≠ 不存在）。
