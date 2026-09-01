"""The machine-disposition vocabulary scan over the ``live`` package (issues #84, #104).

``MACHINE_DISPOSITIONS`` decides BY STRING whether a reconciliation fact key
may reopen, so a disposition a sweep writes but the set does not classify
shuts its key forever (#65's defect). The scan below reads the package's
SOURCE for every literal that could reach the ``action_taken`` column and
checks each against the set. It lives at package level because its reach is
the whole ``live`` package: a disposition written tomorrow in ``fills.py``
fails here, not in a test named after ``reconcile`` (issue #151). What binds
to ``reconcile``'s own module object — the import-time constant loop and the
construction-time refusal — stays in ``test_reconcile.py``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil

import pytest

from contrib.hyperliquid_perp import live as live_pkg
from contrib.hyperliquid_perp.live import reconcile as reconcile_mod
from contrib.hyperliquid_perp.persistence import repository as repo

from ..conftest import package_sources

# The callables whose POSITIONAL ``action_taken`` the scan must resolve, bound
# against their REAL signatures (``inspect.signature(...).bind_partial``) so a
# reordered field or parameter moves the scan with it — nothing here spells
# an index (issue #104).
_DISPOSITION_WRITERS = {
    "ReconciliationCase": inspect.signature(reconcile_mod.ReconciliationCase),
    "set_reconciliation_action": inspect.signature(repo.set_reconciliation_action),
    "stamp_reconciliation_action_if_unset": inspect.signature(
        repo.stamp_reconciliation_action_if_unset
    ),
}
# Nothing the scan can report appears in a module without one of these.
_SCAN_TOKENS = ("action_taken", *_DISPOSITION_WRITERS)


def _string_literals(value: ast.expr) -> set[str]:
    # Node-wise, not value-wise: skipping the whole value on seeing an
    # f-string would drop ``"foo"`` from ``"foo" if x else f"settled_{y}"``
    # — the same silent skip the conditional descent exists to prevent.
    if isinstance(value, ast.JoinedStr):
        return set()
    if isinstance(value, ast.Constant):
        return {value.value} if isinstance(value.value, str) else set()
    return {s for child in ast.iter_child_nodes(value) for s in _string_literals(child)}


def _disposition_argument(node: ast.Call) -> ast.expr | None:
    """The expression ``node`` passes as ``action_taken``, however it is passed."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    signature = _DISPOSITION_WRITERS.get(name)
    if signature is None:
        keyword = next((k for k in node.keywords if k.arg == "action_taken"), None)
        return None if keyword is None else keyword.value
    if any(isinstance(a, ast.Starred) for a in node.args) or any(
        k.arg is None for k in node.keywords
    ):
        raise AssertionError(
            f"{name}(...) at line {node.lineno} is called with *args/**kwargs: the "
            "vocabulary scan cannot see where action_taken lands — spell the call out"
        )
    try:
        bound = signature.bind_partial(*node.args, **{k.arg: k.value for k in node.keywords})
    except TypeError as exc:
        # A call the real signature rejects could never run; say where it is
        # rather than surfacing inspect's context-free message.
        raise AssertionError(
            f"{name}(...) at line {node.lineno} does not fit its signature: {exc}"
        ) from exc
    return bound.arguments.get("action_taken")


def _machine_disposition_literals(source: str) -> set[str]:
    """Every string literal ``source`` could hand the sweep as a machine disposition.

    Three shapes reach the ``action_taken`` column, and the scan sees all of
    them (issue #104 closed the last two, which the #84 scan was blind to):

    1. an ``action_taken=`` KEYWORD on any call — ``ReconciliationCase`` and
       ``insert_exchange_reconciliation_event`` alike;
    2. a ``ReconciliationCase(...)`` POSITIONAL argument in the field's slot;
    3. the positional ``action_taken`` of the two repository stamp writers,
       ``set_reconciliation_action`` and ``stamp_reconciliation_action_if_unset``.

    Calls are matched by UNQUALIFIED name (``repo.x(...)`` and ``x(...)``
    alike) and positionals are resolved against the real signature. Stated
    exactly so nobody reads it as more: names and attributes (a module
    constant, ``case.action_taken``) are not literals and are not resolved —
    constants are the import-time loop's job, computed values the runtime
    guard's; f-strings are skipped (``f"settled_{status}"`` is derived in
    ``_vocab`` and guarded at runtime); a literal forwarded through a local
    wrapper or an aliased import is invisible (the repository writers check
    only non-emptiness, so such a wrapper must not be introduced); a starred
    call fails the scan loudly rather than being skipped; and any literal
    nested inside the argument (a helper's own string arguments) IS reported —
    loud in the wrong direction, never silent.
    """
    if not any(token in source for token in _SCAN_TOKENS):
        return set()
    literals: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            value = _disposition_argument(node)
            if value is not None:
                literals |= _string_literals(value)
    return literals


