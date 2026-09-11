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
import subprocess
import sys
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
    """Guard every scan below: an empty walk would pass them all vacuously.

    An inventory rather than a count, and it has to grow with the package —
    the three modules most likely to reach upstream are the ones that compute
    things, and they joined after this list was first written.
    """
    assert {p.name for p in _SOURCES} >= {
        "upstream.py",
        "store.py",
        "fetch.py",
        "gaps.py",
        "cli.py",
        "ports.py",
        "vocabulary.py",
        "features.py",
        "dsl.py",
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
            # The lazily-imported names: the exchange reader, and the three
            # analytics that drag pandas in. They are covered by
            # ``test_pins.py``, which asserts the same identity on what
            # ``context_analytics()`` returns — NOT by the audit test above,
            # which only asks whether the name still exists upstream.
            continue
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


def test_the_commands_that_touch_no_indicator_do_not_load_the_indicator_stack():
    """The deferred imports, checked by what is actually in ``sys.modules``.

    Two of the borrowed names are built inside a call rather than imported at
    module scope — the exchange reader (the Hyperliquid SDK) and the three
    analytics (pandas and stockstats, 511 ms against 57 ms for the whole store
    layer). That arrangement is published in the README and the module
    docstrings as a measured fact, and nothing enforced it: adding one
    ``from .features import ...`` line to ``cli.py`` made ``gaps`` pay the
    pandas cost with every test still green.

    Checked in a SUBPROCESS because this one cannot be undone in-process —
    by the time the suite runs, ``test_features`` has imported pandas for its
    own reasons, and ``sys.modules`` never forgets.
    """
    probe = (
        "import sys; import contrib.autoresearch.cli as cli; "
        "from contrib.autoresearch.cli import main; main(['vocab']); "
        "heavy = sorted(m for m in ('pandas', 'stockstats', 'hyperliquid') if m in sys.modules); "
        "print(heavy)"
    )
    root = Path(upstream.__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=root,
        check=True,
    )
    assert result.stdout.strip().endswith("[]"), result.stdout


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
