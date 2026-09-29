"""MAA 远程控制的任务体（**纯逻辑**：不 import astrbot、也不 import core）。

协议依据：官方《远程控制协议》中文原文（2026-09-29 抓取）

    https://docs.maa.plus/zh-cn/protocol/remote-control-schema.html

摘下四段**原文措辞**——它们不是背景资料，而是直接决定本模块设计的事实：

1. 任务体形如 `{"id": "<唯一 id>", "type": "<任务类型>"}`：
   「`id`：任务的唯一 id，字符串类型，在汇报任务时会使用」。
2. **端点必须可重入**：「该端点**应当可以重入并且重复返回需要执行的任务**，
   **MAA 会自动记录任务 id，对于相同的 id，不会重复执行。**」
   ⇒ 我们**可以**一直返回同一条任务直到它被汇报；**同 id 重复下发是安全的**。
   这条是整套去重设计的地基：只要「同一次换班 ⇒ 同一个 id」成立，
   **重复轮询与重试就不可能变成「多跑一次」**（见 `queue.py`）。
3. 「这些任务会被**按顺序执行**」；`LinkStart` = **启动一键长草（整套）**，
   `LinkStart-Base` = 「立即根据当前配置，**单独执行**一键长草中的对应子功能」。
   所有者要的是「**跑完全套流程**」，所以默认是 `LinkStart`。
4. 基建相关的任务**只有** `LinkStart-Base` 这一支能单独表达，而它「根据**当前配置**」
   执行——**协议里没有任何字段能传班次**（`09-maa-trigger-assessment.md` §1.3、
   `10-maa-shift-switching.md` §4.1 已实测确认）。
   ⚠️ 所以本模块**绝不允许**出现「帮你切到第 N 班」这类文案：我们只能「让它跑一次」，
   跑完前进到哪一班是 MAA 按它自己的规则定的。

本文件只处理**任务体的形状与类型白名单**：不碰网络、不碰框架、不碰调度。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_TASK_TYPES",
    "DEFAULT_TASK_TYPE",
    "HEART_BEAT",
    "LINK_START",
    "LINK_START_BASE",
    "STOP_TASK",
    "SUPPORTED_TASK_TYPES",
    "TASK_TYPE_LABELS",
    "TaskTypeError",
    "build_control_task",
    "build_task",
    "coerce_task_type",
    "task_type_label",
]

#: 启动一键长草（整套流程）。所有者原话：「定时启动 maa 让它跑完全套流程」。
LINK_START = "LinkStart"

#: 「立即执行任务」——**不是**给用户选的任务类型，而是本模块自己用的控制指令。
#:
#: 官方原文（`https://docs.maa.plus/zh-cn/protocol/remote-control-schema.html`）：
#:
#:   StopTask「'结束当前任务'任务，将会**尝试**结束当前运行的任务。如果任务列表还有
#:   其他任务会继续开始执行下一个。该任务**不会等待并确认当前任务已停止才会返回**，
#:   因此请使用心跳任务来确认停止命令是否已生效。」
#:
#: 两处措辞直接决定用法：**「尝试」** ⇒ 撤销的文案不许写成「已取消」；
#: **「不会等待并确认」** ⇒ 想知道停没停，只能靠 `HeartBeat`。
STOP_TASK = "StopTask"

#: 心跳。「会立即返回，并且将当前'顺序执行的任务'队列中正在执行的任务的 Id 作为
#: Payload 返回，如果当前没有任务执行，返回空字符串。」
HEART_BEAT = "HeartBeat"

#: 只跑基建子功能。比整套快得多，但**不是**所有者要的那个。
LINK_START_BASE = "LinkStart-Base"

#: 本模块**允许下发**的类型。刻意只有两个：能下发什么由产品决定，
#: 而不是把官方列表原样铺开——铺开等于给了用户一堆我们没考虑过的行为。
SUPPORTED_TASK_TYPES: tuple[str, ...] = (LINK_START, LINK_START_BASE)

DEFAULT_TASK_TYPE = LINK_START

#: 官方文档列出的全部可下发类型。**只用于报错时把话说准确**——
#: 「你写的这个协议不支持」与「你写的这个我们还没实现」是两件事，
#: 混成一句会让用户去查一个并不存在的文档问题。
ALL_TASK_TYPES: frozenset[str] = frozenset(
    {
        LINK_START,
        LINK_START_BASE,
        "LinkStart-WakeUp",
        "LinkStart-Combat",
        "LinkStart-Recruiting",
        "LinkStart-Mall",
        "LinkStart-Mission",
        "LinkStart-AutoRoguelike",
        "LinkStart-Reclamation",
        "CaptureImage",
        "CaptureImageNow",
        "StopTask",
        "HeartBeat",
        "Toolbox-GachaOnce",
        "Toolbox-GachaTenTimes",
        "Settings-ConnectAddress",
        "Settings-Stage1",
    }
)

TASK_TYPE_LABELS: dict[str, str] = {
    LINK_START: "跑一整套（一键长草）",
    LINK_START_BASE: "只跑基建",
}


class TaskTypeError(ValueError):
    """下发的任务类型不在白名单里。消息面向使用者，必须能直接看懂。"""


def task_type_label(task_type: str) -> str:
    """给任务类型一个人话标签；未知类型原样返回，不编。"""
    return TASK_TYPE_LABELS.get(task_type, task_type)


def build_task(task_id: str, task_type: str = DEFAULT_TASK_TYPE) -> dict[str, str]:
    """构造一条下发任务。

    Args:
        task_id: 稳定且唯一。**必须由调用方保证「同一次换班 ⇒ 同一个 id」**——
            整个去重都建立在这条性质上（见 `queue.task_id_for`）。
        task_type: 白名单内的类型。

    Returns:
        `{"id": ..., "type": ...}`。**只有这两个键**：官方说「MAA 只会读取 tasks」，
        多塞字段没有收益，却会让人以为它能带参数（例如班次——它带不了）。

    Raises:
        TaskTypeError: 类型不在白名单里。**显式抛错，不静默降级成 LinkStart**：
            用户以为在跑 A、实际跑了 B，是这类功能里最坏的一种错。
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskTypeError("任务 id 不能为空")
    if task_type not in SUPPORTED_TASK_TYPES:
        if task_type in ALL_TASK_TYPES:
            raise TaskTypeError(
                f"任务类型「{task_type}」协议支持，但本插件还没有实现它；"
                f"当前可用：{'、'.join(SUPPORTED_TASK_TYPES)}"
            )
        raise TaskTypeError(
            f"任务类型「{task_type}」不是协议定义的类型；"
            f"当前可用：{'、'.join(SUPPORTED_TASK_TYPES)}"
        )
    return {"id": task_id, "type": task_type}


