"""模块基类（S1 插槽）。

契约冻结在 `docs/architecture/extension.md` §2；**改这里的签名等于改地基**，
必须先停下出影响评估（项目宪法 §5 第 8 条）。

本文件刻意**不在运行时 import astrbot**：注册表与基类不需要框架，
保持这一点才能让 `tests/test_registry.py` 在没有 AstrBot 的环境里跑起来。
框架类型用 `Any` 占位，真实类型是 `astrbot.api.star.Context`。
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any


class Module(ABC):
    """一个可独立开关的功能模块。

    子类必须：

    - 定义类属性 ``name``（模块 id，等于目录名，等于 `_conf_schema.json` 里的开关名）
    - 定义类属性 ``config_key``（该模块在配置里的键）
    - 实现 ``initialize`` 与 ``terminate``

    配置由宿主**按段注入**：``initialize`` 收到的 ``config`` 就是本模块自己那一段，
    因此模块既不从 ``main.py`` 取值，也不需要知道别的模块段存在
    （见 `docs/implementation/implementation.md`「模块如何取得自己的配置（C3 裁决）」）。

    契约不变项（见 `docs/architecture/extension.md` §2）：模块不许从 `main.py` 取值、
    不许 import 别的模块、不许自己起调度循环——定时一律走
    ``context.cron_manager.add_basic_job``。
    """

    name: str
    config_key: str

    @abstractmethod
    async def initialize(self, ctx: Any, config: Mapping[str, Any]) -> None:
        """插件装载时调用：读配置、注册指令与定时任务。

        Args:
            ctx: AstrBot 的 ``Context``。
            config: **本模块自己那一段**配置——由宿主按 ``config_key`` 从整份插件
                配置里取出后传入；该段缺失或类型不对时宿主传空字典。模块不应该
                去读整份配置，也不应该假设别的模块段存在。

        Raises:
            Exception: 配置非法时必须抛出，让加载显式失败——不静默降级
                （项目宪法 §2 第 2 条）。**注意**：本方法抛错时框架只做状态清理、
                不会回头调 ``terminate``，所以自己已经注册的东西要在这里清理干净。
        """

    @abstractmethod
    async def terminate(self) -> None:
        """插件停用或重载时调用。

        必须清理自己注册的一切：定时任务按 ``ak_toolbox:<name>:`` 前缀删除、
        后台任务取消。框架只在插件定义了 ``terminate`` 时才会 await 它，
        所以这是唯一的清理时机。

        实现应保证**可安全重复调用**：宿主回滚一次失败的启动之后，框架不会再调它。
        """

    def commands(self) -> list:
        """该模块自己注册的指令（默认无）。"""
        return []

    def jobs(self) -> list:
        """该模块自己注册的定时任务（默认无）。"""
        return []
