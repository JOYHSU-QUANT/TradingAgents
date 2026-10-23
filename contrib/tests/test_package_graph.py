"""The import graph BETWEEN the packages under ``contrib/``, read off the sources.

Each package already guards its own side: the perp package its internal
layers (``hyperliquid_perp/tests/common/test_layering.py``), each borrowing
package the funnel through its ``upstream.py``, uniswap its isolation. None
of them sees the whole graph, so nothing stopped a NEW edge between two
packages that neither one's own test was written about. This file holds
that graph (dashboard plan PR 1, §2):

- every cross-package import points down an edge in :data:`ALLOWED`, and
  :data:`ALLOWED` itself has no cycle — a package found on disk without a
  row fails, so the next package (the dashboard) declares where it sits
  before it can import anything, and a directory of sources without an
  ``__init__.py`` fails too, rather than go unseen;
- ``contrib.core``, the floor, imports the standard library only;
- how far each package reaches INTO the perp package — which of its
  subpackages it imports, from its sources and from its tests — is frozen
  as of 2026-10-12 and compared by equality, so a new reach fails and a
  retired one has to be pruned here in the PR that pays it off.

Read from the import statements at any depth (a lazy import inside a
function is the same dependency), relative imports resolved against the
file's own package. A dynamic import (``import_module`` or ``__import__``)
is read when its module name is a string literal, as ``uniswap_v3``'s
isolation test reads it; one built at run time is not seen.

This directory deliberately has no ``__init__.py``: the packages' own tests
discover their neighbours as the directories under ``contrib/`` that have
one, and this file belongs to none of them.
"""

from __future__ import annotations

import ast
import functools
import graphlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_CONTRIB = Path(__file__).resolve().parents[1]

# Which packages each package may import (beyond itself). A ceiling: a
# package's own test may be stricter — ``uniswap_v3``'s isolation test still
# refuses every contrib import, ``contrib.core`` included, until a PR that
# adopts the floor relaxes both. ``replay`` sits ABOVE ``autoresearch``
# (it scores with that package's cost model and split); ``carry`` borrows
# from ``autoresearch`` and the perp package but from neither ``replay`` nor
# ``uniswap_v3`` — its spot leg is driven through files, not imports;
# ``dashboard`` runs the other packages' commands and reads what they print.
ALLOWED: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "hyperliquid_perp": frozenset({"core"}),
    "uniswap_v3": frozenset({"core"}),
    "autoresearch": frozenset({"core", "hyperliquid_perp"}),
    "replay": frozenset({"core", "hyperliquid_perp", "autoresearch"}),
    "carry": frozenset({"core", "hyperliquid_perp", "autoresearch"}),
    # The exporter reads the three packages' documents through their command
    # lines, never their modules: the floor is all it imports.
    "dashboard": frozenset({"core"}),
    # The benchmark of the research loop (discovery plan §2): it runs that
    # package's search over synthetic worlds through its two ports, and
    # reaches no trading package — its own ``tests/test_upstream.py`` also
    # holds its tests off them, and the engine.
    "discovery": frozenset({"core", "autoresearch"}),
}

_PERP = "hyperliquid_perp"
# The package root itself (``from contrib import hyperliquid_perp``), as an area.
_ROOT_AREA = "<package>"

# Which parts of the perp package each other package reaches, measured on
# 2026-10-12 after ``contrib.core`` took the strategy-free helpers out of
# ``common``. ``(package, "src" | "tests", area)``.
_PERP_REACH = frozenset(
    {
        ("autoresearch", "src", "domains"),
        ("autoresearch", "src", "exchanges"),
        ("autoresearch", "tests", "domains"),
        ("autoresearch", "tests", "paper"),
        ("carry", "src", "common"),
        ("carry", "src", "domains"),
        ("carry", "src", "exchanges"),
        ("carry", "src", "persistence"),
        ("carry", "tests", "persistence"),
        ("replay", "src", "common"),
        ("replay", "src", "domains"),
        ("replay", "src", "integration"),
        ("replay", "src", "paper"),
        ("replay", "src", "persistence"),
        ("replay", "tests", _ROOT_AREA),
        ("replay", "tests", "domains"),
        ("replay", "tests", "engine_bridge"),
        ("replay", "tests", "integration"),
        ("replay", "tests", "persistence"),
    }
)


def _packages_on_disk(contrib: Path = _CONTRIB) -> list[str]:
    return sorted(p.name for p in contrib.iterdir() if (p / "__init__.py").is_file())


