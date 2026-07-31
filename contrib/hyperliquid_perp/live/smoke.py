"""Testnet smoke-test runner (phase3-spec §20.2, PR 6).

Before a testnet_live run may enter its §20.3 acceptance cycles, the §20.2
checklist of signed exchange actions must pass end-to-end against a real
testnet connection. This module turns that checklist into a per-item,
re-runnable, DB-recorded CLI (`live-smoke`): every test drives the actual PR 1–5
signed client / recovery components, its verdict lands in ``live_smoke_tests``
(append-only — a re-run after a fix supersedes without erasing the record), and
the cycle-entry gate (:func:`smoke_gate_report`) reads the latest non-dry-run
result per test.

The checklist is §20.2's seventeen items verbatim (:data:`SMOKE_TESTS` 1–17)
plus one addition, test 18 "emergency close": §20.3's ``emergency_close_test_passed``
acceptance metric (and §21.3's "emergency close tested" entry criterion) demand
a deliberate emergency-close exercise, and a healthy cycle run produces none —
so the metric can only come from a test that forces it, never from cycle
telemetry. Test 18 is therefore run by the suite and gated like the rest (§20.2
"all smoke tests must pass"); :mod:`.validation` reads tests 15/16/17/18 for the
four §20.3 ``*_test_passed`` booleans.

Design: each test is a thin orchestration over the injected
:class:`SmokeContext` — the signed client for the wire actions, a ``mark_price``
seam for sizing, and a ``run_recovery`` seam for the restart tests (15–17) and
the pre-flight. A real run whose selection places probe orders first runs ONE
passing §19.1 recovery through that seam (the signed client's own order gate
fail-closes on flags only a recovery raises), books every IOC probe as an
``orders`` row so its fill reconciles like any other money, and disarms the
kill switch on exit whenever the pre-flight or a switch-touching test armed it. The
CLI wires those to the real components; the unit tests wire fakes, so the
harness (sequencing, recording, gate, dry-run, cleanup, error containment) is
verifiable offline while the real testnet exercise stays the operator's step
(docs/RUNBOOK-live.md). ``--dry-run`` places nothing and records every selected
test ``skipped`` — it validates the config, the run's existence, and context
construction (a wiring check that can never satisfy the gate). It does NOT run
the per-test bodies, so the cloid derivation / sizing inside them is exercised
only on a real (non-dry-run) placement.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..paper.run_lock import RunLockError
from ..paper.stops import round_to_tick
from ..paper.twap import floor_to_step
from ..persistence import repository as repo
from ..persistence.cloid import cloid_hex, cloid_logical
from ..persistence.db import Database
from .orders import local_status_for_exchange_status, parse_order_status

if TYPE_CHECKING:  # import cost only under type checking; runtime stays lazy
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient

logger = logging.getLogger(__name__)

__all__ = [
    "SMOKE_TESTS",
    "SMOKE_TEST_KEYS",
    "RecoveryResult",
    "SmokeContext",
    "SmokeGateReport",
    "SmokePreflightError",
    "SmokeStepResult",
    "SmokeTest",
    "SmokeTestRunner",
    "rerun_keys_for",
    "smoke_gate_report",
    "validate_only_keys",
]


class RecoveryResult(Protocol):
    """What the ``run_recovery`` seam must return: a §19.1 verdict.

    A typed contract instead of ``Any`` + ``getattr(..., "passed", False)``:
    a seam wired to return the wrong object (or a renamed ``StartupResult``
    field) must surface as an ``error`` verdict / a loud AttributeError — a
    harness bug — not be silently absorbed into a ``failed`` verdict that
    sends the operator down the "exchange refused" triage path.
    """

    passed: bool


@dataclass(frozen=True)
class SmokeTest:
    """One §20.2 checklist item: its 1-based number, stable key, and label."""

    number: int
    key: str
    name: str


# §20.2's seventeen items verbatim, plus test 18 (emergency close, see module
# docstring). The KEY is the stable identity the store and the validator read;
# the NUMBER and NAME are for operator-facing reports. Order is execution order:
# a slice submitted in test 3 is the order test 4 queries, so the sequence is
# meaningful and the runner honours it.
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
)

# The stable identities, in one place: the gate iterates them, the validator
# maps §20.3 booleans through a subset of them, and --only validates against them.
SMOKE_TEST_KEYS: tuple[str, ...] = tuple(t.key for t in SMOKE_TESTS)
_BY_KEY: dict[str, SmokeTest] = {t.key: t for t in SMOKE_TESTS}
# ``key`` is the identity the gate, the validator, and ``--only`` all key off of;
# a silent collision (a future item copy-pasted from an existing one) would drop
# a test from the suite via the dict fold. Turn that into a loud import-time error.
if len(_BY_KEY) != len(SMOKE_TESTS):
    raise AssertionError("SMOKE_TESTS keys must be unique")

# The tests whose execution touches the account-wide dead man's switch: the
# three restart tests (their §19.1 recovery ARMS the scheduleCancel and never
# clears it) and the kill-switch test itself (arms/refreshes before clearing).
# A run arms the switch either through one of these OR through the pre-flight
# recovery an order-placing selection triggers (see :data:`_ORDER_PLACING_TESTS`);
# only such a run disarms on exit — one that armed nothing must NOT fire an
# account-wide clear that could wipe a concurrently-running ``live --loop``'s
# own arm on the same wallet.
_KILL_SWITCH_TESTS: frozenset[str] = frozenset(
    {
        "kill_switch_arm_refresh",
        "restart_reconciliation",
        "startup_with_existing_position",
        "startup_with_stale_open_order",
    }
)

# The tests that place a probe order on the wire. The signed client's own
# §4.1 order gate fail-closes on ``startup_reconciliation_passed`` /
# ``kill_switch_active`` — flags only a real §19.1 recovery raises — so a real
# run that selects any of these must run the pre-flight recovery first (see
# :meth:`SmokeTestRunner._preflight_recovery`); a selection without them (e.g.
# ``--only`` the restart tests, which run recovery themselves) skips it.
# ``slice_order_status`` only queries; tests 1/2/14 ride the base
# exchange-action gate, which needs neither runtime flag.
_ORDER_PLACING_TESTS: frozenset[str] = frozenset(
    {
        "slice_order_submit",
        "slice_plan_cancel",
        "multi_slice_fill",
        "reduce_only_close",
        "stop_loss_create",
        "stop_loss_modify",
        "stop_loss_cancel",
        "take_profit_create",
        "take_profit_modify",
        "take_profit_cancel",
        "emergency_close",
    }
)

# The tests whose probe is a resting reduce-only trigger. Hyperliquid's
# reduce-only semantics make a flat-account trigger a bet on venue leniency
# (nothing to reduce), so the runner stages one small real long under the whole
# block — every probe is then the exact §17 protection shape live emits, a SELL
# trigger shrinking a real long (decision 2026-07-29). Opened lazily by the
# first of these tests, closed as soon as no later selected test is in the set.
_TRIGGER_PROBE_TESTS: frozenset[str] = frozenset(
    {
        "slice_plan_cancel",
        "stop_loss_create",
        "stop_loss_modify",
        "stop_loss_cancel",
        "take_profit_create",
        "take_profit_modify",
        "take_profit_cancel",
    }
)

# These three sets are literal copies of SMOKE_TESTS keys: a key renamed there
# without them would silently drop a test from its policy bucket — a suite that
# armed the kill switch would skip the exit disarm, an order-placing test would
# skip the pre-flight, a trigger test would probe flat. Fail at import (the
# same guard validation.py carries for its §20.3 key literals).
for _copied in (_KILL_SWITCH_TESTS, _ORDER_PLACING_TESTS, _TRIGGER_PROBE_TESTS):
    if not _copied <= set(SMOKE_TEST_KEYS):
        raise AssertionError(
            f"smoke policy set drifted from SMOKE_TESTS: {sorted(_copied - set(SMOKE_TEST_KEYS))}"
        )

# Just over Hyperliquid's ~$10 minimum order value, so a probe order the suite
# means to REST or FILL is not refused for being dust.
_PROBE_NOTIONAL_USDC = Decimal("11")

# Every trigger probe is LONG-protection shaped: a SELL that would shrink a
# hypothetical long — SL far below the mark (fires only if price halves), TP far
# above (fires only if price 1.5×es). The side is what makes the far distance
# "never fires": a BUY-SL below the mark (short protection) is already in its
# fired region at placement, so the exchange rejects it as wrong-side or
# instant-triggers it — either way the probe cannot rest (§20.2 tests 5/8–13
# need a RESTING order to modify/cancel).
_TRIGGER_PROBE_IS_BUY = False


@dataclass
class SmokeContext:
    """Everything a smoke test needs, injected so the harness is fake-able.

    The CLI builds this from the live components (``cli._build_smoke_session`` /
    ``_build_real_smoke_session``, both driven by ``_cmd_live_smoke`` — NOT
    ``_cmd_live``); the unit tests build it from fakes. ``run_recovery`` runs
    one §19.1 startup recovery and returns an object exposing ``.passed`` (the
    real :func:`~.startup.run_startup_recovery` result, or a fake); it is the
    seam the three restart tests (15–17) drive so they never reconstruct the
    engine. ``now`` supplies the clock (wall clock in production, a fake in
    tests) for cloid uniqueness and result timestamps.
    """

    # The SIGNED client, never the read-only one — cli builds both in the same
    # scope a dozen lines apart, and typing this ``Any`` made passing the wrong
    # one type-check cleanly while every ack field (``accepted``,
    # ``filled_size``, ``status``, ``error``) went unchecked at ~20 wire call
    # sites too. ``None`` exactly when dry_run (enforced in __post_init__).
    signed: HyperliquidSignedClient | None
    db: Database
    run_id: str
    coin: str
    network: str
    payload_dir: Path
    owner_prefix: str
    mark_price: Callable[[], Decimal]
    qty_step: Decimal
    tick_size: Decimal
    now: Callable[[], datetime]
    dry_run: bool = False
    kill_switch_deadline: timedelta = timedelta(seconds=120)
    # The exchange-side sizing regime smoke test 2 WRITES to the account (the
    # suite is the only writer in the system). Wired from the run's own
    # ``live.safety`` so the suite can never leave the account in a regime the
    # config did not ask for (decision 2026-07-29); the defaults are the §20.1
    # values, which are also the only ones Phase 3 v1 config validation admits
    # for leverage.
    leverage: int = 1
    is_cross: bool = True
    run_recovery: Callable[[], RecoveryResult] | None = None
    # Refreshes the run lease (the CLI wires paper.run_lock.heartbeat_run_lock).
    # A full suite runs up to 4 real recoveries + ~14 wire actions — past
    # LOCK_STALE_SECONDS an un-refreshed lease reads as abandoned and a
    # concurrent ``live --loop`` could silently take the run over mid-suite.
    # Raising RunLockError from it means a successor owns the run: abort.
    heartbeat: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        # The pairing the runner already half-trusts: _execute gates the test
        # bodies on dry_run alone, while _disarm_kill_switch separately defends
        # with ``signed is None``. Left unenforced, a context with dry_run=False
        # and signed=None walks into a real test body and dies on an opaque
        # "NoneType has no attribute health_check", which _execute's broad
        # except relabels as a harness ``error`` — a wiring mistake wearing a
        # harness-bug costume. Name it at construction instead.
        if self.dry_run != (self.signed is None):
            raise ValueError(
                "SmokeContext: signed must be None for a dry run and present for a real "
                f"one (dry_run={self.dry_run}, signed={'set' if self.signed else 'None'})"
            )


@dataclass(frozen=True)
class SmokeStepResult:
    """One test's verdict, before it is stamped into ``live_smoke_tests``."""

    status: Literal["passed", "failed", "error", "skipped"]  # == _SMOKE_STATUSES, checked below
    detail: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        # A red verdict with no error_message would persist a live_smoke_tests
        # row an operator can never diagnose — the exact silent-audit-gap this
        # append-only table exists to prevent. A green/skipped verdict carrying
        # one would be a leftover from whatever failed before it, misleadingly
        # attached to a passing row (2026-07-30 type-design pass).
        carries_error = self.status in ("failed", "error")
        if carries_error and self.error_message is None:
            raise ValueError(f"SmokeStepResult({self.status!r}) must carry error_message")
        if not carries_error and self.error_message is not None:
            raise ValueError(f"SmokeStepResult({self.status!r}) must not carry error_message")


