"""The borrow from ``hyperliquid_perp`` is one-way, read-only, and funnelled.

Every check here reads the IMPORT GRAPH, parsed, rather than searching the
sources for a string. The difference is the whole value of the file: these
modules discuss the upstream package at length in their docstrings, so a text
scan would light up on prose and stay dark on the one line that matters — a
module that actually reaches upstream on its own. Parsed imports cannot be
fooled either way.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from contrib.autoresearch import upstream

_PACKAGE = Path(upstream.__file__).resolve().parent
_UPSTREAM_PACKAGE = "contrib.hyperliquid_perp"
_SOURCES = sorted(
    path
    for path in _PACKAGE.rglob("*.py")
    if "__pycache__" not in path.parts and "tests" not in path.parts
)


def _imported_modules(path: Path) -> set[str]:
    """Every module name ``path`` imports, absolute and relative alike.

    A relative import is resolved against this package, so ``from .upstream
    import Candle`` reads as ``contrib.autoresearch.upstream`` and can never
    be mistaken for a reach upstream — and an upstream import written
    relatively (this package is a sibling, so ``from ..hyperliquid_perp ...``
    is spellable) is resolved into exactly the name being looked for.
    """
    package_parts = ["contrib", "autoresearch"] + list(path.relative_to(_PACKAGE).parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                found.add(node.module or "")
            else:
                base = package_parts[: len(package_parts) - node.level + 1]
                found.add(".".join([*base, node.module] if node.module else base))
        elif isinstance(node, ast.Call):
            # A module name can also arrive as a STRING: importlib.import_module
            # and __import__ are imports that the import statements above cannot
            # see, so a single dynamic call would otherwise sail past the one
            # test enforcing the plan's one-way borrow. Only literal arguments
            # are readable here, which is the honest limit of a static scan --
            # a computed name would still escape, and that is worth knowing
            # rather than papering over.
            called = node.func
            name = getattr(called, "attr", None) or getattr(called, "id", None)
            if name in {"import_module", "__import__"}:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        found.add(argument.value)
    return found


def test_the_scan_has_sources_to_walk():
    """Guard every scan below: an empty walk would pass them all vacuously."""
    assert {p.name for p in _SOURCES} >= {
        "upstream.py",
        "store.py",
        "fetch.py",
        "gaps.py",
        "cli.py",
        "ports.py",
    }


@pytest.mark.parametrize(("module_name", "attribute"), upstream.BORROWED)
def test_every_borrowed_name_still_exists_upstream(module_name, attribute):
    module = importlib.import_module(module_name)
    assert hasattr(module, attribute), f"{module_name}.{attribute} is gone"


def test_re_exports_are_the_upstream_objects_themselves():
    """Identity, not equality: a local re-implementation would satisfy equality.

    The point of borrowing is that the research store and the paper run hold
    the SAME types, so a research result stays comparable with what the live
    path saw. A shim that merely looked alike would break that quietly.
    """
    for module_name, attribute in upstream.BORROWED:
        if not hasattr(upstream, attribute):
            continue  # the lazily-imported reader; the audit test above covers it
        assert getattr(upstream, attribute) is getattr(
            importlib.import_module(module_name), attribute
        )


def test_only_upstream_py_imports_the_perp_package():
    offenders = {
        path.relative_to(_PACKAGE).as_posix(): sorted(
            name for name in _imported_modules(path) if name.startswith(_UPSTREAM_PACKAGE)
        )
        for path in _SOURCES
        if path.name != "upstream.py"
    }
    reaching = {name: mods for name, mods in offenders.items() if mods}
    assert reaching == {}, f"these reach upstream directly instead of through upstream.py: {reaching}"


def test_upstream_imports_nothing_it_has_not_declared():
    """``BORROWED`` is the whole borrow, not a sample of it.

    ``upstream.py`` builds the exchange reader inside a function, so its
    module-level imports alone would under-report. The scan walks the whole
    tree, which is why the lazily-imported reader has to be declared too.
    """
    declared = {module for module, _attr in upstream.BORROWED}
    actual = {
        name
        for name in _imported_modules(_PACKAGE / "upstream.py")
        if name.startswith(_UPSTREAM_PACKAGE)
    }
    assert actual == declared


def test_the_borrow_reaches_no_persistence_module_of_the_perp_package():
    """Plan §3.2: read its domain types, never open its store.

    Stated against the module PATH rather than against a filename, because
    what is forbidden is the whole layer: ``persistence`` is where its
    connection, its migrations and its repository live, and borrowing any of
    them is how this package would end up holding a handle on a store a live
    paper run is writing to.
    """
    borrowed_layers = {module for module, _attr in upstream.BORROWED}
    forbidden = {m for m in borrowed_layers if ".persistence" in m or m.endswith(".persistence")}
    assert forbidden == set()
