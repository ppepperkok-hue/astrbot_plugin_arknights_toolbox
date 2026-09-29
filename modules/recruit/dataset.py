"""公开招募的数据模型与装载。

**纯逻辑**：只依赖标准库（json / dataclasses / pathlib / unicodedata），
不 import astrbot（`docs/architecture/rules.md` §2，ruff 的 TID 禁入会拦）。

为什么把「装载」和「计算」分成两个文件：装载要处理文件形态与各种畸形输入
（出错的地方多），计算只做集合运算（容易测透）。混在一起会让两边都难测。

## 两个稀有度保证标签从哪来

【资深干员】和【高级资深干员】在数据文件里**不逐条登记**，而是按星级推导。
依据 PRTS Wiki「公开招募」页原文：

    稀有需求标签：【资深干员】和【高级资深干员】…选择后以 9:00:00 开始招募时，
    这两个标签将不会被划去而必得 5★/6★ 干员（同时选择时优先最高稀有度）
    6 星干员在公开招募中必须通过需求标签【高级资深干员】追加出现概率以供招募。

推导规则（三条都能复现上面那句话）：

- 6★ 同时带【资深干员】与【高级资深干员】
- 5★ 带【资深干员】

于是：只选【资深干员】→ 命中 5★∪6★ → 必得 5★；只选【高级资深干员】→ 只命中 6★
→ 必得 6★；两个都选 → 交集落在 6★ → 「优先最高稀有度」。**按星级推导而不是
抄一份名单**，数据就不可能与星级不同步——抄名单必然会在某次更新后漂移。
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SENIOR_TAG = "资深干员"
TOP_OPERATOR_TAG = "高级资深干员"

#: 带【资深干员】的最低星级（含）与带【高级资深干员】的星级。
SENIOR_MIN_STARS = 5
TOP_OPERATOR_STARS = 6

MIN_STARS = 1
MAX_STARS = 6

DEFAULT_DATA_FILENAME = "recruit_pool.json"


class RecruitDataError(ValueError):
    """数据文件缺失或形状不对。

    装载期一律**显式失败**：少读了干员、或星级读错一格，算出来的「必出 5★」
    就是错的，而错的结果看起来和对的完全一样（项目宪法 §2 第 2 条）。
    """


def normalize_tag(raw: str) -> str:
    """把标签归一化成可比对的形状。

    NFKC 把全角与兼容字符折成标准形（手打常带全角空格或异体字），再去掉全部
    空白——游戏内标签本身不含空格，用户多敲一个空格不该被当成「未知标签」。
    数据文件与用户输入都过这一道，两边才可能对得上。
    """
    return "".join(unicodedata.normalize("NFKC", str(raw)).split())


def derive_rarity_tags(stars: int) -> frozenset[str]:
    """按星级给出应带的稀有度保证标签（见模块 docstring 的推导规则）。"""
    derived: set[str] = set()
    if stars >= SENIOR_MIN_STARS:
        derived.add(SENIOR_TAG)
    if stars >= TOP_OPERATOR_STARS:
        derived.add(TOP_OPERATOR_TAG)
    return frozenset(derived)


@dataclass(frozen=True)
class Operator:
    """一个可通过公开招募获得的干员。

    Attributes:
        name: 干员名（中文，与游戏一致）。
        stars: **显示星级 1–6**，不是游戏数据里的内部 0–5。内部值差一格，
            是这类数据最容易埋的 off-by-one。
        tags: 已归一化的标签集合，**含按星级推导出的稀有度标签**。
    """

    name: str
    stars: int
    tags: frozenset[str]

    def has_all(self, wanted: frozenset[str]) -> bool:
        """所选标签是否全部命中（`wanted ⊆ tags`）。"""
        return wanted <= self.tags


@dataclass(frozen=True)
class RecruitData:
    """一份完整的招募索引。

    Attributes:
        operators: 全部可招募干员。
        tags: 全部可填标签（含推导出的稀有度标签），已排序——「不带参数时列出
            可用标签」直接用这个，不必再去扫一遍干员。
        tag_set: `tags` 的集合形式，用于 O(1) 判断「这个标签认得吗」。
        source: 数据来源元信息，原样来自数据文件。**必须存在**：没有出处的数据
            等于没有依据（见 `load_data`）。
        schema: 数据文件结构版本。
    """

    operators: tuple[Operator, ...]
    tags: tuple[str, ...]
    tag_set: frozenset[str]
    source: Mapping[str, Any] = field(default_factory=dict)
    schema: int = 1


def build_operator(name: str, stars: int, tags: Iterable[str]) -> Operator:
    """构造一个 `Operator`，并把按星级推导的稀有度标签并进去。

    测试与装载都走这里，保证「稀有度标签怎么来的」只有一处实现。
    """
    if stars < MIN_STARS or stars > MAX_STARS:
        raise RecruitDataError(f"星级必须在 {MIN_STARS}–{MAX_STARS} 之间，收到 {stars!r}")
    normalized = {normalize_tag(t) for t in tags}
    normalized.discard("")
    return Operator(
        name=str(name), stars=int(stars), tags=frozenset(normalized | derive_rarity_tags(stars))
    )


def build_data(
    operators: Sequence[Operator],
    *,
    source: Mapping[str, Any] | None = None,
    schema: int = 1,
) -> RecruitData:
    """把干员列表补齐成 `RecruitData`（算出标签表与索引集合）。"""
    if not operators:
        raise RecruitDataError("干员列表为空——空数据会让每个标签都「没有结果」，必须显式失败")

    tags = sorted({tag for op in operators for tag in op.tags})
    return RecruitData(
        operators=tuple(operators),
        tags=tuple(tags),
        tag_set=frozenset(tags),
        source=dict(source or {}),
        schema=int(schema),
    )


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecruitDataError(f"{where} 必须是对象，收到 {type(value).__name__}")
    return value


def _require_sequence(value: Any, where: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RecruitDataError(f"{where} 必须是数组，收到 {type(value).__name__}")
    return value


def parse_data(payload: Any, *, where: str = "数据") -> RecruitData:
    """从已解析的 JSON 对象构造 `RecruitData`。

    与 `load_data` 分开，是为了让「读文件」和「校验形状」各自可测。
    """
    root = _require_mapping(payload, where)

    source = root.get("source")
    if not isinstance(source, Mapping) or not source:
        raise RecruitDataError(
            f"{where} 缺少 `source`（数据出处）。没有出处的招募数据无法核对，"
            "宁可拒绝装载也不要用一份查不到来源的表去算「必出几星」"
        )

    raw_ops = _require_sequence(root.get("operators"), f"{where}.operators")
    operators: list[Operator] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_ops):
        node = _require_mapping(item, f"{where}.operators[{index}]")
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RecruitDataError(f"{where}.operators[{index}].name 必须是非空字符串")
        stars = node.get("stars")
        if not isinstance(stars, int) or isinstance(stars, bool):
            raise RecruitDataError(f"{where}.operators[{index}].stars 必须是整数")
        raw_tags = _require_sequence(node.get("tags"), f"{where}.operators[{index}].tags")
        if name in seen:
            raise RecruitDataError(f"{where} 出现重复干员：{name}")
        seen.add(name)
        operators.append(build_operator(name, stars, [str(t) for t in raw_tags]))

    return build_data(operators, source=source, schema=int(root.get("schema", 1)))


def load_data(path: Path | str | None = None) -> RecruitData:
    """从 JSON 文件装载招募索引。

    Args:
        path: 数据文件路径；默认取模块目录下的 `data/recruit_pool.json`。

    Raises:
        RecruitDataError: 文件不存在、不是合法 JSON、或结构不符合约定。
            **每一种都显式抛出**，不静默退回空数据。
    """
    target = Path(path) if path is not None else default_data_path()
    if not target.is_file():
        raise RecruitDataError(f"找不到招募数据文件：{target}")
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 权限/IO 异常，难以在测试里稳定复现
        raise RecruitDataError(f"读取招募数据文件失败：{target}（{exc}）") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecruitDataError(f"招募数据文件不是合法 JSON：{target}（{exc}）") from exc
    return parse_data(payload, where=target.name)


def default_data_path() -> Path:
    """内置数据文件的路径（与 `dataset.py` 同级目录下的 `data/`）。"""
    return Path(__file__).resolve().parent / "data" / DEFAULT_DATA_FILENAME
