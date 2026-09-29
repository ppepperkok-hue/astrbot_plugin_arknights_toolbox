"""森空岛模块的装配层（阶段二：扫码授权 + 签到）。

## 本模块的边界（用户裁决 + 项目铁律）

- **核心换班提醒绝不许被本模块拖累**：本模块与 `shift_reminder` 之间**零依赖**。
  本模块整体失效（凭据过期、接口不通、森空岛倒闭）时，换班提醒照常工作。
- **失效必须显式告知**：授权过期就提示重新授权，**绝不静默**。
- **凭据只落 `plugin_data/`**，不进日志、不进报错、不进仓库；展示一律脱敏
  （`credentials.py` 只暴露长度）。
- **不做**：绕过、模拟客户端、反检测、`dId` 指纹、签到以外的写操作。

## 为什么自己写而不是复用现成插件

用户最初的指令是「有插件能复用就直接复用」。评估报告
（`docs/project-plan/08-reuse-assessment.md`）实测后给出结论：**运行时复用不可行**
（对方的业务能力全挂在消息事件上，没有对外接口），可行的是**代码级复用**——
但首选参考实现都拖 `httpx` + `nonebot`，与本项目「运行时零第三方依赖」冲突。
所以做法是：**学做法，用标准库自己实现**；许可不佳的（无 LICENSE、AGPL）一律不碰。

## 未验证的地方（如实标注，不装）

`07-skland-api.md` §3 记着签名算法**没能端到端验证**——因为服务端对未登录请求
**根本不校验签名**，没有一次真实授权就无法区分。所以阶段二的**第一件事**是用真实
授权验证它（`/ak skland check` 就是探针：签名错了它会回 10001/10000 而不是 10002）。

同样未验证的还有：`attendance` 与 `player/info` 的成功响应体结构（见 `api.py` 里的
`UNVERIFIED` 标注）。**干员疲劳／基建状态／干员查询**因此**本阶段不做**——那几条路由
连存在性都没实测过，照文档猜路径正是本项目栽过的坑（`FormData` 那条 API 事实）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 两种运行场景的导入差异（与 modules/recruit/module.py 同一取舍）：
#   * AstrBot 加载时插件是一个包，插件根目录不在 sys.path 上 → 只能用父级相对导入；
#   * 从仓库根跑 pytest 时顶层包是 `modules`，`...` 会越界 → 必须用绝对导入。
# 先绝对、失败再回退相对，注册表的自动发现才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.module import Module
    from core.permission import session_allowed
    from core.storage import JsonStateStore
except ImportError:  # pragma: no cover
    from ...core.module import Module
    from ...core.permission import session_allowed
    from ...core.storage import JsonStateStore

from . import api, login, qr
from .credentials import (
    CREDENTIALS_FILENAME,
    CredentialState,
    SklandCredential,
    SklandCredentialStore,
)

COMMAND_NAMES = ("skland",)

#: 定时任务前缀。`terminate` 按它清理——容器被 kill 时 `terminate` 可能没跑，
#: 所以注册前也要按这个前缀清一遍（照 `shift_reminder` 的做法）。
JOB_PREFIX = "ak_toolbox:skland:"
SIGNIN_JOB_NAME = f"{JOB_PREFIX}signin"

PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"

#: 扫码轮询节奏。**有上限**：用户不扫就自己结束，不留下永远跑的后台任务。
POLL_INTERVAL_SECONDS = 2.0
POLL_ATTEMPTS = 60

#: 二维码像素倍率。登录链接约 60~70 字符，8 倍下约 330px——手机扫得动。
QR_SCALE = 8

SUBCOMMANDS = ("status", "login", "logout", "check", "signin")

_HELP = (
    "可用：/ak skland（看状态）、login（扫码授权）、logout（清除授权）、"
    "check（测一次连接）、signin（手动签到）"
)


class SklandSetupError(RuntimeError):
    """本模块无法工作（数据目录准备失败等基础设施问题）。

    抛出它等于把「本模块起不来」交给宿主记录与转达——**不要自己吞掉**，
    那会让宿主汇总里写着「已装载 skland」而模块其实是坏的。
    """


class SklandModule(Module):
    """森空岛模块。"""

    name = "skland"
    config_key = "skland"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._store: SklandCredentialStore | None = None
        self._client: api.SklandClient | None = None
        self._config: Mapping[str, Any] = {}
        self._job_ids: list[str] = []
        self._login_task: asyncio.Task[None] | None = None

    # --- 生命周期 -----------------------------------------------------------

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """准备数据目录、装载凭据、按配置注册签到任务。

        Raises:
            SklandSetupError: 数据目录准备失败。**未授权不在此列**——那是正常状态。
        """
        self._ctx = ctx
        self._config = dict(config or {})
        try:
            data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - 统一转成领域错误再上抛
            self._store = None
            raise SklandSetupError(f"数据目录准备失败：{type(exc).__name__}: {exc}") from exc

        self._store = SklandCredentialStore(JsonStateStore(data_dir / CREDENTIALS_FILENAME))
        self._client = api.SklandClient()
        self._load_credentials_into_client()

        await self._sync_signin_job(ctx)

        status = self._store.status()
        if status.state is CredentialState.CORRUPT:
            logger.error(
                "[ak_toolbox][skland] %s（存档：%s）——本模块不可用，等用户删档后重新授权",
                status.describe(),
                self._store.path,
            )
        elif status.state is CredentialState.MISSING:
            logger.info(
                "[ak_toolbox][skland] 模块已装载，还没授权。"
                "发 /ak skland login 扫码；换班提醒不受本模块影响。"
            )
        else:
            logger.info("[ak_toolbox][skland] 模块已装载，%s", status.describe())

    async def terminate(self) -> None:
        """取消登录轮询、清理定时任务；可安全重复调用。"""
        task, self._login_task = self._login_task, None
        if task is not None and not task.done():
            task.cancel()

        cron_manager = getattr(self._ctx, "cron_manager", None) if self._ctx else None
        job_ids, self._job_ids = self._job_ids, []
        if cron_manager is not None and job_ids:
            try:
                for job in await cron_manager.list_jobs():
                    if getattr(job, "job_id", None) in set(job_ids):
                        await cron_manager.delete_job(job.job_id)
            except Exception:  # noqa: BLE001 - 清理失败不冒泡，留痕即可
                logger.exception("[ak_toolbox][skland] 清理定时任务失败")

        self._ctx = None
        self._store = None
        self._client = None

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """配置变了就重建签到任务（`save_config` 不会自己重排定时任务）。"""
        self._config = dict(config or {})
        await self._sync_signin_job(self._ctx)

    @property
    def unavailable_reason(self) -> str | None:
        """凭据存档坏了 = 干不了活（每次调用都会抛），如实报告。

        **未授权不算不可用**：那正是本模块第一阶段要处理的事——它还能扫码。
        """
        if self._store is None:
            return "数据目录未就绪（模块初始化失败）"
        if self._store.status().state is CredentialState.CORRUPT:
            return f"凭据存档损坏，需要先删掉 {self._store.path}"
        return None

    # --- 配置与定时任务 -----------------------------------------------------

    def _signin_enabled(self) -> bool:
        return bool(self._config.get("signin_enabled", False))

    def _signin_cron(self) -> str:
        value = self._config.get("signin_cron")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "0 8 * * *"

    async def _sync_signin_job(self, ctx: Any) -> None:
        """按当前配置让签到任务存在或不存在。幂等，`initialize`/`apply_config` 共用。"""
        cron_manager = getattr(ctx, "cron_manager", None) if ctx is not None else None
        if cron_manager is None:
            return

        # 先清（前缀匹配）：容器被 kill 时 terminate 没跑，旧 job 会留在库里，
        # 不清就会越积越多并重复触发。
        try:
            stale = [
                job
                for job in await cron_manager.list_jobs()
                if str(getattr(job, "name", "")).startswith(JOB_PREFIX)
            ]
            for job in stale:
                await cron_manager.delete_job(job.job_id)
            purged = len(stale)
        except Exception:  # noqa: BLE001 - 清不掉就别注册，免得重复
            logger.exception("[ak_toolbox][skland] 清理历史签到任务失败，跳过注册")
            self._job_ids = []
            return

        self._job_ids = []
        if not self._signin_enabled():
            if purged:
                logger.info("[ak_toolbox][skland] 签到已关闭，清理了 %d 个任务", purged)
            return

        try:
            job = await cron_manager.add_basic_job(
                name=SIGNIN_JOB_NAME,
                cron_expression=self._signin_cron(),
                handler=self._run_signin_job,
                description="森空岛每日签到",
                timezone="Asia/Shanghai",
                payload={},
            )
        except Exception:  # noqa: BLE001 - 注册失败要显式，不要静默
            logger.exception("[ak_toolbox][skland] 注册签到任务失败（签到将不会自动执行）")
            return
        self._job_ids = [getattr(job, "job_id", "")]
        logger.info(
            "[ak_toolbox][skland] 已注册签到任务（%s，时区 Asia/Shanghai）", self._signin_cron()
        )

    async def _run_signin_job(self, *_: Any, **__: Any) -> None:
        """定时签到的入口。**任何失败都只记日志**——签到失败不该让调度器炸掉。"""
        try:
            result = await self._do_signin()
        except Exception:  # noqa: BLE001 - 后台任务不能冒泡
            logger.exception("[ak_toolbox][skland] 自动签到失败")
            return
        logger.info("[ak_toolbox][skland] 自动签到：%s", result)

    # --- 凭据与客户端 -------------------------------------------------------

    def _load_credentials_into_client(self) -> None:
        """把存档里的凭据装进客户端；坏档/未授权时保持无凭据状态。"""
        if self._store is None or self._client is None:
            return
        credential = self._store.load()
        if credential is None:
            return
        self._client.update_credentials(credential.cred, credential.token)

    def _has_credentials(self) -> bool:
        return self._client is not None and self._client.has_credentials

    async def _do_signin(self) -> str:
        """执行一次签到并返回一句人话结果。"""
        if not self._has_credentials():
            return "没有授权，跳过（发 /ak skland login 扫码）"
        assert self._client is not None
        payload = await asyncio.to_thread(self._client.sign_in)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if isinstance(data, Mapping) and data:
            return "签到成功"
        return "接口返回成功但响应体里没有 data（结构未验证，已如实记录）"

    async def _do_check(self) -> str:
        """健康检查：打真实取数接口。

        **不用 `auth/refresh`**——实测它对无效凭据也返回成功，拿它当健康检查会得到
        「一切正常」然后每个取数接口都失败。
        """
        if not self._has_credentials():
            return "还没有授权。发 /ak skland login 扫码。"
        assert self._client is not None
        try:
            uid = await asyncio.to_thread(self._client.binding_uid)
        except api.SklandUnauthorized as exc:
            self._forget_credentials("凭据已失效")
            return f"凭据无效，需要重新授权（{exc}）。已清除本地凭据，请发 /ak skland login。"
        except api.SklandLoginExpired as exc:
            self._forget_credentials("登录已失效")
            return f"登录已失效，需要重新授权（{exc}）。已清除本地凭据，请发 /ak skland login。"
        except api.SklandError as exc:
            return f"连接失败：{exc}"
        return f"连接正常（绑定 uid={uid}，已脱敏显示长度 {len(uid)}）"

    def _forget_credentials(self, why: str) -> None:
        """凭据失效时把它清掉并留痕——**不要留着一个永远失败的凭据装作还有授权**。"""
        if self._store is not None:
            try:
                self._store.clear()
            except Exception:  # noqa: BLE001 - 清不掉也要把结论说出去
                logger.exception("[ak_toolbox][skland] 清除失效凭据失败")
        if self._client is not None:
            self._client.update_credentials("", "")
        logger.warning("[ak_toolbox][skland] %s，已清除本地凭据", why)

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        """处理 `/ak skland [子命令 ...]`。

        宿主只把 `/ak` 之后的**第一个词**转给模块，所以子命令由本模块从
        `event.message_str` 自己解析——与 `shift_reminder` 解析 `/ak import <文件名>`
        的做法一致。
        """
        if command not in COMMAND_NAMES:
            return False

        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="操作森空岛模块",
        )
        if not allowed:
            await self._reply(event, reason)
            return True

        sub = self._parse_subcommand(str(getattr(event, "message_str", "") or ""))
        if sub in ("", "status"):
            await self._reply(event, self._status_text())
        elif sub == "login":
            await self._cmd_login(event)
        elif sub == "logout":
            await self._cmd_logout(event)
        elif sub == "check":
            await self._reply(event, await self._do_check())
        elif sub == "signin":
            await self._reply(event, await self._do_signin())
        else:
            await self._reply(event, f"未知的 skland 子命令：{sub}\n{_HELP}")
        return True

    @staticmethod
    def _parse_subcommand(message_str: str) -> str:
        """取 `/ak skland` 之后的第一个词；没有则返回空串（表示看状态）。"""
        tokens = message_str.strip().split()
        if tokens and tokens[0].lstrip("/").lower() == "ak":
            tokens = tokens[1:]
        if tokens and tokens[0].lower() == "skland":
            tokens = tokens[1:]
        return tokens[0].lower() if tokens else ""

    # --- 扫码授权 -----------------------------------------------------------

    async def _cmd_login(self, event: Any) -> None:
        """发起扫码授权：取二维码 → 发图片 → 有上限地轮询。"""
        if self._store is None or self._client is None:
            await self._reply(event, "模块没有起来（数据目录准备失败），看 /ak 里的原因。")
            return
        if self._store.status().state is CredentialState.CORRUPT:
            await self._reply(
                event,
                f"凭据存档是坏的，先删掉再授权：{self._store.path}",
            )
            return
        if self._login_task is not None and not self._login_task.done():
            await self._reply(event, "上一次扫码还没结束，扫完或者等它超时（约 2 分钟）。")
            return

        try:
            payload = await asyncio.to_thread(self._scan_login_request)
            ticket = login.parse_scan_ticket(payload)
        except login.ScanLoginError as exc:
            await self._reply(event, f"取二维码失败：{exc}")
            return
        except api.SklandError as exc:
            await self._reply(event, f"取二维码失败：{exc}")
            return

        try:
            png = qr.qr_png(ticket.scan_url, scale=QR_SCALE)
        except qr.QrError as exc:
            # 渲染不出来就退回文字链接：**扫码链路不能因为画不出图就断掉**。
            logger.warning("[ak_toolbox][skland] 二维码渲染失败：%s", exc)
            await self._reply(
                event,
                f"二维码生成失败（{exc}）。请把下面这段链接在森空岛 App 里打开：\n{ticket.scan_url}",
            )
        else:
            await self._reply_with_image(
                event,
                png,
                "用森空岛 App 扫这个码并确认授权。扫完我会自己回报结果（等约 2 分钟）。",
            )

        self._login_task = asyncio.create_task(
            self._poll_scan(event, ticket.scan_id, event.unified_msg_origin)
        )

    def _scan_login_request(self) -> Any:
        """第一步：向通行证侧要一张二维码票据（**不需要凭据**）。

        `UNVERIFIED`：本步骤的**请求已实测**（200 且返回 scanId/scanUrl），
        这里只是把它接进客户端。
        """
        assert self._client is not None
        return self._client.call_unauthenticated(
            "POST",
            f"{api.HYPERGRYPH_BASE}/general/v1/gen_scan/login",
            {"appCode": api.APP_CODE_SKLAND},
        )

    async def _poll_scan(self, event: Any, scan_id: str, umo: str) -> None:
        """轮询扫码状态，拿到 scanCode 就走完后三步。

        任务**有上限**，因为它是用户指令触发的短流程；契约禁止的是「自己起调度循环」，
        不是「一次有界的异步等待」。`terminate` 会取消它。
        """
        try:
            for _ in range(POLL_ATTEMPTS):
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                try:
                    payload = await asyncio.to_thread(self._scan_status_request, scan_id)
                    reading = login.interpret_scan_status(payload)
                except (api.SklandError, login.ScanLoginError) as exc:
                    await self._send(umo, f"查询扫码状态失败：{exc}")
                    return

                if reading.state is login.ScanState.SCANNED:
                    await self._complete_login(umo, reading.scan_code)
                    return
                if reading.state is login.ScanState.FAILED:
                    await self._send(umo, f"扫码失败：{reading.message}")
                    return
            await self._send(umo, "二维码超时了（约 2 分钟）。再发一次 /ak skland login。")
        except asyncio.CancelledError:  # pragma: no cover - 重载时取消
            logger.info("[ak_toolbox][skland] 扫码轮询被取消")
            raise

    def _scan_status_request(self, scan_id: str) -> Any:
        assert self._client is not None
        return self._client.call_unauthenticated(
            "GET",
            f"{api.HYPERGRYPH_BASE}/general/v1/scan_status?scanId={scan_id}",
        )

    async def _complete_login(self, umo: str, scan_code: str) -> None:
        """扫到了：走完换 token → grant → cred 三步，成功后落盘。"""
        if self._client is None or self._store is None:
            return
        try:
            token_payload = await asyncio.to_thread(
                self._client.call_unauthenticated,
                "POST",
                f"{api.HYPERGRYPH_BASE}/user/auth/v1/token_by_scan_code",
                login.token_request(scan_code),
            )
            passport_token = login.parse_token_by_scan_code(token_payload)

            grant_payload = await asyncio.to_thread(
                self._client.call_unauthenticated,
                "POST",
                f"{api.HYPERGRYPH_BASE}/user/oauth2/v2/grant",
                login.grant_request(api.APP_CODE_SKLAND, passport_token),
            )
            grant_code = login.parse_grant_code(grant_payload)

            cred_payload = await asyncio.to_thread(
                self._client.call_unauthenticated,
                "POST",
                f"{api.ZONAI_BASE}/user/auth/generate_cred_by_code",
                login.cred_request(grant_code),
            )
            cred, token = login.parse_credential(cred_payload)
        except (api.SklandError, login.ScanLoginError) as exc:
            # 失败要说清是哪一步，**且绝不回显任何 token/cred**。
            await self._send(umo, f"授权流程失败（没有拿到凭据）：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免后台任务静默死掉
            logger.exception("[ak_toolbox][skland] 授权流程异常")
            await self._send(umo, f"授权流程异常：{type(exc).__name__}（详见日志）")
            return

        self._store.save(SklandCredential(cred=cred, token=token))
        self._client.update_credentials(cred, token)
        logger.info("[ak_toolbox][skland] 授权成功，凭据已落盘（不打印内容）")
        await self._send(umo, "授权成功。发 /ak skland check 验证一次连接，签到我已能代劳。")

        # 授权后立刻验一次：签名算法此前**从未端到端验证过**，这是唯一能证它的地方。
        try:
            outcome = await self._do_check()
        except Exception:  # noqa: BLE001 - 验证失败不影响已完成的授权
            logger.exception("[ak_toolbox][skland] 授权后的连接验证异常")
            return
        await self._send(umo, f"连接验证：{outcome}")

    async def _cmd_logout(self, event: Any) -> None:
        if self._store is None:
            await self._reply(event, "模块没有起来，看 /ak 里的原因。")
            return
        existed = self._store.status().state is not CredentialState.MISSING
        try:
            self._store.clear()
        except Exception as exc:  # noqa: BLE001 - 删不掉要说出来
            await self._reply(event, f"清除凭据失败：{type(exc).__name__}（详见日志）")
            logger.exception("[ak_toolbox][skland] 清除凭据失败")
            return
        if self._client is not None:
            self._client.update_credentials("", "")
        await self._reply(event, "已清除本地凭据。" if existed else "本来就没有授权，无需清除。")

    # --- 文本 ---------------------------------------------------------------

    def _status_text(self) -> str:
        """状态回执——**每个失败都要带原因**。"""
        if self._store is None:
            return (
                "【森空岛】这个模块没有起来（数据目录准备失败），"
                "用 /ak 可以看到宿主记下的具体原因。换班提醒不受影响。"
            )
        status = self._store.status()
        lines = [f"【森空岛】{status.describe()}"]
        if status.state is CredentialState.CORRUPT:
            lines.append(f"存档位置：{self._store.path}")
            lines.append("删掉这个文件即可回到「未授权」，然后重新授权。")
        elif status.state is CredentialState.MISSING:
            lines.append("发 /ak skland login 扫码授权。")
        else:
            lines.append("发 /ak skland check 测一次连接，或 /ak skland signin 手动签到。")
        lines.append(
            f"自动签到：{'开（' + self._signin_cron() + '）' if self._signin_enabled() else '关'}"
        )
        lines.append(
            "本模块天生会失效（凭据过期、接口不通），所以换班提醒刻意不依赖它"
            "——它挂了，换班照常提醒。"
        )
        lines.append(_HELP)
        return "\n".join(lines)

    # --- 发送 ---------------------------------------------------------------

    async def _reply(self, event: Any, text: str) -> None:
        await self._send(event.unified_msg_origin, text)

    async def _reply_with_image(self, event: Any, png: bytes, caption: str) -> None:
        """发一条「文字 + 图片」的消息。

        图片走 `Image.fromBytes`（已核实其实现：内部转成 `base64://`），
        **不需要落临时文件**——少一个要清理的东西。
        """
        if self._ctx is None:
            logger.warning("[ak_toolbox][skland] 模块尚未初始化，无法回执")
            return
        chain = MessageChain([Plain(caption), Image.fromBytes(png)])
        try:
            sent = await self._ctx.send_message(event.unified_msg_origin, chain)
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][skland] 发送二维码失败（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][skland] 发送二维码失败：平台返回未成功")

    async def _send(self, umo: str, text: str) -> None:
        """给指定会话发文本。

        平台离线时 `send_message` **会抛异常**，这里必须包住：回执发不出去只是遗憾，
        异常冒泡出去会让整个流程失败（本项目 2026-09-29 的线上事故就是这个形状）。
        """
        if self._ctx is None:
            logger.warning("[ak_toolbox][skland] 模块尚未初始化，无法回执")
            return
        try:
            sent = await self._ctx.send_message(umo, MessageChain().message(text))
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][skland] 回执发送异常（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][skland] 回执发送失败")
