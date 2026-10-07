"""The borrow is funnelled through ``upstream.py``, and the edge never points back.

The same parsed-import scan ``contrib.replay`` and ``contrib.autoresearch``
run, with this package's own rules (carry plan §1.2): it may import its two
upstream packages and no other package under ``contrib/``; NO package under
``contrib/`` — nor any package's tests — may import ``contrib.carry``; and
``contrib.uniswap_v3`` is never imported, not even by a test, because the
spot leg is reached through its store and the handoff alone. Read from the
import graph, not searched for as a string.
"""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from contrib.carry import upstream

_PACKAGE = Path(upstream.__file__).resolve().parent
_CONTRIB = _PACKAGE.parent
_NAME = "contrib.carry"


def _sources(package_dir: Path, *, include_tests: bool) -> list[Path]:
    return sorted(
        path
        for path in package_dir.rglob("*.py")
        if "__pycache__" not in path.parts and (include_tests or "tests" not in path.parts)
    )


def _imported_modules(path: Path, package_dir: Path, package_name: str) -> set[str]:
    """Every module name ``path`` imports, relative imports resolved against its package."""
    package_parts = package_name.split(".") + list(path.relative_to(package_dir).parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
            else:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                found.add(".".join([*base, node.module] if node.module else base))
    return found


def _is_within(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def _is_upstream(name: str) -> bool:
    return any(_is_within(name, package) for package in upstream.UPSTREAM_PACKAGES)


def _neighbours() -> list[Path]:
    return sorted(
        child
        for child in _CONTRIB.iterdir()
        if child.is_dir() and (child / "__init__.py").exists() and child != _PACKAGE
    )


def test_the_scan_has_sources_to_walk():
    assert len(_sources(_PACKAGE, include_tests=False)) >= 7
    names = {n.name for n in _neighbours()}
    assert names >= {"hyperliquid_perp", "autoresearch", "replay", "uniswap_v3"}


@pytest.mark.parametrize(("module_name", "attribute"), upstream.BORROWED + upstream.VENUE_BORROWED)
def test_every_borrowed_name_still_exists_upstream(module_name, attribute):
    assert hasattr(importlib.import_module(module_name), attribute)


@pytest.mark.parametrize(("module_name", "attribute"), upstream.BORROWED)
def test_re_exports_are_the_upstream_objects_themselves(module_name, attribute):
    assert getattr(upstream, attribute) is getattr(importlib.import_module(module_name), attribute)


def test_all_lists_every_borrow_and_only_the_borrows_and_the_funnels_own_names():
    own = {"BORROWED", "UPSTREAM_PACKAGES", "VENUE_BORROWED", "load_market"}
    assert set(upstream.__all__) == own | {attribute for _, attribute in upstream.BORROWED}


def test_every_declared_borrow_is_inside_a_declared_package():
    declared = upstream.BORROWED + upstream.VENUE_BORROWED
    assert all(_is_upstream(module_name) for module_name, _ in declared)


def test_every_borrow_is_used_by_some_module_of_the_package():
    """A name kept alive in three lists for nothing is a borrow to drop.

    A test counts as a user: the sample floor is borrowed so the suite can
    pin the reading's behaviour at the perp package's own threshold. Matched
    as a whole word, so ``from_epoch_ms`` does not stand in for ``epoch_ms``.
    """
    bodies = [
        path.read_text(encoding="utf-8")
        for path in _sources(_PACKAGE, include_tests=True)
        if path.name not in ("upstream.py", "test_upstream.py")
    ]
    unused = {
        attribute
        for _, attribute in upstream.BORROWED
        if not any(re.search(rf"\b{re.escape(attribute)}\b", body) for body in bodies)
    }
    assert unused == set()


def test_only_upstream_py_imports_an_upstream_package():
    offenders = {
        path.name
        for path in _sources(_PACKAGE, include_tests=False)
        if path.name != "upstream.py"
        and any(_is_upstream(name) for name in _imported_modules(path, _PACKAGE, _NAME))
    }
    assert offenders == set()


def test_upstream_imports_exactly_what_it_declares():
    tree = ast.parse((_PACKAGE / "upstream.py").read_text(encoding="utf-8"))
    at_module_level: set[tuple[str, str]] = set()
    inside_load_market: set[tuple[str, str]] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and _is_upstream(node.module):
            at_module_level.update((node.module, alias.name) for alias in node.names)
        if isinstance(node, ast.FunctionDef) and node.name == "load_market":
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.module:
                    inside_load_market.update((inner.module, alias.name) for alias in inner.names)
    assert at_module_level == set(upstream.BORROWED)
    assert inside_load_market == set(upstream.VENUE_BORROWED)


def test_no_module_of_this_package_imports_a_neighbour_that_is_not_upstream():
    """Every package found on disk that is not an upstream is off limits, tests included."""
    forbidden = [
        f"contrib.{n.name}"
        for n in _neighbours()
        if f"contrib.{n.name}" not in upstream.UPSTREAM_PACKAGES
    ]
    assert {"contrib.uniswap_v3", "contrib.replay"} <= set(forbidden)
    offenders = {
        path.name
        for path in _sources(_PACKAGE, include_tests=True)
        if any(
            _is_within(name, never)
            for name in _imported_modules(path, _PACKAGE, _NAME)
            for never in forbidden
        )
    }
    assert offenders == set()


@pytest.mark.parametrize("neighbour", _neighbours(), ids=lambda p: p.name)
def test_no_neighbour_imports_this_package(neighbour: Path):
    # Only a source whose text holds the word can import the package: the AST walk
    # is kept for those, and the hundreds of files that never say it are not parsed.
    offenders = {
        str(path.relative_to(_CONTRIB))
        for path in _sources(neighbour, include_tests=True)
        if "carry" in path.read_text(encoding="utf-8")
        and any(
            _is_within(name, _NAME)
            for name in _imported_modules(path, neighbour, f"contrib.{neighbour.name}")
        )
    }
    assert offenders == set()


def test_importing_every_module_leaves_the_venue_sdk_unloaded():
    modules = [
        f"{_NAME}.{path.stem}"
        for path in _sources(_PACKAGE, include_tests=False)
        if path.stem != "__init__"
    ]
    script = (
        "import importlib, sys; "
        f"[importlib.import_module(m) for m in {modules!r}]; "
        "loaded = [m for m in sys.modules if m == 'hyperliquid' or m.startswith('hyperliquid.')]; "
        "print(loaded)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=_CONTRIB.parent,
    )
    assert result.stdout.strip() == "[]"
