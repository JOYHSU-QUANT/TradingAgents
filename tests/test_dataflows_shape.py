"""Shape ratchets for ``tradingagents/dataflows``, read off the sources.

The data-source modules the fork wrote (nine vendors, ``fear_greed`` and
``throttle``; none exists upstream) grew without any guard on their shape:
nothing in the repo watched function length, cross-module private imports
or the clock seams the tests patch. ``contrib/`` has had these guards since
refactor plan v2's T0; this file gives the dataflows the same floor
(dataflows refactor plan 2026-10-16, D0), freezing what was measured on
2026-10-19 so it can only improve:

- :data:`_LONG_FUNCTIONS` — every function over :data:`LONG_FUNCTION_LINES`
  in the modules in scope, with its length as an upper bound;
- :data:`_PRIVATE_CROSS_IMPORTS` — every underscore name a module in scope
  imports from another dataflows module, compared by equality;
- :data:`_CLOCK_SEAMS` — the modules in scope whose module-level ``_utc_now``
  the tests ``monkeypatch.setattr`` to freeze time, compared by equality.

Scope is :data:`_IN_SCOPE`: the fork's modules plus ``interface.py``, the
one upstream file the fork rebuilt (``route_to_vendor``). The other upstream
modules (``fred``, ``polymarket``, ``stocktwits``, the Alpha Vantage and
yfinance families) are the upstream project's to shape; measuring them here
would turn the next sync into table edits for code that is not ours.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterable
from pathlib import Path

from .conftest import dataflows_module_trees, package_module_trees

# The modules the fork wrote (first commit by the fork, absent from upstream
# ``main``) plus ``interface.py``. The same twelve ``pyproject.toml``
# ``[tool.mypy] files`` lists under ``tradingagents/dataflows/``; a module
# added to one is added to the other.
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
    ("sosovalue_common", "load_rolling_snapshot"): 194,
    ("sosovalue_macro", "get_economic_calendar_data"): 1053,
    ("sosovalue_treasuries", "get_btc_treasury_data"): 582,
}

# ``(importer, source, name)`` for every underscore name a module in scope
# imports from another dataflows module. All of it is the cache kernel the
# six vendors take from ``sosovalue_common``; D1 moves that kernel into a
# neutral module under public names, and this set shrinks there.
_PRIVATE_CROSS_IMPORTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("cftc_cot", "sosovalue_common", "_cache_dir"),
        ("cftc_cot", "sosovalue_common", "_cache_rejecter"),
        ("cftc_cot", "sosovalue_common", "_read_cache_preamble"),
        ("cftc_cot", "sosovalue_common", "_stale_caveat"),
        ("farside", "sosovalue_common", "_cache_dir"),
        ("farside", "sosovalue_common", "_cache_rejecter"),
        ("farside", "sosovalue_common", "_read_cache_preamble"),
        ("farside", "sosovalue_common", "_stale_caveat"),
        ("hyperliquid_whales", "sosovalue_common", "_cache_dir"),
        ("hyperliquid_whales", "sosovalue_common", "_cache_rejecter"),
        ("hyperliquid_whales", "sosovalue_common", "_read_cache_preamble"),
        ("hyperliquid_whales", "sosovalue_common", "_stale_caveat"),
        ("sosovalue", "sosovalue_common", "_cache_dir"),
        ("sosovalue", "sosovalue_common", "_cache_rejecter"),
        ("sosovalue", "sosovalue_common", "_concentration_share_str"),
        ("sosovalue", "sosovalue_common", "_is_finite_number"),
        ("sosovalue", "sosovalue_common", "_is_iso_date"),
        ("sosovalue", "sosovalue_common", "_is_non_negative_int"),
        ("sosovalue", "sosovalue_common", "_is_valid_ticker"),
        ("sosovalue", "sosovalue_common", "_plural"),
        ("sosovalue", "sosovalue_common", "_plural_days"),
        ("sosovalue", "sosovalue_common", "_read_cache_preamble"),
        ("sosovalue", "sosovalue_common", "_request"),
        ("sosovalue", "sosovalue_common", "_sanitize"),
        ("sosovalue", "sosovalue_common", "_sign"),
        ("sosovalue", "sosovalue_common", "_stale_caveat"),
        ("sosovalue_macro", "sosovalue_common", "_cache_dir"),
        ("sosovalue_macro", "sosovalue_common", "_cache_rejecter"),
        ("sosovalue_macro", "sosovalue_common", "_coverage_gap_note"),
        ("sosovalue_macro", "sosovalue_common", "_days_unobserved"),
        ("sosovalue_macro", "sosovalue_common", "_is_iso_date"),
        ("sosovalue_macro", "sosovalue_common", "_is_non_negative_int"),
        ("sosovalue_macro", "sosovalue_common", "_is_safe_text"),
        ("sosovalue_macro", "sosovalue_common", "_plural"),
        ("sosovalue_macro", "sosovalue_common", "_plural_days"),
        ("sosovalue_macro", "sosovalue_common", "_read_cache_preamble"),
        ("sosovalue_macro", "sosovalue_common", "_request"),
        ("sosovalue_macro", "sosovalue_common", "_sanitize"),
        ("sosovalue_macro", "sosovalue_common", "_stale_caveat"),
        ("sosovalue_macro", "sosovalue_common", "_valid_dated_rows"),
        ("sosovalue_treasuries", "sosovalue_common", "_cache_dir"),
        ("sosovalue_treasuries", "sosovalue_common", "_cache_rejecter"),
        ("sosovalue_treasuries", "sosovalue_common", "_concentration_share_str"),
        ("sosovalue_treasuries", "sosovalue_common", "_coverage_gap_note"),
        ("sosovalue_treasuries", "sosovalue_common", "_days_unobserved"),
        ("sosovalue_treasuries", "sosovalue_common", "_is_finite_number"),
        ("sosovalue_treasuries", "sosovalue_common", "_is_iso_date"),
        ("sosovalue_treasuries", "sosovalue_common", "_is_non_negative_int"),
        ("sosovalue_treasuries", "sosovalue_common", "_is_safe_text"),
        ("sosovalue_treasuries", "sosovalue_common", "_is_valid_ticker"),
        ("sosovalue_treasuries", "sosovalue_common", "_plural"),
        ("sosovalue_treasuries", "sosovalue_common", "_plural_days"),
        ("sosovalue_treasuries", "sosovalue_common", "_read_cache_preamble"),
        ("sosovalue_treasuries", "sosovalue_common", "_request"),
        ("sosovalue_treasuries", "sosovalue_common", "_sanitize"),
        ("sosovalue_treasuries", "sosovalue_common", "_stale_caveat"),
        ("sosovalue_treasuries", "sosovalue_common", "_valid_dated_rows"),
    }
)

# The modules whose ``_utc_now`` a test freezes with ``monkeypatch.setattr``.
# The binding is a test contract: it must stay a module-level name (a
# ``def``, an assignment or an import binding all count) so a patch on the
# module reaches every call in it. Frozen by hand rather than derived from
# the tests on purpose: a PR that moves a seam and repoints its tests has to
# edit this row too, which is the review the plan's §2.6 asks for. The three
# SoSoValue vendors read the clock through ``sosovalue_common`` and have none
# of their own; the neutral kernel D1 creates is added here when it gets one.
_CLOCK_SEAM = "_utc_now"
_CLOCK_SEAMS = frozenset(
    {
        "cftc_cot",
        "cme_basis",
        "deribit",
        "farside",
        "hyperliquid_whales",
        "sosovalue_common",
    }
)


@functools.cache
def _modules(package: Path | None) -> tuple[tuple[str, ast.Module], ...]:
    """``(module name, parsed tree)`` for the dataflows package (``None``) or the sources under ``package``.

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
    """The package sibling ``node`` imports from, or ``None`` if it is not one.

    ``from .x import ...`` and ``from <package_name>.x import ...`` both name
    the sibling ``x``; ``from . import x`` (a module bound as a name), a
    deeper path and anything outside the package are not read here.
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


def _private_cross_imports(
    package: Path | None = None,
    package_name: str = "tradingagents.dataflows",
    scope: frozenset[str] = _IN_SCOPE,
) -> frozenset[tuple[str, str, str]]:
    """``(importer, source, name)`` per underscore name a module in scope imports from a sibling.

    At any depth: a lazy import inside a function is the same dependency.
    The IMPORTED name is what is read, not the binding: ``from .x import _y
    as z`` still reaches into ``x`` for ``_y``, and that reach is the debt.
    """
    found: set[tuple[str, str, str]] = set()
    for importer, tree in _modules(package):
        if importer not in scope:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            source = _sibling_module(node, package_name)
            if source is None:
                continue
            found.update(
                (importer, source, alias.name) for alias in node.names if alias.name.startswith("_")
            )
    return frozenset(found)


def _module_level_bindings(tree: ast.Module) -> set[str]:
    """The names a module binds at its top level: defs, classes, assignments, imports.

    Only direct children of the module: a name bound inside a function or an
    ``if`` block is not a seam a test can patch before the module is used.
    """
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
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


# -- the tables --------------------------------------------------------------------


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


def test_every_module_in_scope_is_on_disk():
    # A typo in ``_IN_SCOPE`` for a module with no long functions and no
    # seam (``throttle``) would otherwise drop it from every scan unnoticed.
    on_disk = {module for module, _tree in _modules(None)}
    assert on_disk >= _IN_SCOPE, f"not on disk: {sorted(_IN_SCOPE - on_disk)}"


def test_the_clock_seams_are_exactly_the_modules_frozen_on_2026_10_19():
    found = _clock_seams()
    assert found == _CLOCK_SEAMS, _ratchet_message(
        f"modules binding {_CLOCK_SEAM} at module level", found, _CLOCK_SEAMS
    )


# -- the scanners themselves -----------------------------------------------------------


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
    # redefinition at one level keeps the longer figure.
    _write(
        tmp_path,
        "m",
        "def f():\n    pass\n\n"
        "def f():\n    pass\n    pass\n\n"
        "def g():\n    def f():\n        pass\n        pass\n        pass\n\n"
        "def h():\n    def f():\n        pass\n",
    )
    ((_m, tree),) = _modules(tmp_path)
    assert _function_lengths(tree) == {"f": 3, "g": 5, "g.f": 4, "h": 3, "h.f": 2}


def test_the_long_function_scan_reads_only_the_scope_and_only_over_the_line(tmp_path):
    body = "def f():\n" + "    pass\n" * LONG_FUNCTION_LINES
    _write(tmp_path, "ours", body + "\n\ndef g():\n    pass\n")
    _write(tmp_path, "theirs", body)
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
        "from . import _module_bound\n"
        "from ..up import _parent\n"
        "import pkg.flows.d\n"
        "def f():\n"
        "    from .e import _lazy\n",
    )
    _write(tmp_path, "z", "from .b import _out_of_scope\n")
    found = _private_cross_imports(tmp_path, "pkg.flows", frozenset({"a"}))
    # ``_z`` is read for the imported name, not its binding ``w``; the
    # module-bound, parent, deeper, prefix-only and foreign imports are not
    # sibling edges, and ``z`` is outside the scope.
    assert found == {
        ("a", "b", "_x"),
        ("a", "b", "_z"),
        ("a", "c", "_abs"),
        ("a", "e", "_lazy"),
    }


def test_the_seam_scan_reads_module_level_bindings_of_every_kind(tmp_path):
    _write(tmp_path, "by_def", "def _utc_now():\n    pass\n")
    _write(tmp_path, "by_assign", "import x\n_utc_now = x.now\n")
    _write(tmp_path, "by_annotated", "_utc_now: object = None\n")
    _write(tmp_path, "by_import", "from .by_def import _utc_now\n")
    _write(tmp_path, "by_alias", "from .by_def import now as _utc_now\n")
    _write(tmp_path, "by_plain_import", "import _utc_now\n")
    _write(tmp_path, "nested", "def f():\n    def _utc_now():\n        pass\n")
    _write(tmp_path, "guarded", "if True:\n    _utc_now = 1\n")
    _write(tmp_path, "other_name", "def utc_now():\n    pass\n")
    _write(tmp_path, "out_of_scope", "def _utc_now():\n    pass\n")
    everything = frozenset(p.stem for p in tmp_path.glob("*.py")) - {"out_of_scope"}
    assert _clock_seams(tmp_path, everything) == {
        "by_def",
        "by_assign",
        "by_annotated",
        "by_import",
        "by_alias",
        "by_plain_import",
    }
