"""从班次名字里读时长提示（纯逻辑）。

**为什么单独一个文件**：名字解析与「排班表结构解析」是两件独立的事——前者只吃一个
字符串，后者管整份 JSON 的形状。分开之后，名字解析可以用大量边界用例直接压测，
不必每次造一份完整排班表。

**背景（这个文件存在的理由）**：本项目早前的决定是「**不**从排班表解析班次时长」，
理由是 ``plans[].name`` 写法五花八门。那个决定**只对了一半**：

- 名字确实五花八门——MAA 自带样本里有 ``12H第一班``、``A+B 16H``、``第1班``、
  ``A+B 高效长班 1``、``A 组 12 H`` 这些写法；
- 但 **riic.autos（ArknightsInfraCalc-v3）导出的名字是规整的**：
  ``Shift 1 · 12h`` / ``Shift 2 · 6h`` / ``Shift 3 · 6h``，**里面确实带着时长**；
- 而 MAA 协议的 ``period`` / ``duration`` 字段在真实导出里**根本不存在**（实测）。

所以这里采取「能读就读、读不出就不给」：读到就替用户省掉填时刻、凑 24 小时、
保证首尾相接这三道门槛；读不出就退回手填，绝不硬猜。

**两条硬规则**（都是从真实样本反推出来的，不是拍脑袋）：

1. **必须有单位**：``12h`` / ``12H`` / ``12 小时`` / ``6h30m``。裸数字一律不算——
   否则 ``A+B 高效长班 1`` 这种以序号结尾的名字会被读成「1 小时」。
2. **必须落在名字末尾**（允许尾随空白与标点）。这条挡掉 ``12H第一班``：那个 ``12H``
   确实像时长，但它后面紧跟中文而**没有分隔符**，无法判断它是「这一班 12 小时」还是
   「12 小时轮次下的第一班」——**分不清就不猜**。（实测该文件三个名字都是
   ``12H第N班``，合计 36 小时，本来也不自洽。）

即便如此，**单条名字读出的数字都不是结论**——真正的闸门是
:func:`suggest_shift_minutes`：三个名字**全部**读得出、且**合计正好 24 小时**才作数。

纯逻辑：不 import astrbot、不碰文件系统、不读系统时间。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from .schedule import MINUTES_PER_DAY

__all__ = [
    "MINUTES_PER_DAY",
    "parse_duration_hint",
    "suggest_shift_minutes",
]

#: 时长必须带单位，且必须落在名字末尾。单位里 `小时` 要排在 `时` 前面（交替分支
#: 从左到右匹配），否则 `12 小时` 会在 `12 时` 处提前成功、把 `间` 留在外面而失配。
#:
#: 数字前面**不许紧贴正负号**：`-6h` 里的负号改变了含义，与其当成 `6h` 来读，
#: 不如拒掉。（`Shift 1 - 12H` 这种把 `-` 当分隔符的写法仍然成立——`-` 与数字之间
#: 隔了空格；`Shift-2|6h` 里的 `-` 也不在数字前面。）
_DURATION_RE = re.compile(
    r"(?<![-+])(?P<hours>\d+(?:\.\d+)?)\s*(?P<hunit>小[时時]|[时時]|h|H)"
    r"(?:\s*(?P<minutes>\d+)\s*(?P<munit>分[钟鐘]|分|m|M))?"
    r"\s*$"
)

#: 名字末尾允许出现的装饰性字符，解析前先削掉（`Shift 1 · 12h。` 也算数）。
_TRAILING_NOISE = "。.、,，·・-—–_|:：;；)]}）】」』 \t"

#: `hours` 用小数表示（如 `8.5H`），乘 60 后可能带浮点噪声，取整前先四舍五入。
_MINUTES_PER_HOUR = 60


def parse_duration_hint(name: str) -> int | None:
    """从班次名字里读出一个时长（分钟）；读不出来返回 ``None``。

    规则见模块 docstring 的两条硬规则：**必须有单位**、**必须落在末尾**。

    容错范围（都来自真实样本或用户手打习惯）：全角数字与全角空格（走 NFKC 归一化）、
    ``h`` / ``H`` / ``小时`` / ``时``、可选的分部分（``6h30m``）、数字与单位之间的空格、
    以及末尾的标点；``·`` / ``-`` / ``|`` 等分隔符在名字中间，不影响「取末尾」这条规则。

    Args:
        name: ``plans[].name``，例如 ``"Shift 1 · 12h"``。

    Returns:
        分钟数（``"6h30m"`` → ``390``）；以下情况一律返回 ``None``：

        - 不是字符串、空串、纯空白；
        - **没有单位**（``"A+B 高效长班 1"``、``"第1班"``）；
        - 单位后面还有内容（``"12H第一班"``）；
        - 数值不是正数或超过 24 小时。

    Note:
        返回值**只是提示**。单独一条读出来不足以决定班次配置——必须交给
        :func:`suggest_shift_minutes` 用「三段合计 24 小时」这道闸门复核。
    """
    if not isinstance(name, str):
        return None

    # 全角数字/字母/空格统一成半角，这样 `１２ｈ`、`12　小时` 都能命中。
    text = unicodedata.normalize("NFKC", name).strip().rstrip(_TRAILING_NOISE)
    if not text:
        return None

    match = _DURATION_RE.search(text)
    if match is None:
        return None

    minutes = int(round(float(match.group("hours")) * _MINUTES_PER_HOUR))
    if match.group("minutes"):
        minutes += int(match.group("minutes"))

    if minutes <= 0 or minutes > MINUTES_PER_DAY:
        return None
    return minutes


def suggest_shift_minutes(names: Sequence[str], *, expected_count: int) -> tuple[int, ...] | None:
    """把一串班次名字折算成**可用的**时长建议；任何一处不成立就返回 ``None``。

    这是真正把关的函数。**三个条件同时成立**才给出建议：

    1. 名字个数**正好**是 ``expected_count``（本插件固定三班，由调用方传入，
       不从别处复制这个常量）；
    2. **每一个**名字都读得出时长（部分读出=不可用，不许拿读得出的那几个凑）；
    3. 合计**正好** 24 小时。

    按真实样本校准过的行为：``Shift 1 · 12h`` / ``Shift 2 · 6h`` / ``Shift 3 · 6h``
    → ``(720, 360, 360)`` ✓；``A+B 16H`` / ``A+C 4H`` / ``B+C 4H`` → ``(960, 240, 240)``
    ✓（那确实是 16/4/4 的真实布局）；``A 组 12 H`` / ``B 组 12H`` / ``C 组 8.5H``
    → ``None``（合计 1950 分钟，不自洽）；``12H第N班``、``第N班``、``A+B 高效长班 N``
    → ``None``（读不出或全部读不出）。

    Args:
        names: 各班次名字，按文件里的顺序。
        expected_count: 期望的班次数（本插件传 `roster.SHIFT_COUNT`）。

    Returns:
        分钟数元组（顺序与 ``names`` 一致）；任一条件不成立时返回 ``None``。
    """
    if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
        return None
    if expected_count <= 0 or len(names) != expected_count:
        return None

    minutes: list[int] = []
    for name in names:
        hint = parse_duration_hint(name)
        if hint is None:
            return None
        minutes.append(hint)

    if sum(minutes) != MINUTES_PER_DAY:
        return None
    return tuple(minutes)
