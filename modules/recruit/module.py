"""公开招募模块的装配层。

这是本模块**唯一接触 AstrBot 的文件**：数据装载在 `dataset`、匹配与组合在
`calculator`、文案在 `render`，它们都是纯逻辑，不 import 框架。

## 本模块不注册任何定时任务

招募计算是「你问我答」，没有周期性行为，所以 `terminate` 没有 job 要清。**不要**
为了对称而留一个空的 job 前缀清理（那属于提前造扩展点）。

## 一条刻意的策略：装载失败不抛异常

`initialize` 里数据读不出来时，这里**只记 ERROR 并把原因留在实例上**，不向上抛。
理由是宿主已核实的行为：任一模块 `initialize` 抛错，宿主会回滚**全部**模块、
插件整体加载失败（`main.py` 的 C1 回滚）。而招募数据是随代码一起发布的只读资源，
它坏了只说明这个模块没用，**不该连累换班提醒**——这与
`docs/implementation/implementation.md` §2.7「一个模块失效不许拖垮核心功能」同一条
原则。失败依然是显式的：ERROR 日志 + 用户发指令时看到的确切原因，两处都能看见。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

# 两种运行场景的导入差异（与 modules/shift_reminder/module.py 同一取舍）：
#   * AstrBot 加载时插件是一个包，插件根目录不在 sys.path 上 → 只能用父级相对导入；
#   * 从仓库根跑 pytest 时顶层包是 `modules`，`...` 会越界 → 必须用绝对导入。
# 先绝对、失败再回退相对，注册表的自动发现才 import 得动它。
try:  # pragma: no cover - 走哪支取决于运行场景，两支都是真实路径
    from core.module import Module
    from core.permission import session_allowed
except ImportError:  # pragma: no cover
    from ...core.module import Module
    from ...core.permission import session_allowed

from .dataset import RecruitData, RecruitDataError, load_data
from .parsing import parse_tags
from .render import render_query, render_tag_list

COMMAND_NAMES = ("recruit",)

MAX_OPERATORS_KEY = "max_operators"
MAX_COMBINATIONS_KEY = "max_combinations"
DEFAULT_MAX_OPERATORS = 20
DEFAULT_MAX_COMBINATIONS = 6
#: 展示条数的硬上界：配置写成 1000 会让一条 QQ 消息长到没法看，压回来并留痕。
LIMIT_CEILING = 60


def read_limit(config: Mapping[str, Any], key: str, default: int) -> int:
    """读一个展示条数上限。

    非法值（非整数、小于 1、超过 `LIMIT_CEILING`）**压回合法范围并写 WARNING**，
    不抛异常也不静默：这是纯展示参数，写错了不该让整个模块起不来，但也不能
    装作没发生（项目宪法 §2 第 2 条）。
    """
    raw = config.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "[ak_toolbox][recruit] 配置 %s=%r 不是整数，改用默认值 %d", key, raw, default
        )
        return default
    if raw < 1:
        logger.warning("[ak_toolbox][recruit] 配置 %s=%d 小于 1，改用默认值 %d", key, raw, default)
        return default
    if raw > LIMIT_CEILING:
        logger.warning(
            "[ak_toolbox][recruit] 配置 %s=%d 超过上限 %d，已压到上限", key, raw, LIMIT_CEILING
        )
        return LIMIT_CEILING
    return raw


class RecruitModule(Module):
    """公开招募计算模块。"""

    name = "recruit"
    config_key = "recruit"

    def __init__(self) -> None:
        self._ctx: Any = None
        self._data: RecruitData | None = None
        self._load_error: str | None = None
        self._max_operators = DEFAULT_MAX_OPERATORS
        self._max_combinations = DEFAULT_MAX_COMBINATIONS

    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """装载招募数据并读展示参数。

        数据装载失败**不抛异常**（理由见模块 docstring）：记 ERROR、把原因留下，
        指令被调用时原样转告用户。
        """
        self._ctx = ctx
        self._max_operators = read_limit(config, MAX_OPERATORS_KEY, DEFAULT_MAX_OPERATORS)
        self._max_combinations = read_limit(config, MAX_COMBINATIONS_KEY, DEFAULT_MAX_COMBINATIONS)

        try:
            data = load_data()
        except RecruitDataError as exc:
            self._data = None
            self._load_error = str(exc)
            logger.error("[ak_toolbox][recruit] 招募数据装载失败，本模块将不可用：%s", exc)
            return

        self._data = data
        self._load_error = None
        logger.info(
            "[ak_toolbox][recruit] 已装载招募数据：%d 位干员、%d 个标签（来源：%s）",
            len(data.operators),
            len(data.tags),
            data.source.get("api", "未注明") if isinstance(data.source, Mapping) else "未注明",
        )

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """配置改动后只更新展示参数。

        数据本身随代码发布，重读没有意义；这里**不做**资源分配（那是
        `initialize` 的职责，见 `core/module.py` 的契约说明）。
        """
        self._max_operators = read_limit(config, MAX_OPERATORS_KEY, DEFAULT_MAX_OPERATORS)
        self._max_combinations = read_limit(config, MAX_COMBINATIONS_KEY, DEFAULT_MAX_COMBINATIONS)

    async def terminate(self) -> None:
        """本模块没有定时任务或后台任务，只清引用；可安全重复调用。"""
        self._ctx = None

    # --- 指令 ---------------------------------------------------------------

    async def handle_command(self, command: str, event: Any) -> bool:
        """处理 `/ak recruit [标签…]`。"""
        if command not in COMMAND_NAMES:
            return False

        allowed, reason = session_allowed(
            is_group=not event.is_private_chat(),
            is_admin=event.is_admin(),
            action="查公开招募",
        )
        if not allowed:
            await self._reply(event, reason)
            return True

        await self._cmd_recruit(event)
        return True

    async def _cmd_recruit(self, event: Any) -> None:
        if self._data is None:
            await self._reply(
                event,
                f"【公开招募】这个模块暂时用不了：招募数据装载失败（{self._load_error}）。"
                "换班提醒不受影响。",
            )
            return

        tags = parse_tags(str(event.message_str or ""))
        if not tags:
            await self._reply(event, render_tag_list(self._data))
            return

        await self._reply(
            event,
            render_query(
                self._data,
                tags,
                max_operators=self._max_operators,
                max_combinations=self._max_combinations,
            ),
        )

    async def _reply(self, event: Any, text: str) -> None:
        """给指令发起者回一条消息。

        平台离线时 `send_message` 会抛异常，这里**必须包住**：回执发不出去只是遗憾，
        异常冒泡出去会让整个指令 handler 失败，用户发指令结果毫无反应
        （2026-09-29 线上事故的同类路径）。
        """
        if self._ctx is None:
            logger.warning("[ak_toolbox][recruit] 模块尚未初始化，无法回执")
            return
        try:
            sent = await self._ctx.send_message(
                event.unified_msg_origin, MessageChain().message(text)
            )
        except Exception:  # noqa: BLE001 - 回执失败不冒泡，留痕即可
            logger.exception("[ak_toolbox][recruit] 回执发送异常（平台可能已离线）")
            return
        if not sent:
            logger.warning("[ak_toolbox][recruit] 回执发送失败：%s", text)
