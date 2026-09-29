"""从配置映射里读值的通用规则（**纯逻辑**：只依赖标准库）。

**这不是 AstrBot 自己的 `core/config/astrbot_config.py`** —— 那是框架的模块，
本文件属于本插件包（``<插件>.core.config``），只处理「怎么从一个 mapping 里取值」。

## 为什么需要它

宿主按 ``_conf_schema.json`` 的 ``items`` **逐项**生成配置。若某个 ``items`` 条目
漏写了 ``default``，那一项在全新安装后就是**空串**——而不是缺失。

于是「**用户没动过这一项**」与「**用户明确填了空**」在配置里长得**一模一样**，
而这两者的正确行为都是**用默认值**：一个空的任务类型、空的提前量都没有意义。

反过来的边界同样重要：**空串不是"填错了"**。真正填错的值（``"NotATask"``、
``"30"`` 这种类型错、``0`` 这种越界）**必须继续当场报错**——用户改了个错值却以为
生效了，比直接报错糟得多（项目宪法 §2 第 2 条「失败要显式」）。

## 这条规则只在这里定义一次

`is_unset` 是「什么算没设置」的**唯一定义**。不要在别处再写一遍
``value is None or value == ""``：本项目已多次因「同一个事实两份实现」出事
（权限判定、``Content-Type`` 只设在签名分支），而两份判断一旦分叉，**不会报错**，
只会让某些字段静默地走另一条路。

⚠️ **`core/shifts.py` 不 import 本文件**，也不适用本规则。原因有两条，都不是妥协：

1. 它被刻意定成**叶子**（有 AST 护栏钉着，见 ``tests/test_core_shifts.py``）；
2. 更要紧的是**它的字段不该回退**：三班时长**互相依赖**（必须合计 24 小时且首尾
   相接），静默补一个默认值会得到一个**合法但不是用户想要**的表——提醒会在错误的
   时刻响，而且**不会报错**。那正是本项目最怕的"静默错位"。

所以边界是：**字段独立、给错默认值最多是"不如意" ⇒ 空即默认**（提前量、任务类型、
TTL）；**字段互相依赖、给错默认值会静默错位 ⇒ 空即报错**（班次槽位）。班次那边保持
严格，且**每个槽位字段在 schema 里都有 `items` 级默认值**，全新安装不会产生空串。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def is_unset(value: Any) -> bool:
    """这个值算「没设置」吗？

    ``None``、以及**只有空白字符的字符串**都算。别的都算「设了值」——
    **包括 ``0`` / ``False`` / ``"0"``**：它们是有意义的取值，不是缺失。

    ``0`` 与 ``False`` 尤其要紧：``bool`` 是 ``int`` 的子类，用真假值判断
    「有没有设置」会把用户明确填的 ``0`` 吞掉（``modules/maa`` 的 TTL 就用 ``0``
    表达「不要这个任务」，它必须走到校验那一步去报错，而不是被当成缺失）。
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def setting(config: Mapping[str, Any], key: str, default: Any) -> Any:
    """读 ``config[key]``；**缺失、``None``、空串一律回退 ``default``**。

    返回值与 ``default`` 都是 ``Any``：配置来自 JSON，里面本来就可能是任何类型。
    **这里不做类型校验**——那是调用方的事，也正是「非法值仍要报错」能成立的原因：
    本函数的兜底只针对"空"，一个类型不对的值会原样送下去、在那里被当场拒掉。

    Args:
        config: 该模块自己的那一段配置。
        key: 扁平键名。
        default: 唯一来源的默认值（调用方持有的常量，别在这里再写一个字面量）。

    Returns:
        配置里的原值，或 ``default``。
    """
    value = config.get(key)
    return default if is_unset(value) else value


def unset_keys(config: Mapping[str, Any], keys: Iterable[str]) -> tuple[str, ...]:
    """这批键里哪些是「没设置」的（供调用方如实记一条 WARN）。

    为什么要单独暴露它：回退到默认值**不该是静默的**。装配层拿它拼一条
    WARN，用户查日志时能看到「这一项我用了默认值」，而不是以为配置生效了。
    """
    return tuple(key for key in keys if is_unset(config.get(key)))
