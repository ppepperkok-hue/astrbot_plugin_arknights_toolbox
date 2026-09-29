#!/usr/bin/env python3
"""Guard a failure mode that bit this project four times in one night.

The shape of the bug: something *silently matches nothing*. No exception, no
log line, no failing test — the feature is simply inert.

Observed instances (2026-09-29):
  1. `.gitignore` had a bare `data/`, so `modules/recruit/data/*.json` was
     never committed: tests passed locally, deployment was missing a file.
  2. The host logged "loaded modules: shift_reminder, recruit" while `recruit`
     had already recorded itself as unusable.
  3. CSS used `var(--border)` when the token is actually `--line` — no border,
     no error.
  4. The room card never emitted `data-room`, so every room-colour selector
     matched nothing.

This script checks the *cross-boundary* consistency that unit tests miss: the
server's room keys vs the selectors and colour tokens the stylesheet defines.

Exit code 1 (with a precise list) when they drift apart.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CSS = REPO / "pages" / "shift-reminder" / "style.css"
APP_JS = REPO / "pages" / "shift-reminder" / "app.js"


def main() -> int:
    from modules.shift_reminder.roster import ROOM_LABELS

    problems: list[str] = []

    css = CSS.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    server_keys = set(ROOM_LABELS)
    selectors = set(re.findall(r'\[data-room="([a-z0-9-]+)"\]', css))
    tokens = set(re.findall(r"(--room-[a-z0-9-]+)\s*:", css))

    # 1) every room the server can emit must have a colour rule
    missing_selectors = sorted(server_keys - selectors)
    if missing_selectors:
        problems.append(
            "样式表里没有这些房型的选择器（它们会没有颜色，且不报错）："
            + "、".join(missing_selectors)
        )

    # 2) the JS must actually emit data-room, otherwise nothing matches
    if "dataset.room" not in js and "data-room" not in js:
        problems.append("app.js 没有输出 data-room —— 所有房型配色选择器都会匹配不上")

    # 3) every selector must resolve to a real token, or it paints nothing
    unresolved: list[str] = []
    for block in re.finditer(r"([^{}]*?)\{([^{}]*)\}", css):
        selector, body = block.group(1), block.group(2)
        if "data-room=" not in selector:
            continue
        for token in re.findall(r"var\((--room-[a-z0-9-]+)\)", body):
            if token not in tokens:
                unresolved.append(f"{selector.strip()[:60]} -> {token}")
    if unresolved:
        problems.append(
            "这些选择器引用了不存在的 CSS 变量（会静默失效）：\n    "
            + "\n    ".join(sorted(set(unresolved)))
        )

    # 4) tokens defined but never used are dead weight, not a failure — report only
    unused = sorted(t for t in tokens if f"var({t})" not in css and t != "--room-color")

    print(f"server room keys : {len(server_keys)} -> {sorted(server_keys)}")
    print(f"css selectors    : {len(selectors)} -> {sorted(selectors)}")
    print(f"css room tokens  : {len(tokens)}")
    if unused:
        print(f"note: unused tokens (not an error) -> {unused}")

    if problems:
        print()
        print("FAIL: cross-boundary inconsistency detected")
        for p in problems:
            print("  - " + p)
        return 1

    print()
    print("OK: every server room key has a working colour selector")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