def test_every_disposition_the_sweep_writes_is_in_the_machine_vocabulary():
    # Issue #84. The set decides by STRING whether a fact key reopens, so a
    # machine stamp missing from the classification shuts its key forever —
    # #65's defect, returning silently for that one disposition.
    #
    # Reads the SOURCE rather than a hand-kept list, because a hand-kept list
    # is a third copy with the same drift problem: it would pass while a NEW
    # disposition added at a call site went unclassified, which is precisely
    # what #84 says the predecessor test failed to catch.
    #
    # Reach and ownership: the module docstring. Shapes and exclusions:
    # ``_machine_disposition_literals``.
    written_in: dict[str, set[str]] = {}
    for module in package_sources(live_pkg):
        for literal in _machine_disposition_literals(module.read_text(encoding="utf-8")):
            written_in.setdefault(literal, set()).add(module.name)
    # Named, not counted: a bare ``>= 3`` would fail on the very refactor this
    # module keeps doing (hoisting a literal into a module constant, which
    # makes it BETTER protected), while still passing if a fourth literal was
    # added unclassified. The membership assertion below is what carries the
    # weight; this only proves the scan still sees the module.
    assert {"local_row_reopened", "settled_never_sent"} <= written_in.keys(), sorted(written_in)
    unclassified = {
        literal: sorted(modules)
        for literal, modules in written_in.items()
        if literal not in repo.MACHINE_DISPOSITIONS
    }
    assert not unclassified, f"unclassified machine dispositions: {unclassified}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # #104-2 (3): a literal handed positionally to a repository stamp
        # writer — the shape a module other than reconcile.py would use (none
        # does today; #104's "fills.py already calls it" was never the case).
        pytest.param(
            "repo.set_reconciliation_action(tx, event_id, 'made_it_up')",
            {"made_it_up"},
            id="stamp-writer-positional",
        ),
        pytest.param(
            "repo.stamp_reconciliation_action_if_unset(tx, event_id, 'made_it_up')",
            {"made_it_up"},
            id="if-unset-positional",
        ),
        pytest.param(
            "repo.set_reconciliation_action(tx, event_id, action_taken='made_it_up')",
            {"made_it_up"},
            id="stamp-writer-keyword",
        ),
        # #104-2 (1): the positional construction, with a DIFFERENT literal in
        # the slot before ``action_taken`` (``detail``) that the scan must not
        # mistake for a disposition — the signature, not a number, decides.
        # This param is the signature-drift pin #104 asked for: a field added
        # before ``action_taken`` shifts the literal out of its slot and fails.
        pytest.param(
            "ReconciliationCase('orphan_exchange_order', 'BTC', None, '0xab', "
            "'some detail prose', 'made_it_up' if resolved else None)",
            {"made_it_up"},
            id="case-positional",
        ),
        pytest.param(
            "ReconciliationCase('orphan_exchange_order', 'BTC', None, '0xab', 'some detail prose')",
            set(),
            id="case-positional-short",
        ),
        # A writer the map does not know still contributes its keyword.
        pytest.param(
            "repo.insert_exchange_reconciliation_event(conn, run_id=r, action_taken='made_it_up')",
            {"made_it_up"},
            id="unmapped-call-keyword",
        ),
        # Negative control: the shapes production really writes that carry NO
        # literal must not be reported, or the scan would fail on the exact
        # hoisting refactor that makes a disposition better protected.
        pytest.param(
            "repo.set_reconciliation_action(conn, existing['event_id'], case.action_taken)\n"
            "repo.stamp_reconciliation_action_if_unset(tx, event_id, _READ_SUCCEEDED_DISPOSITION)\n"
            "ReconciliationCase(case_type='x', symbol='BTC', local_value=None, exchange_value=k,\n"
            "                   action_taken=_ORPHAN_BACKFILLED_DISPOSITION if resolved else None)\n",
            set(),
            id="names-and-computed-values",
        ),
    ],
)
def test_the_scan_resolves_every_call_shape(source, expected):
    assert _machine_disposition_literals(source) == expected


def test_every_repository_writer_with_a_positional_action_taken_is_mapped():
    # The map spells NAMES, and a name it lacks is a positional slot the scan
    # never resolves — the blind spot the helper's docstring admits for
    # wrappers, opened silently by the next repository writer. So the
    # repository half is derived from the package: every public repository
    # function — in the facade or in any submodule, since a writer reachable
    # by its module path is a writer — that takes ``action_taken`` other than
    # keyword-only (the generic keyword catch covers those) must be in the
    # map. ``ReconciliationCase`` is the one non-repository entry.
    modules = [
        repo,
        *(
            importlib.import_module(f"{repo.__name__}.{m.name}")
            for m in pkgutil.iter_modules(repo.__path__)
        ),
    ]
    positional = {
        name
        for module in modules
        for name, function in inspect.getmembers(module, inspect.isfunction)
        if function.__module__.startswith(repo.__name__)
        and not name.startswith("_")
        and any(
            p.name == "action_taken" and p.kind is not p.KEYWORD_ONLY
            for p in inspect.signature(function).parameters.values()
        )
    }
    assert positional == _DISPOSITION_WRITERS.keys() - {"ReconciliationCase"}


def test_the_scan_refuses_a_call_it_cannot_read():
    # A starred call would put ``action_taken`` in a slot the AST cannot
    # locate; skipping it would be the silent miss the scan exists to close.
    with pytest.raises(AssertionError, match="cannot see"):
        _machine_disposition_literals("repo.set_reconciliation_action(*head, 'made_it_up')")
    # A call the signature rejects could never run; it is reported with its
    # line rather than as inspect's context-free TypeError.
    with pytest.raises(AssertionError, match="line 1 does not fit"):
        _machine_disposition_literals("repo.set_reconciliation_action(tx, 1, 'x', 'extra')")
