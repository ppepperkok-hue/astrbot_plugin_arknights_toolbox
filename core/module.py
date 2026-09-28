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

    async def apply_config(self, config: Mapping[str, Any]) -> None:
        """插件配置被改动后由宿主调用：让运行中的状态跟上新配置。

        为什么需要它（已核实 AstrBot 4.28.1 源码）：``AstrBotConfig.save_config``
        （``core/config/astrbot_config.py:262``）只保证**内存与磁盘**更新，**不会**
        让已经注册的定时任务跟着变。模块是在 ``initialize`` 里一次性读配置的，
        所以重新保存配置之后必须有人重建运行状态——否则会得到
        「面板显示新配置、实际仍按旧配置跑」这种最难查的错。

        Args:
            config: **本模块自己那一段**的新配置（与 ``initialize`` 同一形状）。

        Note:
            默认什么都不做：不需要热更新的模块不必关心它。需要热更新的模块
            应在这里**重建自己的运行状态**（例如重新注册定时任务），
            但**不要在这里做首次分配资源**——那是 ``initialize`` 的职责，
            本方法可能被反复调用。
        """
        return None

    async def handle_command(self, command: str, event: Any) -> bool:
        """处理 ``/ak <command>``。

        AstrBot 的指令是用装饰器在**插件类**上静态注册的，模块无法自行注册，
        所以 ``/ak`` 由宿主统一注册，再逐个问模块要不要处理这个子命令
        （见 `docs/architecture/extension.md` §2 的修订说明）。

        Args:
            command: 子命令名，已由宿主去空白并转小写。
            event: AstrBot 的 ``AstrMessageEvent``。

        Returns:
            处理了返回 ``True``，宿主会**阻止该事件继续传播**
            （``event.stop_event()``，见 AstrBot 4.28.1 的
            ``core/platform/astr_message_event.py:348``），因此实现方**必须自己把回复
            发出去**——例如 ``ctx.send_message(...)``。返回 ``False`` 表示不认识这个
            子命令，宿主会继续问下一个模块，最后统一回「未知子命令」。
        """
        return False
