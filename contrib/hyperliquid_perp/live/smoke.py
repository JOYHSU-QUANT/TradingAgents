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
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from ..paper.run_lock import RunLockError
from ..paper.stops import round_to_tick
from ..persistence import repository as repo
from ..persistence.cloid import cloid_hex, cloid_logical
from ..persistence.db import Database
from .orders import parse_order_status

logger = logging.getLogger(__name__)

__all__ = [
    "SMOKE_TESTS",
    "SMOKE_TEST_KEYS",
    "RecoveryResult",
    "SmokeContext",
    "SmokePreflightError",
    "SmokeStepResult",
    "SmokeTest",
    "SmokeTestRunner",
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
assert len(_BY_KEY) == len(SMOKE_TESTS), "SMOKE_TESTS keys must be unique"

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
    assert _copied <= set(SMOKE_TEST_KEYS), (
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

    The CLI builds this from the live components (`_cmd_live`'s gate/client/
    recovery wiring); the unit tests build it from fakes. ``run_recovery`` runs
    one §19.1 startup recovery and returns an object exposing ``.passed`` (the
    real :func:`~.startup.run_startup_recovery` result, or a fake); it is the
    seam the three restart tests (15–17) drive so they never reconstruct the
    engine. ``now`` supplies the clock (wall clock in production, a fake in
    tests) for cloid uniqueness and result timestamps.
    """

    signed: Any
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


@dataclass(frozen=True)
class SmokeStepResult:
    """One test's verdict, before it is stamped into ``live_smoke_tests``."""

    status: Literal["passed", "failed", "error", "skipped"]  # repo.LIVE_SMOKE_TEST_STATUSES
    detail: str | None = None
    error_message: str | None = None


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
        # Only a run that armed the switch disarms on exit — via the pre-flight
        # recovery or a switch-touching test; a run that armed nothing must NOT
        # fire an account-wide clear that could wipe a concurrent ``live
        # --loop``'s own arm (Q2 + exit-check finding, 2026-07-27). Keyed off
        # ``selected`` (not ``executed``) so a mid-suite crash still disarms what
        # the pre-flight or a restart test's recovery may already have armed.
        arms_kill_switch = needs_preflight or any(t.key in _KILL_SWITCH_TESTS for t in selected)
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
            # In the ``finally`` so a mid-suite crash disarms too — EXCEPT when
            # the lease was superseded: the successor owns the wallet's switch
            # now, and an account-wide clear would strip its dead-man cover.
            # The staged long is flattened FIRST: it is real exposure, and the
            # close is an IOC the disarm does not depend on.
            self._close_staged_long()
            if arms_kill_switch and not self._lease_superseded:
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

    def _refresh_kill_switch(self) -> None:
        """Re-arm the pre-flight's dead-man switch before each test (§18).

        The pre-flight recovery arms the account-wide scheduleCancel at
        now + ``kill_switch_deadline`` (120s) and nothing else refreshes it
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
        self.ctx.signed.schedule_cancel(cancel_at=self.ctx.now() + self.ctx.kill_switch_deadline)

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
        method = getattr(self, f"_test_{test.key}")
        try:
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
            self.ctx.signed.clear_scheduled_cancel()
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

    def _require_accepted(self, ack: Any, what: str) -> None:
        if not ack.accepted:
            raise _SmokeAbort(f"{what} was refused by the exchange: {ack.error}")

    def _require_recovery(self) -> RecoveryResult:
        if self.ctx.run_recovery is None:
            raise _SmokeAbort(
                "no run_recovery seam wired — the restart tests need the §19.1 "
                "recovery components (the CLI supplies them; a bare context cannot)"
            )
        return self.ctx.run_recovery()

    # -- the §20.2 tests --------------------------------------------------

    def _test_signed_client_init(self) -> SmokeStepResult:
        # §6.1 authorization was proven by the `live` gate before the suite
        # started; this re-affirms the signed transport reaches testnet and the
        # agent address is bound.
        self.ctx.signed.health_check()
        agent = self.ctx.signed.agent_address
        if not agent:
            raise _SmokeAbort("signed client has no agent_address after init")
        return SmokeStepResult("passed", detail=f"agent {agent} on {self.ctx.network}")

    def _test_update_leverage(self) -> SmokeStepResult:
        # Writes the RUN'S OWN configured regime, not a spec literal: this is
        # the only place in the system that sets the exchange-side regime, and
        # it must never flip an account to a leverage/margin mode the config
        # did not declare (decision 2026-07-29).
        self.ctx.signed.update_leverage(
            coin=self.ctx.coin, leverage=self.ctx.leverage, is_cross=self.ctx.is_cross
        )
        regime = f"{self.ctx.leverage}x {'cross' if self.ctx.is_cross else 'isolated'}"
        return SmokeStepResult("passed", detail=f"{self.ctx.coin} leverage set to {regime}")

    def _test_slice_order_submit(self) -> SmokeStepResult:
        # A far-below-mark IOC buy: the wire action round-trips but never fills
        # (so nothing is left to clean up) — a pure "can we submit a slice" test.
        logical, cloid = self._register_cloid(role="entry", tag=f"submit-{self._tag()}")
        size = self._probe_size()
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("0.5"), up=False)
        ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=True,
            size=size,
            limit_price=price,
            cloid_hex=cloid,
        )
        self._record_probe_order(
            ack,
            cloid_logical=logical,
            cloid_hex_value=cloid,
            role="entry",
            side="buy",
            qty=size,
            price=price,
        )
        # A far IOC that does not cross returns 'error'/'not filled' from some
        # venues; what the test proves is that the ACTION reached the matching
        # engine without a top-level envelope error (that would have raised).
        self._last_submit = {"cloid_hex": cloid, "oid": ack.exchange_order_id or ""}
        detail = f"submitted IOC (status={ack.status}, oid={ack.exchange_order_id})"
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
        payload = self.ctx.signed.query_order_by_cloid(self._last_submit["cloid_hex"])
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
        # The same "so far below mark it never fires" rule every SL probe uses —
        # one encoding, so a retune of the margin cannot drift between tests.
        trigger, limit = self._trigger_prices("sl")
        ack = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl="sl",
            cloid_hex=cloid,
        )
        self._require_accepted(ack, "resting probe order")
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
                logical, cloid = self._register_cloid(
                    role="entry", tag=f"fill-{self._tag()}", slice_index=i
                )
                ack = self.ctx.signed.place_ioc_limit(
                    coin=self.ctx.coin,
                    is_buy=True,
                    size=size,
                    limit_price=marketable,
                    cloid_hex=cloid,
                )
                self._record_probe_order(
                    ack,
                    cloid_logical=logical,
                    cloid_hex_value=cloid,
                    role="entry",
                    side="buy",
                    qty=size,
                    price=marketable,
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
        except Exception:
            # Harness error (→ ``error`` verdict): same flatten, note in the log.
            if filled > 0:
                self._best_effort_close(filled)
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
        logical, cloid = self._register_cloid(role="entry", tag=tag)
        ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=True,
            size=size,
            limit_price=price,
            cloid_hex=cloid,
        )
        self._record_probe_order(
            ack,
            cloid_logical=logical,
            cloid_hex_value=cloid,
            role="entry",
            side="buy",
            qty=size,
            price=price,
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
                "locally — verify the position on the exchange and close it manually"
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
        logical, cloid = self._register_cloid(role="close", tag=f"reduce-{self._tag()}")
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("0.99"), up=False)
        ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=False,
            size=size,
            limit_price=price,
            cloid_hex=cloid,
            reduce_only=True,
        )
        self._record_probe_order(
            ack,
            cloid_logical=logical,
            cloid_hex_value=cloid,
            role="close",
            side="sell",
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
        """
        return self._attempt_close(size)[1]

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
        # Rounded AWAY from the mark (down for the SL pair, up for the TP pair)
        # so legalization can only widen the never-fires margin, and the limit
        # keeps its side of the trigger.
        if tpsl == "sl":
            trigger = self._round_price(mark * Decimal("0.5"), up=False)
            limit = self._round_price(mark * Decimal("0.49"), up=False)
        else:
            trigger = self._round_price(mark * Decimal("1.5"), up=True)
            limit = self._round_price(mark * Decimal("1.51"), up=True)
        return trigger, limit

    def _trigger_create(self, tpsl: str, role: str, label: str) -> SmokeStepResult:
        self._ensure_staged_long()
        _, cloid = self._register_cloid(role=role, tag=f"{tpsl}-create-{self._tag()}")
        trigger, limit = self._trigger_prices(tpsl)
        ack = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=cloid,
        )
        self._require_accepted(ack, f"{label} create")
        # Leave it cancelled so a create test never strands a resting order.
        cleanup = self._best_effort_cancel(cloid)
        detail = f"{label} placed (oid={ack.exchange_order_id})"
        if cleanup:
            detail += f"; {cleanup}"
        return SmokeStepResult("passed", detail=detail)

    def _trigger_modify(self, tpsl: str, role: str, label: str) -> SmokeStepResult:
        self._ensure_staged_long()
        _, create_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-a-{self._tag()}")
        trigger, limit = self._trigger_prices(tpsl)
        created = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=create_cloid,
        )
        self._require_accepted(created, f"{label} modify (initial place)")
        _, modify_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-b-{self._tag()}")
        # Nudge the trigger one tick — §17.4 modify-before-cancel updates in
        # place. Rounded in the nudge's own direction so legalization can never
        # collapse the new price back onto the (already-legal) original.
        nudge_up = tpsl == "tp"
        if nudge_up:
            new_trigger, new_limit = trigger + self.ctx.tick_size, limit + self.ctx.tick_size
        else:
            new_trigger, new_limit = trigger - self.ctx.tick_size, limit - self.ctx.tick_size
        try:
            modified = self.ctx.signed.modify_trigger_order(
                target=created.exchange_order_id,
                coin=self.ctx.coin,
                is_buy=_TRIGGER_PROBE_IS_BUY,
                size=self._probe_size(),
                limit_price=self._round_price(new_limit, up=nudge_up),
                trigger_price=self._round_price(new_trigger, up=nudge_up),
                tpsl=tpsl,
                cloid_hex=modify_cloid,
            )
            self._require_accepted(modified, f"{label} modify")
        except _SmokeAbort as exc:
            # A refused modify leaves the ORIGINAL order untouched and still
            # resting under create_cloid (§17.4 contract) — the success-path
            # cleanup below would cancel modify_cloid, a cloid the exchange
            # never bound. Cancel the order that actually rests, and carry the
            # outcome in the failure detail.
            note = self._best_effort_cancel(create_cloid)
            note = note or f"cleanup: cancelled the original {label} probe ({create_cloid})"
            raise _SmokeAbort(f"{exc}; {note}") from None
        except Exception:
            # Unknown outcome (the modify may or may not have re-labeled the
            # order) — sweep both cloids best-effort; exactly one exists, the
            # other cancel refuses and lands in the log only.
            self._best_effort_cancel(modify_cloid)
            self._best_effort_cancel(create_cloid)
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
        trigger, limit = self._trigger_prices(tpsl)
        placed = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=_TRIGGER_PROBE_IS_BUY,
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=cloid,
        )
        self._require_accepted(placed, f"{label} cancel (initial place)")
        self._cancel_tested_probe(cloid, f"{label} cancel")
        return SmokeStepResult("passed", detail=f"{label} placed and cancelled")

    def _cancel_tested_probe(self, cloid: str, what: str) -> None:
        """Cancel a probe where the cancel itself IS the tested action.

        A refusal fails the test — no second attempt would prove anything —
        but the probe is still resting on the exchange, and that must be said
        (one wording, shared by tests 5/10/13).
        """
        cancel = self.ctx.signed.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid)
        if not cancel.success:
            raise _SmokeAbort(
                f"{what} refused: {cancel.error} — the probe trigger "
                f"order is still resting on the exchange (cloid {cloid})"
            )

    def _best_effort_cancel(self, cloid_hex_value: str) -> str | None:
        """Cancel a probe's resting order; return a note if it did NOT cancel.

        The create/modify already passed, so a failed cancel does not turn the
        test red — but a resting SL/TP trigger left live on the exchange must not
        vanish from the audit trail: the note (from an exception OR an
        unsuccessful ack) is folded into the step ``detail`` and logged.
        """
        try:
            ack = self.ctx.signed.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid_hex_value)
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
        return None

    def _test_kill_switch_arm_refresh(self) -> SmokeStepResult:
        # §18: arm the dead man's switch, refresh it (a second scheduleCancel),
        # then disarm — proving the wire action round-trips both ways.
        deadline = self.ctx.now() + self.ctx.kill_switch_deadline
        self.ctx.signed.schedule_cancel(cancel_at=deadline)
        refreshed = self.ctx.now() + self.ctx.kill_switch_deadline
        self.ctx.signed.schedule_cancel(cancel_at=refreshed)
        self.ctx.signed.clear_scheduled_cancel()
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
        close_logical, close_cloid = self._register_cloid(
            role="emergency_close", tag=f"emrg-{self._tag()}"
        )
        close_ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=False,
            size=opened,
            limit_price=close_price,
            cloid_hex=close_cloid,
            reduce_only=True,
            protective=True,
        )
        self._record_probe_order(
            close_ack,
            cloid_logical=close_logical,
            cloid_hex_value=close_cloid,
            role="emergency_close",
            side="sell",
            qty=opened,
            price=close_price,
            reduce_only=True,
        )
        self._require_full_close(close_ack, opened, "emergency close")
        return SmokeStepResult(
            "passed", detail=f"emergency-closed {opened} in full (aggressive reduce-only IOC)"
        )


# --------------------------------------------------------------------------
# Cycle-entry gate (§20.2: all smoke tests must pass)
# --------------------------------------------------------------------------


def smoke_gate_report(
    conn: Any, run_id: str
) -> tuple[bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
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
    return passed, tuple(missing), tuple(failed), tuple(errored)


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
            "--only slice_order_submit,slice_order_status"
        )
    return keys
