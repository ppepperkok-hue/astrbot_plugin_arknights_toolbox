"""森空岛凭据的存取（**纯逻辑，不 import astrbot**）。

为什么单独一个文件而不是塞进 `module.py`
------------------------------------------
`module.py` 是装配层，需要框架；而「凭据怎么读、怎么写、坏了怎么判」是纯逻辑。
分开之后这一段能被 pytest 直接覆盖，且**凭据内容不可能出现在日志里**——
本文件里所有对外可见的字符串一律脱敏（见 `SklandCredential.masked`）。

设计上的两条硬要求（来自 `docs/implementation/implementation.md` §2.7 铁律）
------------------------------------------------------------------------------

1. **凭据只落 `plugin_data/`**：目录由调用方传入，本文件不取全局路径、不含机器路径。
2. **状态必须显式**：「没授权」「授权了」「存档坏了」是**三种不同状态**，
   不许把「坏了」静默当成「没授权」——那会让用户反复重新扫码却永远好不了
   （项目宪法 §2 第 2 条：看起来成功但什么都没发生是最高优先级的 bug）。

写入复用 `core.storage.JsonStateStore` 的原子替换，不另写一套落盘逻辑
（本项目刚因为「同一个规则两份实现」被审查抓过一次）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from core.storage import CorruptedStoreError, JsonStateStore

__all__ = [
    "CREDENTIALS_FILENAME",
    "CRED_FIELD",
    "TOKEN_FIELD",
    "USER_ID_FIELD",
    "CredentialState",
    "CredentialStatus",
    "SklandCredential",
    "SklandCredentialStore",
]

#: 凭据存档文件名。固定名，不由任何外部输入拼成（路径穿越在源头就没有机会）。
CREDENTIALS_FILENAME = "skland_credentials.json"

CRED_FIELD = "cred"
TOKEN_FIELD = "token"
USER_ID_FIELD = "user_id"


class CredentialState(StrEnum):
    """凭据的三种状态。

    用 `StrEnum` 而非裸字符串：拼错状态名会当场 AttributeError，而不是悄悄
    走进某个 `else` 分支——那正是本项目最怕的「静默匹配不上」。
    """

    MISSING = "missing"
    """从没授权过（首次运行的正常状态）。"""

    PRESENT = "present"
    """存档存在且形状合法（**不代表服务端仍认它**，见 SSOT §5）。"""

    CORRUPT = "corrupt"
    """存档存在但读不出来——要人介入，不许当成「没授权」。"""


@dataclass(frozen=True)
class SklandCredential:
    """一次成功授权拿到的三样东西。

    **不要把这个对象整个打进日志**：它含有等同账号访问权的凭据。
    需要展示时用 :meth:`masked`。

    Attributes:
        cred: 请求头 `cred` 的值，等同于「你是谁」。
        token: 签名用的 HMAC 密钥（`docs/project-plan/07-skland-api.md` §3）。
        user_id: 森空岛账号 id；阶段二登录后才有，允许为空。
    """

    cred: str
    token: str
    user_id: str = ""

    def __post_init__(self) -> None:
        if not self.cred:
            raise ValueError("cred 不能为空")
        if not self.token:
            raise ValueError("token 不能为空")

    def masked(self) -> dict[str, str]:
        """脱敏后的展示形式：**只暴露长度，不暴露任何字符**。

        为什么连前几位都不给：凭据泄露的代价太高，而「看前四位」对排查毫无帮助
        ——排障要的是「有没有设置」「多长」「什么时候坏的」，不是它的内容。
        """
        return {
            CRED_FIELD: f"已设置（{len(self.cred)} 字符）",
            TOKEN_FIELD: f"已设置（{len(self.token)} 字符）",
            USER_ID_FIELD: self.user_id or "未知",
        }


@dataclass(frozen=True)
class CredentialStatus:
    """凭据状态快照（可直接展示给用户）。"""

    state: CredentialState
    detail: str
    user_id: str = ""

    @property
    def usable(self) -> bool:
        """本地看不出问题——**注意这不等于服务端会认**（见 SSOT §5）。"""
        return self.state is CredentialState.PRESENT

    def describe(self) -> str:
        """一句人话，用于指令回执与日志。"""
        if self.state is CredentialState.PRESENT:
            who = f"，账号 {self.user_id}" if self.user_id else ""
            return f"已授权（本地凭据完整{who}）"
        if self.state is CredentialState.CORRUPT:
            return f"凭据存档损坏：{self.detail}"
        return "未授权"


class SklandCredentialStore:
    """把 `SklandCredential` 存进（或读出）某个目录。

    并发：调用方（装配层）在初始化与指令里串行使用，本类不自带锁——
    AstrBot 的指令处理是单事件循环上的协程，而落盘是毫秒级操作。
    """

    def __init__(self, directory: Path) -> None:
        self._store = JsonStateStore(Path(directory) / CREDENTIALS_FILENAME)

    @property
    def path(self) -> Path:
        """存档路径（排障要能看见它落在哪；不含凭据内容）。"""
        return self._store.path

    def status(self) -> CredentialStatus:
        """读状态。**本方法永不抛异常**——它在模块装载路径上被调用。"""
        try:
            data = self._read_fields()
        except CorruptedStoreError as exc:
            return CredentialStatus(CredentialState.CORRUPT, str(exc))

        if data is None:
            return CredentialStatus(CredentialState.MISSING, "还没有授权过")

        cred, token = data[CRED_FIELD], data[TOKEN_FIELD]
        if not _non_empty_str(cred) or not _non_empty_str(token):
            # 一个凭据字段都没有 = 从没存过（或刚被 clear 干净）→ 未授权。
            # 只要**存过一部分**（例如有 cred 没 token），那才是坏档。
            if not _any_present(cred, token, data[USER_ID_FIELD]):
                return CredentialStatus(CredentialState.MISSING, "还没有授权过")
            return CredentialStatus(
                CredentialState.CORRUPT,
                "存档里缺少 cred 或 token（或类型不对）",
            )
        user_id = data[USER_ID_FIELD]
        return CredentialStatus(
            CredentialState.PRESENT,
            "本地凭据完整",
            user_id=user_id if isinstance(user_id, str) else "",
        )

    def load(self) -> SklandCredential | None:
        """取出凭据；**没授权或坏了一律返回 `None`**。

        调用方要区分这两种情况时用 :meth:`status`——本方法刻意不抛异常，
        因为「凭据不可用」在调用点通常意味着「降级并告知用户」，
        而不是「中止流程」。
        """
        status = self.status()
        if not status.usable:
            return None
        data = self._read_fields()
        if data is None:  # pragma: no cover - 与 status 之间不可达的竞态窗口
            return None
        cred, token = data[CRED_FIELD], data[TOKEN_FIELD]
        user_id = data[USER_ID_FIELD]
        return SklandCredential(
            cred=str(cred),
            token=str(token),
            user_id=user_id if isinstance(user_id, str) else "",
        )

    def save(self, credential: SklandCredential) -> None:
        """写入凭据（覆盖旧值）。

        Raises:
            CorruptedStoreError: 已有存档坏掉时**拒绝写入**。
                覆盖一个读不出来的存档会掩掉「它曾经坏过」这个事实；
                宁可让用户先删掉它，也不要静默吞掉异常。
            TypeError: 传入的不是 `SklandCredential`。

        Note:
            三个键分三次写（`JsonStateStore` 没有多键原子写）。中途断电会留下
            「有 cred 没 token」的半截存档——那种状态会被 :meth:`status` 明确判为
            `CORRUPT` 并要人介入，不会伪装成可用。
        """
        if not isinstance(credential, SklandCredential):
            raise TypeError(f"期望 SklandCredential，收到 {type(credential).__name__}")
        self._read_fields()  # 坏档时在这一步抛出，不覆盖
        self._store.set(CRED_FIELD, credential.cred)
        self._store.set(TOKEN_FIELD, credential.token)
        self._store.set(USER_ID_FIELD, credential.user_id)

    def clear(self) -> None:
        """删掉本地凭据。重复调用安全。

        直接删文件而不是逐键清空：逐键清空会在磁盘上留一个 `{}`，用户「删掉了
        凭据」却还看得见一个文件，容易以为没删干净。删文件语义最直白。
        """
        self._store.path.unlink(missing_ok=True)

    def _read_fields(self) -> dict[str, object] | None:
        """读三个字段；**文件不存在返回 `None`**，文件坏了抛 `CorruptedStoreError`。

        为什么先 `is_file()` 再逐键读：`JsonStateStore.get()` 对「文件不存在」
        与「键不存在」返回同一个默认值，光靠它**区分不出这两种情况**
        （曾因此写出一个「刚存好就被报成未授权」的 bug）。
        文件存在性用文件系统判，内容正确性交给 `JsonStateStore` 判——
        它才是那份错误分类的唯一来源。
        """
        if not self._store.path.is_file():
            return None
        return {
            CRED_FIELD: self._store.get(CRED_FIELD),
            TOKEN_FIELD: self._store.get(TOKEN_FIELD),
            USER_ID_FIELD: self._store.get(USER_ID_FIELD, ""),
        }


def _non_empty_str(value: object) -> bool:
    """是不是非空字符串（`bool` 也是 `int`，但这里只收 `str`）。"""
    return isinstance(value, str) and bool(value)


def _any_present(*values: object) -> bool:
    """这些字段里有没有任何一个「像是存过东西」。

    用来区分两种「取不到凭据」：**从没存过**（全空 / 全 `None`）与**存了一半**
    （有 `cred` 没 `token`）。前者是正常的未授权，后者是坏档。
    """
    return any(value not in (None, "") for value in values)
