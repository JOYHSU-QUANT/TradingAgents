"""Guards for the package's three import rules, read off the sources.

- Isolation: nothing under ``contrib/uniswap_v3`` imports another package
  under ``contrib/``, and no other package there imports this one.
- Purity: ``domain/`` and ``ports.py`` import the standard library and each
  other only. CI type-checks them in a job that installs mypy alone.
- The engine: the ``tradingagents`` package is imported from ``agent/``
  and from the tests only, so that the judge is reached through the agent
  layer and nowhere else, and a command that asks no judge waits on none
  of the engine's dependencies.

All three read every import statement in a file, at any depth: a lazy import
inside a function and one under ``TYPE_CHECKING`` are the same dependency
here. A dynamic import (``import_module`` or ``__import__``) is read when
its module name is a string literal; one built at run time is not seen.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OWN = "contrib.uniswap_v3"
_PURE = (f"{_OWN}.domain", f"{_OWN}.ports")
_ENGINE = "tradingagents"
# Where the engine may be imported from.
_ENGINE_GATES = (f"{_OWN}.agent", f"{_OWN}.tests")
# Every other package under ``contrib/``, found on disk so that one added
# later is guarded without being listed here.
_NEIGHBOURS = sorted(
    f"contrib.{path.name}"
    for path in (_REPO_ROOT / "contrib").iterdir()
    if (path / "__init__.py").is_file() and f"contrib.{path.name}" != _OWN
)


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _sources(package: str, root: Path = _REPO_ROOT) -> list[Path]:
    """Every ``.py`` file of the dotted ``package`` (a directory or a single module)."""
    path = root.joinpath(*package.split("."))
    return sorted(path.rglob("*.py")) if path.is_dir() else [path.with_suffix(".py")]


def _imports(source: Path, root: Path = _REPO_ROOT) -> set[str]:
    """The absolute dotted name of everything ``source`` imports.

    ``import a.b`` gives ``a.b``. ``from a import b`` gives ``a.b``, whether
    ``b`` is a module or a name inside one, so ``from contrib import replay``
    and ``from contrib.replay import x`` both land inside ``contrib.replay``.
    A relative import is resolved against the file's own package first.
    """
    package = source.relative_to(root).parent.parts
    found: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # One dot is the file's own package; each further dot climbs one.
            base = package[: max(0, len(package) - (node.level - 1))] if node.level else ()
            module = ".".join([*base, *([node.module] if node.module else [])])
            found.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and _is_dynamic_import(node.func)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _is_dynamic_import(func: ast.expr) -> bool:
    """``__import__(...)``, ``import_module(...)`` or ``<anything>.import_module(...)``."""
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return isinstance(func, ast.Attribute) and func.attr == "import_module"


def _offenders(package: str, is_offender) -> list[tuple[str, str]]:
    sources = _sources(package)
    # An empty list would pass any check below without having read anything.
    assert sources, f"{package} has no sources to read"
    return sorted(
        (source.relative_to(_REPO_ROOT).as_posix(), name)
        for source in sources
        for name in _imports(source)
        if is_offender(name)
    )


def test_the_neighbours_found_on_disk_include_the_three_known_ones():
    assert {"contrib.hyperliquid_perp", "contrib.autoresearch", "contrib.replay"} <= set(_NEIGHBOURS)


def test_the_package_imports_no_other_contrib_package():
    offenders = _offenders(_OWN, lambda name: _within(name, "contrib") and not _within(name, _OWN))
    assert not offenders, f"contrib/uniswap_v3 reaches into another contrib package: {offenders}"


@pytest.mark.parametrize("neighbour", _NEIGHBOURS)
def test_no_neighbour_imports_the_package(neighbour):
    offenders = _offenders(neighbour, lambda name: _within(name, _OWN))
    assert not offenders, f"{neighbour} imports contrib/uniswap_v3: {offenders}"


def test_the_pure_layers_import_only_the_standard_library_and_each_other():
    def impure(name: str) -> bool:
        if any(_within(name, pure) for pure in _PURE):
            return False
        return name.split(".")[0] not in sys.stdlib_module_names

    offenders = [found for pure in _PURE for found in _offenders(pure, impure)]
    assert not offenders, f"domain/ and ports.py import beyond the standard library: {offenders}"


def test_the_engine_is_imported_from_the_agent_layer_and_the_tests_only():
    offenders = [
        (source, name)
        for source, name in _offenders(_OWN, lambda name: _within(name, _ENGINE))
        if not any(
            source.startswith(gate.replace(".", "/") + "/") for gate in _ENGINE_GATES
        )
    ]
    assert not offenders, f"the engine is imported outside agent/ and tests/: {offenders}"
    # The rule guards something: the agent layer does import the engine.
    assert _offenders(f"{_OWN}.agent", lambda name: _within(name, _ENGINE))


def test_the_import_scan_resolves_every_shape(tmp_path):
    # The real sources use two or three of these shapes; the rest are pinned
    # here so that a rule the scan stopped seeing fails in this test rather
    # than passing silently in the guards above.
    package = tmp_path / "contrib" / "uniswap_v3"
    (package / "domain").mkdir(parents=True)
    (package / "domain" / "m.py").write_text(
        "import os.path\n"
        "import contrib.replay.score as score\n"
        "from decimal import Decimal\n"
        "from . import types\n"
        "from .types import Bar\n"
        "from .. import ports\n"
        "from ..strategies.registry import build\n"
        "from ... import replay\n"
        "from ...replay.score import mark\n"
        "from contrib.autoresearch import split\n"
        "def lazy():\n"
        "    from contrib.hyperliquid_perp import config\n"
        "    importlib.import_module('contrib.replay.pool')\n"
        "    import_module('contrib.replay.probe')\n"
        "    __import__('contrib.autoresearch.dsl')\n"
        "    importlib.import_module(name)\n"
        "if TYPE_CHECKING:\n"
        "    from ...autoresearch.store import Store\n",
        encoding="utf-8",
    )
    assert _imports(package / "domain" / "m.py", root=tmp_path) == {
        "os.path",
        "contrib.replay.score",
        "decimal.Decimal",
        "contrib.uniswap_v3.domain.types",
        "contrib.uniswap_v3.domain.types.Bar",
        "contrib.uniswap_v3.ports",
        "contrib.uniswap_v3.strategies.registry.build",
        "contrib.replay",
        "contrib.replay.score.mark",
        "contrib.autoresearch.split",
        "contrib.hyperliquid_perp.config",
        "contrib.replay.pool",
        "contrib.replay.probe",
        "contrib.autoresearch.dsl",
        "contrib.autoresearch.store.Store",
    }
    # A package's ``__init__`` resolves one dot to the package itself.
    (package / "__init__.py").write_text(
        "from .domain import types\nfrom .. import replay\n", encoding="utf-8"
    )
    assert _imports(package / "__init__.py", root=tmp_path) == {
        "contrib.uniswap_v3.domain.types",
        "contrib.replay",
    }
    assert _sources("contrib.uniswap_v3", root=tmp_path) == [
        package / "__init__.py",
        package / "domain" / "m.py",
    ]
    assert _sources("contrib.uniswap_v3.domain.m", root=tmp_path) == [package / "domain" / "m.py"]