def _sources(package_dir: Path) -> Iterator[Path]:
    for path in sorted(package_dir.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def _is_dynamic_import(func: ast.expr) -> bool:
    """``__import__(...)``, ``import_module(...)`` or ``<anything>.import_module(...)``."""
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _dynamic_import_name(node: ast.AST) -> str | None:
    """The module a dynamic import names as a string literal, else ``None``."""
    if (
        isinstance(node, ast.Call)
        and _is_dynamic_import(node.func)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
    return None


def _imported_names(path: Path, contrib: Path = _CONTRIB) -> set[str]:
    """Every absolute dotted name ``path`` imports, at any depth.

    ``from a import b`` gives ``a.b`` whether ``b`` is a module or a name in
    one, so ``from contrib import replay`` lands inside ``contrib.replay``
    exactly as ``from contrib.replay import x`` does. Relative imports are
    resolved against the file's own package, ``contrib.<pkg>.<sub>...``; a
    dynamic import is taken by its literal name (:func:`_dynamic_import_name`).
    """
    own = ["contrib", *path.relative_to(contrib).parent.parts]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = own[: len(own) - (node.level - 1)]
                base = ".".join([*parts, node.module] if node.module else parts)
            found.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        elif (name := _dynamic_import_name(node)) is not None:
            found.add(name)
    return found


def _contrib_target(name: str) -> tuple[str, str] | None:
    """``(package, area)`` for a name under ``contrib.``; ``None`` for anything else.

    The area is the package's first-level child (``domains``, ``persistence``,
    ``engine_bridge``) or :data:`_ROOT_AREA` for the package itself.
    """
    parts = name.split(".")
    if parts[0] != "contrib" or len(parts) < 2:
        return None
    return parts[1], parts[2] if len(parts) > 2 else _ROOT_AREA


@functools.cache
def _cross_package_imports(contrib: Path = _CONTRIB) -> tuple[tuple[str, str, str, str, str], ...]:
    """``(importer, "src"|"tests", file, target package, target area)`` per cross-package import.

    Cached per root: two checks read the same scan, so the tree is parsed once.
    """
    found = []
    for package in _packages_on_disk(contrib):
        for path in _sources(contrib / package):
            role = "tests" if "tests" in path.relative_to(contrib / package).parts else "src"
            for name in sorted(_imported_names(path, contrib)):
                target = _contrib_target(name)
                if target is not None and target[0] != package:
                    rel = path.relative_to(contrib).as_posix()
                    found.append((package, role, rel, target[0], target[1]))
    return tuple(found)


# -- the graph -----------------------------------------------------------------


def test_every_package_on_disk_declares_where_it_sits():
    assert set(_packages_on_disk()) == set(ALLOWED), (
        "give each package under contrib/ a row in ALLOWED (and drop the row of one "
        f"that is gone): on disk {_packages_on_disk()}, declared {sorted(ALLOWED)}"
    )


def test_every_directory_with_sources_is_a_package():
    # The scan finds packages by their ``__init__.py``, as the packages' own
    # neighbour discovery does. A directory of sources without one would be
    # importable as a namespace package and invisible to every check here, so
    # it is refused by name instead. This directory is the one exception
    # (``__pycache__`` holds no sources).
    unpackaged = sorted(
        p.name
        for p in _CONTRIB.iterdir()
        if p.is_dir()
        and p.name not in {"tests", "__pycache__"}
        and not (p / "__init__.py").is_file()
        and any(p.rglob("*.py"))
    )
    assert unpackaged == [], f"give these contrib/ directories an __init__.py: {unpackaged}"


def test_the_declared_graph_has_no_cycle():
    # ``prepare`` raises ``CycleError`` naming the cycle.
    graphlib.TopologicalSorter(ALLOWED).prepare()


def test_every_declared_edge_names_a_declared_package():
    stray = {(p, t) for p, targets in ALLOWED.items() for t in targets if t not in ALLOWED}
    assert stray == set()


def test_every_cross_package_import_points_down_a_declared_edge():
    offenders = sorted(
        f"{rel}: imports contrib.{target}"
        for package, _role, rel, target, _area in _cross_package_imports()
        if target not in ALLOWED.get(package, frozenset())
    )
    assert offenders == [], offenders


# -- the floor ---------------------------------------------------------------------

_STDLIB = frozenset(sys.stdlib_module_names)


def _core_source_imports(contrib: Path = _CONTRIB) -> Iterator[tuple[str, str]]:
    """``(file, top-level module)`` for every import in core's sources, tests excluded.

    Read through :func:`_imported_names`, so a relative import resolves as
    the graph check resolves it: a sibling inside core is core's own and is
    skipped, and climbing out of core lands in ``contrib``.
    """
    core = contrib / "core"
    for path in _sources(core):
        if "tests" in path.relative_to(core).parts:
            continue
        rel = path.relative_to(contrib).as_posix()
        for name in sorted(_imported_names(path, contrib)):
            if not (name == "contrib.core" or name.startswith("contrib.core.")):
                yield rel, name.split(".")[0]


def test_core_imports_the_standard_library_only():
    # Anything core imports must ship with Python, so every package can
    # afford core and none of them inherits a dependency through it. Its
    # tests may import pytest; the graph check above holds them off every
    # other contrib package.
    offenders = sorted(f"{rel}: {top}" for rel, top in _core_source_imports() if top not in _STDLIB)
    assert offenders == [], offenders


# -- how far each package reaches into the perp package --------------------------


def test_the_reach_into_the_perp_package_is_exactly_what_it_was_on_2026_10_12():
    # Equality, as the perp package's own ratchets: a new subpackage reached
    # fails (move the helper down into ``contrib.core``, or borrow through a
    # subpackage already reached), and so does one no longer reached, so this
    # list is pruned in the PR that pays the reach off.
    found = {
        (package, role, area)
        for package, role, _rel, target, area in _cross_package_imports()
        if target == _PERP
    }
    assert found == _PERP_REACH, (
        f"new {sorted(found - _PERP_REACH)}, retired {sorted(_PERP_REACH - found)}"
    )


# -- the scanners themselves -----------------------------------------------------------


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_scan_resolves_every_import_shape(tmp_path):
    for pkg in ("alpha", "beta", "beta/inner", "gamma"):
        _write(tmp_path, f"{pkg}/__init__.py")
    _write(
        tmp_path,
        "alpha/mod.py",
        "import contrib.beta.inner\n"
        "from contrib import gamma\n"
        "from .. import beta\n"
        "from ..gamma.x import y\n"
        "from . import sibling\n"
        "def f():\n"
        "    from contrib.beta.deep import z\n"
        "    importlib.import_module('contrib.gamma.lazy')\n"
        "    __import__('contrib.beta.dunder')\n"
        "    importlib.import_module(computed)\n",
    )
    found = {
        (target, area)
        for package, _role, _rel, target, area in _cross_package_imports(tmp_path)
        if package == "alpha"
    }
    assert found == {
        ("beta", "inner"),
        ("gamma", _ROOT_AREA),
        ("beta", _ROOT_AREA),
        ("gamma", "x"),
        ("beta", "deep"),
        ("gamma", "lazy"),
        ("beta", "dunder"),
    }


def test_the_scan_tells_tests_from_sources(tmp_path):
    for pkg in ("alpha", "alpha/tests", "beta"):
        _write(tmp_path, f"{pkg}/__init__.py")
    _write(tmp_path, "alpha/a.py", "from contrib.beta.src_only import x\n")
    _write(tmp_path, "alpha/tests/test_a.py", "from contrib.beta.tests_only import x\n")
    found = {(role, area) for _p, role, _rel, _t, area in _cross_package_imports(tmp_path)}
    assert found == {("src", "src_only"), ("tests", "tests_only")}


@pytest.mark.parametrize(
    ("text", "offending"),
    [
        ("import os\nfrom decimal import Context\nfrom . import sibling\n", set()),
        ("import numpy\n", {"numpy"}),
        ("from contrib.hyperliquid_perp import x\n", {"contrib"}),
        ("from ..hyperliquid_perp import x\n", {"contrib"}),
        ("def f():\n    import yaml\n", {"yaml"}),
        ("import importlib\nimportlib.import_module('numpy.linalg')\n", {"numpy"}),
    ],
)
def test_the_floor_scan_flags_anything_outside_the_standard_library(tmp_path, text, offending):
    _write(tmp_path, "core/__init__.py")
    _write(tmp_path, "core/helper.py", text)
    _write(tmp_path, "core/tests/test_helper.py", "import pytest\n")
    found = {top for _rel, top in _core_source_imports(tmp_path) if top not in _STDLIB}
    assert found == offending