def coerce_task_type(value: Any) -> str:
    """把配置里读来的任务类型归一化成白名单内的值，不合法就抛 `TaskTypeError`。

    单独抽出来是因为**配置是用户能改的**，而配置错误必须是「明确报错」，
    不能静默回落到默认值——那会让用户改了个错值却以为生效了。
    """
    if isinstance(value, str) and value.strip() in SUPPORTED_TASK_TYPES:
        return value.strip()
    raise TaskTypeError(
        f"任务类型配置不合法：{value!r}；当前可用：{'、'.join(SUPPORTED_TASK_TYPES)}"
    )


#: 本模块自己下发的控制指令。**刻意与 `SUPPORTED_TASK_TYPES` 分开**：
#: 那两个是「用户能选的任务」，这两个是「系统用来控制 MAA 的」。
#: 合在一起会让配置里也能填 `StopTask`——那等于让用户以为在下发任务，
#: 实际是在叫停自己。
CONTROL_TASK_TYPES: frozenset[str] = frozenset({STOP_TASK, HEART_BEAT})


def build_control_task(task_id: str, task_type: str) -> dict[str, str]:
    """构造一条控制指令（`StopTask` / `HeartBeat`）。

    Args:
        task_id: 本条控制指令自己的 id。**与换班任务的 id 空间无关**——
            官方只要求「同一个 id 不会重复执行」，而控制指令**不是换班任务**，
            它不参与「同一次换班 ⇒ 同一个 id」那套去重，也不影响班次计数。
            刻意不复用 `queue.task_id_for`：那会把控制指令和换班时刻绑在一起，
            让"撤了再撤"第二次发不出去（同 id 会被 MAA 忽略）。
        task_type: 必须是 `CONTROL_TASK_TYPES` 之一。

    Raises:
        TaskTypeError: 类型不对。显式抛错，不让调用方误发一个用户任务类型。
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise TaskTypeError("控制指令 id 不能为空")
    if task_type not in CONTROL_TASK_TYPES:
        raise TaskTypeError(
            f"「{task_type}」不是控制指令；可用：{'、'.join(sorted(CONTROL_TASK_TYPES))}"
        )
    return {"id": task_id, "type": task_type}
