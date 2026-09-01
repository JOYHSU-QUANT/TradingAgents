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
    # module rather than widening this set. ``market_data_config`` is the one
    # parser on the list — it runs on every load (the block is always
    # present), so a lazy import would buy nothing — and ``schema`` is the
    # stdlib-only DTO module it reaches for the candle-interval vocabulary.
    #
    # The set is checked as a CLOSURE, not as config.py's direct imports
    # alone: every admitted module's own in-package imports must stay inside
    # the same set. Otherwise a ``from .volume_profile import ...`` added to
    # ``schema`` or to the parser would drag the compute module into every
    # load while both files' direct import lists looked innocent.
    #
    # TOP-LEVEL statements only, unlike the ``common/`` check below which walks
    # the whole tree. The invariant here is about what merely IMPORTING
    # config.py costs, and a lazy import inside a branch is this repo's
    # sanctioned escape hatch — ``load_config`` already uses it for
    # ``live.config``/``risk_gate``, precisely so ``--context-only`` does not
    # pay for the risk-gate domain unless a ``live:`` block exists.
    allowed = {
        "common.config_coercion",
        "common.constants",
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
    """Every in-package module ``source`` imports at top level, transitively.

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
    """The file under ``root`` a dotted tail names: ``x.py``, or ``x/__init__.py`` for a package.

    ``None`` for the root itself (``""``) and for a tail nothing on disk
    answers to.
    """
    if not tail:
        return None
    module = root.joinpath(*tail.split("."))
    if module.with_suffix(".py").is_file():
        return module.with_suffix(".py")
    if (module / "__init__.py").is_file():
        return module / "__init__.py"
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
    # would walk into). The tree also plants an import in an ANCESTOR package's
    # ``__init__`` (``domains/``): the interpreter runs it before any module
    # below it, so the walk must reach it even though no tail names it.
    for pkg in ("common", "domains", "domains/perp"):
        (tmp_path / pkg).mkdir()
        (tmp_path / pkg / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "domains" / "__init__.py").write_text("from ..paper import p\n", encoding="utf-8")
    perp = tmp_path / "domains" / "perp"
    (perp / "schema.py").write_text("from ...persistence import db\n", encoding="utf-8")
    (perp / "guard.py").write_text(
        "from . import schema\n"
        "from .. import perp\n"
        "from ...common import a\n"
        "from ... import audit\n"
        "from ...commonplace import q\n",
        encoding="utf-8",
    )
    closure = _load_time_import_closure(perp / "guard.py", root=tmp_path)
    assert closure == {
        "domains.perp.schema",
        "persistence",  # reached THROUGH schema: the second hop
        "paper",  # reached through the ancestor ``domains/__init__.py``
        "domains.perp",
        "common",
        "",
        "commonplace",
    }
    assert {t for t in closure if _above_the_floor(t)} == {
        "persistence",
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
    """Dotted tails (``domains.perp.x``) of ``source``'s TOP-LEVEL in-package imports.

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
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ImportFrom):
            base = tail(node.module, node.level)
            names = [imported(base, alias.name) for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [_package_tail(alias.name) for alias in node.names]
        else:
            continue
        found.update(n for n in names if n is not None)
    return found


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
