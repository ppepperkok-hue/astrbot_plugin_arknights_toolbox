"""到点询问的判定：**现在是哪一次换班、该不该问**（纯逻辑）。

不 import `core`、不 import astrbot：本模块需要的那点形状用
:class:`BoundaryLookup` 声明出来，由装配层把真正的班次表注入进来
（这正是 `docs/architecture/extension.md` §3 要求纯逻辑层用的做法——
`modules/shift_reminder/webapi.py` 是同一个范式）。

## 只问一个问题：**"现在"是不是某一次换班的开始**

判定刻意**复用班次表自己的 `starting_at`**，而不是再把时刻算一遍：这样
「什么算一次换班」在本项目里**只有一个定义**。本文件里没有第二份时刻计算。

### 与 `core.shifts.boundaries_between` 的分工（别把两者改得一样）

| 函数 | 问的问题 | "正好等于现在"的那一刻 |
| --- | --- | --- |
| `boundaries_between` | **下一次**换班在什么时候 | **不算**（它要的是严格晚于现在的未来时刻） |
| `due_boundary`（本文件） | **刚刚**是不是开始了一次换班 | **算**（换班正在此刻发生） |

两者对同一边界给出相反结论，**这是对的**：一个在找"下一次"，一个在认"这一次"。
把任一边改成跟另一边一致，都会造出一个真实的 bug——提醒会在换班那一刻误判成
"下一次还早"，或询问会在换班前就发出。

## 为什么要一个"宽限窗口"

只认"正好这一分钟"，会让**重启**变成一次静默的漏问：容器在 08:59:50 重启，
09:00 那一分钟没人跑巡检，这次换班就再也不会被问。所以判定放宽成
「开始时刻落在 `[现在 - 宽限, 现在]` 之内」——窗口远小于最短班次（默认 10 分钟，
而班次以小时计），所以**不会**把上一次换班误认成这一次。

窗口内出现多次换班时（只在极短的班次配置下才可能）取**最近的一次**，因为那才是
"刚刚开始的这一班"。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

__all__ = [
    "BoundaryLookup",
    "DueAsk",
    "ask_key",
    "due_boundary",
]

MINUTES_PER_DAY = 24 * 60


class BoundaryLookup(Protocol):
    """本模块需要班次表提供的那一点能力。

    只声明用得到的部分（`starting_at`），而不是让装配层传一个具体类型：
    纯逻辑层不能 import `core`，而这样声明同时把"我到底依赖什么"写在了明处。
    """

    def starting_at(self, minute_of_day: int) -> Any | None:
        """返回在该分钟开始的班次；没有则 ``None``。"""
        ...


@dataclass(frozen=True)
class DueAsk:
    """一次"刚刚开始的换班"，以及我们要不要去问。

    Attributes:
        name: 班次名（来自用户的配置，只用于显示与去重键）。
        moment: 该班次的**开始时刻**（秒与微秒归零，保留 `now` 的时区信息）。
        minutes_ago: 距现在过了几分钟。**给文案用**——"已经过了 8 分钟"与
            "刚刚开始"该说得不一样，否则用户会怀疑我们迟到了还是时区错了。
    """

    name: str
    moment: datetime
    minutes_ago: int

    @property
    def key(self) -> str:
        """这一次换班的**稳定标识**，用于"只问一次"的去重。

        同一个班次的同一个开始时刻，在任何进程、任何时刻都得到同一个 key
        （跨重启稳定），所以"问过就不再问"才成立。
        """
        return ask_key(self.name, self.moment)


def ask_key(name: str, moment: datetime) -> str:
    """换班标识：``<班次名>@YYYY-MM-DDTHH:MM``。

    只精确到**分钟**：班次开始时刻本身就是整分钟，秒与微秒参与只会让同一个
    边界算出两个 key，于是"只问一次"失效、用户被问两遍。
    """
    return f"{name}@{moment:%Y-%m-%dT%H:%M}"


def due_boundary(
    table: BoundaryLookup,
    now: datetime,
    grace_minutes: int,
) -> DueAsk | None:
    """`now` 落在一个「刚刚开始」的换班窗口里吗？是则返回它，否则 ``None``。

    Args:
        table: 班次表（任何提供 ``starting_at`` 的对象；装配层传 `core.shifts.ShiftTable`）。
        now: 当前时刻。**必须已经带好用户配置的时区**——用服务器本地时间算会让
            跨时区部署的询问落在错误的钟点上。
        grace_minutes: 宽限窗口（分钟），必须非负。

    Returns:
        窗口内**最近**的一次换班，或 ``None``。

    Raises:
        ValueError: 宽限为负。负数窗口没有意义（它会让判定永远为假），
            而"永远不触发"是一种最难察觉的坏法——当场报错。
    """
    if grace_minutes < 0:
        raise ValueError(f"宽限窗口不能为负，收到 {grace_minutes}")

    now_minute = now.hour * 60 + now.minute
    for ago in range(grace_minutes + 1):
        # 取模处理跨天：现在是 00:05、往前 10 分钟就是前一天的 23:55。
        candidate = (now_minute - ago) % MINUTES_PER_DAY
        shift = table.starting_at(candidate)
        if shift is None:
            continue
        # 秒与微秒归零，得到的才是"那次换班"的时刻本身（`now` 可能带秒）。
        # 用 `now - ago 分钟` 而不是重新拼日期：跨天、跨月、跨年都由 timedelta 负责。
        moment = (now - timedelta(minutes=ago)).replace(second=0, microsecond=0)
        return DueAsk(name=str(shift.name), moment=moment, minutes_ago=ago)
    return None
