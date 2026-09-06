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
  engines (issue #122).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp import common as common_pkg
from contrib.hyperliquid_perp.common import decimal_context
from contrib.hyperliquid_perp.domains.perp import margin
from contrib.hyperliquid_perp.persistence import models

from ..conftest import package_sources

_PACKAGE = "contrib.hyperliquid_perp"


def _within(name: str | None, pkg: str) -> bool:
    """``name`` is the package ``pkg`` itself or a dotted name inside it.

    The one spelling of "is `pkg` or lies under `pkg`" the three predicates
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
    walk_packages_of(".".join(source.resolve().relative_to(root).parent.parts))
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
        "common/no_decision.py",
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
    # any of the three fails here by name. (``common/`` is also covered by the
    # tree-wide check below; it is listed so the policy's acceptance is stated
    # once, beside the guards it serves.)
    closure = _load_time_import_closure(_SOURCE_ROOT / module)
    offenders = {t for t in closure if _above_the_floor(t)}
    assert not offenders, f"{module} reaches above domains/common at load time: {sorted(offenders)}"


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
    dotted tail an allowlist is written in. A relative import is resolved
    against the module's own package depth, so ``from .schema import x``
    inside ``domains/perp/`` and ``from ...common.constants import y`` come
    back as ``domains.perp.schema`` / ``common.constants``.

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
    own_package = source.resolve().relative_to(root).parent.parts

    def tail(name: str | None, level: int) -> str | None:
        if level == 0:
            return _package_tail(name)
        # ``level`` dots: one for the module's own package, each further one
        # climbs a package. Climbing past the package root is an ImportError
        # at runtime; resolve it to the root ("") rather than let a negative
        # slice bound silently drop packages from the END of the path.
        base = list(own_package[: max(0, len(own_package) - (level - 1))])
        return ".".join([*base, name] if name else base)

    def imported(base: str | None, name: str) -> str | None:
        if base is None:
            return None
        submodule = f"{base}.{name}" if base else name
        return submodule if _module_file(submodule, root) is not None else base

    found: set[str] = set()
    for node in _load_time_statements(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            base = tail(node.module, node.level)
            names = [imported(base, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [_package_tail(alias.name) for alias in node.names]
        else:
            continue
        found.update(n for n in names if n is not None)
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
