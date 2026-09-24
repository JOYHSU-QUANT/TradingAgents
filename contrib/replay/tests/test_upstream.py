"""The borrow is funnelled through ``upstream.py``, and the edge never points back.

The same parsed-import scan ``contrib.autoresearch.tests.test_upstream``
runs, extended with the one rule this package adds to the layering
(replay plan §3-1): ``contrib.replay`` may import both neighbours, and
NEITHER neighbour — nor either's tests — may import ``contrib.replay``.
Read from the import graph, not searched for as a string, so the prose
that discusses the packages does not trip it and a real import cannot hide
behind a relative spelling.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from contrib import autoresearch, hyperliquid_perp
from contrib.replay import upstream

_PACKAGE = Path(upstream.__file__).resolve().parent
_CONTRIB = _PACKAGE.parent


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


# -- the borrow ---------------------------------------------------------------


@pytest.mark.parametrize(("module_name", "attribute"), upstream.BORROWED)
def test_every_borrowed_name_still_exists_upstream(module_name, attribute):
    assert hasattr(importlib.import_module(module_name), attribute)


def test_re_exports_are_the_upstream_objects_themselves():
    for module_name, attribute in upstream.BORROWED:
        assert getattr(upstream, attribute) is getattr(importlib.import_module(module_name), attribute)


def test_every_declared_borrow_is_inside_a_declared_package():
    assert all(_is_upstream(module_name) for module_name, _ in upstream.BORROWED)


def test_only_upstream_py_imports_a_neighbour():
    offenders = {
        path.name
        for path in _sources(_PACKAGE, include_tests=False)
        if path.name != "upstream.py"
        and any(_is_upstream(name) for name in _imported_modules(path, _PACKAGE, "contrib.replay"))
    }
    assert offenders == set()


def test_upstream_imports_nothing_it_has_not_declared():
    declared = {module_name for module_name, _ in (*upstream.BORROWED, *upstream.ENGINE_BORROWED)}
    imported = {
        name
        for name in _imported_modules(_PACKAGE / "upstream.py", _PACKAGE, "contrib.replay")
        if _is_upstream(name) or _is_engine(name)
    }
    assert imported == declared


# -- the engine half, borrowed lazily ----------------------------------------------

# The packages the engine half reaches: the upstream engine and the message
# types it speaks. Named here so the scan can hold every other module off them.
_ENGINE_PACKAGES = ("tradingagents", "langchain_core", "langchain")


def _is_engine(name: str) -> bool:
    return any(_is_within(name, package) for package in _ENGINE_PACKAGES)


@pytest.mark.parametrize(("module_name", "attribute"), upstream.ENGINE_BORROWED)
def test_every_engine_borrow_still_exists(module_name, attribute):
    assert hasattr(importlib.import_module(module_name), attribute)


def test_the_engine_surface_has_one_field_per_engine_borrow():
    fields = set(upstream.Engine.__dataclass_fields__)
    assert fields == {attribute for _, attribute in upstream.ENGINE_BORROWED}


def test_only_upstream_py_reaches_the_engine():
    offenders = {
        path.name
        for path in _sources(_PACKAGE, include_tests=False)
        if path.name != "upstream.py"
        and any(_is_engine(name) for name in _imported_modules(path, _PACKAGE, "contrib.replay"))
    }
    assert offenders == set()


def test_importing_every_module_leaves_the_engine_unloaded():
    """The engine half is loaded by ``load_engine`` alone: a scorecard never pays for it."""
    modules = sorted(
        f"contrib.replay.{path.stem}"
        for path in _sources(_PACKAGE, include_tests=False)
        if path.stem not in ("__init__", "__main__")
    )
    code = "; ".join(
        [
            "import sys",
            *(f"import {name}" for name in modules),
            f"print(sorted(m for m in sys.modules if m.split('.')[0] in {_ENGINE_PACKAGES!r}))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=_CONTRIB.parent,
    )
    assert result.stdout.strip() == "[]"


# -- the reverse edge ----------------------------------------------------------


@pytest.mark.parametrize(
    ("package", "name"),
    [(hyperliquid_perp, "contrib.hyperliquid_perp"), (autoresearch, "contrib.autoresearch")],
    ids=["hyperliquid_perp", "autoresearch"],
)
def test_neither_neighbour_imports_this_package(package, name):
    package_dir = Path(package.__file__).resolve().parent
    sources = _sources(package_dir, include_tests=True)
    assert sources, f"{name} has no sources to scan"
    offenders = {
        str(path.relative_to(_CONTRIB))
        for path in sources
        if any(_is_within(m, "contrib.replay") for m in _imported_modules(path, package_dir, name))
    }
    assert offenders == set()


def test_the_scan_sees_an_absolute_and_a_relative_reach(tmp_path):
    package_dir = tmp_path / "pkg"
    (package_dir / "sub").mkdir(parents=True)
    (package_dir / "sub" / "a.py").write_text(
        "import contrib.replay.score\nfrom ...replay import cli\n", encoding="utf-8"
    )
    found = _imported_modules(package_dir / "sub" / "a.py", package_dir, "contrib.pkg")
    assert found == {"contrib.replay.score", "contrib.replay"}


def test_all_lists_exactly_the_borrowed_names_and_the_tables():
    """The literal ``__all__`` and ``BORROWED`` are two spellings of one list; hold them equal.

    Plus the tables themselves and the lazy half's two names (the surface
    type and the function that loads it).
    """
    assert set(upstream.__all__) == {
        "BORROWED",
        "ENGINE_BORROWED",
        "UPSTREAM_PACKAGES",
        "Engine",
        "load_engine",
        *(n for _, n in upstream.BORROWED),
    }


def test_every_borrowed_name_is_used_by_some_module():
    """``BORROWED`` is an audit list: an entry nothing reads makes it lie."""
    sources = [p for p in _sources(_PACKAGE, include_tests=False) if p.name != "upstream.py"]
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    unused = {name for _, name in upstream.BORROWED if name not in text}
    assert unused == set()
