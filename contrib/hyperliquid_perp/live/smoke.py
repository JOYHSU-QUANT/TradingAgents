"""Testnet smoke-test runner (phase3-spec §20.2, PR 6).

Before a testnet_live run may enter its §20.3 acceptance cycles, the §20.2
checklist of signed exchange actions must pass end-to-end against a real
testnet connection. This module turns that checklist into a per-item,
re-runnable, DB-recorded CLI (`live-smoke`): every test drives the actual PR 1–5
signed client / recovery components, its verdict lands in ``live_smoke_tests``
(append-only — a re-run after a fix supersedes without erasing the record), and
the cycle-entry gate (:func:`smoke_gate_passed`) reads the latest non-dry-run
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
seam for sizing, and a ``run_recovery`` seam for the restart tests (15–17). The
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
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any, Literal

from ..persistence import repository as repo
from ..persistence.cloid import cloid_hex, cloid_logical
from ..persistence.db import Database

logger = logging.getLogger(__name__)

__all__ = [
    "SMOKE_TESTS",
    "SMOKE_TEST_KEYS",
    "SmokeContext",
    "SmokeStepResult",
    "SmokeTest",
    "SmokeTestRunner",
    "smoke_gate_passed",
    "smoke_gate_report",
    "validate_only_keys",
]


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

# Just over Hyperliquid's ~$10 minimum order value, so a probe order the suite
# means to REST or FILL is not refused for being dust.
_PROBE_NOTIONAL_USDC = Decimal("11")


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
    run_recovery: Callable[[], Any] | None = None


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

    # -- orchestration ----------------------------------------------------

    def run(self, *, only: Sequence[str] | None = None) -> list[SmokeTest]:
        """Execute the selected tests in order, persisting each verdict.

        ``only`` restricts to the named test keys (validated by the CLI); the
        default runs all of :data:`SMOKE_TESTS`. Returns the tests that were
        executed (in order) — the caller reports them and computes the gate from
        the store, never from this return value, so a crash mid-suite still
        leaves every completed verdict durable.
        """
        selected = self._select(only)
        executed: list[SmokeTest] = []
        try:
            for test in selected:
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
        finally:
            # A restart test's recovery arms the dead man's switch; clear it so a
            # completed suite never leaves an armed scheduleCancel behind (Q2,
            # 2026-07-27). In the ``finally`` so a mid-suite crash disarms too.
            self._disarm_kill_switch()
        return executed

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

    def _disarm_kill_switch(self) -> None:
        """Clear any dead man's switch a restart test's recovery armed (§18).

        The restart tests (15–17) each drive a real §19.1 startup recovery whose
        first step ARMS the scheduleCancel. Test 14 clears its own arm, but runs
        before 15–17, so without this a full suite would exit leaving the wallet
        with an armed scheduleCancel that fires ~``kill_switch_deadline`` later and
        cancels every resting order. Real runs only (a dry run placed no wire
        actions and carries no signed client); best-effort — a failed disarm is
        logged, never raised: the verdicts are already durable and the next
        ``live --loop`` re-arms and refreshes the switch anyway.
        """
        if self.ctx.dry_run or self.ctx.signed is None:
            return
        try:
            self.ctx.signed.clear_scheduled_cancel()
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask the suite's verdicts
            logger.warning(
                "smoke: best-effort kill-switch disarm FAILED — %s: %s",
                type(exc).__name__,
                exc,
            )

    # -- shared helpers ---------------------------------------------------

    def _register_cloid(self, *, role: str, tag: str, slice_index: int = 0) -> str:
        """Derive a unique, attributable cloid for a smoke order and register it.

        ``tag`` (a per-execution marker, e.g. the wall-clock stamp) keeps a
        re-run's cloid distinct from a prior run's so the exchange never sees a
        reused cloid_hex, while the §19.3 reverse lookup still resolves every
        smoke order to this run (bot-owned). Registered before the send, exactly
        as the live submit path does (§8.3 rule 1).
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
        return hex_

    def _tag(self) -> str:
        # A whitespace-free, per-execution marker for cloid uniqueness. The
        # cloid segment guard rejects spaces, so use the compact ISO basic form.
        return self.ctx.now().strftime("%Y%m%dT%H%M%S%f")

    def _round_price(self, raw: Decimal) -> Decimal:
        step = self.ctx.tick_size
        return (raw / step).to_integral_value(rounding=ROUND_HALF_EVEN) * step

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

    def _require_recovery(self) -> Any:
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
        self.ctx.signed.update_leverage(coin=self.ctx.coin, leverage=1, is_cross=True)
        return SmokeStepResult("passed", detail=f"{self.ctx.coin} leverage set to 1x cross")

    def _test_slice_order_submit(self) -> SmokeStepResult:
        # A far-below-mark IOC buy: the wire action round-trips but never fills
        # (so nothing is left to clean up) — a pure "can we submit a slice" test.
        cloid = self._register_cloid(role="entry", tag=f"submit-{self._tag()}")
        mark = self.ctx.mark_price()
        price = self._round_price(mark * Decimal("0.5"))
        ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=True,
            size=self._probe_size(),
            limit_price=price,
            cloid_hex=cloid,
        )
        # A far IOC that does not cross returns 'error'/'not filled' from some
        # venues; what the test proves is that the ACTION reached the matching
        # engine without a top-level envelope error (that would have raised).
        self._last_submit = {"cloid_hex": cloid, "oid": ack.exchange_order_id or ""}
        return SmokeStepResult(
            "passed",
            detail=f"submitted IOC (status={ack.status}, oid={ack.exchange_order_id})",
        )

    def _test_slice_order_status(self) -> SmokeStepResult:
        if self._last_submit is None:
            raise _SmokeAbort("no prior submit to query (run test 3 first, or select both)")
        status = self.ctx.signed.query_order_by_cloid(self._last_submit["cloid_hex"])
        if status is None:
            raise _SmokeAbort("orderStatus returned nothing for the submitted cloid")
        return SmokeStepResult("passed", detail="orderStatus resolved the submitted cloid")

    def _test_slice_plan_cancel(self) -> SmokeStepResult:
        # A resting order to cancel: the only resting-order primitive is a
        # trigger order, so a far reduce-only SL is placed then cancelled — the
        # same cancel-by-cloid wire action the engine uses to abort a plan's
        # resting orders.
        cloid = self._register_cloid(role="cleanup_cancel", tag=f"cancel-{self._tag()}")
        mark = self.ctx.mark_price()
        trigger = self._round_price(mark * Decimal("0.5"))
        limit = self._round_price(mark * Decimal("0.49"))
        ack = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=True,
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl="sl",
            cloid_hex=cloid,
        )
        self._require_accepted(ack, "resting probe order")
        cancel = self.ctx.signed.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid)
        if not cancel.success:
            raise _SmokeAbort(f"cancel-by-cloid refused: {cancel.error}")
        return SmokeStepResult("passed", detail="placed a resting order and cancelled it by cloid")

    def _test_multi_slice_fill(self) -> SmokeStepResult:
        # Two marketable IOC slices open a small long; the fills report confirms
        # the fill path. Best-effort close afterwards keeps the account flat (the
        # reduce-only close is itself test 7, but 6 must not strand a position if
        # 7 is deselected).
        size = self._probe_size()
        mark = self.ctx.mark_price()
        marketable = self._round_price(mark * Decimal("1.01"))
        filled = Decimal(0)
        for i in range(2):
            cloid = self._register_cloid(role="entry", tag=f"fill-{self._tag()}", slice_index=i)
            ack = self.ctx.signed.place_ioc_limit(
                coin=self.ctx.coin,
                is_buy=True,
                size=size,
                limit_price=marketable,
                cloid_hex=cloid,
            )
            self._require_accepted(ack, f"slice {i}")
            if ack.filled_size is not None:
                filled += ack.filled_size
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
        size = self._probe_size()
        mark = self.ctx.mark_price()
        open_cloid = self._register_cloid(role="entry", tag=f"close-open-{self._tag()}")
        open_ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=True,
            size=size,
            limit_price=self._round_price(mark * Decimal("1.01")),
            cloid_hex=open_cloid,
        )
        self._require_accepted(open_ack, "close-test entry")
        opened = open_ack.filled_size or Decimal(0)
        if opened <= 0:
            raise _SmokeAbort("entry did not fill — nothing to reduce-only close")
        close_ack = self._reduce_only_close(opened)
        self._require_accepted(close_ack, "reduce-only close")
        return SmokeStepResult("passed", detail=f"opened {opened} and reduce-only closed it")

    def _reduce_only_close(self, size: Decimal) -> Any:
        cloid = self._register_cloid(role="close", tag=f"reduce-{self._tag()}")
        mark = self.ctx.mark_price()
        return self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=False,
            size=size,
            limit_price=self._round_price(mark * Decimal("0.99")),
            cloid_hex=cloid,
            reduce_only=True,
        )

    def _best_effort_close(self, size: Decimal) -> str | None:
        """Flatten a probe position; return a note if the close did NOT succeed.

        The core test (a fill happened) already passed, so a cleanup failure
        does not turn it red — but a stray funded position must not vanish from
        the audit trail. The note (from an exception OR a reduce-only close the
        exchange rejected at the per-order level — ``place_ioc_limit`` returns an
        unaccepted ``OrderAck`` rather than raising, so the ack must be checked,
        mirroring :meth:`_best_effort_cancel`) is folded into the step's
        ``detail`` (durable in ``live_smoke_tests``) and logged, so the operator
        can act on the residual position.
        """
        try:
            ack = self._reduce_only_close(size)
        except Exception as exc:  # noqa: BLE001 — cleanup must not turn a passing test red
            logger.warning(
                "smoke: best-effort close of %s FAILED — %s: %s", size, type(exc).__name__, exc
            )
            return f"cleanup: reduce-only close of {size} FAILED ({type(exc).__name__}: {exc})"
        if not ack.accepted:
            logger.warning("smoke: best-effort close of %s refused: %s", size, ack.error)
            return f"cleanup: reduce-only close of {size} refused ({ack.error})"
        return None

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
        if tpsl == "sl":
            trigger = self._round_price(mark * Decimal("0.5"))
            limit = self._round_price(mark * Decimal("0.49"))
        else:
            trigger = self._round_price(mark * Decimal("1.5"))
            limit = self._round_price(mark * Decimal("1.51"))
        return trigger, limit

    def _trigger_create(self, tpsl: str, role: str, label: str) -> SmokeStepResult:
        cloid = self._register_cloid(role=role, tag=f"{tpsl}-create-{self._tag()}")
        trigger, limit = self._trigger_prices(tpsl)
        ack = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=(tpsl == "sl"),
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
        create_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-a-{self._tag()}")
        trigger, limit = self._trigger_prices(tpsl)
        created = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=(tpsl == "sl"),
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=create_cloid,
        )
        self._require_accepted(created, f"{label} modify (initial place)")
        modify_cloid = self._register_cloid(role=role, tag=f"{tpsl}-mod-b-{self._tag()}")
        # Nudge the trigger one tick — §17.4 modify-before-cancel updates in place.
        if tpsl == "tp":
            new_trigger, new_limit = trigger + self.ctx.tick_size, limit + self.ctx.tick_size
        else:
            new_trigger, new_limit = trigger - self.ctx.tick_size, limit - self.ctx.tick_size
        modified = self.ctx.signed.modify_trigger_order(
            target=created.exchange_order_id,
            coin=self.ctx.coin,
            is_buy=(tpsl == "sl"),
            size=self._probe_size(),
            limit_price=self._round_price(new_limit),
            trigger_price=self._round_price(new_trigger),
            tpsl=tpsl,
            cloid_hex=modify_cloid,
        )
        self._require_accepted(modified, f"{label} modify")
        cleanup = self._best_effort_cancel(modify_cloid)
        detail = f"{label} modified in place (§17.4)"
        if cleanup:
            detail += f"; {cleanup}"
        return SmokeStepResult("passed", detail=detail)

    def _trigger_cancel(self, role: str, label: str) -> SmokeStepResult:
        tpsl = "sl" if role == "stop_loss" else "tp"
        cloid = self._register_cloid(role=role, tag=f"{tpsl}-cancel-{self._tag()}")
        trigger, limit = self._trigger_prices(tpsl)
        placed = self.ctx.signed.place_trigger_order(
            coin=self.ctx.coin,
            is_buy=(tpsl == "sl"),
            size=self._probe_size(),
            limit_price=limit,
            trigger_price=trigger,
            tpsl=tpsl,
            cloid_hex=cloid,
        )
        self._require_accepted(placed, f"{label} cancel (initial place)")
        cancel = self.ctx.signed.cancel_by_cloid(coin=self.ctx.coin, cloid_hex=cloid)
        if not cancel.success:
            raise _SmokeAbort(f"{label} cancel refused: {cancel.error}")
        return SmokeStepResult("passed", detail=f"{label} placed and cancelled")

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
        if not getattr(result, "passed", False):
            raise _SmokeAbort("startup recovery verdict did not pass on a clean restart")
        return SmokeStepResult("passed", detail="restart recovery verdict passed")

    def _test_startup_with_existing_position(self) -> SmokeStepResult:
        # Recovery must reconcile (adopt) an existing exchange position without
        # entering manual safe mode. The precondition (a live position) is the
        # operator's setup on testnet; the seam runs recovery and reports the
        # verdict, which is unclean if the position was not reconciled.
        result = self._require_recovery()
        if not getattr(result, "passed", False):
            raise _SmokeAbort("recovery did not cleanly reconcile the existing position")
        return SmokeStepResult("passed", detail="existing position reconciled at startup")

    def _test_startup_with_stale_open_order(self) -> SmokeStepResult:
        # Recovery must cancel a stale bot-owned resting order (§19.3) and reach
        # a clean verdict.
        result = self._require_recovery()
        if not getattr(result, "passed", False):
            raise _SmokeAbort("recovery did not clear the stale bot-owned order")
        return SmokeStepResult("passed", detail="stale bot-owned order swept at startup")

    def _test_emergency_close(self) -> SmokeStepResult:
        # §17.2 emergency close = a single aggressive reduce-only IOC on a live
        # position. Open a small long, then close it with the emergency-close
        # wire shape (protective, reduce-only) so the escalation path's terminal
        # action is proven end-to-end.
        size = self._probe_size()
        mark = self.ctx.mark_price()
        open_cloid = self._register_cloid(role="entry", tag=f"emrg-open-{self._tag()}")
        open_ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=True,
            size=size,
            limit_price=self._round_price(mark * Decimal("1.01")),
            cloid_hex=open_cloid,
        )
        self._require_accepted(open_ack, "emergency-close entry")
        opened = open_ack.filled_size or Decimal(0)
        if opened <= 0:
            raise _SmokeAbort("entry did not fill — nothing to emergency-close")
        close_cloid = self._register_cloid(role="emergency_close", tag=f"emrg-{self._tag()}")
        close_ack = self.ctx.signed.place_ioc_limit(
            coin=self.ctx.coin,
            is_buy=False,
            size=opened,
            limit_price=self._round_price(mark * Decimal("0.97")),
            cloid_hex=close_cloid,
            reduce_only=True,
            protective=True,
        )
        self._require_accepted(close_ack, "emergency close")
        return SmokeStepResult(
            "passed", detail=f"emergency-closed {opened} (aggressive reduce-only IOC)"
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


def smoke_gate_passed(conn: Any, run_id: str) -> bool:
    """True iff every §20.2 smoke test's latest non-dry-run result is ``passed``."""
    passed, *_rest = smoke_gate_report(conn, run_id)
    return passed


def validate_only_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Return the given keys if all are real test keys, else raise ValueError.

    The CLI's ``--only`` guard: a typo'd key must name itself, not silently run
    an empty suite (which would then read as "all selected tests passed").
    """
    keys = tuple(keys)
    unknown = [k for k in keys if k not in _BY_KEY]
    if unknown:
        raise ValueError(
            f"unknown smoke test key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(SMOKE_TEST_KEYS)}"
        )
    return keys