# The Literal above is a copy of the repository's status registry, and a comment
# is not a guard. Drift here does not fail quietly: insert_smoke_test_result's
# check_enum raises from inside _record(), which run() calls OUTSIDE any
# try/except — so it escapes the per-test loop as a bare traceback, mid-suite,
# on a run that may hold a staged real long and an armed kill switch. Bind it.
_SMOKE_STATUSES = frozenset({"passed", "failed", "error", "skipped"})
if _SMOKE_STATUSES != repo.LIVE_SMOKE_TEST_STATUSES:
    raise AssertionError("SmokeStepResult.status drifted from repository.LIVE_SMOKE_TEST_STATUSES")


class _SmokeAbort(Exception):
    """A test's own assertion failed — carries the operator-facing reason.

    Distinct from an unexpected exception: an abort is a controlled ``failed``
    verdict (the exchange refused a well-formed action, a fill never arrived),
    while any other exception is an ``error`` verdict (the harness itself broke).
    """


class SmokePreflightError(Exception):
    """The pre-flight §19.1 recovery failed — the suite never started.

    Raised out of :meth:`SmokeTestRunner.run` BEFORE any test executes (so no
    verdict is recorded): probe orders may not go on the wire until one real
    recovery has passed on the run. The CLI reports it as a gate-not-satisfied
    outcome; the operator fixes the run state and re-runs.
    """


