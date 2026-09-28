"""提醒消息的渲染。

纯逻辑模块：禁止 import astrbot。
"""

from datetime import datetime

from .schedule import Shift, format_hhmm


def render_reminder(
    *,
    ending: Shift,
    starting: Shift,
    change_at: datetime,
    lead_minutes: int,
    extra: list[str] | None = None,
) -> str:
    """渲染一条换班提醒。

    Args:
        ending: 即将结束的班次。
        starting: 即将开始的班次。
        change_at: 换班时刻。
        lead_minutes: 提前量（分钟），用于文案里说明"还有多久"。
        extra: **S3 插槽**——V2 的干员名单会从这里进来，V1 恒为 None。

    Returns:
        可直接发送的纯文本消息。
    """
    lines = [
        f"【换班提醒】{lead_minutes} 分钟后换班（{change_at:%H:%M}）",
        f"当前：{ending.name}　{format_hhmm(ending.start_minute)}–{format_hhmm(ending.end_minute)}",
        f"即将：{starting.name}　{format_hhmm(starting.start_minute)} 开始",
    ]
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)


def render_status(
    *,
    now: datetime,
    current: Shift,
    upcoming: Shift,
    change_at: datetime,
    module_states: dict[str, bool],
    recent_sends: list[str],
) -> str:
    """渲染 ``/ak status`` 的输出。"""
    gap_minutes = max(0, int((change_at - now).total_seconds() // 60))
    hours, minutes = divmod(gap_minutes, 60)
    lines = [
        "【工具箱状态】",
        f"当前：{current.name}（{format_hhmm(current.start_minute)}–"
        f"{format_hhmm(current.end_minute)}）",
        f"下一班：{upcoming.name}　{change_at:%H:%M}（还有 {hours} 小时 {minutes} 分）",
        "模块："
        + "，".join(f"{name}={'开' if on else '关'}" for name, on in module_states.items()),
    ]
    if recent_sends:
        lines.append("最近发送：")
        lines.extend(f"  {item}" for item in recent_sends)
    else:
        lines.append("最近发送：无记录")
    return "\n".join(lines)
