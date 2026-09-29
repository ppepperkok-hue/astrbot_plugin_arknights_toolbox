"""干员头像：中文名 → 外链 URL 的解析（纯逻辑）。

为什么需要这一层
----------------
排班表里只有**中文干员名**，而头像文件是按**编号_英文名**命名的（``002_amiya.png``）。
没有这层映射，页面就只能给用户看首字色块——那是**能用的回退**，但不是用户想要的
观感。映射数据由 ``scripts/build_avatar_map.py`` 从 MIT 许可来源生成，**可重跑**，
不是手抄的。

为什么只发 URL、不发图片
------------------------
1. **版权**：游戏头像的版权属于鹰角。数据来源仓库（arkntools/arknights-toolbox-data）
   整体是 MIT，但它自己的 README 明确写明**游戏资源除外**。所以图片一律**外链引用**，
   绝不打包进仓库。
2. **体积**：400+ 张图会占好几 MB，而插件包有体积上限。
3. **离线**：外链取不到时页面必须**照常可用**——所以 :meth:`AvatarIndex.url_for` 查不到
   就返回 None，调用方退回中文首字色块。那条回退**同步可用、不依赖网络**。

换图源（用户可配置）
--------------------
地址模板来自数据文件的 ``avatar_url_template`` 字段，默认值是 :data:`DEFAULT_URL_TEMPLATE`。
要换图源：**改数据文件里的这个字段**（或用 ``scripts/build_avatar_map.py`` 指向别的仓库
重新生成）。改完重启插件即可，页面不需要动。

纯逻辑模块：**禁止 import astrbot**（由 ruff.toml 的 TID 禁入规则强制）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

#: 数据文件缺少 ``avatar_url_template`` 时用的兜底模板。
#:
#: 指向与映射数据**同一个仓库**（同源才不会有编号错配），走 jsdelivr 的 GitHub 通道。
#: ``{avatar_id}`` 会被替换成形如 ``002_amiya`` 的文件名。
DEFAULT_URL_TEMPLATE = (
    "https://cdn.jsdelivr.net/gh/arkntools/arknights-toolbox-data@master"
    "/assets/img/avatar/{avatar_id}.png"
)

#: 模板里代表头像文件名的占位符。
AVATAR_ID_PLACEHOLDER = "{avatar_id}"


class AvatarLookup(Protocol):
    """页面组装只需要这一个能力。

    webapi 不该 import 本模块的具体类——留个结构化协议，测试里传个假对象就够了。
    """

    def url_for(self, name: str) -> str | None:
        """给中文干员名返回头像 URL；没有映射时返回 None（调用方走回退）。"""
        ...


class AvatarIndex:
    """干员名到头像 URL 的映射表。

    Attributes:
        url_template: 组装 URL 用的模板，含 :data:`AVATAR_ID_PLACEHOLDER`。
    """

    __slots__ = ("_by_name", "url_template")

    def __init__(self, by_name: Mapping[str, str], url_template: str) -> None:
        self._by_name = dict(by_name)
        self.url_template = url_template or DEFAULT_URL_TEMPLATE

    @property
    def size(self) -> int:
        """映射条目数。"""
        return len(self._by_name)

    def url_for(self, name: str) -> str | None:
        """给干员名返回完整的外链 URL；查不到或名字不可用时返回 None。

        Args:
            name: 排班表里的中文名（或 ``12F`` 这类英文名）。前后空白会被忽略。

        Returns:
            URL 字符串；无映射时为 None——**不编造 URL**，否则页面会显示一个断图，
            比直接走首字色块更糟。
        """
        if not isinstance(name, str):
            return None
        avatar_id = self._by_name.get(name.strip())
        if not avatar_id:
            return None
        return self.url_template.replace(AVATAR_ID_PLACEHOLDER, avatar_id)

    def url_map(self, names: Iterable[str]) -> dict[str, str]:
        """给一批干员名建立 ``名字 → URL`` 表，只保留**有映射**的那些。

        页面上只用到名单里出现过的干员，所以没必要把 400 多条映射整份发过去。
        """
        result: dict[str, str] = {}
        for name in names:
            if name in result:
                continue
            url = self.url_for(name)
            if url:
                result[name] = url
        return result


def parse_avatar_payload(payload: Any) -> AvatarIndex | None:
    """把数据文件的 JSON 内容解析成 :class:`AvatarIndex`。

    形状不对时返回 None（**不抛异常、不静默造空表**）：调用方据此记一条 WARNING
    并降级到首字色块——页面照常可用，但失败留下痕迹（项目宪法 §2 第 2 条）。

    Args:
        payload: 已解析的 JSON 对象。

    Returns:
        索引；``names`` 不是对象、或一条有效映射都没有时为 None。
    """
    if not isinstance(payload, Mapping):
        return None
    names = payload.get("names")
    if not isinstance(names, Mapping):
        return None

    by_name: dict[str, str] = {}
    for raw_name, raw_id in names.items():
        if not isinstance(raw_name, str) or not isinstance(raw_id, str):
            continue
        name, avatar_id = raw_name.strip(), raw_id.strip()
        if name and avatar_id:
            by_name[name] = avatar_id

    if not by_name:
        return None

    template = payload.get("avatar_url_template")
    if not isinstance(template, str) or AVATAR_ID_PLACEHOLDER not in template:
        # 模板缺了占位符就拼不出 URL——与其拼出一堆相同地址，不如用默认值。
        template = DEFAULT_URL_TEMPLATE
    return AvatarIndex(by_name, template)


def load_avatar_index(path: Path) -> AvatarIndex | None:
    """从数据文件读取头像索引。

    读不到、解析不了、形状不对时**都返回 None**——把「为什么失败」留给调用方记日志，
    这里不重复处理。头像只是锦上添花，**绝不该因为它让插件加载失败**。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parse_avatar_payload(payload)