class SmokeTestRunner:
    """Runs the §20.2 checklist against one run, recording each verdict.

    Stateless between runs except for the cross-test handles a sequence needs
    (the cloid a submit test leaves for the following status test): those live
    on the instance for the duration of one :meth:`run`.
    """

    def __init__(self, ctx: SmokeContext) -> None:
        self.ctx = ctx
        # Cross-test handles within one run() (test 3 → 4 share a cloid).
        self._last_submit: dict[str, str] | None = None
        # Monotonic per-runner counter folded into every tag: cloid uniqueness
        # must not depend on the clock advancing between two probes (a frozen
        # test clock — or two real sends in one microsecond — would collide on
        # the orders.cloid_hex UNIQUE index).
        self._tag_seq = 0
        # Set True if the end-of-suite kill-switch disarm was attempted and
        # FAILED — the CLI surfaces it as a prominent operator warning (the
        # wallet may still hold an armed scheduleCancel).
        self.kill_switch_disarm_failed: bool = False
        # Set True when the lease heartbeat reported this process superseded:
        # a successor now owns the run (and the wallet's kill switch), so the
        # exit disarm is SUPPRESSED — an account-wide clear here would strip
        # the successor's dead-man cover until its next refresh.
        self._lease_superseded: bool = False
        # Evidence that THIS process may have armed the account-wide switch, as
        # opposed to the intent read off the selection: set the moment an arming
        # path is entered, so a pre-flight that dies before reaching the arm
        # (no seam wired, a raise on the way in) does not fire an account-wide
        # clear for something nobody armed (2026-07-30 concurrency review).
        self._kill_switch_may_be_armed: bool = False
        # The staged long the trigger-probe tests protect (see
        # :data:`_TRIGGER_PROBE_TESTS`): 0 until the first trigger test opens
        # it, reset to 0 once flattened. Non-zero across an exit path means the
        # close is still owed (run()'s finally is the backstop).
        self._staged_long: Decimal = Decimal(0)
        # The cleanup note when the staged long could NOT be flattened (None
        # once flat). Public for the same reason as
        # :attr:`kill_switch_disarm_failed`: the close happens BETWEEN tests,
        # so it has no step row to land in, and a real funded position left on
        # the wire must not be visible only in a log line the operator may not
        # be capturing. The CLI prints it as a prominent warning.
        self.staged_long_residual: str | None = None
        # Cloids of trigger probes that may still be RESTING on the exchange.
        # Added BEFORE the wire call (so a lost ack leaves a handle rather than a
        # mystery) and discarded only on a confirmed cancel.
        #
        # Trigger probes deliberately book no ``orders`` row, so without this the
        # runner kept no handle on them at all: each probe was cancelled inline
        # and nothing survived the call frame, leaving run()'s finally nothing to
        # sweep — while _disarm_kill_switch removed the one backstop (the
        # wallet's scheduleCancel) that would have swept them at its deadline. A
        # stranded probe is not cosmetic: §19.3's sweep validates-never-cancels
        # protective roles, so the next `live` startup files it as
        # orphan_exchange_order, which validate counts cumulatively — pinning
        # that run-id at exit 5 for good (2026-07-31 lifecycle review).
        self._resting_probes: set[str] = set()
        # The note when probes could NOT be swept (None when all are clear).
        # Public for the same reason as :attr:`staged_long_residual`.
        self.probe_residual: str | None = None
        # Notes from every best-effort close that did NOT fully succeed —
        # accidental fills (a far IOC the book crossed anyway, a slice that
        # filled before its sibling aborted) as opposed to the staging long the
        # runner opens deliberately. Those go through _ensure_staged_long, which
        # has had an operator-visible flag since 2026-07-29; these went to a
        # step ``detail`` on a PASSED row, so a real funded position could be
        # left on the wire with nothing but a green row to show for it — the
        # same file treating its own position better than an unintended one
        # (2026-07-31 lifecycle review).
        self.position_residuals: list[str] = []

    # -- orchestration ----------------------------------------------------

    def run(self, *, only: Sequence[str] | None = None) -> list[SmokeTest]:
        """Execute the selected tests in order, persisting each verdict.

        ``only`` restricts to the named test keys (validated by the CLI); the
        default runs all of :data:`SMOKE_TESTS`. Returns the tests that were
        executed (in order) — the caller reports them and computes the gate from
        the store, never from this return value, so a crash mid-suite still
        leaves every completed verdict durable.

        An ``error`` verdict (the harness itself broke — as opposed to
        ``failed``, the exchange refusing a well-formed action) STOPS the suite
        (decision 2026-07-29): after a harness error the account state is
        unknown, and the remaining tests would put more real orders on the wire
        against it. The remaining tests are simply not executed — no rows are
        written for them (a written ``skipped`` row would supersede a previously
        ``passed`` latest-per-key result and poison the gate's buckets); the
        gate reads the store and reports them as before. A ``failed`` verdict
        continues: the refusal is itself the evidence, and the account state is
        known.
        """
        selected = self._select(only)
        # A real run whose selection places probe orders must run one passing
        # §19.1 recovery first: the signed client's own order gate fail-closes on
        # the two runtime flags only a recovery (plus the kill-switch arm inside
        # it) raises, so without this every order-placing test is rejected by the
        # run's own gate (review round 2026-07-27).
        needs_preflight = not self.ctx.dry_run and any(
            t.key in _ORDER_PLACING_TESTS for t in selected
        )
        # Only a run that armed the switch disarms on exit — a run that armed
        # nothing must NOT fire an account-wide clear that could wipe a
        # concurrent ``live --loop``'s own arm (Q2 + exit-check finding,
        # 2026-07-27). That used to be predicted from ``selected``, which
        # over-claims: a pre-flight that dies on the way in (no seam wired, a
        # raise before the arm) armed nothing yet still cleared. It is now
        # EVIDENCE — ``_kill_switch_may_be_armed``, set at each of the three
        # paths that can arm (pre-flight, a restart test's recovery, test 14)
        # BEFORE the arming call, so a crash after arming still disarms
        # (2026-07-30 concurrency review).
        executed: list[SmokeTest] = []
        try:
            if needs_preflight:
                self._preflight_recovery()
            for index, test in enumerate(selected):
                # Outside _execute on purpose: a RunLockError (superseded — a
                # successor owns the run) must ABORT the suite, not degrade
                # into one test's error verdict while orders keep going out.
                self._heartbeat()
                if needs_preflight:
                    self._refresh_kill_switch()
                result = self._execute(test)
                self._record(test, result)
                executed.append(test)
                logger.info(
                    "smoke %02d %s: %s%s",
                    test.number,
                    test.key,
                    result.status,
                    "" if result.detail is None else f" — {result.detail}",
                )
                # Flatten the staged trigger-block long as soon as no later
                # selected test needs it — BEFORE the restart tests, whose
                # clean-restart recovery (test 15) must not find an unexpected
                # position the operator never staged.
                if self._staged_long > 0 and not any(
                    t.key in _TRIGGER_PROBE_TESTS for t in selected[index + 1 :]
                ):
                    # Sweep BEFORE flattening, for the reason the exit block
                    # states: probes are reduce-only triggers on this long, so
                    # closing first makes the exchange auto-cancel them and every
                    # later cancel comes back refused — reporting a permanent
                    # exit 5 over orders that are already gone. This is the
                    # in-loop close; the exit block's ordering alone did not
                    # cover it (2026-07-31 exit check).
                    self._sweep_resting_probes()
                    self._close_staged_long()
                if result.status == "error":
                    logger.error(
                        "smoke %02d %s errored — stopping the suite: the harness "
                        "broke, so the account state is unknown and the remaining "
                        "tests must not put more orders on the wire (their stored "
                        "verdicts are untouched)",
                        test.number,
                        test.key,
                    )
                    break
        finally:
            # In the ``finally`` so a mid-suite crash cleans up too — but EVERY
            # action here is externally visible (a real reduce-only IOC, an
            # account-wide scheduleCancel clear), so both are gated on still
            # owning the run. A superseded process must touch neither: the
            # successor has already adopted this wallet's position through its
            # own §19.1 recovery and armed its own dead-man cover, so closing
            # "our" staged long would flatten ITS position and clearing the
            # switch would strip ITS cover — the same rule the daemon's own
            # shutdown lane states (2026-07-30 concurrency review).
            still_ours = self._still_owns_run()
            if still_ours:
                # Probes BEFORE the staged long: they are reduce-only triggers
                # sitting on that long, so flattening first makes the exchange
                # auto-cancel them (reduceOnlyCanceled) and every sweep cancel
                # then comes back refused — a residual warning for orders that
                # are already gone. And both BEFORE the disarm: the wallet's
                # scheduleCancel is the last backstop for a probe we cannot
                # cancel ourselves, so clearing it first turns "swept in ~30s by
                # the dead man" into "rests until an operator notices".
                self._sweep_resting_probes()
                self._close_staged_long()
            elif self._resting_probes:
                # Not ours to cancel — the successor owns this wallet — but the
                # probes are still on its book, so name them.
                self.probe_residual = (
                    f"{len(self._resting_probes)} trigger probe(s) were left resting "
                    f"(cloids: {', '.join(sorted(self._resting_probes))}): this run's "
                    "lease was taken over mid-suite, so cancelling them now would act "
                    "on a wallet this process no longer owns. Cancel them manually"
                )
                logger.warning("smoke: %s", self.probe_residual)
            # A SEPARATE if, not an elif chained onto the probes above: a trigger
            # test opens the staged long and registers a probe within two lines
            # of each other, so a takeover mid-block leaves BOTH — and chaining
            # reported the probes while silently dropping the funded position,
            # exactly inverting the severities (2026-07-31 exit check).
            if not still_ours and self._staged_long > 0:
                # Real exposure we are no longer entitled to close. Hand it over
                # loudly rather than silently: the successor's recovery adopts
                # the position, and the operator gets told which run owns it.
                self.staged_long_residual = (
                    f"the staged trigger-block long ({self._staged_long}) was NOT closed: "
                    "this run's lease was taken over by another process mid-suite, which "
                    "now owns the position. Verify on the exchange that the successor "
                    "adopted it (its §19.1 recovery reconciles open positions)"
                )
                logger.warning("smoke: %s", self.staged_long_residual)
            if self._kill_switch_may_be_armed and still_ours:
                self._disarm_kill_switch()
        return executed

    def _heartbeat(self) -> None:
        """Refresh the run lease between tests (real runs; no-op un-wired).

        ``RunLockError`` means this process was superseded (its lease went
        stale past LOCK_STALE_SECONDS and another ``live``/``live-smoke``
        legitimately took the run over): mark the lease lost — which also
        suppresses the exit disarm — and re-raise so the suite stops before
        the next wire action. Any OTHER failure (a transient store error)
        propagates unchanged: the suite still aborts (fail-closed — orders
        must not continue under an uncertain lease) but the normal exit
        disarm still runs, because no successor exists to own the switch.
        """
        if self.ctx.heartbeat is None:
            return
        try:
            self.ctx.heartbeat()
        except RunLockError:
            self._lease_superseded = True
            raise

    def _still_owns_run(self) -> bool:
        """Positively re-verify the lease before an externally-visible cleanup action.

        ``_lease_superseded`` alone is absence-of-evidence: it is only set when
        a heartbeat happens to RAISE, so a suite that aborts on the way in (a
        :class:`SmokePreflightError`, a harness exception between tests) leaves
        it False even though the lease may have gone stale — and the cleanup
        then acts on a run a successor already owns. Re-checking here turns the
        exit path's question from "did we happen to notice?" into "do we still
        hold it?" (2026-07-30 concurrency review).

        A transient store failure is NOT proof of supersession: the lease is
        most likely still ours and the cleanup still matters, so it is logged
        and treated as owned. Only a real ``RunLockError`` gives the run away.
        """
        if self._lease_superseded:
            return False
        if self.ctx.heartbeat is None:
            return True
        try:
            self.ctx.heartbeat()
        except RunLockError:
            self._lease_superseded = True
            return False
        except Exception as exc:  # noqa: BLE001 — see docstring: not proof of supersession
            logger.warning(
                "smoke: could not re-verify the run lease before cleanup (%s: %s) — "
                "proceeding as the owner",
                type(exc).__name__,
                exc,
            )
        return True

    def _refresh_kill_switch(self) -> None:
        """Re-arm the pre-flight's dead-man switch before each test (§18).

        The pre-flight recovery arms the account-wide scheduleCancel at
        now + ``kill_switch_deadline`` (the run's configured cover, floored at
        120s by the CLI) and nothing else refreshes it
        until a restart test's own recovery — a full suite runs longer than
        that, so the switch would FIRE mid-suite: the exchange cancels the
        resting trigger probe under tests 5/8–13 (a spurious ``failed`` in the
        "exchange refused" triage bucket) and the wallet then sits uncovered
        until test 15 re-arms (exit-check 2026-07-28). Clearing the arm after
        the pre-flight instead would defeat the dead-man cover §18 wants over
        a crashed suite's resting probes — refreshing keeps both properties.
        Called only on real runs whose selection triggered the pre-flight (the
        only un-refreshed arm; restart tests re-arm inside each recovery). A
        refresh failure propagates: probes must not go out under an uncertain
        switch (the same fail-closed stance as the lease heartbeat), and the
        exit disarm still runs.
        """
        self._wire.schedule_cancel(cancel_at=self.ctx.now() + self.ctx.kill_switch_deadline)

    @property
    def _wire(self) -> HyperliquidSignedClient:
        """The signed client, proven present, for the paths that must have one.

        Every caller runs only on a non-dry-run context, where
        ``SmokeContext.__post_init__`` already pairs ``dry_run`` with
        ``signed``. Stating that once here beats ``assert`` noise at ~20 wire
        call sites, and leaves the two places that genuinely tolerate ``None``
        — the dry-run fork and the exit disarm — reading as the deliberate
        exceptions they are.
        """
        signed = self.ctx.signed
        if signed is None:  # unreachable while __post_init__ holds
            raise AssertionError("a smoke wire action was reached with no signed client")
        return signed

    def _select(self, only: Sequence[str] | None) -> list[SmokeTest]:
        if only is None:
            return list(SMOKE_TESTS)
        # Preserve canonical order regardless of the order --only listed them,
        # so a status test never runs before the submit it depends on.
        wanted = set(only)
        return [t for t in SMOKE_TESTS if t.key in wanted]

    def _execute(self, test: SmokeTest) -> SmokeStepResult:
        """Dispatch one test, containing failures as verdicts, not crashes."""
        if self.ctx.dry_run:
            return SmokeStepResult(
                "skipped",
                detail="dry-run: preconditions only, placed no orders",
            )
        try:
            # Resolved INSIDE the try: a key with no matching method would
            # otherwise raise AttributeError before any containment, escaping
            # run()'s loop past _record() — so, uniquely, no live_smoke_tests
            # row for the attempted test, breaking the append-only audit
            # promise — while a staged real long and an armed kill switch may
            # still be outstanding. The import-time guard at the bottom of this
            # module makes it unreachable; this keeps it contained regardless.
            method = getattr(self, f"_test_{test.key}")
            return method()
        except _SmokeAbort as exc:
            return SmokeStepResult("failed", error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 — a broken test is an ``error`` verdict, not a crash
            logger.exception("smoke test %s raised", test.key)
            return SmokeStepResult("error", error_message=f"{type(exc).__name__}: {exc}")

    def _record(self, test: SmokeTest, result: SmokeStepResult) -> None:
        with self.ctx.db.transaction() as conn:
            repo.insert_smoke_test_result(
                conn,
                run_id=self.ctx.run_id,
                test_number=test.number,
                test_key=test.key,
                test_name=test.name,
                status=result.status,
                network=self.ctx.network,
                dry_run=self.ctx.dry_run,
                detail=result.detail,
                error_message=result.error_message,
                executed_at=self.ctx.now(),
            )

    def _preflight_recovery(self) -> None:
        """One passing §19.1 recovery before any probe order (decision 2026-07-27).

        Run only when the selection contains an order-placing test (see
        :data:`_ORDER_PLACING_TESTS`): the recovery raises the gate's
        ``startup_reconciliation_passed`` / ``kill_switch_active`` flags (arming
        the switch — the exit disarm covers it) and, as a side effect, proves the
        true startup ordering — recovery before any order — once per invocation.
        A failure aborts the whole suite with :class:`SmokePreflightError` and
        records no verdict: the run is not in a state where probe orders belong
        on the wire.
        """
        if self.ctx.run_recovery is None:
            raise SmokePreflightError(
                "no run_recovery seam wired — the order-placing tests need the "
                "pre-flight §19.1 recovery (the CLI supplies it; a bare context cannot)"
            )
        # Set BEFORE the call, not after: recovery arms the account-wide switch
        # as a side effect partway through, so a recovery that raises AFTER
        # arming must still disarm. Everything that fails before this line
        # (no seam wired) armed nothing and now correctly skips the disarm.
        self._kill_switch_may_be_armed = True
        try:
            result = self.ctx.run_recovery()
        except Exception as exc:  # noqa: BLE001 — a raised recovery (e.g. §18 arming
            # failure, which propagates by contract) is the same abort outcome as an
            # unclean verdict: nothing ran, exit 4 — not main()'s generic exit 2.
            raise SmokePreflightError(
                f"pre-flight §19.1 recovery raised — {type(exc).__name__}: {exc} "
                "(no test ran; fix the run/exchange state and re-run the suite)"
            ) from exc
        # Direct attribute access on purpose (RecoveryResult contract): a seam
        # returning the wrong object must raise loudly (a harness bug), not be
        # absorbed by a getattr default into "recovery did not pass".
        if not result.passed:
            raise SmokePreflightError(
                "pre-flight §19.1 recovery did not pass — the run is not in a clean "
                "state to place probe orders (inspect the recovery log / safe-mode "
                "status, fix the run, and re-run the suite)"
            )
        logger.info("smoke pre-flight: §19.1 recovery passed — order gate flags are up")

    def _disarm_kill_switch(self) -> None:
        """Clear the dead man's switch the pre-flight or a switch test armed (§18).

        Called only when the run armed the switch (pre-flight recovery, or a
        switch-touching test per :data:`_KILL_SWITCH_TESTS`): a §19.1 recovery
        ARMS the account-wide scheduleCancel and never clears it, so without this
        the suite would exit leaving the wallet with an armed scheduleCancel that
        fires ~``kill_switch_deadline`` later and cancels every resting order. Real runs
        only (a dry run placed no wire actions and carries no signed client);
        best-effort — a failed disarm is logged and recorded on
        :attr:`kill_switch_disarm_failed` (never raised: the verdicts are already
        durable), so the CLI can warn the operator that the wallet may still be
        armed. The next ``live --loop`` also re-arms and refreshes the switch.
        """
        if self.ctx.dry_run or self.ctx.signed is None:
            return
        try:
            self._wire.clear_scheduled_cancel()
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the suite's verdicts
            self.kill_switch_disarm_failed = True
            logger.warning(
                "smoke: best-effort kill-switch disarm FAILED — %s: %s",
                type(exc).__name__,
                exc,
            )

    # -- shared helpers ---------------------------------------------------

    def _register_cloid(self, *, role: str, tag: str, slice_index: int = 0) -> tuple[str, str]:
        """Derive a unique, attributable cloid pair for a smoke order and register it.

        ``tag`` (a per-execution marker, e.g. the wall-clock stamp) keeps a
        re-run's cloid distinct from a prior run's so the exchange never sees a
        reused cloid_hex, while the §19.3 reverse lookup still resolves every
        smoke order to this run (bot-owned). Registered before the send, exactly
        as the live submit path does (§8.3 rule 1). Returns
        ``(cloid_logical, cloid_hex)`` — the §8.2 two-layer id travels together.
        """
        logical = cloid_logical(
            prefix=self.ctx.owner_prefix,
            run_id=self.ctx.run_id,
            symbol=self.ctx.coin,
            output_id="smoke",
            plan_id=tag,
            leg="na",
            slice_index=slice_index,
            order_role=role,
        )
        hex_ = cloid_hex(logical)
        with self.ctx.db.transaction() as conn:
            repo.insert_cloid_mapping(
                conn,
                cloid_logical=logical,
                cloid_hex=hex_,
                run_id=self.ctx.run_id,
                symbol=self.ctx.coin,
                order_role=role,
                created_at=self.ctx.now(),
            )
        return logical, hex_

    def _record_probe_order(
        self,
        ack: Any,
        *,
        cloid_logical: str,
        cloid_hex_value: str,
        role: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool = False,
    ) -> None:
        """Book an IOC probe locally so its fill (if any) can be applied (§14).

        A probe fill is real exchange money on this run: without an ``orders``
        row the fill backfill can never attach it (the §14 oid→order mapping),
        filing a permanent, unstampable ``fill_unmapped`` case that turns every
        later §19.1 recovery unclean (decision 2026-07-27). Only IOC probes are
        booked — an IOC is terminal at the ack (filled or canceled) BY EXCHANGE
        SEMANTICS, so a terminal row can never trip the §12.3 open-order lane;
        the ack contract itself still admits ``resting``, and that anomaly is
        handled fail-safe below (cancel, book the true state, fail the test)
        rather than mis-booked as ``canceled``. Trigger
        probes are NOT booked: they rest far from the mark and are cancelled
        in-test, and a stranded one is backfilled bot-owned by the reconciler's
        own orphan lane — a local "open" row whose cancel succeeded would
        instead file ``order_missing_on_exchange``. An error ack booked nothing
        on the exchange (no oid) — nothing to book here either.
        """
        if ack.exchange_order_id is None:
            return
        if ack.status == "resting":
            # The premise above ("an IOC is terminal at the ack") failed: the
            # ack contract itself admits "resting" (accepted, on the book,
            # nothing filled — see OrderAck), and booking a LIVE order as
            # "canceled" would both lie to the audit trail and leave the §12.3
            # reconciler to find it as an orphan later. Fail-safe: cancel it,
            # book the true final state, and fail the test loudly — an IOC
            # that rests is an exchange-semantics anomaly worth stopping on.
            note = self._best_effort_cancel(cloid_hex_value)
            cancelled = note is None
            self._insert_probe_row(
                status="canceled" if cancelled else "open",
                filled=Decimal(0),
                ack=ack,
                cloid_logical=cloid_logical,
                cloid_hex_value=cloid_hex_value,
                role=role,
                side=side,
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
            raise _SmokeAbort(
                f"IOC probe {cloid_hex_value} came back 'resting' — an IOC must be "
                "terminal at the ack; "
                + (note if note is not None else "cancelled it and booked the row as canceled")
            )
        filled = ack.filled_size or Decimal(0)
        status = "filled" if filled > 0 else "canceled"
        self._insert_probe_row(
            status=status,
            filled=filled,
            ack=ack,
            cloid_logical=cloid_logical,
            cloid_hex_value=cloid_hex_value,
            role=role,
            side=side,
            qty=qty,
            price=price,
            reduce_only=reduce_only,
        )

    def _place_and_record_ioc(
        self,
        *,
        role: str,
        tag: str,
        is_buy: bool,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool = False,
        protective: bool = False,
        slice_index: int = 0,
    ) -> tuple[str, str, Any]:
        """Register a cloid, place the IOC, and book the probe row — one shot.

        The register -> place -> record sequence every order-placing smoke
        test shares, collapsed so the five call sites cannot drift apart the
        way :func:`_open_probe_long` already prevents for the open-leg shape
        (2026-07-30 simplify pass). Returns ``(cloid_logical, cloid_hex, ack)``
        for callers that need the cloid for later bookkeeping or the ack for a
        fill/acceptance check.
        """
        logical, cloid = self._register_cloid(role=role, tag=tag, slice_index=slice_index)
        try:
            ack = self._wire.place_ioc_limit(
                coin=self.ctx.coin,
                is_buy=is_buy,
                size=qty,
                limit_price=price,
                cloid_hex=cloid,
                reduce_only=reduce_only,
                protective=protective,
            )
        except Exception as exc:  # noqa: BLE001 — re-raised below; this only ASKS
            # Unknown outcome (§8.3 rule 11): a lost ack or a timeout may have
            # left this IOC filled on the exchange. The suite bypasses
            # LiveOrderSubmitter — no intent row is written before the wire
            # call — so without asking, a fill that landed here leaves NO local
            # trace at all: the backfill later maps it by exchange_order_id,
            # finds no orders row, and books fill_unmapped plus a position
            # mismatch, which pins this run-id's validate at exit 5.
            #
            # The cloid is in hand and orderStatus is an ungated read, so ask
            # once and book what it says. Failing that, the exposure is at least
            # NAMED rather than silent.
            recovered = self._recover_lost_ioc(
                exc,
                cloid_logical=logical,
                cloid_hex_value=cloid,
                role=role,
                side="buy" if is_buy else "sell",
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
            raise recovered from exc
        self._record_probe_order(
            ack,
            cloid_logical=logical,
            cloid_hex_value=cloid,
            role=role,
            side="buy" if is_buy else "sell",
            qty=qty,
            price=price,
            reduce_only=reduce_only,
        )
        return logical, cloid, ack

    def _recover_lost_ioc(
        self,
        exc: Exception,
        *,
        cloid_logical: str,
        cloid_hex_value: str,
        role: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool,
    ) -> Exception:
        """Ask orderStatus what became of an IOC whose ack was lost.

        Returns the exception to raise: the original when nothing more can be
        said, or one carrying the recovered outcome. Never raises on its own —
        a failed recovery must not replace the transport error that caused it.

        Booking the row is the whole point: it is what lets §14's backfill
        attach the fill by ``exchange_order_id`` instead of filing an unmappable
        ``fill_unmapped`` case against this run forever.
        """
        try:
            resolved = parse_order_status(self._wire.query_order_by_cloid(cloid_hex_value))
        except Exception:  # noqa: BLE001 — recovery is best-effort by definition
            logger.warning(
                "smoke: orderStatus recovery for lost IOC %s could not resolve",
                cloid_hex_value,
                exc_info=True,
            )
            return RuntimeError(
                f"{type(exc).__name__}: {exc}; the outcome of probe {cloid_hex_value} is "
                "UNKNOWN (orderStatus could not be read) — it may have filled on the "
                "exchange with no local order row. Check the wallet before starting "
                "cycles; an unmapped fill pins this run's validate at exit 5"
            )
        if resolved is None:
            # The documented unknownOid marker: the exchange never saw it, so
            # nothing landed and there is nothing to book.
            return exc
        exchange_order_id, status_word = resolved
        local_status = local_status_for_exchange_status(status_word)
        try:
            self._insert_probe_row(
                status=local_status,
                # The venue says filled/canceled/etc, not HOW MUCH: orderStatus
                # carries no size here. Booking 0 keeps the row honest about what
                # was observed — §14's backfill attaches the real fill by
                # exchange_order_id, which is the entire reason for this row.
                filled=Decimal(0),
                # The row's ack-derived fields, from what orderStatus actually
                # said. status_reason distinguishes it from an ack-booked row:
                # OrderAck.status and the venue's own word overlap on "filled",
                # so exchange_raw_status alone cannot show the difference.
                ack=SimpleNamespace(
                    exchange_order_id=exchange_order_id,
                    average_price=None,
                    status=status_word,
                ),
                cloid_logical=cloid_logical,
                cloid_hex_value=cloid_hex_value,
                role=role,
                side=side,
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
        except Exception:  # noqa: BLE001 — see the "never raises" contract above
            # A busy store (reconcile.py calls a locked DB "a routine hazard")
            # must not replace the transport error with a sqlite one and lose
            # the "this probe may have filled" message entirely.
            logger.warning(
                "smoke: could not book the recovered probe row for %s",
                cloid_hex_value,
                exc_info=True,
            )
            return RuntimeError(
                f"{type(exc).__name__}: {exc}; probe {cloid_hex_value} DID reach the "
                f"exchange (oid={exchange_order_id}, status={status_word}) but its order "
                "row could NOT be booked — record it manually or use a new run-id, or "
                "any fill lands unmapped and pins this run's validate at exit 5"
            )
        return RuntimeError(
            f"{type(exc).__name__}: {exc}; probe {cloid_hex_value} DID reach the "
            f"exchange (oid={exchange_order_id}, status={status_word}) — its order row "
            "has been booked so §14's backfill can attach any fill"
        )

    def _insert_probe_row(
        self,
        *,
        status: str,
        filled: Decimal,
        ack: Any,
        cloid_logical: str,
        cloid_hex_value: str,
        role: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool,
    ) -> None:
        with self.ctx.db.transaction() as conn:
            repo.insert_order(
                conn,
                # Deterministic and collision-free, mirroring the reconciler's
                # orphan lane: one probe row per cloid.
                order_id=f"smoke|{cloid_hex_value}",
                mode="live",
                run_id=self.ctx.run_id,
                symbol=self.ctx.coin,
                order_role=role,
                side=side,
                order_type="ioc_limit",
                qty=qty,
                filled_qty=filled,
                remaining_qty=qty - filled,
                status=status,
                status_reason="smoke_probe",
                price=price,
                reduce_only=reduce_only,
                cloid_logical=cloid_logical,
                cloid_hex=cloid_hex_value,
                exchange_order_id=ack.exchange_order_id,
                exchange_status=status,
                exchange_raw_status=ack.status,
                is_bot_owned=True,
                timestamp=self.ctx.now(),
            )

    def _tag(self) -> str:
        # A whitespace-free, per-execution marker for cloid uniqueness: wall
        # clock (compact ISO basic form — the cloid segment guard rejects
        # spaces) plus the runner-scoped sequence number, so two probes in the
        # same instant still derive distinct cloids.
        self._tag_seq += 1
        return f"{self.ctx.now().strftime('%Y%m%dT%H%M%S%f')}-{self._tag_seq}"

    def _round_price(self, raw: Decimal, *, up: bool) -> Decimal:
        """Exchange-legal probe price (tick grid + the 5-sig-fig px rule).

        Delegates to the same :func:`~..paper.stops.round_to_tick` the live
        engine and protection use, so a probe can never carry a price shape
        the real order paths would not. ``up`` picks the safe side per probe:
        toward-marketable for the fill-intent IOCs, away-from-mark for the
        never-fires prices.
        """
        return round_to_tick(raw, self.ctx.tick_size, up=up)

    def _probe_size(self) -> Decimal:
        """The smallest qty whose notional clears the exchange minimum."""
        mark = self.ctx.mark_price()
        if mark <= 0:
            raise _SmokeAbort(f"mark price is non-positive ({mark}); cannot size a probe order")
        step = self.ctx.qty_step
        raw = _PROBE_NOTIONAL_USDC / mark
        steps = (raw / step).to_integral_value(rounding=ROUND_CEILING)
        return max(step, steps * step)

    def _staged_probe_size(self) -> Decimal:
        """The qty a reduce-only trigger probe must carry: the staged long itself.

        Live sizes an SL to ``floor_to_step(abs(position.size))``
        (protection._establish), and the whole point of staging a real long is
        that every probe carries the exact §17 wire shape. Re-deriving the size
        from :meth:`_probe_size` re-read the mark on every call, so a mark move
        between the staging fill and a later probe — or a partial staging fill,
        which ``_open_probe_long`` accepts as long as something filled — sized the
        reduce-only order LARGER than the position it reduces. That refusal is
        exactly what staging exists to prevent, and it would land in the "exchange
        refused" bucket with no code fix available. Callers must have run
        :meth:`_ensure_staged_long` first.
        """
        with localcontext(DECIMAL_CONTEXT):
            size = floor_to_step(self._staged_long, self.ctx.qty_step)
        if size <= 0:
            raise _SmokeAbort(
                f"staged long {self._staged_long} floors to zero at qty step "
                f"{self.ctx.qty_step} — no reduce-only probe can be sized against it"
            )
        return size

    def _require_accepted(self, ack: Any, what: str) -> None:
        if not ack.accepted:
            raise _SmokeAbort(f"{what} was refused by the exchange: {ack.error}")

    def _require_oid(self, ack: Any, what: str) -> str:
        """The accepted ack's exchange order id — required, not merely hoped for.

        ``OrderAck.__post_init__`` gives every ``resting``/``filled`` ack an
        ``exchange_order_id``, so this cannot be None once
        :meth:`_require_accepted` has passed. Stating it matters anyway,
        because the one caller feeds it straight to ``modify_trigger_order``'s
        ``target``: a None slipping through would put a malformed modify on the
        wire against a real resting order rather than failing here.
        """
        self._require_accepted(ack, what)
        if ack.exchange_order_id is None:
            # NOT a _SmokeAbort: that bucket means "the exchange refused", and
            # the suite continues placing wire actions after one. An accepted
            # ack with no id is an OrderAck-contract violation — a harness or
            # client bug, which the 2026-07-29 decision says must record
            # ``error`` and stop the suite (account state is no longer known).
            raise AssertionError(f"{what} was accepted without an exchange order id")
        return str(ack.exchange_order_id)

    def _require_recovery(self) -> RecoveryResult:
        if self.ctx.run_recovery is None:
            raise _SmokeAbort(
                "no run_recovery seam wired — the restart tests need the §19.1 "
                "recovery components (the CLI supplies them; a bare context cannot)"
            )
        # A §19.1 recovery ARMS the account-wide switch as a side effect, so the
        # restart tests are an arming path too — flag before the call for the
        # same reason the pre-flight does (arm may happen, then it may raise).
        self._kill_switch_may_be_armed = True
        return self.ctx.run_recovery()

    # -- the §20.2 tests --------------------------------------------------

    def _test_signed_client_init(self) -> SmokeStepResult:
        # §6.1 authorization was proven by the `live` gate before the suite
        # started; this re-affirms the signed transport reaches testnet and the
        # agent address is bound.
        self._wire.health_check()
        agent = self._wire.agent_address
        if not agent:
            raise _SmokeAbort("signed client has no agent_address after init")
        return SmokeStepResult("passed", detail=f"agent {agent} on {self.ctx.network}")

    def _test_update_leverage(self) -> SmokeStepResult:
        # Writes the RUN'S OWN configured regime, not a spec literal: this is
        # the only place in the system that sets the exchange-side regime, and
        # it must never flip an account to a leverage/margin mode the config
        # did not declare (decision 2026-07-29).
        self._wire.update_leverage(
            coin=self.ctx.coin, leverage=self.ctx.leverage, is_cross=self.ctx.is_cross
        )
        regime = f"{self.ctx.leverage}x {'cross' if self.ctx.is_cross else 'isolated'}"
        return SmokeStepResult("passed", detail=f"{self.ctx.coin} leverage set to {regime}")

    def _test_slice_order_submit(self) -> SmokeStepResult:
        # A far-below-mark IOC buy: the wire action round-trips but never fills
        # (so nothing is left to clean up) — a pure "can we submit a slice" test.
        size = self._probe_size()
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("0.5"), up=False)
        _logical, cloid, ack = self._place_and_record_ioc(
            role="entry", tag=f"submit-{self._tag()}", is_buy=True, qty=size, price=price
        )
        # A far IOC that does not cross returns 'error'/'not filled' from some
        # venues; what the test proves is that the ACTION reached the matching
        # engine without a top-level envelope error (that would have raised).
        # The exchange's own rejection TEXT (not just the coarse status) is
        # folded in so a real bug (bad size/tick/margin) is still visible in
        # the durable row even though the verdict stays green (2026-07-30).
        self._last_submit = {"cloid_hex": cloid, "oid": ack.exchange_order_id or ""}
        detail = f"submitted IOC (status={ack.status}, oid={ack.exchange_order_id}"
        detail += f", error={ack.error})" if ack.error else ")"
        filled = ack.filled_size or Decimal(0)
        if filled > 0:
            # The far price is an assumption, not a guarantee: a thin/volatile
            # testnet book CAN cross 50% of mark, and a filled probe is a real
            # funded long this suite promises not to strand. Flatten it and
            # carry the anomaly in the durable detail; the verdict stays with
            # the wire round-trip the test exists to prove (decision 2026-07-29).
            note = self._best_effort_close(filled)
            detail += f"; unexpectedly FILLED {filled} at the far price — " + (
                note or f"cleanup: reduce-only closed {filled}"
            )
        return SmokeStepResult("passed", detail=detail)

    def _test_slice_order_status(self) -> SmokeStepResult:
        # Interpreted with the SAME vocabulary as live/orders.py
        # (parse_order_status): a truthy payload is NOT proof — the venue's
        # miss shape is {"status": "unknownOid"}, so an any-payload check
        # would vacuously pass forever (exit-check 2026-07-28). Both legs are
        # meaningful: a booked cloid must resolve to its order (§8.3 rule 10),
        # and a never-booked cloid (test 3's far IOC refused per-order) must
        # answer unknownOid — the §19.2 confirmed-absent leg the duplicate
        # protocol relies on. A malformed payload raises out of the parser →
        # an ``error`` verdict, matching its fail-loud contract.
        if self._last_submit is None:
            # NOT a _SmokeAbort: the missing precondition means test 3 (selected
            # per the --only guard) failed before leaving its handle — a
            # dependency cascade, not an exchange refusal. ``failed`` would file
            # it in the "exchange refused" triage bucket and point the operator
            # at the venue; ``error`` files it with the harness/selection issues
            # where the root cause (test 3's own verdict) lives, and stops the
            # suite per the error policy (decision 2026-07-29).
            return SmokeStepResult(
                "error",
                error_message=(
                    "no prior submit to query — slice_order_submit (test 3) did not "
                    "complete in this process, so its cascade lands here; fix test 3 "
                    "first (this is not an exchange refusal)"
                ),
            )
        payload = self._wire.query_order_by_cloid(self._last_submit["cloid_hex"])
        resolved = parse_order_status(payload)
        booked_oid = self._last_submit["oid"]
        if booked_oid:
            if resolved is None:
                raise _SmokeAbort(
                    f"orderStatus answered unknownOid for a cloid the exchange "
                    f"booked (oid {booked_oid})"
                )
            oid, status = resolved
            return SmokeStepResult(
                "passed", detail=f"orderStatus resolved the cloid to oid {oid} ({status})"
            )
        if resolved is not None:
            raise _SmokeAbort(
                f"orderStatus resolved a cloid the venue refused to book "
                f"(oid {resolved[0]}) — the submit ack and orderStatus disagree"
            )
        return SmokeStepResult(
            "passed",
            detail="orderStatus answered unknownOid for the never-booked cloid (§19.2 negative leg)",
        )

    def _test_slice_plan_cancel(self) -> SmokeStepResult:
        # A resting order to cancel: the only resting-order primitive is a
        # trigger order, so a far reduce-only SL is placed then cancelled — the
        # same cancel-by-cloid wire action the engine uses to abort a plan's
        # resting orders.
        self._ensure_staged_long()
        _, cloid = self._register_cloid(role="cleanup_cancel", tag=f"cancel-{self._tag()}")
        self._track_probe(cloid)
        # The same "so far below mark it never fires" rule every SL probe uses —
        # one encoding, so a retune of the margin cannot drift between tests.
        trigger, limit = self._trigger_prices("sl")
        ack = self._wire.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._staged_probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl="sl",
            cloid_hex=cloid,
        )
        self._require_probe_accepted(ack, "resting probe order", cloid)
        self._cancel_tested_probe(cloid, "cancel-by-cloid")
        return SmokeStepResult("passed", detail="placed a resting order and cancelled it by cloid")

    def _test_multi_slice_fill(self) -> SmokeStepResult:
        # Two marketable IOC slices open a small long; the fills report confirms
        # the fill path. Best-effort close afterwards keeps the account flat (the
        # reduce-only close is itself test 7, but 6 must not strand a position if
        # 7 is deselected).
        size = self._probe_size()
        mark = self.ctx.mark_price()
        marketable = self._round_price(mark * Decimal("1.01"), up=True)
        filled = Decimal(0)
        try:
            for i in range(2):
                _logical, _cloid, ack = self._place_and_record_ioc(
                    role="entry",
                    tag=f"fill-{self._tag()}",
                    is_buy=True,
                    qty=size,
                    price=marketable,
                    slice_index=i,
                )
                self._require_accepted(ack, f"slice {i}")
                if ack.filled_size is not None:
                    filled += ack.filled_size
        except _SmokeAbort as exc:
            # Slice 1 failing after slice 0 filled must NOT strand the fill:
            # the happy-path close below never runs once an abort propagates,
            # and the suite's own docstring promise ("6 must not strand a
            # position") holds on the failure path too.
            if filled > 0:
                note = self._best_effort_close(filled)
                note = note or f"cleanup: closed the {filled} already filled"
                raise _SmokeAbort(f"{exc}; {note}") from None
            raise
        except Exception as exc:
            # Harness error (→ ``error`` verdict, which STOPS the suite with the
            # account state declared unknown). Same flatten — and chain the note
            # the way the _SmokeAbort sibling above does: this is the path where
            # "is a funded position still open?" matters most, and dropping the
            # return value left the answer only in a log line while the durable
            # error_message said nothing (2026-07-31 asymmetry review).
            if filled > 0:
                note = self._best_effort_close(filled)
                if note:
                    raise RuntimeError(f"{type(exc).__name__}: {exc}; {note}") from exc
            raise
        if filled <= 0:
            raise _SmokeAbort("neither slice filled — cannot confirm the multi-slice fill path")
        cleanup = self._best_effort_close(filled)
        detail = f"filled {filled} across 2 slices"
        if cleanup:
            detail += f"; {cleanup}"
        return SmokeStepResult("passed", detail=detail)

    def _test_reduce_only_close(self) -> SmokeStepResult:
        # Open a small long, then close it with a single aggressive reduce-only
        # IOC (§9.4 close shape). Verifies the reduce-only close fills.
        opened = self._open_probe_long(
            tag=f"close-open-{self._tag()}",
            what="close-test entry",
            no_fill="entry did not fill — nothing to reduce-only close",
        )
        close_ack = self._reduce_only_close(opened)
        self._require_full_close(close_ack, opened, "reduce-only close")
        return SmokeStepResult(
            "passed", detail=f"opened {opened} and reduce-only closed it in full"
        )

    def _open_probe_long(self, *, tag: str, what: str, no_fill: str) -> Decimal:
        """Open the small probe long the close-shaped tests (7 / 18) start from.

        One encoding of the open leg — marketable price offset, fill detection,
        booking — so the two tests that share this precondition can never drift
        apart. Returns the filled size (> 0), or raises.
        """
        size = self._probe_size()
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("1.01"), up=True)
        _logical, _cloid, ack = self._place_and_record_ioc(
            role="entry", tag=tag, is_buy=True, qty=size, price=price
        )
        self._require_accepted(ack, what)
        opened = ack.filled_size or Decimal(0)
        if opened <= 0:
            raise _SmokeAbort(no_fill)
        return opened

    def _ensure_staged_long(self) -> None:
        """Open the small long the resting-trigger probes protect (2026-07-29).

        With a flat account a reduce-only trigger has nothing to reduce, and
        whether the venue rests it anyway is leniency the suite must not bet
        on: a refusal would fail tests 5/8–13 as "exchange refused" with no
        code fix possible. One staged long under the whole trigger block makes
        every probe the exact §17 protection shape live emits. Opened lazily by
        the first trigger test in the selection (so a selection without them
        never opens it); a failure to open fails THAT test via _SmokeAbort.
        """
        if self._staged_long > 0:
            return
        try:
            self._staged_long = self._open_probe_long(
                tag=f"stage-{self._tag()}",
                what="trigger-block staging entry",
                no_fill=(
                    "staging entry did not fill — no position for the trigger probes to protect"
                ),
            )
        except _SmokeAbort:
            # A controlled abort means nothing is on the wire: the ack was
            # refused, the IOC rested (booked at filled=0), or it did not fill.
            raise
        except Exception:
            # Anything else — a store error between the exchange's fill and the
            # local book — may leave a REAL funded long that this runner never
            # learned the size of, so it can neither close nor size a retry.
            # Say so on the operator surface rather than exit silently clean.
            self.staged_long_residual = (
                "the trigger-block staging entry may have FILLED but could not be booked "
                "locally — verify the position on the exchange and close it manually, then use a NEW run-id for acceptance (a post-genesis manual fill books fill_unmapped, which pins this run's validate at exit 5)"
            )
            raise
        logger.info("smoke: staged %s long for the trigger-probe block", self._staged_long)

    def _close_staged_long(self) -> None:
        """Flatten the staged trigger-block long (best-effort, loudly logged).

        Called from the run loop as soon as no later selected test needs the
        position, and from run()'s ``finally`` as the crash/stop backstop. On a
        failed close the size is kept so the backstop retries, and the note
        lands on :attr:`staged_long_residual` for the CLI to surface — a real
        funded position must never be left on the wire with only a log line to
        say so. A later successful close clears the flag again.
        """
        if self._staged_long <= 0:
            return
        # Decrement by what ACTUALLY closed, not by what was requested: a
        # partial fill leaves a smaller residual, and a retry that re-sends the
        # original size would be reduce-only-oversized — refused outright by
        # some venues, and never converging on the true remainder.
        staged = self._staged_long
        closed, note = self._attempt_close(staged)
        self._staged_long -= closed
        if note is None:
            logger.info("smoke: staged trigger-block long (%s) flattened", staged)
            self.staged_long_residual = None
        else:
            logger.warning("smoke: staged trigger-block long NOT flat — %s", note)
            self.staged_long_residual = note

    def _reduce_only_close(self, size: Decimal) -> Any:
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("0.99"), up=False)
        _logical, _cloid, ack = self._place_and_record_ioc(
            role="close",
            tag=f"reduce-{self._tag()}",
            is_buy=False,
            qty=size,
            price=price,
            reduce_only=True,
        )
        return ack

    def _best_effort_close(self, size: Decimal) -> str | None:
        """Flatten a probe position; return a note if the close did NOT succeed.

        The core test (a fill happened) already passed, so a cleanup failure
        does not turn it red — but a stray funded position must not vanish from
        the audit trail. The note (from an exception OR a reduce-only close the
        exchange rejected at the per-order level — ``place_ioc_limit`` returns an
        unaccepted ``OrderAck`` rather than raising, so the ack must be checked,
        mirroring :meth:`_best_effort_cancel`) is folded into the step's
        ``detail`` (durable in ``live_smoke_tests``) and logged, so the operator
        can act on the residual position. Callers that must RETRY want
        :meth:`_attempt_close`, which also reports how much actually closed.

        A note is ALSO recorded on :attr:`position_residuals` so the CLI can
        warn about it. Every caller here is cleaning up an ACCIDENTAL fill, and
        folding the note into a passed step's detail was the only signal: a real
        funded position on the wire, reported by a green row.
        """
        closed, note = self._attempt_close(size)
        if note is not None:
            self.position_residuals.append(f"{size - closed} left after cleanup — {note}")
        # Deliberately APPEND-ONLY. The first draft cleared the list on a fully
        # successful close, reasoning that a later close supersedes an earlier
        # partial one — but the list is runner-level and every caller is cleaning
        # up a DIFFERENT probe, so a clean close in test 7 silently dropped test
        # 3's still-open position and the CLI printed nothing at all. Nor is
        # there a retry path for the situation the clearing was meant to serve:
        # _attempt_close does its own retrying internally, so no caller ever
        # re-closes the same residual (2026-07-31 exit check).
        return note

    def _attempt_close(self, size: Decimal) -> tuple[Decimal, str | None]:
        """``(closed, note)`` — the close's real outcome, note ``None`` if full.

        The amount matters to a caller that retries (the staged trigger-block
        long): a partial fill leaves a SMALLER residual, and re-sending the
        original size would be an oversized reduce-only — refused by some
        venues, and never converging. :meth:`_best_effort_close` is the
        note-only view for callers that fold the outcome into a step detail.
        """
        try:
            ack = self._reduce_only_close(size)
        except Exception as exc:  # noqa: BLE001 — cleanup must not turn a passing test red
            logger.warning(
                "smoke: best-effort close of %s FAILED — %s: %s", size, type(exc).__name__, exc
            )
            return Decimal(0), (
                f"cleanup: reduce-only close of {size} FAILED ({type(exc).__name__}: {exc})"
            )
        if not ack.accepted:
            logger.warning("smoke: best-effort close of %s refused: %s", size, ack.error)
            return Decimal(0), f"cleanup: reduce-only close of {size} refused ({ack.error})"
        closed = ack.filled_size or Decimal(0)
        if closed < size:
            # Same evidence standard as _require_full_close: an accepted close
            # is not a FULL close — a partial cleanup must not read as flat.
            logger.warning("smoke: best-effort close filled only %s of %s", closed, size)
            return closed, (
                f"cleanup: reduce-only close filled only {closed} of {size} — residual remains"
            )
        return closed, None

    def _require_full_close(self, close_ack: Any, opened: Decimal, what: str) -> None:
        """Fail unless the close leg filled the whole ``opened`` size (Q4 2026-07-28).

        ``accepted`` alone would let a thin testnet book record ``passed`` — and
        flip a §20.3 ``*_test_passed`` boolean — on partial evidence, while a
        residual position silently survives a suite the RUNBOOK advertises as
        self-cleaning. Any shortfall (refusal or partial fill) best-effort
        closes the residual and carries both facts in the failure detail.
        """
        closed = (close_ack.filled_size or Decimal(0)) if close_ack.accepted else Decimal(0)
        if close_ack.accepted and closed >= opened:
            return
        residual = opened - closed
        note = self._best_effort_close(residual) or f"cleanup: closed the residual {residual}"
        reason = (
            f"{what} was refused by the exchange: {close_ack.error}"
            if not close_ack.accepted
            else f"{what} filled {closed} of {opened} — the close must fill in full"
        )
        raise _SmokeAbort(f"{reason}; {note}")

    def _test_stop_loss_create(self) -> SmokeStepResult:
        return self._trigger_create("sl", "stop_loss", "SL")

    def _test_stop_loss_modify(self) -> SmokeStepResult:
        return self._trigger_modify("sl", "stop_loss", "SL")

    def _test_stop_loss_cancel(self) -> SmokeStepResult:
        return self._trigger_cancel("stop_loss", "SL")

    def _test_take_profit_create(self) -> SmokeStepResult:
        return self._trigger_create("tp", "take_profit", "TP")

    def _test_take_profit_modify(self) -> SmokeStepResult:
        return self._trigger_modify("tp", "take_profit", "TP")

    def _test_take_profit_cancel(self) -> SmokeStepResult:
        return self._trigger_cancel("take_profit", "TP")

    def _trigger_prices(self, tpsl: str) -> tuple[Decimal, Decimal]:
        """(trigger_price, limit_price) for a resting reduce-only trigger.

        Placed far from the mark (SL well below, TP well above) so the probe
        never actually fires during the smoke run — the test proves the place /
        modify / cancel wire actions, not a trigger execution.
        """
        mark = self.ctx.mark_price()
        # Triggers are rounded AWAY from the mark so legalization can only widen
        # the never-fires margin. The LIMIT is not a mirror of that: every probe
        # is a SELL (_TRIGGER_PROBE_IS_BUY), and live derives a sell's limit as
        # ``trigger * (1 - slip)`` — BELOW the trigger, marketable when it fires
        # (protection._protected_limit). The SL pair got that for free, since
        # further-from-mark is also lower; the TP pair did not, and a limit
        # ABOVE a sell trigger is the one shape live never emits. Since the
        # point of staging a real long is that each probe carries the exact §17
        # wire shape, put both limits on the marketable side and round them the
        # way live does (down for a sell).
        if tpsl == "sl":
            trigger = self._round_price(mark * Decimal("0.5"), up=False)
            limit = self._round_price(mark * Decimal("0.49"), up=False)
        else:
            trigger = self._round_price(mark * Decimal("1.5"), up=True)
            limit = self._round_price(mark * Decimal("1.49"), up=False)
        return trigger, limit

    def _trigger_create(self, tpsl: str, role: str, label: str) -> SmokeStepResult:
        self._ensure_staged_long()
        _, cloid = self._register_cloid(role=role, tag=f"{tpsl}-create-{self._tag()}")
        self._track_probe(cloid)
        trigger, limit = self._trigger_prices(tpsl)
        ack = self._wire.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._staged_probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=cloid,
        )
        self._require_probe_accepted(ack, f"{label} create", cloid)
        # Leave it cancelled so a create test never strands a resting order.
        cleanup = self._best_effort_cancel(cloid)
        detail = f"{label} placed (oid={ack.exchange_order_id})"
        if cleanup:
            detail += f"; {cleanup}"
        return SmokeStepResult("passed", detail=detail)

    def _trigger_modify(self, tpsl: str, role: str, label: str) -> SmokeStepResult:
        self._ensure_staged_long()
        _, create_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-a-{self._tag()}")
        self._track_probe(create_cloid)
        trigger, limit = self._trigger_prices(tpsl)
        # ONE size for both the place and the modify. Sizing each call separately
        # meant the "modify in place" silently RESIZED the order whenever the mark
        # moved between the two calls — so what §17.4 is supposed to prove here
        # (an update in place) was not what the wire actually saw.
        probe_size = self._staged_probe_size()
        created = self._wire.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=probe_size,
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=create_cloid,
        )
        self._require_probe_accepted(created, f"{label} modify (initial place)", create_cloid)
        created_oid = self._require_oid(created, f"{label} modify (initial place)")
        _, modify_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-b-{self._tag()}")
        self._track_probe(modify_cloid)
        # Nudge the trigger one tick — §17.4 modify-before-cancel updates in
        # place. Rounded in the nudge's own direction so legalization can never
        # collapse the new price back onto the (already-legal) original.
        nudge_up = tpsl == "tp"
        if nudge_up:
            new_trigger, new_limit = trigger + self.ctx.tick_size, limit + self.ctx.tick_size
        else:
            new_trigger, new_limit = trigger - self.ctx.tick_size, limit - self.ctx.tick_size
        try:
            modified = self._wire.modify_trigger_order(
                target=created_oid,
                coin=self.ctx.coin,
                is_buy=_TRIGGER_PROBE_IS_BUY,
                size=probe_size,
                limit_price=self._round_price(new_limit, up=nudge_up),
                trigger_price=self._round_price(new_trigger, up=nudge_up),
                tpsl=tpsl,
                cloid_hex=modify_cloid,
            )
            self._require_accepted(modified, f"{label} modify")
            # §17.4 re-labels IN PLACE, so the moment the modify is accepted the
            # two cloids name ONE order and create_cloid is a phantom. Retire it
            # here rather than off the cleanup's success below: tying it to the
            # cancel meant a refused cancel reported "2 probes may be resting"
            # when at most 1 can be (2026-07-31 exit check).
            self._resting_probes.discard(create_cloid)
        except _SmokeAbort as exc:
            # A refused modify leaves the ORIGINAL order untouched and still
            # resting under create_cloid (§17.4 contract) — the success-path
            # cleanup below would cancel modify_cloid, a cloid the exchange
            # never bound. Cancel the order that actually rests, and carry the
            # outcome in the failure detail.
            # The exchange ANSWERED and refused, so modify_cloid was never
            # bound to anything — retire it, or the exit sweep chases a cloid
            # that does not exist and reports a phantom residual.
            self._resting_probes.discard(modify_cloid)
            note = self._best_effort_cancel(create_cloid)
            note = note or f"cleanup: cancelled the original {label} probe ({create_cloid})"
            raise _SmokeAbort(f"{exc}; {note}") from None
        except Exception as exc:
            # Unknown outcome (the modify may or may not have re-labeled the
            # order) — sweep both cloids best-effort; exactly one exists, the
            # other cancel refuses. Chain whatever the sweep says into the
            # raised message, the way the _SmokeAbort sibling above already
            # does: this branch ends the whole suite with the account state
            # declared unknown, so "which of the two is still resting" is
            # precisely the fact the operator needs, and dropping it left it
            # only in a log line (2026-07-31 asymmetry review).
            notes = [
                note
                for note in (
                    self._best_effort_cancel(modify_cloid),
                    self._best_effort_cancel(create_cloid),
                )
                if note
            ]
            if len(notes) < 2:
                # One of the two cancels succeeded, which means it named the one
                # order that existed — so the other cloid is a phantom and must
                # not be carried into the exit sweep as a residual.
                self._resting_probes.discard(modify_cloid)
                self._resting_probes.discard(create_cloid)
            if notes:
                raise RuntimeError(f"{type(exc).__name__}: {exc}; {'; '.join(notes)}") from exc
            raise
        cleanup = self._best_effort_cancel(modify_cloid)
        detail = f"{label} modified in place (§17.4)"
        if cleanup:
            detail += f"; {cleanup}"
        return SmokeStepResult("passed", detail=detail)

    def _trigger_cancel(self, role: str, label: str) -> SmokeStepResult:
        self._ensure_staged_long()
        tpsl = "sl" if role == "stop_loss" else "tp"
        _, cloid = self._register_cloid(role=role, tag=f"{tpsl}-cancel-{self._tag()}")
        self._track_probe(cloid)
        trigger, limit = self._trigger_prices(tpsl)
        placed = self._wire.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._staged_probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=cloid,
        )
        self._require_probe_accepted(placed, f"{label} cancel (initial place)", cloid)
        self._cancel_tested_probe(cloid, f"{label} cancel")
        return SmokeStepResult("passed", detail=f"{label} placed and cancelled")

    def _cancel_tested_probe(self, cloid: str, what: str) -> None:
        """Cancel a probe where the cancel itself IS the tested action.

        A refusal fails the test — no second attempt would prove anything —
        but the probe is still resting on the exchange, and that must be said
        (one wording, shared by tests 5/10/13).
        """
        cancel = self._wire.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid)
        if not cancel.success:
            raise _SmokeAbort(
                f"{what} refused: {cancel.error} — the probe trigger "
                f"order is still resting on the exchange (cloid {cloid})"
            )
        self._resting_probes.discard(cloid)

    def _track_probe(self, cloid_hex_value: str) -> str:
        """Record a probe cloid as possibly-resting; returns it for chaining.

        Called BEFORE the place goes on the wire: if the ack is lost the order
        may well be live, and the whole point is to hold a handle for the exit
        sweep rather than discover the gap later as an orphan case.
        """
        self._resting_probes.add(cloid_hex_value)
        return cloid_hex_value

    def _require_probe_accepted(self, ack, what: str, cloid_hex_value: str) -> None:
        """:meth:`_require_accepted` for a TRACKED probe: a refusal retires it.

        The exchange answering "no" is positive evidence that no order exists
        under this cloid, so keeping the handle would have the exit sweep chase
        a phantom and report a residual that was never placed.

        A transport EXCEPTION never reaches here — it raises before there is an
        ack to inspect — which is exactly right: that is the unknown-outcome
        case, and the handle must survive it.
        """
        try:
            self._require_accepted(ack, what)
        except _SmokeAbort:
            self._resting_probes.discard(cloid_hex_value)
            raise

    def _sweep_resting_probes(self) -> None:
        """Cancel any probe still believed to rest, and report what would not go.

        Runs BEFORE the kill-switch disarm: the wallet's scheduleCancel is the
        only remaining backstop for a probe we cannot cancel, so clearing it
        first would turn "swept in ~30s by the dead man" into "rests until an
        operator notices".
        """
        for cloid_hex_value in sorted(self._resting_probes):
            self._best_effort_cancel(cloid_hex_value)
        if not self._resting_probes:
            self.probe_residual = None
            return
        self.probe_residual = (
            f"{len(self._resting_probes)} trigger probe(s) may still REST on the "
            f"exchange (cloids: {', '.join(sorted(self._resting_probes))}). They carry "
            "no local orders row, and §19.3's startup sweep validates rather than "
            "cancels protective roles — so the next `live` start files each as an "
            "orphan_exchange_order, which pins this run-id's `validate` at exit 5 "
            "permanently. Cancel them on the exchange before starting cycles"
        )
        logger.warning("smoke: %s", self.probe_residual)

    def _best_effort_cancel(self, cloid_hex_value: str) -> str | None:
        """Cancel a probe's resting order; return a note if it did NOT cancel.

        The create/modify already passed, so a failed cancel does not turn the
        test red — but a resting SL/TP trigger left live on the exchange must not
        vanish from the audit trail: the note (from an exception OR an
        unsuccessful ack) is folded into the step ``detail`` and logged.

        A CONFIRMED cancel also retires the cloid from :attr:`_resting_probes`;
        anything else leaves it there for the exit sweep to retry and, failing
        that, to report.
        """
        try:
            ack = self._wire.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid_hex_value)
        except Exception as exc:  # noqa: BLE001 — cleanup only; the create/modify already passed
            logger.warning(
                "smoke: best-effort cancel of %s FAILED — %s: %s",
                cloid_hex_value,
                type(exc).__name__,
                exc,
            )
            return f"cleanup: cancel of {cloid_hex_value} FAILED ({type(exc).__name__}: {exc})"
        if not ack.success:
            logger.warning(
                "smoke: best-effort cancel of %s refused: %s", cloid_hex_value, ack.error
            )
            return f"cleanup: cancel of {cloid_hex_value} refused ({ack.error})"
        self._resting_probes.discard(cloid_hex_value)
        return None

    def _test_kill_switch_arm_refresh(self) -> SmokeStepResult:
        # §18: arm the dead man's switch, refresh it (a second scheduleCancel),
        # then disarm — proving the wire action round-trips both ways.
        # Flagged before the first arm: if this test dies between arming and its
        # own clear, the exit disarm is what covers the wallet.
        self._kill_switch_may_be_armed = True
        deadline = self.ctx.now() + self.ctx.kill_switch_deadline
        self._wire.schedule_cancel(cancel_at=deadline)
        refreshed = self.ctx.now() + self.ctx.kill_switch_deadline
        self._wire.schedule_cancel(cancel_at=refreshed)
        self._wire.clear_scheduled_cancel()
        return SmokeStepResult("passed", detail="scheduleCancel armed, refreshed, and cleared")

    def _test_restart_reconciliation(self) -> SmokeStepResult:
        result = self._require_recovery()
        if not result.passed:
            raise _SmokeAbort("startup recovery verdict did not pass on a clean restart")
        return SmokeStepResult("passed", detail="restart recovery verdict passed")

    def _test_startup_with_existing_position(self) -> SmokeStepResult:
        # Recovery must reconcile (adopt) an existing exchange position without
        # entering manual safe mode. The precondition (a live position) is the
        # operator's setup on testnet; the seam runs recovery and reports the
        # verdict, which is unclean if the position was not reconciled.
        result = self._require_recovery()
        if not result.passed:
            raise _SmokeAbort("recovery did not cleanly reconcile the existing position")
        return SmokeStepResult("passed", detail="existing position reconciled at startup")

    def _test_startup_with_stale_open_order(self) -> SmokeStepResult:
        # Recovery must cancel a stale bot-owned resting order (§19.3) and reach
        # a clean verdict.
        result = self._require_recovery()
        if not result.passed:
            raise _SmokeAbort("recovery did not clear the stale bot-owned order")
        return SmokeStepResult("passed", detail="stale bot-owned order swept at startup")

    def _test_emergency_close(self) -> SmokeStepResult:
        # §17.2 emergency close = a single aggressive reduce-only IOC on a live
        # position. Open a small long, then close it with the emergency-close
        # wire shape (protective, reduce-only) so the escalation path's terminal
        # action is proven end-to-end.
        opened = self._open_probe_long(
            tag=f"emrg-open-{self._tag()}",
            what="emergency-close entry",
            no_fill="entry did not fill — nothing to emergency-close",
        )
        mark = self.ctx.mark_price()
        close_price = self._round_price(mark * Decimal("0.97"), up=False)
        _logical, _cloid, close_ack = self._place_and_record_ioc(
            role="emergency_close",
            tag=f"emrg-{self._tag()}",
            is_buy=False,
            qty=opened,
            price=close_price,
            reduce_only=True,
            protective=True,
        )
        self._require_full_close(close_ack, opened, "emergency close")
        return SmokeStepResult(
            "passed", detail=f"emergency-closed {opened} in full (aggressive reduce-only IOC)"
        )


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


# The dispatch table is reflective (``getattr(self, f"_test_{key}")``), so it is
# the one mapping in this module with no compile-time link between the registry
# and the code. It is also the most consequential: a new SMOKE_TESTS entry whose
# method is missing (or either side typo'd) passes the uniqueness guard above
# and only breaks when that key is SELECTED — mid-suite, on a real run. Bind it
# here, the same way the three policy sets are bound to SMOKE_TEST_KEYS.
_MISSING_TEST_METHODS = [
    t.key for t in SMOKE_TESTS if not hasattr(SmokeTestRunner, f"_test_{t.key}")
]
if _MISSING_TEST_METHODS:
    raise AssertionError(
        f"SMOKE_TESTS keys with no SmokeTestRunner._test_<key> method: {_MISSING_TEST_METHODS}"
    )
