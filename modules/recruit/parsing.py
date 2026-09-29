"""从指令原文里取出标签。

纯逻辑（不 import astrbot）。单独成文件是因为「用户到底打了什么」是这类指令最
容易出错的地方：已核实 AstrBot 4.28.1 的唤醒阶段只剥掉**唤醒前缀**、指令名留
在文本里，所以模块收到的可能是 `ak recruit 输出 近战位`，也可能已经带斜杠。
"""

from __future__ import annotations

#: 出现在文本开头的指令词。游戏内的招募标签全是中文，所以这几个英文词不可能
#: 是用户想查的标签，可以直接丢掉。
_COMMAND_TOKENS = frozenset({"ak", "recruit"})


def parse_tags(message_str: str) -> tuple[str, ...]:
    """取出标签，保留用户输入的顺序。

    Args:
        message_str: 事件里的原始消息文本。

    Returns:
        标签元组；没给标签时是空元组（调用方据此走「列出所有标签」那条路）。
    """
    parts = str(message_str or "").split()
    while parts and parts[0].lstrip("/").lower() in _COMMAND_TOKENS:
        parts.pop(0)
    return tuple(parts)
