"""Guards for the package's two import rules, read off the sources.

- Isolation: nothing under ``contrib/uniswap_v3`` imports another package
  under ``contrib/``, and none of the three neighbours imports this one.
- Purity: ``domain/`` and ``ports.py`` import the standard library and each
  other only. CI type-checks them in a job that installs mypy alone.

Both read every import in a file, at any depth: a lazy import inside a
function and one under ``TYPE_CHECKING`` are the same dependency here.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OWN = "contrib.uniswap_v3"
_NEIGHBOURS = ("contrib.hyperliquid_perp", "contrib.autoresearch", "contrib.replay")
_PURE = (f"{_OWN}.domain", f"{_OWN}.ports")


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
    return found


def _offenders(package: str, is_offender) -> list[tuple[str, str]]:
    return sorted(
        (source.relative_to(_REPO_ROOT).as_posix(), name)
        for source in _sources(package)
        for name in _imports(source)
        if is_offender(name)
    )


def test_the_package_imports_no_other_contrib_package():
    offenders = _offenders(_OWN, lambda name: _within(name, "contrib") and not _within(name, _OWN))
    assert not offenders, f"contrib/uniswap_v3 reaches into another contrib package: {offenders}"


@pytest.mark.parametrize("neighbour", _NEIGHBOURS)
def test_no_neighbour_imports_the_package(neighbour):
    # An empty list would pass the check below without having read anything.
    assert _sources(neighbour), f"{neighbour} has no sources to read"
    offenders = _offenders(neighbour, lambda name: _within(name, _OWN))
    assert not offenders, f"{neighbour} imports contrib/uniswap_v3: {offenders}"


def test_the_pure_layers_import_only_the_standard_library_and_each_other():
    def impure(name: str) -> bool:
        if any(_within(name, pure) for pure in _PURE):
            return False
        return name.split(".")[0] not in sys.stdlib_module_names

    offenders = [found for pure in _PURE for found in _offenders(pure, impure)]
    assert not offenders, f"domain/ and ports.py import beyond the standard library: {offenders}"


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
