"""``modules/shift_reminder/scheduler.py`` 的纯逻辑测试。

不 import astrbot；cron 表达式、幂等键、熔断状态机都在这里钉住。
"""

from datetime import datetime

import pytest

from modules.shift_reminder.schedule import ConfigError, Shift
from modules.shift_reminder.scheduler import (
    FailureBreaker,
    idempotency_key,
    reminder_cron_expression,
)

MORNING = Shift("早班", 8 * 60, 12 * 60)
EVENING = Shift("晚班", 20 * 60, 6 * 60)
NIGHT = Shift("夜班", 2 * 60, 6 * 60)


# --- reminder_cron_expression ---------------------------------------------


@pytest.mark.parametrize(
    ("shift", "lead_minutes", "expected"),
    [
        (MORNING, 10, "50 7 * * *"),
        (EVENING, 10, "50 19 * * *"),
        (NIGHT, 10, "50 1 * * *"),  # 跨天：02:00 − 10min 仍在当天凌晨
        (MORNING, 0, "0 8 * * *"),
        (NIGHT, 0, "0 2 * * *"),
        (NIGHT, 120, "0 0 * * *"),  # 02:00 − 2h = 00:00
        (NIGHT, 180, "0 23 * * *"),  # 02:00 − 3h 绕回前一天 23:00
        (MORNING, 480, "0 0 * * *"),  # 08:00 − 8h = 00:00
        (MORNING, 1440, "0 8 * * *"),  # 提前量正好一整天，绕回自己
    ],
)
def test_reminder_cron_expression(shift: Shift, lead_minutes: int, expected: str) -> None:
    assert reminder_cron_expression(shift, lead_minutes) == expected


def test_reminder_cron_expression_has_five_fields() -> None:
    assert len(reminder_cron_expression(MORNING, 10).split()) == 5


def test_reminder_cron_expression_rejects_negative_lead() -> None:
    with pytest.raises(ConfigError, match="提前量不能为负数"):
        reminder_cron_expression(MORNING, -1)


# --- idempotency_key -------------------------------------------------------


def test_idempotency_key_is_stable_for_same_shift_and_time() -> None:
    moment = datetime(2026, 9, 29, 20, 0)

    assert idempotency_key("晚班", moment) == idempotency_key("晚班", moment)


def test_idempotency_key_differs_by_time() -> None:
    assert idempotency_key("晚班", datetime(2026, 9, 29, 20, 0)) != idempotency_key(
        "晚班", datetime(2026, 9, 30, 20, 0)
    )


def test_idempotency_key_differs_by_shift() -> None:
    moment = datetime(2026, 9, 29, 20, 0)

    assert idempotency_key("晚班", moment) != idempotency_key("夜班", moment)


def test_idempotency_key_ignores_seconds_and_microseconds() -> None:
    """换班时刻是整分钟，秒的抖动不该让去重键失效、导致重复推送。"""
    assert idempotency_key("晚班", datetime(2026, 9, 29, 20, 0, 59, 999999)) == idempotency_key(
        "晚班", datetime(2026, 9, 29, 20, 0, 0)
    )


# --- FailureBreaker --------------------------------------------------------


def test_breaker_opens_exactly_at_threshold() -> None:
    breaker = FailureBreaker(threshold=3)

    assert breaker.is_open is False
    assert breaker.record_failure() is False  # 第 1 次
    assert breaker.is_open is False
    assert breaker.record_failure() is False  # 第 2 次
    assert breaker.is_open is False
    assert breaker.record_failure() is True  # 第 3 次：本次刚触发
    assert breaker.is_open is True
    assert breaker.consecutive_failures == 3


def test_breaker_does_not_retrigger_while_open() -> None:
    """已熔断时继续失败不该重复报「刚触发」，否则日志会被刷屏。"""
    breaker = FailureBreaker(threshold=2)
    breaker.record_failure()

    assert breaker.record_failure() is True
    assert breaker.record_failure() is False
    assert breaker.is_open is True
    assert breaker.consecutive_failures == 3


def test_breaker_success_resets_count_and_closes() -> None:
    breaker = FailureBreaker(threshold=2)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open is True

    breaker.record_success()

    assert breaker.is_open is False
    assert breaker.consecutive_failures == 0
    assert breaker.record_failure() is False, "解除后应重新从 1 开始计数"


def test_breaker_success_before_threshold_prevents_opening() -> None:
    breaker = FailureBreaker(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()

    assert breaker.record_failure() is False
    assert breaker.is_open is False


def test_breaker_default_threshold_is_three() -> None:
    breaker = FailureBreaker()

    assert breaker.threshold == 3
    assert breaker.is_open is False
    assert breaker.consecutive_failures == 0


def test_breaker_threshold_one_opens_on_first_failure() -> None:
    breaker = FailureBreaker(threshold=1)

    assert breaker.record_failure() is True
    assert breaker.is_open is True


@pytest.mark.parametrize("bad_threshold", [0, -1])
def test_breaker_rejects_bad_threshold(bad_threshold: int) -> None:
    with pytest.raises(ValueError, match="熔断阈值必须 >= 1"):
        FailureBreaker(threshold=bad_threshold)
