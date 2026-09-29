"""重新生成干员头像映射数据（`modules/shift_reminder/data/avatar_map.json`）。

用法：
    python scripts/build_avatar_map.py            # 联网抓取并重写数据文件
    python scripts/build_avatar_map.py --check    # 只报告覆盖率，不写文件

为什么要这个脚本
----------------
排班表里只有**中文干员名**，而头像文件是按**编号_英文名**命名的（`002_amiya.png`）。
中间那层映射必须有一份数据，而且必须**可追溯、可重跑**——不能靠手抄。

数据来源（单一来源，避免跨源拼接产生错配）
------------------------------------------
`arkntools/arknights-toolbox-data`（**MIT**，LICENSE 文件已核实存在）：

- `assets/locales/cn/character.json` —— 结构是 `{头像文件名: 中文名}`，键正好就是
  头像的文件名（不含扩展名），所以映射**不需要再跨源对齐**。
- `assets/img/avatar/*.png` —— 头像本体（**本脚本只取文件名清单，不下载图片**）。

许可边界（重要，别越线）
------------------------
该仓库自己的 README 写明：仓库代码是 MIT，但**游戏资源（含图片）的版权属于鹰角**。
因此本项目：

- **只**取「中文名 → 头像文件名」这一层**事实映射**（干员叫什么、编号是多少是事实，
  不是创作表达）；
- **绝不把头像图片放进仓库**，只用外链引用；
- 数据文件里注明来源与许可，便于后来者复核。

幂等：同一来源重复运行产生完全相同的文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "modules" / "shift_reminder" / "data" / "avatar_map.json"

DATA_REPO = "arkntools/arknights-toolbox-data"
DATA_REF = "master"
LICENSE_SPDX = "MIT"

NAMES_URL = f"https://cdn.jsdelivr.net/gh/{DATA_REPO}@{DATA_REF}/assets/locales/cn/character.json"
LISTING_URL = f"https://data.jsdelivr.com/v1/packages/gh/{DATA_REPO}@{DATA_REF}?structure=flat"
AVATAR_URL_TEMPLATE = (
    f"https://cdn.jsdelivr.net/gh/{DATA_REPO}@{DATA_REF}/assets/img/avatar/{{avatar_id}}.png"
)

USER_AGENT = "ak-toolbox-avatar-map/1.0 (+https://github.com/ppepperkok-hue/astrbot_plugin_arknights_toolbox)"
TIMEOUT_SECONDS = 60


def fetch_text(url: str) -> str:
    """抓一个文本资源。失败时抛出，**不静默返回空**——脚本宁可红着退出。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8")


def fetch_avatar_ids() -> set[str]:
    """从仓库文件清单里取出**实际存在头像**的编号集合。

    只取清单、不下图片：我们靠这份清单如实报告覆盖率，而不是假设「名字表里有的
    就一定有图」。
    """
    payload = json.loads(fetch_text(LISTING_URL))
    prefix = "/assets/img/avatar/"
    ids: set[str] = set()
    for entry in payload.get("files", []):
        name = str(entry.get("name", ""))
        if name.startswith(prefix) and name.lower().endswith(".png"):
            ids.add(Path(name).stem)
    return ids


def build_mapping(names: dict[str, Any], avatar_ids: set[str]) -> tuple[dict[str, str], list[str]]:
    """把 `{头像文件名: 中文名}` 反转成 `{中文名: 头像文件名}`。

    Returns:
        (映射, 冲突说明列表)。同名干员只保留一个（按编号升序取第一个），
        冲突会记进数据文件，不藏着。
    """
    mapping: dict[str, str] = {}
    conflicts: list[str] = []

    for avatar_id in sorted(avatar_ids):
        raw_name = names.get(avatar_id)
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name:
            continue
        existing = mapping.get(name)
        if existing is not None and existing != avatar_id:
            conflicts.append(f"{name}: 保留 {existing}，忽略 {avatar_id}")
            continue
        mapping[name] = avatar_id

    return mapping, conflicts


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成干员头像映射数据")
    parser.add_argument("--check", action="store_true", help="只报告覆盖率，不写文件")
    args = parser.parse_args()

    try:
        names_raw = json.loads(fetch_text(NAMES_URL))
        avatar_ids = fetch_avatar_ids()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"抓取失败，未改动任何文件：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(names_raw, dict):
        print("名字表结构不是对象，拒绝继续", file=sys.stderr)
        return 1

    names = {str(k): v for k, v in names_raw.items()}
    mapping, conflicts = build_mapping(names, avatar_ids)

    print(f"名字表条目      : {len(names)}")
    print(f"有头像文件的编号: {len(avatar_ids)}")
    print(f"成功建立映射    : {len(mapping)}")
    print(f"名字表有但无头像: {len([k for k in names if k not in avatar_ids])}")
    print(f"同名冲突        : {len(conflicts)}")
    for line in conflicts[:10]:
        print(f"  - {line}")

    if args.check:
        return 0

    payload = {
        "_comment": (
            "干员中文名 -> 头像文件名 的映射。**只有事实数据**：干员叫什么、头像文件叫什么。"
            "不含任何游戏图片、文案或立绘；头像一律外链引用，绝不打包进仓库。"
            f"来源 {DATA_REPO}（{LICENSE_SPDX}，LICENSE 文件已核实）。"
            "★ 该仓库 README 明确：仓库代码 MIT，但**游戏资源（含图片）版权属于鹰角**——"
            "所以本文件只登记文件名，不登记图片本身。"
        ),
        "schema": 1,
        "source": {
            "repo": f"https://github.com/{DATA_REPO}",
            "license": LICENSE_SPDX,
            "names_url": NAMES_URL,
            "avatar_listing_url": LISTING_URL,
            "fetched_at": date.today().isoformat(),
            "fetched_by": "scripts/build_avatar_map.py",
        },
        "avatar_url_template": AVATAR_URL_TEMPLATE,
        "count": len(mapping),
        "conflicts": conflicts,
        "names": dict(sorted(mapping.items())),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
