"""The smoke-test registry and its cycle-entry gate (phase3-spec §20.2).

:data:`SMOKE_TESTS` is the checklist :mod:`.smoke` runs;
:func:`smoke_gate_report` reads the latest non-dry-run verdict per registered
test; :func:`validate_only_keys` and :func:`rerun_keys_for` are the ``--only``
selection rules. Nothing here touches the exchange.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple

from ..persistence import repository as repo

__all__ = [
    "SMOKE_TESTS",
    "SMOKE_TEST_KEYS",
    "SmokeGateReport",
    "SmokeTest",
    "rerun_keys_for",
    "smoke_gate_report",
    "validate_only_keys",
]


@dataclass(frozen=True)
class SmokeTest:
    """One §20.2 checklist item: its 1-based number, stable key, and label."""

    number: int
    key: str
    name: str


# §20.2's seventeen items verbatim, plus test 18 (emergency close, see the
# ``smoke`` module docstring). The KEY is the stable identity the store and the
# validator read; the NUMBER and NAME are for operator-facing reports. Order is
# execution order: a slice submitted in test 3 is the order test 4 queries, so
# the sequence is meaningful and the runner honours it.
SMOKE_TESTS: tuple[SmokeTest, ...] = (
    SmokeTest(1, "signed_client_init", "signed client initialization (incl. §6.1 authorization)"),
    SmokeTest(2, "update_leverage", "updateLeverage"),
    SmokeTest(3, "slice_order_submit", "slice order submit (IOC limit + cloid)"),
    SmokeTest(4, "slice_order_status", "slice order status check (orderStatus by cloid)"),
    SmokeTest(5, "slice_plan_cancel", "slice plan cancel (cancel an unfilled resting order)"),
    SmokeTest(6, "multi_slice_fill", "small entry / multi-slice fill"),
    SmokeTest(7, "reduce_only_close", "reduce-only close"),
    SmokeTest(8, "stop_loss_create", "SL create"),
    SmokeTest(9, "stop_loss_modify", "SL modify"),
    SmokeTest(10, "stop_loss_cancel", "SL cancel"),
    SmokeTest(11, "take_profit_create", "TP create"),
    SmokeTest(12, "take_profit_modify", "TP modify"),
    SmokeTest(13, "take_profit_cancel", "TP cancel"),
    SmokeTest(14, "kill_switch_arm_refresh", "scheduleCancel arm / refresh"),
    SmokeTest(15, "restart_reconciliation", "restart reconciliation"),
    SmokeTest(16, "startup_with_existing_position", "startup with existing position"),
    SmokeTest(17, "startup_with_stale_open_order", "startup with stale bot-owned order"),
    SmokeTest(18, "emergency_close", "emergency close (aggressive reduce-only IOC, §17.2)"),
    SmokeTest(19, "maker_slice_post_cancel", "post-only (Alo) slice rests, is listed, cancels"),
    SmokeTest(
        20,
        "maker_slice_post_only_refusal",
        "post-only (Alo) slice that would cross is refused by name",
    ),
)

# The stable identities, in one place: the gate iterates them, the validator
# maps §20.3 booleans through a subset of them, and --only validates against them.
SMOKE_TEST_KEYS: tuple[str, ...] = tuple(t.key for t in SMOKE_TESTS)
_BY_KEY: dict[str, SmokeTest] = {t.key: t for t in SMOKE_TESTS}


# --------------------------------------------------------------------------
# Cycle-entry gate (§20.2: all smoke tests must pass)
# --------------------------------------------------------------------------


class SmokeGateReport(NamedTuple):
    """The §20.2 gate verdict, slot by NAME.

    Three of the four slots share the same ``tuple[str, ...]`` type — a bare
    tuple would let a transposed return (or a mis-ordered destructuring at a
    new call site) swap ``missing``/``failed``/``errored`` silently past the
    type checker, mislabeling why a smoke test is red in an operator-facing
    real-money go/no-go report. Still positionally unpackable (a plain
    ``NamedTuple``), so existing ``passed, missing, failed, errored = ...``
    call sites are unaffected (2026-07-30 type-design pass).
    """

    passed: bool
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    errored: tuple[str, ...]


def smoke_gate_report(conn: Any, run_id: str) -> SmokeGateReport:
    """``(passed, missing_keys, failed_keys, errored_keys)`` for the §20.2 gate.

    ``passed`` is True only when every :data:`SMOKE_TESTS` key has a latest
    non-dry-run row of ``passed``. The non-passed keys are split three ways so an
    operator at a real-money go/no-go can triage without querying the DB:
    ``missing`` never ran for real (a dry-run row does not count), ``errored``
    ran but the harness itself broke (status ``error`` — a code bug to fix), and
    ``failed`` ran but the exchange refused a well-formed action (status
    ``failed`` or anything else — a config/market issue). All in canonical
    (test-number) order. The gate is the same either way: any non-empty bucket
    fails it.
    """
    latest = repo.latest_smoke_test_results(conn, run_id)
    missing: list[str] = []
    failed: list[str] = []
    errored: list[str] = []
    for key in SMOKE_TEST_KEYS:
        row = latest.get(key)
        if row is None:
            missing.append(key)
        elif row["status"] == "passed":
            continue
        elif row["status"] == "error":
            errored.append(key)
        else:
            failed.append(key)
    passed = not missing and not failed and not errored
    return SmokeGateReport(passed, tuple(missing), tuple(failed), tuple(errored))


def validate_only_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Return the given keys if the selection is runnable, else raise ValueError.

    The CLI's ``--only`` guard: a typo'd key must name itself, not silently run
    an empty suite (which would then read as "all selected tests passed"). A
    selection that CANNOT pass is refused too: ``slice_order_status`` (test 4)
    queries the order test 3 submits in the same process, so selecting it
    without ``slice_order_submit`` would place nothing and still write a real
    FAILED row into the append-only audit — which ``validate`` then reports as
    "exchange refused", dressing an operator selection slip up as an exchange
    problem (decision 2026-07-28).
    """
    keys = tuple(keys)
    unknown = [k for k in keys if k not in _BY_KEY]
    if unknown:
        raise ValueError(
            f"unknown smoke test key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(SMOKE_TEST_KEYS)}"
        )
    if "slice_order_status" in keys and "slice_order_submit" not in keys:
        raise ValueError(
            "slice_order_status (test 4) queries the order slice_order_submit "
            "(test 3) places in the same process — select both, e.g. "
            "--only slice_order_submit slice_order_status"
        )
    return keys


def rerun_keys_for(keys: Iterable[str]) -> tuple[str, ...]:
    """The ``--only`` selection that will actually RUN the given keys.

    The same pairing rule :func:`validate_only_keys` enforces, read backwards. A
    remedy line that echoes the red keys verbatim prints a command the CLI
    REFUSES (exit 1) whenever ``slice_order_status`` is among them — and that is
    the commonest errored key of all, because test 4 errors precisely when test 3
    did not complete. Emitted in registry order so the printed command is stable.
    """
    selection = set(keys)
    if "slice_order_status" in selection:
        selection.add("slice_order_submit")
    return tuple(k for k in SMOKE_TEST_KEYS if k in selection)
