"""小状态的持久化：JSON 键值存档 + JSONL 发送记录。

纯逻辑模块：**禁止 import astrbot**（ruff.toml 的 TID 禁入规则会拦），
路径与阈值全部由调用方传入，模块内不取全局状态——这样才能被 pytest
直接覆盖，不需要 AstrBot 运行时（见 docs/architecture/rules.md §2、§6）。

两种存储各自解决一件具体的事：

- :class:`JsonStateStore` 存**少量、需要整体替换**的状态（绑定的 umo、
  幂等标记、失败计数）。写入是原子替换，避免进程中断留下半截 JSON 把存档毁掉。
- :class:`JsonlSendLog` 存**只追加**的发送记录。JSONL 天生适合追加，
  追加被中断最多损失一行，不影响已有内容。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


class CorruptedStoreError(RuntimeError):
    """存档存在、但内容读不出来。

    与「文件不存在」严格区分：后者是首次运行的正常状态（返回默认值即可），
    前者说明有东西坏了，必须让人当场看见，不许静默当成空配置
    （项目宪法 §2 第 2 条：看起来成功但什么都没发生是最高优先级的 bug）。
    """


def write_json_atomic(path: Path, payload: Any) -> None:
    """把一份 JSON 文档**原子**写到 ``path``。

    做法是「临时文件 + ``os.replace``」：先在同目录写 ``<name>.tmp``，再整体换过去。
    任何时刻磁盘上的正式文件要么是旧内容、要么是新内容，**不会出现半截 JSON**
    ——读取方（可能是另一个模块）因此永远不会解析到写了一半的内容。

    为什么它是模块级函数而不是 `JsonStateStore` 的私有方法：跨模块共享的那份
    「班次表」也要原子写（见 `core/shift_share.py`），而且它**不是**键值存档
    （每次整份覆盖，不该先把旧内容读进来合并）。两份实现迟早分叉，所以原子写的
    实现只有这一处，`JsonStateStore._write` 也走它。

    Args:
        path: 目标文件；父目录不存在时会被创建。
        payload: 任何可被 ``json.dumps`` 序列化的对象。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp_path, target)
    finally:
        # 成功时 os.replace 已经把临时文件搬走；失败时把它清掉，不留垃圾。
        tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class SendRecord:
    """一次推送的留痕。

    时间用 ISO 8601 序列化，人和机器都读得懂。
    """

    at: datetime
    shift: str
    ok: bool
    detail: str = ""

    def to_json_line(self) -> str:
        """序列化成一行 JSON（不含换行）。"""
        return json.dumps(
            {
                "at": self.at.isoformat(),
                "shift": self.shift,
                "ok": self.ok,
                "detail": self.detail,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json_line(cls, line: str) -> SendRecord:
        """从一行 JSON 还原。

        Raises:
            ValueError: 不是 JSON、不是对象、或缺少 ``at`` 字段。
        """
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"发送记录必须是 JSON 对象，收到 {type(raw).__name__}")
        at = raw.get("at")
        if not isinstance(at, str):
            raise ValueError("发送记录缺少 at 字段")
        return cls(
            at=datetime.fromisoformat(at),
            shift=str(raw.get("shift", "")),
            ok=bool(raw.get("ok", False)),
            detail=str(raw.get("detail", "")),
        )


class JsonStateStore:
    """小型键值状态的 JSON 持久化。

    写入走「临时文件 + ``os.replace``」的原子替换：先在同目录写
    ``<name>.tmp``，再整体换过去。这样任何时刻磁盘上的正式文件要么是旧内容、
    要么是新内容，不会出现半截 JSON。
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """存档路径（测试与排障要能看见它落在哪）。"""
        return self._path

    def get(self, key: str, default: Any = None) -> Any:
        """读一个键。

        **文件不存在**时返回 ``default``（首次运行是正常情况）；
        文件存在但读不出来时抛 :class:`CorruptedStoreError`。
        """
        return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写一个键并立即落盘。"""
        data = self._load()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> None:
        """删除一个键。键不存在时是空操作，重复调用安全（幂等）。"""
        data = self._load()
        if key in data:
            del data[key]
            self._write(data)

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {}
        text = self._path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptedStoreError(f"状态存档不是合法 JSON：{self._path}（{exc}）") from exc
        if not isinstance(data, dict):
            raise CorruptedStoreError(
                f"状态存档根节点必须是对象，实际是 {type(data).__name__}：{self._path}"
            )
        return data

    def _write(self, data: dict[str, Any]) -> None:
        # 原子写的实现只有一处（见 `write_json_atomic`）：这里不重复那段
        # 「临时文件 + os.replace」，否则两边迟早分叉。
        write_json_atomic(self._path, data)


class JsonlSendLog:
    """发送记录的 JSONL 追加日志，自动只保留最近 ``keep`` 条。

    读取时**跳过解析不了的行**：JSONL 是追加写的，进程在写一半时被杀会留下
    半截行；这种行本身没有可用信息，跳过它比让整份日志读不出来更合理。
    这是有意为之的取舍，不是掩盖错误——真正写坏整份文件的情况不存在，
    因为每次只追加一行。
    """

    def __init__(self, path: Path, keep: int = 50) -> None:
        if keep < 1:
            raise ValueError("keep 必须 >= 1")
        self._path = Path(path)
        self._keep = keep

    @property
    def path(self) -> Path:
        """日志路径。"""
        return self._path

    @property
    def keep(self) -> int:
        """最多保留多少条。"""
        return self._keep

    def append(self, record: SendRecord) -> None:
        """追加一条记录；超过 ``keep`` 时顺带截断。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json_line() + "\n")
        self._truncate()

    def recent(self, limit: int = 5) -> list[SendRecord]:
        """最近的 ``limit`` 条记录，**最新的在前**。"""
        if limit <= 0:
            return []
        return list(reversed(self._read_all()[-limit:]))

    def _read_all(self) -> list[SendRecord]:
        """按写入顺序读出所有能解析的记录（旧的在前面）。"""
        if not self._path.is_file():
            return []
        records: list[SendRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            record = self._parse_or_none(line)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _parse_or_none(line: str) -> SendRecord | None:
        """解析一行；空行与解析不了的行返回 ``None``（见类 docstring）。"""
        if not line.strip():
            return None
        try:
            return SendRecord.from_json_line(line)
        except ValueError:
            return None

    def _truncate(self) -> None:
        records = self._read_all()
        if len(records) <= self._keep:
            return
        kept = records[-self._keep :]
        self._path.write_text(
            "".join(record.to_json_line() + "\n" for record in kept),
            encoding="utf-8",
        )
