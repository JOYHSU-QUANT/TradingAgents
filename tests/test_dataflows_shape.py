"""Shape ratchets for ``tradingagents/dataflows``, read off the sources.

The data-source modules the fork wrote (nine vendors, ``fear_greed`` and
``throttle``, and since D1-a the ``vendor_common`` kernel the vendors share;
none exists upstream) grew without any guard on their shape: nothing in the
repo watched function length, cross-module private imports or the clock
seams the tests patch. ``contrib/`` has had import-graph guards
of this kind since refactor plan v2's T0 (the length table is new to the
repo); this file gives the dataflows the same floor (dataflows refactor plan
2026-10-16, D0), freezing what was measured on 2026-10-19 so it can only
improve:

- :data:`_LONG_FUNCTIONS` — every function over :data:`LONG_FUNCTION_LINES`
  in the modules in scope, with its length as an upper bound;
- :data:`_PRIVATE_CROSS_IMPORTS` — every underscore name a module in scope
  reaches in another dataflows module, by import or through a module bound
  as a name, compared by equality;
- :data:`_CLOCK_SEAMS` — the modules in scope whose module-level ``_utc_now``
  the tests ``monkeypatch.setattr`` to freeze time, compared by equality;
- :data:`_KERNEL_FLOOR` — the only package modules ``vendor_common`` may
  import, so no vendor's types reach the shared kernel.

Scope is :data:`_IN_SCOPE`: the fork's modules plus ``interface.py``, the
one upstream file the fork rebuilt (``route_to_vendor``). The other modules
(:data:`_UPSTREAM`) are the upstream project's to shape; measuring them here
would turn the next sync into table edits for code that is not ours. Every
module on disk is in one list or the other, so a new module has to be placed
before the suite passes.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterable
from pathlib import Path

from .conftest import dataflows_module_trees, package_module_trees, repo_text

# The modules the fork wrote (first commit by the fork, absent from upstream
# ``main``) plus ``interface.py``. The same thirteen ``pyproject.toml``
# ``[tool.mypy] files`` lists under ``tradingagents/dataflows/``, and a test
# below holds the two lists equal.
_IN_SCOPE = frozenset(
    {
        "cftc_cot",
        "cme_basis",
        "deribit",
        "farside",
        "fear_greed",
        "hyperliquid_whales",
        "interface",
        "sosovalue",
        "sosovalue_common",
        "sosovalue_macro",
        "sosovalue_treasuries",
        "throttle",
        "vendor_common",
    }
)

# Upstream's modules (and ``__init__``), named so a module on disk that is in
# neither list fails: a new module has to be placed in scope (or declared
# upstream) before anything else passes.
_UPSTREAM = frozenset(
    {
        "__init__",
        "alpha_vantage",
        "alpha_vantage_common",
        "alpha_vantage_fundamentals",
        "alpha_vantage_indicator",
        "alpha_vantage_news",
        "alpha_vantage_stock",
        "config",
        "date_window",
        "errors",
        "fred",
        "market_data_validator",
        "polymarket",
        "reddit",
        "stocktwits",
        "symbol_utils",
        "utils",
        "y_finance",
        "yfinance_common",
        "yfinance_news",
    }
)

LONG_FUNCTION_LINES = 150

# ``(module, qualified function name): lines`` for every function over the
# line in the modules in scope. The length is an UPPER BOUND, not an exact
# figure: a function may only shrink, a new one over the line fails, and one
# that drops under it is pruned here in the PR that shortened it. Equality
# on the figure would make every edit inside a frozen body, a comment line
# included, a table edit; the bound is deliberately looser than the T0
# ratchets' equality, and the price is that a shortened function keeps its
# old headroom until its row is lowered. The plan's D2 takes the five
# renderers apart, D3 the router's loop body; the three entry points it
# leaves whole (``cftc_cot``, ``cme_basis``, ``fear_greed``, §6) are frozen
# all the same.
_LONG_FUNCTIONS: dict[tuple[str, str], int] = {
    ("cftc_cot", "get_futures_positioning"): 191,
    ("cme_basis", "get_futures_basis"): 215,
    ("deribit", "_dvol_section"): 332,
    ("deribit", "_fetch_dvol"): 354,
    ("deribit", "_reading_line"): 229,
    ("deribit", "get_options_market_data"): 674,
    ("farside", "_parse_flow_table"): 153,
    ("farside", "get_etf_flow_data"): 301,
    ("fear_greed", "get_fear_greed_data"): 203,
    ("interface", "route_to_vendor"): 486,
    ("sosovalue", "get_etf_flow_data"): 582,
    ("sosovalue_treasuries", "get_btc_treasury_data"): 582,
    ("vendor_common", "load_rolling_snapshot"): 194,
}

# ``(importer, source, name)`` for every underscore name a module in scope
# reaches in another dataflows module. One is left, on purpose: the kernel's
# clock seam, which ``cftc_cot``'s own ``_utc_now`` reads through the module
# bound as ``vendor_common`` — an underscore name because it is the name the
# tests patch (:data:`_CLOCK_SEAM`).
_PRIVATE_CROSS_IMPORTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("cftc_cot", "vendor_common", "_utc_now"),
    }
)

# The modules whose ``_utc_now`` a test freezes with ``monkeypatch.setattr``
# (``cftc_cot``'s is read by its tests and delegates to ``vendor_common``'s,
# which they patch). The binding is a test contract: it must stay a module-level
# name (a ``def``, an assignment or an import binding all count) so a patch
# on the module reaches every call in it. Frozen by hand rather than derived from
# the tests on purpose: a PR that moves a seam and repoints its tests has to
# edit this row too, which is the review the plan's §2.6 asks for. The three
# SoSoValue vendors read the clock through ``vendor_common`` and have none of
# their own; ``sosovalue_common`` reads no wall clock (its request budget runs
# on ``time.monotonic``, patched as ``_monotonic``).
_CLOCK_SEAM = "_utc_now"
_CLOCK_SEAMS = frozenset(
    {
        "cftc_cot",
        "cme_basis",
        "deribit",
        "farside",
        "hyperliquid_whales",
        "vendor_common",
    }
)

# The kernel names no vendor: it may import the package's floor alone (the
# configuration, the error taxonomy, the shared utilities). A vendor module in
# its imports would put that vendor's types back into the shared skeleton —
# the reason the sweep that raises SoSoValue's errors stayed in
# ``sosovalue_common`` and the stale fallback re-raises the base
# ``VendorNotConfiguredError`` — and would close an import cycle with that
# vendor, which imports the kernel.
_KERNEL = "vendor_common"
_KERNEL_FLOOR = frozenset({"config", "errors", "utils"})


@functools.cache
def _modules(package: Path | None) -> tuple[tuple[str, ast.Module], ...]:
    """``(module name, parsed tree)`` for the dataflows package (``None``) or for ``package``.

    Cached per argument, and the argument is never defaulted so every table
    reads one entry: the package is parsed once per run rather than once per
    test. The scanner self-tests pass a ``tmp_path`` of synthetic modules.
    """
    if package is None:
        trees = dataflows_module_trees(containing="")
    else:
        trees = package_module_trees("", containing="", root=package)
    return tuple((path.stem, tree) for path, tree in trees)


def _function_lengths(tree: ast.Module) -> dict[str, int]:
    """``qualified name: lines`` for every function in ``tree``, nested and methods included.

    ``end_lineno - lineno + 1`` on the ``def``, docstring and blank lines
    included. The qualified name is the ``def`` and ``class`` names on the
    way down (``outer.inner``, ``Class.method``), so two closures of one name
    in two renderers are two rows and a new one cannot hide under an old
    one's figure. A name defined twice at one level keeps the longer.
    """
    found: dict[str, int] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                assert child.end_lineno is not None
                found[name] = max(child.end_lineno - child.lineno + 1, found.get(name, 0))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def _long_functions(
    package: Path | None = None, scope: frozenset[str] = _IN_SCOPE
) -> dict[tuple[str, str], int]:
    return {
        (module, name): lines
        for module, tree in _modules(package)
        if module in scope
        for name, lines in _function_lengths(tree).items()
        if lines > LONG_FUNCTION_LINES
    }


def _sibling_module(node: ast.ImportFrom, package_name: str) -> str | None:
    """The package sibling ``node`` imports NAMES from, or ``None`` if it is not one.

    ``from .x import ...`` and ``from <package_name>.x import ...`` both name
    the sibling ``x``; a deeper path and anything outside the package are not
    read here, and ``from . import x`` binds a module rather than names,
    which :func:`_sibling_bindings` reads.
    """
    module = node.module or ""
    prefix = package_name + "."
    if node.level == 1:
        rest = module
    elif node.level == 0 and module.startswith(prefix):
        rest = module[len(prefix) :]
    else:
        return None
    return rest if rest and "." not in rest else None


def _sibling_bindings(tree: ast.Module, package_name: str) -> dict[str, str]:
    """``bound name: sibling module`` for every sibling MODULE ``tree`` binds as a name.

    ``from . import x [as y]``, ``from <package_name> import x [as y]`` and
    ``import <package_name>.x as y``. A plain ``import <package_name>.x``
    binds the top package and is not read: no dataflows module spells it.
    """
    found: dict[str, str] = {}
    prefix = package_name + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            (node.level == 1 and node.module is None)
            or (node.level == 0 and node.module == package_name)
        ):
            found.update((alias.asname or alias.name, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tail = alias.name[len(prefix) :]
                if alias.asname and alias.name.startswith(prefix) and "." not in tail:
                    found[alias.asname] = tail
    return found


def _private_cross_imports(
    package: Path | None = None,
    package_name: str = "tradingagents.dataflows",
    scope: frozenset[str] = _IN_SCOPE,
) -> frozenset[tuple[str, str, str]]:
    """``(importer, source, name)`` per underscore name a module in scope reaches in a sibling.

    Two spellings of one reach: ``from .x import _y`` and ``x._y`` read off a
    module bound as a name (:func:`_sibling_bindings`). At any depth: a lazy
    import inside a function is the same dependency. The name IN THE SIBLING
    is what is recorded, not the local binding: ``from .x import _y as z``
    still reaches into ``x`` for ``_y``, and that reach is the debt.
    """
    found: set[tuple[str, str, str]] = set()
    for importer, tree in _modules(package):
        if importer not in scope:
            continue
        modules = _sibling_bindings(tree, package_name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = _sibling_module(node, package_name)
                if source is not None:
                    found.update(
                        (importer, source, alias.name)
                        for alias in node.names
                        if alias.name.startswith("_")
                    )
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in modules
                and node.attr.startswith("_")
            ):
                found.add((importer, modules[node.value.id], node.attr))
    return frozenset(found)


def _sibling_imports(
    tree: ast.Module, package_name: str = "tradingagents.dataflows"
) -> frozenset[str]:
    """Every package sibling ``tree`` imports, by any spelling and at any depth.

    Unlike :func:`_private_cross_imports` this reads which MODULES are
    imported, not which names. A relative import is resolved to its absolute
    module first, at any level, so ``from .x import y``, ``from ..<last
    part>.x import y`` and ``from <package_name>.x.deep import y`` all import
    ``x``; ``from . import x`` and ``from <package_name> import x`` import
    ``x`` itself; ``import <package_name>.x`` with or without ``as`` imports
    ``x``. A lazy import inside a function is the same dependency. Dynamic
    imports (``importlib``, ``__import__``) and attribute reads off the
    package object are not read.
    """
    found: set[str] = set()
    parts = package_name.split(".")
    prefix = package_name + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                climb = node.level - 1
                if climb >= len(parts):
                    continue
                base = parts[: len(parts) - climb]
                absolute = ".".join([*base, module] if module else base)
            else:
                absolute = module
            if absolute == package_name:
                found.update(alias.name for alias in node.names)
            elif absolute.startswith(prefix):
                found.add(absolute[len(prefix) :].split(".")[0])
        elif isinstance(node, ast.Import):
            found.update(
                alias.name[len(prefix) :].split(".")[0]
                for alias in node.names
                if alias.name.startswith(prefix)
            )
    return frozenset(found)


def _module_level_bindings(tree: ast.Module) -> set[str]:
    """The names a module binds by a direct top-level statement: def, class, assignment, import.

    Direct children only, on purpose: the seams the tests patch are plain
    top-level bindings, and the table is equality, so a seam that moved
    under an ``if`` or ``try`` reads as retired here (loudly, for review)
    rather than as kept.
    """
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                elts = target.elts if isinstance(target, ast.Tuple) else [target]
                found.update(e.id for e in elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return found


def _clock_seams(package: Path | None = None, scope: frozenset[str] = _IN_SCOPE) -> frozenset[str]:
    return frozenset(
        module
        for module, tree in _modules(package)
        if module in scope and _CLOCK_SEAM in _module_level_bindings(tree)
    )


def _ratchet_message(what: str, found: Iterable, frozen: Iterable) -> str:
    found, frozen = set(found), set(frozen)
    return f"{what}: new {sorted(found - frozen)}, retired {sorted(frozen - found)}"


# -- the tables ---------------------------------------------------------------------


def test_no_function_in_scope_is_longer_than_it_was_on_2026_10_19():
    found = _long_functions()
    longer = {
        key: (_LONG_FUNCTIONS[key], lines)
        for key, lines in found.items()
        if key in _LONG_FUNCTIONS and lines > _LONG_FUNCTIONS[key]
    }
    assert longer == {}, f"functions that grew, (frozen, now): {longer}"
    assert found.keys() == _LONG_FUNCTIONS.keys(), _ratchet_message(
        f"functions over {LONG_FUNCTION_LINES} lines", found, _LONG_FUNCTIONS
    )


def test_private_cross_imports_are_exactly_those_frozen_on_2026_10_19():
    found = _private_cross_imports()
    assert found == _PRIVATE_CROSS_IMPORTS, _ratchet_message(
        "private names imported across dataflows modules", found, _PRIVATE_CROSS_IMPORTS
    )


def test_every_module_on_disk_is_in_scope_or_declared_upstream():
    # Equality both ways: a typo in ``_IN_SCOPE`` for a module with no long
    # functions and no seam (``throttle``) would otherwise drop it from every
    # scan unnoticed, and a new module would be measured by nothing.
    on_disk = {module for module, _tree in _modules(None)}
    assert on_disk == _IN_SCOPE | _UPSTREAM, _ratchet_message(
        "dataflows modules", on_disk, _IN_SCOPE | _UPSTREAM
    )
    assert not (_IN_SCOPE & _UPSTREAM), sorted(_IN_SCOPE & _UPSTREAM)


def _mypy_dataflows_files() -> list[str]:
    """The dataflows modules ``pyproject.toml`` ``[tool.mypy] files`` lists, in order.

    Read as text, tomllib being 3.11+ while the suite runs on 3.10: the
    ``files = [`` array alone, a line at a time, comment lines dropped, so
    an entry commented out (or quoted elsewhere in the file) does not count;
    a trailing comment on an entry is fine.
    """
    text = repo_text("pyproject.toml")
    start = text.index("\nfiles = [")
    block = text[start : text.index("\n]", start)]
    entry = re.compile(r'"tradingagents/dataflows/(\w+)\.py",?(\s+#.*)?')
    found = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("#") and (match := entry.fullmatch(line)):
            found.append(match.group(1))
    return found


def test_the_scope_is_the_mypy_file_list():
    # ``pyproject.toml`` ``[tool.mypy] files`` names the same modules, so the
    # type floor and the shape tables cover one set. Sorted lists, not sets:
    # a duplicate entry fails too.
    files = _mypy_dataflows_files()
    assert sorted(files) == sorted(_IN_SCOPE), _ratchet_message(
        "[tool.mypy] files", files, _IN_SCOPE
    )


def test_the_package_has_no_subpackage():
    # Every scan globs one level (``*.py``), as ``package_module_trees`` does;
    # a subpackage under dataflows would be seen by nothing here, nor by the
    # mypy file list. The neutral kernel D1-a created is a module, not a package.
    package = next(path for path, _tree in dataflows_module_trees(containing="")).parent
    dirs = sorted(p.name for p in package.iterdir() if p.is_dir() and p.name != "__pycache__")
    assert dirs == [], dirs


def test_the_clock_seams_are_exactly_the_modules_frozen_on_2026_10_19():
    found = _clock_seams()
    assert found == _CLOCK_SEAMS, _ratchet_message(
        f"modules binding {_CLOCK_SEAM} at module level", found, _CLOCK_SEAMS
    )


def test_the_kernel_imports_only_the_packages_floor():
    found = _sibling_imports(dict(_modules(None))[_KERNEL])
    assert found <= _KERNEL_FLOOR, f"{_KERNEL} imports {sorted(found - _KERNEL_FLOOR)}"


# -- the scanners themselves --------------------------------------------------------


def _write(root: Path, name: str, text: str) -> None:
    (root / f"{name}.py").write_text(text, encoding="utf-8")


def test_the_length_scan_counts_def_to_end_under_qualified_names(tmp_path):
    _write(
        tmp_path,
        "m",
        "def short():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def outer():\n"
        '    """Doc."""\n'
        "\n"
        "    def inner():\n"
        "        return 2\n"
        "\n"
        "    return inner()\n"
        "\n"
        "\n"
        "class C:\n"
        "    async def method(self):\n"
        "        return 3\n",
    )
    ((_m, tree),) = _modules(tmp_path)
    # Docstring and blank lines count; ``inner`` is its own row under its
    # outer's name; the method is read inside its class.
    assert _function_lengths(tree) == {
        "short": 2,
        "outer": 7,
        "outer.inner": 2,
        "C.method": 2,
    }


def test_the_length_scan_tells_two_closures_of_one_name_apart(tmp_path):
    # Keyed by bare name, the longer closure would hide the shorter one's
    # growth; keyed by qualified name, each is its own row. A plain
    # redefinition at one level keeps the longer figure whichever comes
    # first (last-wins would let a short redefinition retire a long one).
    _write(
        tmp_path,
        "m",
        "def f():\n    pass\n    pass\n\n"
        "def f():\n    pass\n\n"
        "def g():\n    def f():\n        pass\n        pass\n        pass\n\n"
        "def h():\n    def f():\n        pass\n\n"
        "def k():\n    pass\n\n"
        "def k():\n    pass\n    pass\n    pass\n",
    )
    ((_m, tree),) = _modules(tmp_path)
    # ``f`` is long then short, ``k`` short then long: both keep the longer.
    assert _function_lengths(tree) == {"f": 3, "g": 5, "g.f": 4, "h": 3, "h.f": 2, "k": 4}


def test_the_long_function_scan_reads_only_the_scope_and_only_over_the_line(tmp_path):
    over = "def f():\n" + "    pass\n" * LONG_FUNCTION_LINES
    at = "def exactly():\n" + "    pass\n" * (LONG_FUNCTION_LINES - 1)
    _write(tmp_path, "ours", over + "\n\n" + at + "\n\ndef g():\n    pass\n")
    _write(tmp_path, "theirs", over)
    # ``exactly`` is AT the line, not over it, and ``theirs`` is out of scope.
    assert _long_functions(tmp_path, frozenset({"ours"})) == {
        ("ours", "f"): LONG_FUNCTION_LINES + 1
    }


def test_the_import_scan_resolves_every_shape_of_a_sibling_import(tmp_path):
    _write(
        tmp_path,
        "a",
        "from .b import _x, y, _z as w\n"
        "from pkg.flows.c import _abs\n"
        "from .c.deep import _rel_deep\n"
        "from pkg.flows.c.deep import _abs_deep\n"
        "from pkg.flowsb import _prefix_only\n"
        "from pkg.other import _elsewhere\n"
        "from ..up import _parent\n"
        "from . import b as fam, _module_bound\n"
        "from pkg.flows import c as cc\n"
        "import pkg.flows.d as dd\n"
        "import pkg.flows.g\n"
        "import other as oo\n"
        "def f():\n"
        "    from .e import _lazy\n"
        "    return fam._via_alias(), fam.public, cc._abs_alias, dd._plain_alias\n"
        "x = pkg.flows.g._unresolved + oo._foreign + _module_bound.public\n",
    )
    _write(tmp_path, "z", "from .b import _out_of_scope\n")
    found = _private_cross_imports(tmp_path, "pkg.flows", frozenset({"a"}))
    # ``_z`` is read for the name in ``b``, not its binding ``w``; a private
    # attribute read off a module bound by any of the three spellings is an
    # edge, a public one is not; the parent, deeper, prefix-only and foreign
    # imports are not sibling edges, a module bound as ``_module_bound`` is
    # not itself a reached name, and ``z`` is outside the scope.
    assert found == {
        ("a", "b", "_x"),
        ("a", "b", "_z"),
        ("a", "b", "_via_alias"),
        ("a", "c", "_abs"),
        ("a", "c", "_abs_alias"),
        ("a", "d", "_plain_alias"),
        ("a", "e", "_lazy"),
    }


def test_the_seam_scan_reads_module_level_bindings_of_every_kind(tmp_path):
    _write(tmp_path, "by_def", "def _utc_now():\n    pass\n")
    _write(tmp_path, "by_assign", "import x\n_utc_now = x.now\n")
    _write(tmp_path, "by_annotated", "_utc_now: object = None\n")
    _write(tmp_path, "by_tuple", "_a, _utc_now = 1, 2\n")
    _write(tmp_path, "by_import", "from .by_def import _utc_now\n")
    _write(tmp_path, "by_alias", "from .by_def import now as _utc_now\n")
    _write(tmp_path, "by_plain_import", "import _utc_now\n")
    _write(tmp_path, "nested", "def f():\n    def _utc_now():\n        pass\n")
    _write(tmp_path, "guarded", "if True:\n    _utc_now = 1\n")
    _write(tmp_path, "other_name", "def utc_now():\n    pass\n")
    _write(tmp_path, "out_of_scope", "def _utc_now():\n    pass\n")
    everything = frozenset(p.stem for p in tmp_path.glob("*.py")) - {"out_of_scope"}
    # ``guarded`` binds the name under an ``if``: read as absent on purpose
    # (see ``_module_level_bindings``).
    assert _clock_seams(tmp_path, everything) == {
        "by_def",
        "by_assign",
        "by_annotated",
        "by_tuple",
        "by_import",
        "by_alias",
        "by_plain_import",
    }


def test_the_sibling_import_scan_reads_every_spelling_at_any_depth(tmp_path):
    _write(
        tmp_path,
        "k",
        "from .a import x\n"
        "from pkg.flows.b import y\n"
        "from .c.deep import z\n"
        "from . import d, e as ee\n"
        "from pkg.flows import f\n"
        "import pkg.flows.g\n"
        "import pkg.flows.h as hh\n"
        "from pkg.flowsb import prefix_only\n"
        "from pkg.other import elsewhere\n"
        "from ..up import parent\n"
        "from ..flows.j import climbed_back\n"
        "from ..flows import l\n"
        "from .....too_far import gone\n"
        "import other\n"
        "def lazy():\n"
        "    from .i import w\n",
    )
    found = _sibling_imports(dict(_modules(tmp_path))["k"], "pkg.flows")
    # Every spelling of a sibling counts, deeper paths by their sibling, and a
    # relative import that climbs out and back in names one too; the
    # prefix-only, foreign, parent and past-the-root imports are not siblings.
    assert found == {"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "l"}
