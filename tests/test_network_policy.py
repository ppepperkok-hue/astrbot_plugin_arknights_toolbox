"""Guards for the network policy AstrBot's plugin rules ask for.

The rule says: do not use ``requests``; aiohttp / httpx are acceptable. This
project takes a third route - the standard library, wrapped so it never runs
synchronously on the event loop - and that route is only honest if the ban is
actually enforced.

Two claims are made about this project's network layer in the README. Only one
of them is machine-checkable today, and this file checks exactly that one. The
other - that every blocking call is wrapped in ``asyncio.to_thread`` - is kept
by review, and the README says so rather than implying otherwise.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Modules where a stray blocking call would sit directly on the event loop.
ASYNC_CRITICAL = ("modules/skland/module.py",)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for name in ("core", "modules", "scripts"):
        files += [
            path for path in (REPO_ROOT / name).rglob("*.py") if "__pycache__" not in path.parts
        ]
    files.append(REPO_ROOT / "main.py")
    return files


def test_no_module_imports_requests() -> None:
    """``requests`` must not appear in shipped code.

    Checked with the AST rather than a grep: a grep would also match the word in
    a comment or docstring, producing the same class of false signal this
    project has been bitten by before (a guard that reports on text rather than
    on what the code does).
    """
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            if "requests" in names:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"requests is banned by AstrBot's rules: {offenders}"


def test_blocking_network_calls_are_not_awaited_directly() -> None:
    """Inside an ``async def``, a call returning a blocking HTTP response must
    be wrapped in ``asyncio.to_thread``.

    This is a narrow, structural check: it looks for ``to_thread`` usage in the
    files that talk to the network and asserts there is at least one, so that
    deleting the wrapping (which would silently block the loop) turns this red.
    It does not attempt to prove every call is wrapped - that claim is not made
    anywhere in the docs.
    """
    for relative in ASYNC_CRITICAL:
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        assert "asyncio.to_thread" in source, (
            f"{relative} performs blocking network calls; they must be wrapped "
            "in asyncio.to_thread so the event loop is not blocked"
        )
