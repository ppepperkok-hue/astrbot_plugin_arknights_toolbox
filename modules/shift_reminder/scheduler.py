"""调度与推送的纯逻辑：cron 表达式、幂等键、失败熔断。

纯逻辑模块：**禁止 import astrbot**。时间换算一律复用
:mod:`modules.shift_reminder.schedule` 里的 ``Shift`` / ``MINUTES_PER_DAY`` /
``ConfigError``，这里不重复造一套时间计算（重复实现是 bug 的温床）。

这三件东西是 S4「调度与推送」里唯一可以脱离框架单测的部分，其余部分
（cron_manager、send_message）属于框架胶水层，留到服务器上手动验收。
"""

from dataclasses import dataclass, field
from datetime import datetime

from .schedule import MINUTES_PER_DAY, ConfigError, Shift

# 5 段 cron 的「日 月 周」固定通配：提醒每天都要发生，与月/周无关。
_CRON_TAIL = "* * *"


def reminder_cron_expression(shift: Shift, lead_minutes: int) -> str:
    """把「班次开始时刻 − 提前量」转成 5 段 cron 表达式（分 时 日 月 周）。

    跨天由取模处理，例如夜班 02:00、提前 10 分钟 → ``50 1 * * *``；
    提前量大于班次开始时刻时会绕回前一天，例如夜班 02:00 提前 3 小时
    → ``0 23 * * *``。

    Args:
        shift: 班次（用它的 ``start_minute``）。
        lead_minutes: 提前量（分钟），必须非负。

    Returns:
        形如 ``"50 7 * * *"`` 的表达式。

    Raises:
        ConfigError: 提前量为负。
    """
    if lead_minutes < 0:
        raise ConfigError("提前量不能为负数")
    minute_of_day = (shift.start_minute - lead_minutes) % MINUTES_PER_DAY
    hour, minute = divmod(minute_of_day, 60)
    return f"{minute} {hour} {_CRON_TAIL}"


def idempotency_key(shift_name: str, change_at: datetime) -> str:
    """同一班次同一次换班的去重键。

    只精确到**分钟**：换班时刻本身就是整分钟，秒与微秒参与只会让键不稳定，
    把「同一次换班」拆成两个键、进而重复推送。
    """
    return f"{shift_name}@{change_at:%Y-%m-%dT%H:%M}"


@dataclass
class FailureBreaker:
    """连续推送失败达阈值后暂停推送，成功一次即解除。

    Args:
        threshold: 连续失败多少次算熔断，必须 >= 1。

    Note:
        ``record_failure()`` 的返回值语义是「**本次刚触发**熔断」，
        因此已处于熔断状态时继续失败返回 ``False``——调用方据此只播报一次
        「已暂停推送」，而不是每失败一次刷一条。
    """

    threshold: int = 3
    _consecutive_failures: int = field(default=0, init=False, repr=False)
    _is_open: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("熔断阈值必须 >= 1")

    @property
    def is_open(self) -> bool:
        """当前是否处于熔断（暂停推送）状态。"""
        return self._is_open

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败次数。"""
        return self._consecutive_failures

    def record_failure(self) -> bool:
        """记一次失败。

        Returns:
            True 表示本次失败刚好把计数推到阈值、**刚触发熔断**。
        """
        self._consecutive_failures += 1
        if not self._is_open and self._consecutive_failures >= self.threshold:
            self._is_open = True
            return True
        return False

    def record_success(self) -> None:
        """记一次成功：清零计数并解除熔断。"""
        self._consecutive_failures = 0
        self._is_open = False
