"""森空岛模块的装配层（阶段一：骨架）。

本文件是**本模块唯一接触 AstrBot 的文件**：凭据的读写全在 `credentials.py`
（纯逻辑，可被 pytest 直接覆盖）。

## 本阶段刻意不做的事

- **不注册任何定时任务**，也**不发任何网络请求**。阶段二才做扫码授权与取数；
  在没搞清接口真实行为之前写业务逻辑就是照文档猜，而本项目刚在
  「照文档抄 API 事实」上栽过（`docs/project-plan/07-skland-api.md` §2 一整节
  都是社区文档过时的路径）。所以 `terminate` 没有 job 要清——**不要**为了对称
  留一个空的清理分支（项目宪法 §5 第 4 条：禁止提前造扩展点）。
- **不实现 dId 设备指纹**（那是模拟设备身份，且参考实现里无人使用，见调研 §3）。

## 「未授权」与「坏掉」必须分开对待（本模块最关键的一个取舍）

这两件事看起来都像「用不了」，但处理方式正好相反：

| 情况 | 该怎么办 | 为什么 |
| --- | --- | --- |
| **未授权**（没有凭据存档） | **正常装载**，状态报「未授权」 | 这是首次使用的**正常状态**，不是失败。模块本身是好的，只是还没授权 |
| **基础设施坏了**（数据目录建不出来） | **抛出**，交给宿主隔离 | 这是真失败。吞掉它会让宿主汇总里仍写着「已装载 skland」——**重演「宿主说装好了、其实模块是坏的」那个谎言**（`plan.md` 待办第 8 条就是这个） |

所以：**未授权不抛，真失败抛**。抛出**不会**让插件崩——宿主已实现逐模块隔离
（`docs/implementation/implementation.md` §2.7），会把本模块记进 `failed_modules`
并把原因如实告诉用户，其余模块照常启动。这比「自己吞掉」更符合
「失败显式化」（项目宪法 §2 第 2 条）。

## 为什么有一个 `/ak skland` 子命令

因为「凭据状态」必须能被用户看见：它本地分「未授权 / 已授权 / 存档损坏」三态，
而这三态的处理方式完全不同（去授权 / 等阶段二 / 删掉坏档）。
留一个只报告状态的命令，是为了让「显式告知」有落点，不是占位接口。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
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

from .credentials import CREDENTIALS_FILENAME, CredentialState, SklandCredentialStore

COMMAND_NAMES = ("skland",)

#: 插件名。这是本文件里**第三份**同样的常量（`main.py` / `shift_reminder` 各一份）
#: ——如实记下这个重复：目前无害（同一个字符串、同一个仓库），但若出现第四处，
#: 就该像 `core/permission.py` 那样收敛到一处，而不是继续复制。
PLUGIN_NAME = "astrbot_plugin_arknights_toolbox"


class SklandSetupError(RuntimeError):
    """本模块无法工作（数据目录准备失败等基础设施问题）。

    抛出它等于把「本模块起不来」这件事交给宿主记录与转达——**不要自己吞掉**，
    那会让宿主汇总里写着「已装载 skland」而模块其实是坏的（见模块 docstring）。
    """


class SklandModule(Module):
    """森空岛模块（阶段一：只报告凭据状态）。"""

    name = "skland"
    config_key = "skland"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._store: SklandCredentialStore | None = None

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """准备数据目录与凭据存档。

        Args:
            ctx: AstrBot 的 `Context`。
            config: 本模块自己那一段配置。当前阶段**没有可配项**——`_conf_schema.json`
                只为它声明了模块开关，所以这里拿到空字典是正常情况。
                将来加参数时才在这里读，不提前占位。

        Raises:
            SklandSetupError: 数据目录准备失败。**未授权不在此列**——那是正常状态。
                抛错时**不注册任何东西**，所以被隔离时没有需要清理的残留
                （`core/module.py` 的契约：抛错时框架不会回头调 `terminate`）。
                注意这里**保留 `ctx`**：被隔离不代表要装死，用户发 `/ak skland`
                问「怎么回事」时它必须答得上来——那正是「失败显式化」的落点。
        """
        self._ctx = ctx
        try:
            data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - 统一转成领域错误再上抛
            # 只清掉「没建起来的东西」，保留 ctx 以便如实回话（见 docstring）。
            self._store = None
            raise SklandSetupError(f"数据目录准备失败：{type(exc).__name__}: {exc}") from exc

        self._store = SklandCredentialStore(JsonStateStore(data_dir / CREDENTIALS_FILENAME))

        status = self._store.status()
        if status.state is CredentialState.CORRUPT:
            # 坏档要人介入，必须显眼——不能悄悄当成「没授权」让用户白扫码。
            logger.error("[ak_toolbox][skland] %s（存档：%s）", status.describe(), status.detail)
        elif status.state is CredentialState.MISSING:
            logger.info(
                "[ak_toolbox][skland] 模块已装载，但还没有森空岛授权（阶段二提供扫码）。"
                "换班提醒不受影响。"
            )
        else:
            logger.info("[ak_toolbox][skland] 模块已装载，%s", status.describe())

    async def terminate(self) -> None:
        """本模块没有定时任务或后台任务，只清引用；可安全重复调用。"""
        self._ctx = None
        self._store = None

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        """处理 `/ak skland`：报告凭据与模块状态。"""
        if command not in COMMAND_NAMES:
            return False

        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="查森空岛状态",
        )
        if not allowed:
            await self._reply(event, reason)
            return True

        await self._reply(event, self._status_text())
        return True

    def _status_text(self) -> str:
        """拼出给用户看的回执——**每个失败都要带原因**。"""
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
            lines.append("扫码授权在阶段二提供，现在还不能用。")
            lines.append(
                "提醒：本模块天生会失效（凭据过期、接口不通），所以换班提醒刻意不依赖它"
                "——它挂了，换班照常提醒。"
            )
        lines.append("（当前阶段只报告状态，不取任何数据。）")
        return "\n".join(lines)

    async def _reply(self, event: Any, text: str) -> None:
        """给指令发起者回一条消息。

        平台离线时 `send_message` 会抛异常，这里**必须包住**：回执发不出去只是遗憾，
        异常冒泡出去会让整个指令 handler 失败，用户发指令结果毫无反应
        （2026-09-29 线上事故的同类路径）。
        """
        if self._ctx is None:
            logger.warning("[ak_toolbox][skland] 模块尚未初始化，无法回执")
            return
        try:
            sent = await self._ctx.send_message(
                event.unified_msg_origin, MessageChain().message(text)
            )
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][skland] 回执发送异常（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][skland] 回执发送失败：%s", text)
