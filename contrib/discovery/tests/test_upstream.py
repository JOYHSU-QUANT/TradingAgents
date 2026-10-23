"""The borrow is funnelled through ``upstream.py``, reaches two packages, and the edge never points back.

The parsed-import scan ``contrib.autoresearch.tests.test_upstream`` and
``contrib.replay.tests.test_upstream`` run, with the rules the discovery
plan adds (§1 #18, §2, §7): this package may import ``contrib.autoresearch``
and ``contrib.core`` and nothing else under ``contrib/`` — nor the engine
``tradingagents`` or the venue SDK, which its tests may not reach either —
and NEITHER of the two packages it borrows from, nor their tests, may
import ``contrib.discovery``. Read from the import graph, not searched for
as a string, so the prose discussing the forbidden packages does not trip
it and a real import cannot hide behind a relative spelling.
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

from contrib import autoresearch, core
from contrib.discovery import upstream

_PACKAGE = Path(upstream.__file__).resolve().parent
_CONTRIB = _PACKAGE.parent

# What this package must never reach, from a source OR a test: every other
# contrib package, the engine and the message types it speaks, and the
# venue SDK (plan §2: only the LLM arm talks to a model, and it does so
# through the research package's own hypothesist).
_FORBIDDEN = (
    "contrib.carry",
    "contrib.dashboard",
    "contrib.hyperliquid_perp",
    "contrib.replay",
    "contrib.uniswap_v3",
    "tradingagents",
    "langchain",
    "langchain_core",
    "hyperliquid",
)


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


def test_the_two_upstream_packages_are_the_plan_s():
    assert upstream.UPSTREAM_PACKAGES == ("contrib.autoresearch", "contrib.core")


@pytest.mark.parametrize(("module_name", "attribute"), upstream.BORROWED)
def test_every_borrowed_name_still_exists_upstream(module_name, attribute):
    assert hasattr(importlib.import_module(module_name), attribute)


def test_re_exports_are_the_upstream_objects_themselves():
    for module_name, attribute in upstream.BORROWED:
        assert getattr(upstream, attribute) is getattr(
            importlib.import_module(module_name), attribute
        )


def test_every_declared_borrow_is_inside_a_declared_package():
    assert all(_is_upstream(module_name) for module_name, _ in upstream.BORROWED)


def test_only_upstream_py_imports_an_upstream_package():
    offenders = {
        path.name
        for path in _sources(_PACKAGE, include_tests=False)
        if path.name != "upstream.py"
        and any(
            _is_upstream(name)
            for name in _imported_modules(path, _PACKAGE, "contrib.discovery")
        )
    }
    assert offenders == set()


def test_upstream_imports_nothing_it_has_not_declared():
    declared = {module_name for module_name, _ in upstream.BORROWED}
    imported = {
        name
        for name in _imported_modules(_PACKAGE / "upstream.py", _PACKAGE, "contrib.discovery")
        if _is_upstream(name)
    }
    assert imported == declared


def test_all_lists_exactly_the_borrowed_names_and_the_tables():
    assert set(upstream.__all__) == {
        "BORROWED",
        "UPSTREAM_PACKAGES",
        *(attribute for _, attribute in upstream.BORROWED),
    }


def test_every_borrowed_name_is_used_by_some_module():
    """``BORROWED`` is an audit list: an entry nothing reads makes it lie.

    Matched on word boundaries: ``Trial`` is a substring of ``TrialRecord``,
    and a plain ``in`` would keep a dropped borrow green.
    """
    sources = [p for p in _sources(_PACKAGE, include_tests=False) if p.name != "upstream.py"]
    text = "\n".join(p.read_text(encoding="utf-8") for p in sources)
    unused = {
        name
        for _, name in upstream.BORROWED
        if re.search(rf"\b{re.escape(name)}\b", text) is None
    }
    assert unused == set()


# -- what is never reached ------------------------------------------------------------


def test_no_source_or_test_reaches_a_forbidden_package():
    offenders = {
        (path.relative_to(_PACKAGE).as_posix(), name)
        for path in _sources(_PACKAGE, include_tests=True)
        for name in _imported_modules(path, _PACKAGE, "contrib.discovery")
        if any(_is_within(name, forbidden) for forbidden in _FORBIDDEN)
    }
    assert offenders == set()


def test_the_scan_has_sources_to_walk():
    """Guard the scans above: an empty walk would pass them all vacuously."""
    names = {path.name for path in _sources(_PACKAGE, include_tests=False)}
    assert names >= {"upstream.py", "records.py", "manifest.py", "verdict.py", "oracle.py"}


# -- the reverse edge ----------------------------------------------------------


@pytest.mark.parametrize(
    ("package", "name"),
    [(autoresearch, "contrib.autoresearch"), (core, "contrib.core")],
    ids=["autoresearch", "core"],
)
def test_neither_upstream_package_imports_this_one(package, name):
    package_dir = Path(package.__file__).resolve().parent
    sources = _sources(package_dir, include_tests=True)
    assert sources, f"{name} has no sources to scan"
    offenders = {
        str(path.relative_to(_CONTRIB))
        for path in sources
        if any(
            _is_within(m, "contrib.discovery")
            for m in _imported_modules(path, package_dir, name)
        )
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
