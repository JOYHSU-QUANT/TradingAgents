"""Guards for the package's prose-only layering contracts.

None of these invariants is exercised anywhere else:

- ``domains/perp/margin`` and ``persistence/models`` re-export the ONE
  ``DECIMAL_CONTEXT`` that lives in ``common`` BY IDENTITY — deleting either
  name would break its importers loudly, but forking a second, equal context
  would not, and nothing else would notice. Both paths stay: the margin one
  is frozen by phase3-spec §2.1 (``paper/engine`` reads it there), the
  models one is the persistence layer's entry point;
- ``common/`` stays at the bottom of the import graph — the rule in
  ``common/__init__``'s docstring that nothing there imports from another
  ``hyperliquid_perp`` package would otherwise be enforced by review only;
- the config loader, the pre-LLM context guards and the no-decision policy
  keep their load-time import closures below the SDK, the store and the
  engines (issue #122);
- ``runtime/``, the kernel both lanes share, keeps its load-time closure on
  the store, the ports and the floor, and reaches neither ``paper`` nor
  ``live`` at any depth (refactor plan v2, T1); its ``__init__`` docstring
  lists exactly the modules on disk, the one list the docs point to;
- ``integration/`` borrows exactly one name from the lanes, the paper cost
  model the decision provider prices the position section with;
- the layering debt measured on 2026-09-22 is frozen so it can only shrink
  (refactor plan v2, T0 — the Ratchets section at the end of this file).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib import hyperliquid_perp as perp_pkg
from contrib.hyperliquid_perp import (
    common as common_pkg,
    integration as integration_pkg,
    live as live_pkg,
    persistence as persistence_pkg,
    runtime as runtime_pkg,
)
from contrib.hyperliquid_perp.common import decimal_context
from contrib.hyperliquid_perp.domains.perp import margin
from contrib.hyperliquid_perp.persistence import models

from ..conftest import package_sources

_PACKAGE = "contrib.hyperliquid_perp"


def _within(name: str | None, pkg: str) -> bool:
    """``name`` is the package ``pkg`` itself or a dotted name inside it.

    The one spelling of "is `pkg` or lies under `pkg`" the predicates
    below share — a copy that baked the dot into a prefix test read the bare
    package as outside (issue #155), so they are not spelled twice.
    """
    return name == pkg or (name or "").startswith(pkg + ".")


def _package_tail(name: str | None) -> str | None:
    """The in-package dotted tail ``name`` refers to, or ``None`` if it is not ours.

    ``"contrib.hyperliquid_perp.domains.perp.x"`` -> ``"domains.perp.x"``, so an
    absolute import can be tested against the same allowlist as a relative one.
    The bare package itself maps to ``""``, which no allowlist contains — a
    ``from contrib.hyperliquid_perp import <compute module>`` is an offender too.
    """
    if not _within(name, _PACKAGE):
        return None
    return name[len(_PACKAGE) + 1 :]  # ``""`` for the bare package


def test_the_decimal_context_reexports_are_the_common_object():
    # Identity, not equality: a re-export that re-declared its own context
    # would keep equal behavior today but fork the definition the next time
    # one side moves.
    assert margin.DECIMAL_CONTEXT is decimal_context.DECIMAL_CONTEXT
    assert models.DECIMAL_CONTEXT is decimal_context.DECIMAL_CONTEXT


def test_the_config_loader_imports_no_compute_module():
    # The rule ``indicator_vocab`` was split out of ``indicators`` to enforce
    # (see that module's docstring): ``load_config`` must not drag a compute
    # module in. Prose alone let it rot once already — the volume-profile floor
    # was first imported straight from ``domains/perp/volume_profile``, which is
    # pure stdlib TODAY but is exactly the code someone later reaches for numpy
    # in, at which point the keyless ``live --config-check`` path would acquire
    # it silently and nothing would fail. Structural, like the check below.
    #
    # To add an import here, put the value in ``common/`` or a ``*_vocab``
    # module and add THAT module here by name, rather than admitting a
    # compute module — ``common.enum_guard`` is on it because ``schema``'s
    # four vocabulary enums inherit their refusal sentence from it (issue
    # #166). ``market_data_config`` is the one
    # parser on the list — it runs on every load (the block is always
    # present), so a lazy import would buy nothing — and ``schema`` is the DTO
    # module it reaches for the candle-interval vocabulary. Named module by
    # module, never by a ``common.*`` prefix: the common-layer check below
    # only forbids IN-PACKAGE imports, so a prefix would admit a ``common``
    # helper that grew a numpy import.
    #
    # The set is checked as a CLOSURE, not as config.py's direct imports
    # alone: every admitted module's own in-package imports must stay inside
    # the same set. Otherwise a ``from .volume_profile import ...`` added to
    # ``schema`` or to the parser would drag the compute module into every
    # load while both files' direct import lists looked innocent.
    #
    # LOAD-TIME statements only, unlike the ``common/`` check below which walks
    # the whole tree. The invariant here is about what merely IMPORTING
    # config.py costs, and a lazy import inside a FUNCTION is this repo's
    # sanctioned escape hatch — ``load_config`` already uses it for
    # ``live.config``/``risk_gate``, precisely so ``--context-only`` does not
    # pay for the risk-gate domain unless a ``live:`` block exists. Nothing
    # else is deferred — see ``_load_time_statements`` (issue #166).
    allowed = {
        "common.config_coercion",
        "common.constants",
        "common.enum_guard",
        "domains.perp.indicator_vocab",
        "domains.perp.market_data_config",
        "domains.perp.schema",
    }
    offenders = _load_time_import_closure(_SOURCE_ROOT / "config.py") - allowed
    assert not offenders, (
        f"config.py's load-time import closure reaches outside {sorted(allowed)}: "
        f"{sorted(offenders)}"
    )


_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _load_time_import_closure(source: Path, root: Path = _SOURCE_ROOT) -> set[str]:
    """Every in-package module ``source`` imports at load time, transitively.

    Walks :func:`_in_package_imports` from module to module, resolving each
    dotted tail to its file (:func:`_module_file`). A tail with no file — the
    bare package root ``""``, or a name that is an attribute of a package's
    ``__init__`` — is kept in the result (it may be an offender) but not
    walked. Every package ON THE WAY to a resolved tail is walked too, and so
    are ``source``'s own packages and the root ``__init__`` — the interpreter
    runs ``domains/__init__.py`` and ``domains/perp/__init__.py`` before
    ``domains/perp/schema.py``, whoever imports it — but none of them is
    reported itself, so an allowlist names modules, not their ancestors.
    ``root`` is a parameter only so the walk itself can be tested on a
    synthetic tree.
    """
    seen: set[str] = set()
    walked = {source}
    queue = [source]

    def walk_packages_of(tail: str) -> None:
        parts = tail.split(".") if tail else []
        for depth in range(1, len(parts) + 1):
            module = _module_file(".".join(parts[:depth]), root)
            if module is not None and module not in walked:
                walked.add(module)
                queue.append(module)

    if (root / "__init__.py").is_file():  # ``""`` deliberately has no file
        walked.add(root / "__init__.py")
        queue.append(root / "__init__.py")
    walk_packages_of(".".join(_own_package(source, root)))
    while queue:
        for tail in _in_package_imports(queue.pop(), root) - seen:
            seen.add(tail)
            walk_packages_of(tail)
    return seen


def _module_file(tail: str, root: Path) -> Path | None:
    """The file under ``root`` a dotted tail names: ``x/__init__.py`` for a package, else ``x.py``.

    The package first, as the interpreter resolves it — a directory with an
    ``__init__`` shadows a same-named module beside it. ``None`` for the root
    itself (``""``) and for a tail nothing on disk answers to.
    """
    if not tail:
        return None
    module = root.joinpath(*tail.split("."))
    if (module / "__init__.py").is_file():
        return module / "__init__.py"
    if module.with_suffix(".py").is_file():
        return module.with_suffix(".py")
    return None


# The layers the guard family may sit on: a tail is below the floor when it IS
# one of these packages or lies inside one (issue #155).
_FLOOR = ("common", "domains.perp")


def _above_the_floor(tail: str) -> bool:
    return not any(_within(tail, pkg) for pkg in _FLOOR)


@pytest.mark.parametrize(
    "module",
    [
        "runtime/no_decision.py",
        "domains/perp/freshness.py",
        "domains/perp/context_guards.py",
    ],
)
def test_the_context_guard_family_and_the_no_decision_policy_stay_below_the_engines(module):
    # Issue #122. The four pre-LLM guards and the no-decision policy are read
    # by both engines and by the keyless entry points, so they must sit BELOW
    # the SDK, the persistence package, ``paper`` and ``live`` — a claim that
    # was prose in ``freshness``'s docstring until the guards moved out of
    # ``engine_bridge`` (which imports the SDK at module level) and the policy
    # out of ``paper`` (whose scheduler import loaded the whole paper engine).
    # Pinned as a load-time import closure, like the config loader's, so a
    # convenience import of ``exchanges``/``persistence``/``paper`` added to
    # any of the three fails here by name. The policy lives in ``runtime/``
    # since refactor plan v2 T1-c, whose package check below allows the store;
    # it keeps this narrower floor because its docstring commits to plain
    # ``sqlite3`` reads — the PR that moves it onto ``repository`` drops it here.
    closure = _load_time_import_closure(_SOURCE_ROOT / module)
    offenders = {t for t in closure if _above_the_floor(t)}
    assert not offenders, f"{module} reaches above domains/common at load time: {sorted(offenders)}"


# Where the shared kernel may sit (refactor plan v2, T1): on its own siblings,
# the store, the ports, the exchange adapter's error family and the floor —
# never on either engine or the SDK.
_RUNTIME_FLOOR = _FLOOR + ("runtime", "persistence", "ports", "exchanges.hyperliquid.errors")


def test_the_runtime_package_loads_nothing_above_the_store():
    # ``runtime/__init__``'s docstring places the package above ``persistence``
    # and below ``paper`` / ``live``; this is the check. Per module, so a
    # convenience import of an engine, the SDK or the whole adapter added to
    # any runtime module fails here by name.
    offenders = {
        (source.relative_to(_SOURCE_ROOT).as_posix(), tail)
        for source in package_sources(runtime_pkg)
        for tail in _load_time_import_closure(source)
        if not any(_within(tail, pkg) for pkg in _RUNTIME_FLOOR)
    }
    assert not offenders, f"runtime/ reaches above the store at load time: {sorted(offenders)}"


def test_the_runtime_package_reaches_neither_engine_at_any_depth():
    # The whole tree, not only load time: a lazy or TYPE_CHECKING import of
    # ``paper`` or ``live`` from the kernel is the same upward edge.
    found = {
        (source.relative_to(_SOURCE_ROOT).as_posix(), symbol)
        for source in package_sources(runtime_pkg)
        for pkg in ("paper", "live")
        for symbol in _symbols_imported_from(source, pkg)
    }
    assert not found, f"runtime/ imports from an engine: {sorted(found)}"


def test_the_runtime_docstring_lists_exactly_the_modules_on_disk():
    # Compared as sorted lists, so a bullet written twice fails too.
    listed = sorted(re.findall(r"^- :mod:`\.(\w+)`", runtime_pkg.__doc__, flags=re.MULTILINE))
    on_disk = sorted(p.stem for p in package_sources(runtime_pkg) if p.name != "__init__.py")
    assert listed == on_disk, f"runtime/__init__ lists {listed}, runtime/ holds {on_disk}"


def test_the_closure_walk_reaches_an_import_two_hops_away(tmp_path):
    # The loader test above only discriminates if the walk RECURSES: today
    # every module config.py imports directly is allowlisted, and the one
    # module reachable only through a second hop (schema) is allowlisted
    # too, so a walker that read config.py's own import list and stopped
    # would pass it just the same. Pin the recursion on a synthetic tree —
    # a -> b -> c, with c a package and a ``from . import`` at the root —
    # so dropping the queue fails HERE, not silently in the guard.
    (tmp_path / "a.py").write_text("from .b import x\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from .c import y\nfrom . import d\n", encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "__init__.py").write_text("from ..e import z\n", encoding="utf-8")
    (tmp_path / "e.py").write_text("", encoding="utf-8")
    assert _load_time_import_closure(tmp_path / "a.py", root=tmp_path) == {"b", "c", "", "e"}


def test_a_bare_package_tail_below_the_floor_is_not_an_offender(tmp_path):
    # Issue #155. Two shapes the dotted-prefix test read as reaching above the
    # floor: ``from . import schema`` inside ``domains/perp/`` and
    # ``from ...common import a``. The first names a MODULE, so it resolves to
    # ``domains.perp.schema`` and the walk goes through it — schema's own
    # import surfaces below; drop the submodule resolution and the guard
    # would accept a ``from . import x`` whose ``x`` reaches the SDK. The
    # second names an attribute, so it resolves to the bare ``common``, below
    # the floor. None of the three guarded modules is written either way
    # today, so pin both on a synthetic tree — where the real offenders must
    # stay red: the package root, a sibling package, and a package whose NAME
    # merely starts with a floor package's (the trap a dotless prefix test
    # would walk into). The tree also plants an import in the ``__init__`` of a
    # package an imported TAIL passes through (``exchanges/``, on the way to
    # ``exchanges.hl``): the interpreter runs it before the module below it,
    # so the walk must reach it although no tail names it — and it is neither
    # one of guard's own packages (the seed test below) nor a bare tail.
    for pkg in ("common", "domains", "domains/perp", "exchanges"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "exchanges" / "__init__.py").write_text("from ..paper import p\n", encoding="utf-8")
    (tmp_path / "exchanges" / "hl.py").write_text("", encoding="utf-8")
    perp = tmp_path / "domains" / "perp"
    (perp / "schema.py").write_text("from ...persistence import db\n", encoding="utf-8")
    (perp / "guard.py").write_text(
        "from . import schema\n"
        "from .. import perp\n"
        "from ...common import a\n"
        "from ...exchanges.hl import c\n"
        "from ... import audit\n"
        "from ...commonplace import q\n",
        encoding="utf-8",
    )
    closure = _load_time_import_closure(perp / "guard.py", root=tmp_path)
    assert closure == {
        "domains.perp.schema",
        "persistence",  # reached THROUGH schema: the second hop
        "exchanges.hl",
        "paper",  # reached through the ancestor ``exchanges/__init__.py``
        "domains.perp",
        "common",
        "",
        "commonplace",
    }
    assert {t for t in closure if _above_the_floor(t)} == {
        "persistence",
        "exchanges.hl",
        "paper",
        "",
        "commonplace",
    }


def test_the_closure_walk_runs_the_source_modules_own_packages(tmp_path):
    # The interpreter runs ``domains/__init__.py``, ``domains/perp/__init__.py``
    # and the root ``__init__`` before ``domains/perp/guard.py`` WHATEVER guard
    # imports, so the walk seeds itself with them. Guard's own import stays
    # inside ``common``, whose packages are not guard's: only the seed reaches
    # ``persistence`` and ``live`` here. (Every real ``__init__`` on the way is
    # import-free today, which is exactly why nothing but this would notice.)
    for pkg in ("common", "domains", "domains/perp"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("from .live import x\n", encoding="utf-8")
    perp = tmp_path / "domains" / "perp"
    (perp / "__init__.py").write_text("from ...persistence import db\n", encoding="utf-8")
    (tmp_path / "common" / "constants.py").write_text("", encoding="utf-8")
    (perp / "guard.py").write_text("from ...common.constants import K\n", encoding="utf-8")
    closure = _load_time_import_closure(perp / "guard.py", root=tmp_path)
    assert closure == {"common.constants", "persistence", "live"}


def test_package_sources_reaches_subpackages_in_path_order(tmp_path):
    # ``live/`` and ``common/`` have no subpackage today, so nothing but this
    # would notice the shared walk reverting to a top-level ``glob`` — the
    # drift issue #151 closed — or losing its ordering. Created out of order
    # on purpose; the non-``.py`` file must not appear.
    (tmp_path / "z.py").write_text("", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("", encoding="utf-8")
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    pkg = SimpleNamespace(__path__=[str(tmp_path)])
    assert package_sources(pkg) == [
        tmp_path / "a.py",
        tmp_path / "sub" / "deep.py",
        tmp_path / "z.py",
    ]


def _own_package(source: Path, root: Path) -> tuple[str, ...]:
    """The package parts of ``source`` under ``root``: ``("domains", "perp")`` for ``domains/perp/x.py``."""
    return source.resolve().relative_to(root).parent.parts


def _imports(
    statements: Iterable[ast.AST], own_package: tuple[str, ...]
) -> Iterator[tuple[str | None, ast.alias, bool]]:
    """``(base, alias, from_import)`` for every import among ``statements``.

    ``base`` is the in-package dotted tail the statement names — for
    ``from X import a`` the tail of ``X`` (:func:`_dotted_tail`), for
    ``import X`` the tail of ``X`` itself (:func:`_package_tail`) — or ``None``
    when it is not ours. The one reader of the level rules: each walker over
    it only chooses which statements to feed it and how to project
    ``(base, alias)`` — the binding rule (``asname`` or not) is the walker's.
    """
    for node in statements:
        if isinstance(node, ast.ImportFrom):
            base = _dotted_tail(node.module, node.level, own_package)
            for alias in node.names:
                yield base, alias, True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield _package_tail(alias.name), alias, False


def _dotted_tail(name: str | None, level: int, own_package: tuple[str, ...]) -> str | None:
    """The in-package tail a ``from <'.' * level><name> import`` names, seen from ``own_package``.

    Level 0 is an absolute import, normalised by :func:`_package_tail`. One
    dot is the module's own package; each further one climbs a package.
    Climbing past the package root is an ImportError at runtime; it resolves
    to the root (``""``) here rather than let a negative slice bound silently
    drop packages from the END of the path.
    """
    if level == 0:
        return _package_tail(name)
    base = list(own_package[: max(0, len(own_package) - (level - 1))])
    return ".".join([*base, name] if name else base)


def _in_package_imports(source: Path, root: Path = _SOURCE_ROOT) -> set[str]:
    """Dotted tails (``domains.perp.x``) of ``source``'s LOAD-TIME in-package imports.

    The statements walked are :func:`_load_time_statements`'s: everything
    the interpreter runs on import, which is everything but a function body.

    Relative level-1 imports are today's style, but the guard must not depend
    on the style holding: an ABSOLUTE
    ``from contrib.hyperliquid_perp.domains.perp.volume_profile import ...``
    (level 0), or a plain ``import contrib.hyperliquid_perp...``, drags in
    exactly the same compute module while passing a level-1-only filter, so
    both node kinds are walked and absolute forms are normalised to the same
    dotted tail an allowlist is written in (:func:`_imports`).

    ``from pkg import x`` resolves to the SUBMODULE ``pkg.x`` when one exists
    on disk — its own imports are part of the closure, so the walk has to
    reach it (issue #155) — and otherwise to ``pkg`` itself: ``x`` is then an
    attribute, and what was imported is the package. ``from . import x``
    (``name is None``) at the top level is therefore ``""`` for an attribute,
    in no allowlist, exactly as :func:`_package_tail` maps the bare absolute
    package, and ``"domains"`` for the package — one keystroke from the
    already-flagged ``from .domains import perp``, and the realistic route to
    the historical offender the loader test's docstring cites. Letting either
    fall through as ``None`` would allow both.
    """
    def imported(base: str | None, name: str) -> str | None:
        if base is None:
            return None
        submodule = f"{base}.{name}" if base else name
        return submodule if _module_file(submodule, root) is not None else base

    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for base, alias, from_import in _imports(_load_time_statements(tree), _own_package(source, root)):
        tail = imported(base, alias.name) if from_import else base
        if tail is not None:
            found.add(tail)
    return found


def _is_type_checking_guard(test: ast.expr) -> bool:
    """``if TYPE_CHECKING:`` or ``if typing.TYPE_CHECKING:`` — the one suite that never runs.

    Only those two spellings: an attribute of anything but ``typing`` (a
    settings object that happens to carry the name) is a runtime flag and is
    walked as a plain ``if``. Any other test that mentions the name
    (``if not TYPE_CHECKING:``, ``if TYPE_CHECKING or X:``) is walked on
    both suites too — a loud false positive in a layering test, never a
    silent miss.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


# The only bodies the interpreter defers past import: a function's. A class
# body, a ``try`` suite, an ``if``/``else``, a ``match`` case, a ``with`` or a
# loop all run when the module is imported.
_DEFERRED_BODIES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _load_time_statements(node: ast.AST) -> Iterator[ast.stmt]:
    """Every statement under ``node`` the interpreter runs when the module is imported.

    Descends through EVERY child node (``ast.iter_child_nodes``) except a
    function body — so a ``try``'s handlers, else and finally, an ``if``'s
    both suites, a ``match`` case, a ``with``, a loop and a CLASS body are all
    reached without naming their node types, and a statement kind added to
    the language later is walked by default rather than silently skipped
    (issue #166: the walk used to read only ``.body``, so an import inside any
    of these was invisible to every closure guard in this file). ``if
    TYPE_CHECKING:`` is the one suite that never executes: its body is
    skipped, its ``else:`` still walked. A lazy import inside a function is
    the sanctioned escape hatch the loader test's comment describes, and
    stays invisible on purpose. Expression nodes are descended too, which is
    harmless: an import is only ever a statement.
    """
    for child in ast.iter_child_nodes(node):
        yield from _load_time_statements_under(child)


def _load_time_statements_under(child: ast.AST) -> Iterator[ast.stmt]:
    """``child`` itself if it is a statement, then whatever runs on import beneath it.

    One visitor for every node however it was reached — a module's child or a
    statement in a TYPE_CHECKING guard's ``else:`` — so the two skip rules
    (function bodies; the guard's own body) apply at every level. The first
    cut recursed straight into the ``else:`` statements, so a ``def`` or a
    nested ``if TYPE_CHECKING:`` placed there was walked as load-time.
    """
    if isinstance(child, ast.stmt):
        yield child
    if isinstance(child, _DEFERRED_BODIES):
        return
    if isinstance(child, ast.If) and _is_type_checking_guard(child.test):
        for stmt in child.orelse:
            yield from _load_time_statements_under(stmt)
        return
    yield from _load_time_statements(child)


def test_the_walk_reaches_every_suite_that_runs_on_import_but_not_a_function_body(tmp_path):
    # Issue #166. No real module is written this way today — the only
    # module-level compound statements holding an import are the nine
    # ``if TYPE_CHECKING:`` blocks, which correctly never run — so the real
    # closures do not move, and the shapes are pinned on a synthetic tree.
    # Reached: every suite of a ``try``, both suites of an ``if``, a ``with``,
    # a ``for``, a ``match`` case, a ``try`` nested in an ``if``, a CLASS body
    # (it runs at import, unlike a function's), the ``else:`` of a
    # TYPE_CHECKING guard, and an ``if`` on a runtime flag that merely shares
    # the name. Not reached: the body of ``if TYPE_CHECKING:`` in both
    # spellings, a function body, a method body inside a walked class, and a
    # function body or nested guard body placed in a TYPE_CHECKING ``else:``
    # (the skip rules apply at every level, not only to a module's children).
    # Discriminating: a ``.body``-only walk sees only ``top``; a walk that
    # names node types drops ``match``/``klass`` — the shapes the first cut
    # of this walk missed. ``FLAG``/``ctx``/``settings`` are never evaluated:
    # this is ``ast.parse``, not an import.
    (tmp_path / "a.py").write_text(
        "import typing\n"
        "from typing import TYPE_CHECKING\n"
        "from .top import t\n"
        "try:\n"
        "    from .try_body import a\n"
        "except ImportError:\n"
        "    from .handler import b\n"
        "else:\n"
        "    from .try_else import c\n"
        "finally:\n"
        "    from .finally_ import d\n"
        "if FLAG:\n"
        "    from .plain_if import e\n"
        "else:\n"
        "    from .plain_else import f\n"
        "with ctx():\n"
        "    from .with_ import g\n"
        "for _ in ():\n"
        "    from .loop import h\n"
        "match FLAG:\n"
        "    case 1:\n"
        "        from .matched import i\n"
        "if FLAG:\n"
        "    try:\n"
        "        from .nested import j\n"
        "    except ImportError:\n"
        "        pass\n"
        "class C:\n"
        "    from .klass import k\n"
        "    def method(self):\n"
        "        from .method import m\n"
        "if TYPE_CHECKING:\n"
        "    from .tc_name import n\n"
        "else:\n"
        "    from .tc_else import o\n"
        "    def lazy_in_else():\n"
        "        from .tc_else_func import s\n"
        "    if TYPE_CHECKING:\n"
        "        from .tc_else_nested import u\n"
        "if typing.TYPE_CHECKING:\n"
        "    from .tc_attr import p\n"
        "if settings.TYPE_CHECKING:\n"
        "    from .runtime_flag import q\n"
        "def lazy():\n"
        "    from .func import r\n",
        encoding="utf-8",
    )
    reached = {
        "top",
        "try_body",
        "handler",
        "try_else",
        "finally_",
        "plain_if",
        "plain_else",
        "with_",
        "loop",
        "matched",
        "nested",
        "klass",
        "tc_else",
        "runtime_flag",
    }
    for name in reached | {"method", "tc_name", "tc_attr", "func", "tc_else_func", "tc_else_nested"}:
        (tmp_path / f"{name}.py").write_text("", encoding="utf-8")
    assert _load_time_import_closure(tmp_path / "a.py", root=tmp_path) == reached


def test_common_imports_nothing_from_the_rest_of_the_package():
    # Structural check on the import statements themselves (not runtime state,
    # which depends on what happens to be imported first): a relative import
    # reaching above common/ (level >= 2) or an absolute import of the contrib
    # package both violate the bottom-of-the-import-graph rule. Sibling
    # imports inside common/ (level 1) stay legal.
    #
    # NOTE the predicate here is deliberately WIDER than _package_tail above:
    # common/ may import no contrib package at all, while config.py may import
    # an allowlisted few from THIS package. Sharing _PACKAGE keeps the root
    # spelled once without pretending the two rules are the same rule.
    def is_contrib(name: str | None) -> bool:
        return _within(name, "contrib")

    offenders = []
    for source in package_sources(common_pkg):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level >= 2 or is_contrib(node.module):
                    offenders.append(f"{source.name}: from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{source.name}: import {alias.name}"
                    for alias in node.names
                    if is_contrib(alias.name)
                )
    assert not offenders, offenders


# --- Ratchets: the layering debt of 2026-09-22, frozen so it can only shrink --
#
# Refactor plan v2, T0. Each allowlist is compared by EQUALITY: a new entry
# fails (move the thing down instead — ``runtime/`` for a paper symbol, a
# ``repository`` read for SQL, an injected collaborator for a cli private),
# and a retired entry fails too, so the list is pruned in the PR that pays
# the debt off rather than going stale.


def _ratchet_message(what: str, found: set[str], frozen: frozenset[str]) -> str:
    return f"{what}: new {sorted(found - frozen)}, retired {sorted(frozen - found)}"


_LIVE_PAPER_IMPORTS = frozenset(
    {
        "paper.stops.StopAction",
        "paper.stops.StopConfig",
        "paper.stops.round_to_tick",
        "paper.stops.stop_loss_decision",
        "paper.stops.take_profit_price",
        "paper.twap.MAX_SLICES",
        "paper.twap.PlanDisposition",
        "paper.twap.Side",
        "paper.twap.build_slice_plan",
        "paper.twap.floor_to_step",
        "paper.twap.rebalance_delta",
        "paper.twap.split_flip_budget",
        "paper.validation.prompt_regime_lines",
    }
)
# The store's upward edges are keyed by DIRECTION — every package above it
# that it reaches, ``paper`` and (since refactor plan v2 T1) ``runtime`` — so
# a symbol that moves between the two stays counted rather than dropping
# out of sight.
_PERSISTENCE_UPWARD_PACKAGES = ("paper", "runtime")
_PERSISTENCE_UPWARD_IMPORTS = frozenset(
    {
        "runtime.accounting.AccountMetrics",
        "runtime.decision.DecisionInput",
    }
)
# ``ports.py`` names two DTOs the layer above it owns, annotation-only (its
# TYPE_CHECKING block says why). Frozen so the edge cannot grow unnoticed;
# whether the two belong lower is a plan question, not this test's.
_PORTS_RUNTIME_IMPORTS = frozenset(
    {
        "runtime.decision.DecisionInput",
        "runtime.market_feed.SnapshotResult",
    }
)


def _symbols_imported_from(source: Path, pkg: str, root: Path = _SOURCE_ROOT) -> set[str]:
    """``<module>.<name>`` for every name ``source`` takes from the package ``pkg``.

    The whole tree, not the load-time closure: a lazy or ``TYPE_CHECKING``
    import is the same dependency for this purpose. A MODULE or PACKAGE bound
    as a name (``from ..paper import accounting``, ``from .. import paper``)
    is expanded into the attribute chains read off it
    (``paper.accounting.replay_within``), so a symbol reached that way counts
    like one imported by name. A module bound but never read stays as its
    bare tail unless a sibling statement reads deeper into it; a plain
    ``import`` without an alias always stays — its reads are spelled through
    ``contrib``, which no binding here tracks.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    plain: set[str] = set()  # unaliased ``import x.y``: never pruned
    modules: dict[str, str] = {}  # bound name -> module tail
    for base, alias, from_import in _imports(ast.walk(tree), _own_package(source, root)):
        if base is None:
            continue
        target = base
        if from_import:
            target = f"{base}.{alias.name}" if base else alias.name
        if not _within(target, pkg):
            continue
        bound = alias.asname or (alias.name if from_import else None)
        if bound is None:
            plain.add(target)
        elif _module_file(target, root) is not None:
            modules[bound] = target
        else:
            found.add(target)
    for node in ast.walk(tree):
        read = _module_read(node, modules, root)
        if read is not None:
            found.add(read)
    found.update(modules.values())
    # A module tail that only prefixes a deeper read (``paper`` under
    # ``paper.engine.AssetSpec``) is the road, not a borrowed name.
    prefixes = {
        s
        for s in found - plain
        if _module_file(s, root) is not None and any(o.startswith(s + ".") for o in found)
    }
    return (found - prefixes) | plain


def _module_read(node: ast.AST, modules: dict[str, str], root: Path) -> str | None:
    """The symbol an attribute chain rooted at a name ``modules`` binds reads, else ``None``.

    The chain is followed only as far as the modules on disk go, plus one
    segment: ``accounting.AccountMetrics.from_row`` is a read of
    ``runtime.accounting.AccountMetrics``, and ``from_row`` is that class's
    business.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not (isinstance(node, ast.Name) and node.id in modules):
        return None
    tail = modules[node.id]
    for part in reversed(parts):
        tail = f"{tail}.{part}"
        if _module_file(tail, root) is None:
            break
    return tail


@pytest.mark.parametrize(
    ("pkg", "targets", "frozen"),
    [
        (live_pkg, ("paper",), _LIVE_PAPER_IMPORTS),
        (persistence_pkg, _PERSISTENCE_UPWARD_PACKAGES, _PERSISTENCE_UPWARD_IMPORTS),
    ],
    ids=["live", "persistence"],
)
def test_the_upward_edges_carry_exactly_the_symbols_frozen_on_2026_09_22(pkg, targets, frozen):
    found = {
        symbol
        for source in package_sources(pkg)
        for target in targets
        for symbol in _symbols_imported_from(source, target)
    }
    assert found == frozen, _ratchet_message(f"{pkg.__name__}'s upward imports", found, frozen)


def test_ports_names_exactly_the_runtime_types_frozen_on_2026_09_23():
    found = _symbols_imported_from(_SOURCE_ROOT / "ports.py", "runtime")
    assert found == _PORTS_RUNTIME_IMPORTS, _ratchet_message(
        "ports.py's runtime imports", found, _PORTS_RUNTIME_IMPORTS
    )


# The engine adapter borrows one name from the lanes: the decision provider
# prices the prompt's position section with the paper fill model's cost
# assumptions on both lanes (PR #160, issue #161).
_INTEGRATION_LANE_IMPORTS = frozenset({"paper.config.PaperTradingConfig"})


def test_integration_borrows_exactly_the_lane_names_frozen_on_2026_09_24():
    found = {
        symbol
        for source in package_sources(integration_pkg)
        for pkg in ("paper", "live")
        for symbol in _symbols_imported_from(source, pkg)
    }
    assert found == _INTEGRATION_LANE_IMPORTS, _ratchet_message(
        "integration's lane imports", found, _INTEGRATION_LANE_IMPORTS
    )


def test_the_symbol_scan_reaches_every_import_shape(tmp_path):
    # No live module is written in the absolute or plain-``import`` forms
    # today, so the shapes are pinned on a synthetic tree.
    for pkg in ("paper", "live"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("", encoding="utf-8")
    for module in ("accounting", "twap", "engine", "stops"):
        (tmp_path / "paper" / f"{module}.py").write_text("", encoding="utf-8")
    (tmp_path / "live" / "x.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "import sqlite3\n"
        "from ..paper.clock import Clock, WallClock\n"
        "from ..paper import accounting, twap as tw\n"
        "from .. import paper, common\n"
        "from ..persistence import db\n"
        "from contrib.hyperliquid_perp.paper.stops import round_to_tick\n"
        "import contrib.hyperliquid_perp.paper.stops as st\n"
        "import contrib.hyperliquid_perp.paper.stops\n"
        "import contrib.hyperliquid_perp.paper.market_feed\n"
        "if TYPE_CHECKING:\n"
        "    from ..paper.engine import AssetSpec\n"
        "def f():\n"
        "    from ..paper.position_facts import read_books\n"
        "    paper.engine.FundingSource\n"
        "    accounting.AccountMetrics.from_row(st.StopConfig)\n"
        "    return accounting.replay_within(accounting.summarize_account(db.x))\n",
        encoding="utf-8",
    )
    assert _symbols_imported_from(tmp_path / "live" / "x.py", "paper", root=tmp_path) == {
        "paper.clock.Clock",
        "paper.clock.WallClock",
        "paper.accounting.AccountMetrics",  # the chain stops at the first non-module
        "paper.accounting.replay_within",
        "paper.accounting.summarize_account",
        "paper.twap",  # bound, never read
        "paper.stops.round_to_tick",
        "paper.stops.StopConfig",  # read through an aliased plain import
        "paper.stops",  # the unaliased plain import stays beside the deeper reads
        "paper.market_feed",
        "paper.engine.AssetSpec",
        "paper.engine.FundingSource",  # read through the package binding
        "paper.position_facts.read_books",
    }


_SQL_SITES_OUTSIDE_PERSISTENCE = {
    "live/validation.py": 9,
    "paper/validation.py": 21,
    "runtime/no_decision.py": 2,
    "runtime/run_lock.py": 2,
}
# A site is a cursor ``execute*`` call or a string literal that opens with a
# SQL statement. Counted, not flagged, so a statement added to a module
# already on the list still moves its number, and one moved into
# ``repository`` moves it back.
_SQL_STATEMENT = re.compile(
    r"\s*(SELECT\b|INSERT INTO\b|UPDATE \S+ SET\b|DELETE FROM\b"
    r"|CREATE (TABLE|INDEX|UNIQUE)\b|ALTER TABLE\b|PRAGMA \w|WITH \w+ AS\b)"
)
_CURSOR_CALLS = frozenset({"execute", "executemany", "executescript"})


def _sql_sites(source: Path) -> int:
    sites = 0
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            sites += node.func.attr in _CURSOR_CALLS
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            sites += bool(_SQL_STATEMENT.match(node.value))
    return sites


def test_sql_sites_outside_persistence_count_exactly_what_they_did_on_2026_09_22():
    # ``persistence/`` owns SQL by design; ``tests/`` may write it to stage a store.
    found: dict[str, int] = {}
    for path in package_sources(perp_pkg):
        rel = path.relative_to(_SOURCE_ROOT)
        if rel.parts[0] not in ("persistence", "tests") and (sites := _sql_sites(path)):
            found[rel.as_posix()] = sites
    frozen = _SQL_SITES_OUTSIDE_PERSISTENCE
    moved = {
        m: (frozen.get(m, 0), found.get(m, 0))
        for m in frozen.keys() | found.keys()
        if frozen.get(m, 0) != found.get(m, 0)
    }
    assert not moved, f"SQL sites outside persistence, (frozen, now): {moved}"


@pytest.mark.parametrize(
    ("body", "sites"),
    [
        ('"""Update the books."""\n', 0),  # prose, not a statement
        ('x = "select 1"\n', 0),  # lower-case is not SQL here
        ('x = "UPDATE the operator"\n', 0),  # a verb alone is not a statement
        ('x = "SELECTED"\n', 0),  # the verb ends at a word boundary
        ("rows = conn.execute(q)\n", 1),
        ("conn.executemany(q, rows)\n", 1),
        ('q = "SELECT 1"\n', 1),
        ('q = f"SELECT * FROM {table}"\n', 1),
        ('q = "  WITH t AS (SELECT 1) SELECT * FROM t"\n', 1),
        ('q = "ALTER TABLE t ADD COLUMN c"\n', 1),
        ('conn.execute("UPDATE t SET a = 1")\n', 2),  # the call and its literal
    ],
)
def test_the_sql_scan_counts_calls_and_statements_but_not_prose(tmp_path, body, sites):
    (tmp_path / "m.py").write_text(body, encoding="utf-8")
    assert _sql_sites(tmp_path / "m.py") == sites


_CLI_PRIVATE_REEXPORTS = frozenset(
    {
        "_EngineDecisionProvider",
        "_HARD_DRIFT_KINDS",
        "_HistoryFundingSource",
        "_LIVE_TICK_SECONDS",
        "_RECOVERY_MAX_TICK_GAP_SECONDS",
        "_UNVERIFIED_MARKER",
        "_build_real_smoke_session",
        "_build_smoke_session",
        "_classify_engine_error",
        "_cmd_export",
        "_cmd_live",
        "_cmd_live_smoke",
        "_cmd_paper",
        "_cmd_safe_mode",
        "_cmd_validate",
        "_config_drift_report",
        "_conflicting_run_lease",
        "_contain_as_recoverable_safe_mode",
        "_day_baseline_from_exchange",
        "_existing_run_row",
        "_live_heartbeat",
        "_live_startup_recovery",
        "_mark_export_verification",
        "_migrate_owned_store",
        "_norm_network",
        "_open_existing_db",
        "_paper_loop",
        "_post_cycle_export",
        "_print_smoke_gate",
        "_raise_keyboard_interrupt",
        "_require_agent_key",
        "_require_api_key",
        "_require_live_run_mode",
        "_retry_pending_funding",
        "_run_config_subset",
        "_run_genesis_network",
        "_run_live_loop",
        "_smoke_gate_buckets",
        "_smoke_startup_recovery",
        "_stamp_breadcrumb",
        "_stamp_reconciliation_case",
        "_still_owns_run",
        "_timing_preflight",
        "_validate_live",
    }
)


def _private_import_bindings(source: Path, root: Path = _SOURCE_ROOT) -> set[str]:
    """The underscore names ``source`` binds at load time by importing.

    The BINDING is what a test monkeypatches: ``asname`` when aliased, else
    the name, and for ``import a.b`` the top package ``a``.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    found: set[str] = set()
    for _base, alias, from_import in _imports(_load_time_statements(tree), _own_package(source, root)):
        bound = alias.asname or (alias.name if from_import else alias.name.split(".")[0])
        if bound.startswith("_"):
            found.add(bound)
    return found


def test_the_cli_package_reexports_exactly_the_private_names_frozen_on_2026_09_22():
    # The ``_cmd_*`` targets are read by ``main()`` in the same file; every
    # other name is re-exported so a test can IMPORT it from the package
    # (PR #75). Patch targets are the defining submodules, never these.
    found = _private_import_bindings(_SOURCE_ROOT / "cli" / "__init__.py")
    assert found == _CLI_PRIVATE_REEXPORTS, _ratchet_message(
        "cli/__init__'s private re-exports", found, _CLI_PRIVATE_REEXPORTS
    )


def test_the_private_binding_scan_reads_the_bound_name_not_the_imported_one(tmp_path):
    # ``cli/__init__`` aliases nothing today, so the alias rules are pinned on
    # a synthetic module.
    (tmp_path / "m.py").write_text(
        "from ._a import _x, y, _z as w, q as _r\n"
        "import _m.sub\n"
        "import pkg.mod as _alias\n"
        "def f():\n"
        "    from ._b import _lazy\n",
        encoding="utf-8",
    )
    assert _private_import_bindings(tmp_path / "m.py", root=tmp_path) == {
        "_x",
        "_r",
        "_m",
        "_alias",
    }
