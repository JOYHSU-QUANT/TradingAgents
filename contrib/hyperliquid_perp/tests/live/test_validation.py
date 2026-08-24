"""Live acceptance validator (§20.3 / §21.4) — metric computation over fake stores."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live import smoke
from contrib.hyperliquid_perp.live.fills import ExchangeFill, post_live_fill
from contrib.hyperliquid_perp.live.validation import (
    _REFRESH_BAR,
    MIN_KILL_SWITCH_REFRESH_RATE,
    MIN_KILL_SWITCH_REFRESH_SAMPLES,
    MIN_LIVE_CYCLES,
    MIN_LIVE_ORDERS,
    LiveValidationReport,
    validate_live_run,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.validation import TrailingFailureStreaks
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from ..conftest import insert_decision_attempts

_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
_D = Decimal


def _init_live_run(
    db: Database,
    *,
    mode: str = "testnet_live",
    run_id: str = "r",
    schedule_cancel_seconds: object | None = None,
) -> None:
    live: dict[str, object] = {"mode": mode}
    if schedule_cancel_seconds is not None:
        # Genesis stores the ``live:`` block VERBATIM, pre-coercion, so tests that
        # care about the deadline have to be able to seed it the way an operator's
        # YAML would — including the quoted form.
        live["kill_switch"] = {"schedule_cancel_seconds": schedule_cancel_seconds}
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=_D(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps({"live": live}),
        created_at=_T0,
    )


def _pass_all_smoke(db: Database, *, run_id: str = "r") -> None:
    with db.transaction() as conn:
        for test in smoke.SMOKE_TESTS:
            repo.insert_smoke_test_result(
                conn,
                run_id=run_id,
                test_number=test.number,
                test_key=test.key,
                test_name=test.name,
                status="passed",
                network="testnet",
                executed_at=_T0,
            )


def _add_cycles(db: Database, n: int, *, run_id: str = "r") -> None:
    insert_decision_attempts(db, ["completed"] * n, run_id=run_id, start=_T0, mode="live")


def _add_orders(db: Database, n: int, *, run_id: str = "r") -> None:
    with db.transaction() as conn:
        for i in range(n):
            repo.insert_live_order_attempt(
                conn,
                attempt_id=f"a{i}",
                run_id=run_id,
                action="place",
                symbol="BTC",
                attempt_index=0,
                status="acknowledged",
                cloid_logical=f"smoke_r_BTC_o_p_na_000_entry_{i}",
                cloid_hex=f"0x{i:032x}",
                side="buy",
                qty=_D("0.001"),
                price=_D(60000),
                reduce_only=False,
                order_role="entry",
                requested_at=_T0,
            )


_REFRESH_STEP_S = 30  # the configured refresh cadence


def _add_refreshes(db: Database, refreshed: int, failed: int = 0, *, run_id: str = "r") -> None:
    """A refresh timeline on the real cadence, with ``failed`` outages in it.

    Availability is measured in TIME, not in event counts, so these events have
    to be spaced: writing them all at one instant makes every run look perfectly
    available (zero elapsed, therefore zero outage) and the gate untestable.

    Each failure is followed by the next success one step later, so it is exactly
    one step of outage, and the timeline spans ``(total - 1)`` steps:

        availability = 1 - failed / (total - 1)

    So 100 ok + 1 failed is exactly 99% — the threshold — and each further
    failure costs another whole percent (2026-08-01 lifecycle review).
    """
    # Interleaved so no two failures are adjacent: an adjacent pair would be ONE
    # outage of two steps rather than two outages — a different (also valid)
    # shape that the arithmetic above does not describe.
    events: list[str] = ["kill_switch_refreshed"] * max(0, refreshed - failed)
    for _ in range(failed):
        events.append("kill_switch_refresh_failed")
        if refreshed >= failed:
            events.append("kill_switch_refreshed")
    with db.transaction() as conn:
        for step, event_type in enumerate(events):
            repo.insert_kill_switch_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                timestamp=_T0 + timedelta(seconds=step * _REFRESH_STEP_S),
            )


def _kill_switch_event(
    db, event_type: str, *, off: int = 0, run_id: str = "r", detail: str | None = None
) -> None:
    with db.transaction() as conn:
        repo.insert_kill_switch_event(
            conn,
            run_id=run_id,
            event_type=event_type,
            detail=detail,
            timestamp=_T0 + timedelta(seconds=off),
        )


def _zero_evidence_shortfall(report: LiveValidationReport) -> str:
    """The refresh gate's zero-evidence shortfall, whichever of its two branches fired.

    Selected on text unique to THAT gate, because ``report.shortfalls`` is a
    multi-element list whose smoke entry enumerates all 18 smoke keys — one of
    them ``kill_switch_arm_refresh``. A predicate as loose as ``"refresh" in s``
    therefore lands on the smoke shortfall, and the assertions below it never
    see the string under test: that is how a round-17 test came to pass while
    every mutation of the sentence it named survived. If the gate emits neither
    branch this raises ``StopIteration``, which is the intended failure — the
    wrong branch firing is exactly the regression being watched for
    (2026-08-01 round-18 review).
    """
    return next(
        s
        for s in report.shortfalls
        if "sample floor" in s or "no kill-switch refresh events yet" in s
    )


def _latch_safe_mode(db, *, mode_type: str, reason: str, run_id: str = "r") -> None:
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            run_id,
            safe_mode_type=mode_type,
            safe_mode_reason=reason,
            safe_mode_entered_at=_T0,
        )


def _healthy(tmp_path, *, mode: str = "testnet_live") -> Database:
    """A live run that meets every §20.3 acceptance condition."""
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode=mode)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    return db


# -- happy paths -----------------------------------------------------------


def test_healthy_testnet_run_is_ready(tmp_path):
    db = _healthy(tmp_path)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "testnet_live"
    assert report.live_ready
    assert report.failures == ()
    assert report.shortfalls == ()
    assert report.restart_reconciliation_passed
    assert report.emergency_close_test_passed
    assert report.kill_switch_refresh_success_rate == Decimal(1)
    # The NUMBERS on the go/no-go summary, not only the booleans. The gates read
    # local variables, so these three fields could each be hardcoded to 0 with the
    # whole suite green (mutation-verified, 2026-08-01 round-13 review) — and they
    # are what the operator actually reads before committing real money.
    # Only the assertions with POWER: this fixture seeds no fills and no failed
    # attempts, so `fill_count == 0` / `api_failed_count == 0` would pass against
    # a field hardcoded to 0 — the very mutation being guarded against.
    assert report.live_order_count == 30
    # The EXACT figure, not `> 0`: the fixture is deterministic (100 refreshes on
    # the 30s cadence spans 99 steps), and `> 0` passes against a field hardcoded
    # to 1 — the mutation this assertion exists to stop (2026-08-01 round-14).
    assert report.kill_switch_covered_seconds == Decimal(99 * _REFRESH_STEP_S)
    assert report.kill_switch_outage_seconds == Decimal(0)
    assert report.kill_switch_outage_episodes == 0
    # The three numbers reach the operator's summary, which is the whole reason
    # they were promoted out of the failure string: a run that PASSES at 99.2% was
    # still exposed, and the go/no-go sheet has to say so. Deleting either line
    # left the suite green (2026-08-01 round-14 mutation probe).
    lines = report.summary_lines()
    assert "kill_switch_outage_seconds: 0 across 0 outage(s) of 2970s covered" in lines
    assert "kill_switch_clean_shutdown: no" in lines


def test_order_count_one_short_is_a_shortfall(tmp_path):
    # §20.3 gate boundary at exactly MIN_LIVE_ORDERS − 1: the failing side of
    # the >= comparison (every other test seeds 30) — a flipped comparison or
    # wrong constant would otherwise ship undetected.
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, MIN_LIVE_ORDERS - 1)
    _add_refreshes(db, 100, 0)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("live_order_count" in s for s in report.shortfalls)


def test_mainnet_tiny_healthy_run_is_ready(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "mainnet_tiny"
    assert report.live_ready


def test_mainnet_smoke_booleans_render_na_in_summary(tmp_path):
    # A mainnet_tiny run's live_smoke_tests is empty by design (§21.3 proves smoke
    # on the sibling testnet run); the four *_test_passed lines must render n/a —
    # not "no" — so the report can't be misread as a mainnet smoke failure.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    joined = "\n".join(report.summary_lines())
    assert "emergency_close_test_passed: n/a" in joined
    assert "restart_reconciliation_passed: n/a" in joined
    assert "startup_with_existing_position_test_passed: n/a" in joined
    assert "startup_with_stale_open_order_test_passed: n/a" in joined
    # The underlying field is unchanged — still a plain bool (False with no rows).
    assert report.emergency_close_test_passed is False


def test_testnet_smoke_booleans_render_verdict_in_summary(tmp_path):
    # On a testnet_live run the same lines render the real yes/no verdict.
    db = _healthy(tmp_path)  # testnet, all smoke passed
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    joined = "\n".join(report.summary_lines())
    assert "emergency_close_test_passed: yes" in joined


# -- shortfalls (exit 4) ---------------------------------------------------


def test_short_cycles_is_a_shortfall_not_failure(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, 10)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert report.failures == ()
    assert any("cycle_count" in s for s in report.shortfalls)


def test_invalid_output_cycles_do_not_count_toward_the_gate(tmp_path):
    """Live counts a cycle STRICTLY (only ``completed``), unlike paper. On
    testnet live_order_count backstops the ≥30 gate, but §21.4 carries no order
    count — so under paper's vocabulary 30 consecutive unparseable cycles that
    placed nothing would report live_ready. paper-BTC produced exactly that
    shape (6/6 invalid_output) after a model swap."""
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    insert_decision_attempts(
        db,
        ["completed"] * 20 + ["invalid_output"] * 10,
        run_id="r",
        start=_T0,
        mode="live",
    )
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.cycle_count == 20  # the 10 unparseable ones are NOT cycles
    assert report.invalid_output_count == 10  # but they are reported
    assert not report.live_ready
    assert report.failures == ()
    assert any("cycle_count" in s for s in report.shortfalls)
    # Non-gating, but the operator is told why the count is short.
    assert any("unparseable model output" in w for w in report.warnings)


def test_missing_smoke_is_a_shortfall(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    assert any("not yet run" in s for s in report.shortfalls)
    assert not report.restart_reconciliation_passed


def _supersede_smoke(db, status: str, *, key: str = "stop_loss_create") -> None:
    """Supersede one smoke test with a non-passed result (a later result_id wins)."""
    with db.transaction() as conn:
        repo.insert_smoke_test_result(
            conn,
            run_id="r",
            test_number=8,
            test_key=key,
            test_name="SL create",
            status=status,
            executed_at=_T0,
        )


def test_failed_smoke_is_a_shortfall_not_a_failure(tmp_path):
    # Decision 2026-07-29: a red smoke item is curable by one
    # `live-smoke --only <key>` re-run, so it is "not yet at the gate" (exit 4)
    # — exit 5 stays reserved for the investigate-before-trusting conditions.
    # The triage split survives in the wording: failed = the exchange refused.
    db = _healthy(tmp_path)
    _supersede_smoke(db, "failed")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    assert any(
        "FAILED (exchange refused)" in s and "stop_loss_create" in s for s in report.shortfalls
    )
    assert not report.live_ready


def test_errored_smoke_is_a_shortfall_with_harness_wording(tmp_path):
    # The errored bucket lands in shortfalls too, but names the harness — not
    # the exchange — so the operator fixes code, not config/market state.
    db = _healthy(tmp_path)
    _supersede_smoke(db, "error")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    assert any(
        "ERRORED (harness/code bug" in s and "stop_loss_create" in s for s in report.shortfalls
    )
    assert not report.live_ready


def test_the_rerun_command_obeys_the_only_pairing_rule(tmp_path):
    """The printed remedy must be a command the CLI will actually accept.

    `slice_order_status` alone is REFUSED by validate_only_keys (exit 1), and it is
    the commonest errored key of all — test 4 errors precisely when test 3 did not
    complete. Echoing the red keys verbatim handed the operator a rejected command
    at exactly the moment they needed a working one.
    """
    db = _healthy(tmp_path)
    _supersede_smoke(db, "error", key="slice_order_status")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    remedy = next(s for s in report.shortfalls if "ERRORED" in s)
    assert "--only slice_order_submit slice_order_status" in remedy
    # And the command really is accepted by the guard that used to reject it.
    from contrib.hyperliquid_perp.live.smoke import validate_only_keys

    validate_only_keys(["slice_order_submit", "slice_order_status"])


def test_an_unpaired_red_key_is_printed_as_itself(tmp_path):
    """Control for the pairing test: no unrelated key gets dragged in."""
    db = _healthy(tmp_path)
    _supersede_smoke(db, "failed", key="stop_loss_modify")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    remedy = next(s for s in report.shortfalls if "FAILED" in s or "refused" in s)
    assert "--only stop_loss_modify" in remedy
    assert "slice_order_submit" not in remedy


# -- integrity failures (exit 5) ------------------------------------------


def test_dedupe_error_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="live_fill_ingest",
            case_type="fill_money_drift",
            symbol="BTC",
            exchange_value="tid|1|deadbeef",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.exchange_fill_dedupe_error_count == 1
    assert not report.live_ready
    assert any("dedupe_error" in f for f in report.failures)


def test_orphan_exchange_order_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="startup",
            case_type="orphan_exchange_order",
            symbol="BTC",
            exchange_value="oid-9",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.orphan_exchange_order_count == 1
    assert report.orphan_exchange_order_distinct_count == 1
    assert any("orphan" in f for f in report.failures)


def test_one_flapping_order_does_not_read_as_many_orphan_orders(tmp_path):
    # Issue #84. A `|local_terminal_read_failed` key records one row per
    # unreadable->readable flap of the venue, so one order that flapped all
    # morning fills this count. The gate is unchanged — one row already fails —
    # but the operator reading it goes to the exchange to look for that many
    # orders, and there is one. Both numbers are now reported, off the same
    # rows, and the failure string carries the distinct one.
    db = _healthy(tmp_path)
    # The real shape: each answered read stamps the row `resolved_read_succeeded`
    # (provisional), which is what re-opens the key for the next failed read —
    # without the stamps the dedupe would keep this at one row.
    with db.transaction() as conn:
        for i in range(7):
            repo.insert_exchange_reconciliation_event(
                conn,
                run_id="r",
                trigger="heartbeat",
                case_type="orphan_exchange_order",
                symbol="BTC",
                exchange_value="0xabab|local_terminal_read_failed",
                action_taken=None if i == 6 else "resolved_read_succeeded",
                timestamp=_T0,
            )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.orphan_exchange_order_count == 7
    assert report.orphan_exchange_order_distinct_count == 1
    assert not report.live_ready  # the gate still fails, on the row count
    assert any("7 (want 0) across 1 distinct order(s)" in f for f in report.failures)
    assert "orphan_exchange_order_distinct_count: 1" in report.summary_lines()


def test_one_order_seen_under_two_fault_shapes_is_still_one_order(tmp_path):
    # The count is per CLOID, not per fact key. This case type writes three key
    # shapes for the same order, and two of them arrive from an ordinary
    # sequence: a failed orderStatus read parks the cloid under
    # `|local_terminal_read_failed` (reconcile.py `_maybe_reopen_terminal_order`),
    # and the pass that gets an answer files `|local_terminal`. Counting keys
    # would report 2 orders to go and find; there is 1.
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        for key in ("0xabab|local_terminal_read_failed", "0xabab|local_terminal"):
            repo.insert_exchange_reconciliation_event(
                conn,
                run_id="r",
                trigger="heartbeat",
                case_type="orphan_exchange_order",
                symbol="BTC",
                exchange_value=key,
                timestamp=_T0,
            )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.orphan_exchange_order_count == 2
    assert report.orphan_exchange_order_distinct_count == 1
    assert any("across 1 distinct order(s)" in f for f in report.failures)


def test_distinct_orphan_orders_are_counted_separately(tmp_path):
    # Negative control for the split above: two genuinely different orphaned
    # orders must not collapse into one, or the number would UNDER-state what
    # the operator has to go and find. The bare-cloid key shape (a plain orphan
    # back-fill) is deliberately one of the two.
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        for key, action in (
            ("0xabab|local_terminal_read_failed", "resolved_read_succeeded"),
            ("0xabab|local_terminal_read_failed", None),
            ("0xcdcd", None),
        ):
            repo.insert_exchange_reconciliation_event(
                conn,
                run_id="r",
                trigger="heartbeat",
                case_type="orphan_exchange_order",
                symbol="BTC",
                exchange_value=key,
                action_taken=action,
                timestamp=_T0,
            )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.orphan_exchange_order_count == 3
    assert report.orphan_exchange_order_distinct_count == 2


def test_post_init_rejects_more_orphan_keys_than_orphan_rows():
    # They count the same rows one way or another, so this shape is impossible
    # from the tally — and from a hand-built report it would print a summary
    # telling the operator to find more orders than the audit trail holds.
    with pytest.raises(ValueError, match="exceeds orphan_exchange_order_count"):
        _make_report(orphan_exchange_order_count=1, orphan_exchange_order_distinct_count=2)


def test_post_init_rejects_orphan_rows_with_no_key():
    # Every orphan case the sweep constructs carries a fact key, and therefore
    # an order, so "rows but nothing to look for" is a corrupt read rather than
    # a quiet zero.
    with pytest.raises(ValueError, match="every recorded orphan row belongs to some order"):
        _make_report(orphan_exchange_order_count=3, orphan_exchange_order_distinct_count=0)


def test_position_mismatch_is_a_failure(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="exchange_position_mismatch",
            symbol="BTC",
            exchange_value="0.5",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.local_exchange_position_mismatch_count == 1
    assert not report.live_ready


def _post_live_fill(db: Database, *, run_id: str = "r") -> None:
    """A real, consistent live fill: opens 1 BTC long @ 100 (exchange-basis).

    The replay-mismatch/-raise tests below need a run whose books actually
    moved before they corrupt something — corrupting an untouched, all-zero
    ledger would exercise nothing about the replay arithmetic itself. Mirrors
    ``tests/live/test_fills.py``'s own fill-construction pattern (an
    ``ExchangeFill`` posted through :func:`post_live_fill`), which is also what
    makes ``accounting.replay_within`` take the ``live=True`` fold (§15) this
    file's ``account_replay_mismatch_count`` derivation is documented against.
    """
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id="o1",
            mode="live",
            run_id=run_id,
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=_D("1"),
            status="filled",
            price=_D("100"),
            timestamp=_T0,
        )
    fill = ExchangeFill.parse(
        {
            "coin": "BTC",
            "side": "B",
            "px": "100",
            "sz": "1",
            "closedPnl": "0",
            "crossed": True,
            "oid": 777,
            "time": int(_T0.timestamp() * 1000),
            "tid": 1,
            "fee": "0.05",
            "feeToken": "USDC",
        }
    )
    post_live_fill(db, run_id=run_id, fill=fill, order_id="o1")


def test_corrupted_live_account_state_is_a_counted_replay_mismatch_failure(tmp_path):
    """Mirrors the paper-side ``test_corrupted_account_state_reports_replay_mismatch``:
    corrupt the materialized ledger after a real live fill so replay disagrees
    with it (``account_matches`` goes False, no raise) and confirm the exact
    source arithmetic — ``len(position_mismatches) + (0 if account_matches else 1)``
    — lands as 1, and as a gating failure, never a shortfall or warning. A
    regression that dropped or miscounted this would let corrupted books slip
    a real-money go/no-go gate silently.
    """
    db = _healthy(tmp_path)
    _post_live_fill(db)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE current_account_state SET wallet_balance = '123456' WHERE run_id = 'r'"
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    # No position mismatch (only the ledger was touched) plus the account
    # disagreement: 0 + 1 == 1, exactly the source's derivation.
    assert report.account_replay_mismatch_count == 1
    assert not report.live_ready
    assert any("account_replay_mismatch_count = 1" in f for f in report.failures)
    assert not any("account_replay_mismatch_count" in s for s in report.shortfalls)
    assert not any("account_replay_mismatch_count" in w for w in report.warnings)


def test_replay_raise_on_a_live_run_is_counted_as_one_failure(tmp_path):
    """Mirrors the paper-side ``test_replay_raise_contained_as_counted_integrity_failure``:
    corrupt a fill cell so ``Decimal()`` cannot read it inside
    ``accounting.replay_within``'s live fold. ``validate_live_run`` must contain
    the raise in its try/except and count it as
    ``account_replay_mismatch_count == 1`` (the ``replayed is None`` branch) —
    never let the exception escape uncaught, and never silently report 0.
    """
    db = _healthy(tmp_path)
    _post_live_fill(db)
    with db.transaction() as conn:
        conn.execute("UPDATE fills SET fill_price = 'garbage' WHERE run_id = 'r'")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.account_replay_mismatch_count == 1
    assert not report.live_ready
    assert any(f.startswith("accounting replay raised:") for f in report.failures)
    assert not any("accounting replay raised" in s for s in report.shortfalls)
    assert not any("accounting replay raised" in w for w in report.warnings)


def test_low_refresh_rate_is_a_failure(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 90, 10)  # ten 30s outages in a 99-step timeline
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_success_rate < Decimal("0.99")
    assert any("refresh_success_rate" in f for f in report.failures)


def test_no_refresh_events_is_a_shortfall(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    # no refresh events at all
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_success_rate is None
    assert report.failures == ()
    # WHICH zero-evidence branch fired, not merely that one did: the gate has two
    # (this one, and the "rows exist but live-smoke wrote them" one), and
    # "refresh events" matches both — erasing the daemon-vs-suite distinction the
    # branch exists to draw (2026-08-01 round-18 review).
    # The WHOLE sentence: the tail naming the bar is what tells the operator this
    # is a shortfall and not a 0% failure, and it could drift into nonsense with
    # the suite green (2026-08-01 round-19 mutation probe).
    #
    # The bar is DERIVED, not quoted. Spelling "99%" here pinned the production
    # constant from the wrong side: raising MIN_KILL_SWITCH_REFRESH_RATE left
    # this green while the branch went on telling the operator the old number —
    # which is what this assertion exists to prevent (issue #100).
    assert _zero_evidence_shortfall(report) == (
        f"no kill-switch refresh events yet (need a rate >= {_REFRESH_BAR})"
    )


def test_the_rendered_bar_states_the_bar_it_was_rendered_from():
    """Deriving the expected sentence cannot catch a bad renderer — this can.

    Every branch quoting the bar now interpolates ``_REFRESH_BAR``, and so does
    the assertion above, so a mis-rendered bar would satisfy both. Reading the
    rendered string back is independent of how it was produced: at ``:.0f`` a
    0.995 bar printed as "100%", and the gate would have refused at 99.5% while
    telling the operator it needed a hundred (issue #100).
    """
    assert Decimal(_REFRESH_BAR.rstrip("%")) / 100 == MIN_KILL_SWITCH_REFRESH_RATE


def test_unprotected_window_from_protection_events(tmp_path):
    db = _healthy(tmp_path)
    # SL repair exhausted (onset) then SL placed 12s later (close) = 12s window.
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_exhausted",
            symbol="BTC",
            timestamp=_T0,
        )
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_placed",
            symbol="BTC",
            timestamp=_T0 + timedelta(seconds=12),
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unprotected_position_seconds == Decimal(12)
    assert report.unprotected_window_count == 1
    assert not report.unresolved_unprotected_window
    assert any("unprotected_position_seconds" in f for f in report.failures)


def test_open_unprotected_window_flagged(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_blocked",
            symbol="BTC",
            timestamp=_T0,
        )
    with db:
        # No closing event → window still open, measured to `now`.
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=30))
    assert report.unresolved_unprotected_window
    assert report.unprotected_position_seconds == Decimal(30)


def test_a_blocked_event_with_a_resting_sl_opens_no_window(tmp_path):
    """§17.4 is modify-before-cancel, so a gate-refused MODIFY leaves the
    previous SL on the book at a stale trigger — not unprotected. protection.py
    records that order's id on the event; counting those seconds would fail an
    otherwise healthy 30-cycle run on one kill-switch refresh blip, and §20.3's
    unprotected-seconds verdict is exit 5, non-curable."""
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_blocked",
            symbol="BTC",
            order_id="r:stop_loss:p0",  # the previous SL, still resting
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=30))
    assert report.unprotected_position_seconds == Decimal(0)
    assert report.unprotected_window_count == 0
    assert not report.unresolved_unprotected_window
    assert report.live_ready  # a healthy run is not failed by a stale-band pause


def test_a_blocked_event_without_a_resting_sl_still_opens_a_window(tmp_path):
    """The control for the test above: the SAME event type with no order_id
    means nothing is on the book, which is a genuine unprotected window."""
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="stop_loss_repair_blocked",
            symbol="BTC",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=30))
    assert report.unprotected_position_seconds == Decimal(30)
    assert report.unresolved_unprotected_window
    assert any("unprotected_position_seconds" in f for f in report.failures)


def test_emergency_close_event_warns_but_does_not_gate_testnet(tmp_path):
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_protection_order_event(
            conn,
            run_id="r",
            event_type="emergency_close_triggered",
            symbol="BTC",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.emergency_close_event_count == 1
    # An emergency_close_triggered CLOSES any open unprotected window but is not
    # itself an onset, so with no onset there is no unprotected time and the run
    # stays ready on testnet — the event is a warning, not a gate.
    assert report.live_ready
    assert any("emergency close" in w for w in report.warnings)


# -- mainnet_tiny-specific gate -------------------------------------------


def _breach_daily_loss(db, *, still_active: bool, run_id: str = "r") -> None:
    """Record a §10.3 daily-loss episode; ``still_active`` keeps it unresolved."""
    with db.transaction() as conn:
        repo.insert_safe_mode_event(
            conn,
            run_id=run_id,
            event_type="safe_mode_entered",
            safe_mode_type="recoverable",
            reason="daily_loss",
            timestamp=_T0,
        )
        if still_active:
            repo.upsert_scheduler_state(
                conn,
                run_id,
                safe_mode_type="recoverable",
                safe_mode_reason="daily_loss",
                safe_mode_entered_at=_T0,
            )


def test_mainnet_unresolved_daily_loss_is_a_failure(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    _breach_daily_loss(db, still_active=True)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.daily_loss_breached
    assert report.daily_loss_active
    assert any("daily-loss safe-mode episode" in f for f in report.failures)


def test_mainnet_released_daily_loss_warns_but_does_not_gate(tmp_path):
    """§10.3 is a RECOVERABLE guard that auto-releases at the next UTC midnight.

    Gating on "ever breached" made one ordinary risk event on day 2 pin a real-money
    run at exit 5 permanently — 30 more real cycles under a fresh run-id, with no
    --stamp-case escape — while the sibling reconciliation line beside it was already
    current-state and human-clearable. Decided 2026-07-30: gate on unresolved, warn
    on released. Negative control for the test above.
    """
    db = _healthy(tmp_path, mode="mainnet_tiny")
    _breach_daily_loss(db, still_active=False)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.daily_loss_breached  # still on the record
    assert not report.daily_loss_active
    assert not any("daily-loss" in f for f in report.failures)
    assert any("has since released" in w for w in report.warnings)
    assert report.live_ready


def test_a_daily_loss_absorbed_into_another_episode_still_gates(tmp_path):
    """The current-state trio keeps only the FIRST reason of an episode.

    A daily-loss trigger absorbed into a recoverable episode entered for something
    else lives only in the history rows, so a plain ``safe_mode_reason`` comparison
    would miss it and the mainnet gate would pass over a live cap breach.
    """
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_safe_mode_event(
            conn,
            run_id="r",
            event_type="safe_mode_entered",
            safe_mode_type="recoverable",
            reason="no_market_data",
            timestamp=_T0,
        )
        repo.insert_safe_mode_event(
            conn,
            run_id="r",
            event_type="safe_mode_reason_added",
            safe_mode_type="recoverable",
            reason="daily_loss",
            timestamp=_T0 + timedelta(seconds=30),
        )
        repo.upsert_scheduler_state(
            conn,
            "r",
            safe_mode_type="recoverable",
            safe_mode_reason="no_market_data",  # the FIRST reason, not daily_loss
            safe_mode_entered_at=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.daily_loss_active
    assert any("daily-loss safe-mode episode" in f for f in report.failures)


def test_mainnet_unresolved_reconciliation_is_a_failure(tmp_path):
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="199",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 1
    assert any("unresolved_reconciliation" in f for f in report.failures)


def test_unknown_execution_mode_uses_strict_gate(tmp_path):
    db = Database(tmp_path / "live.db")
    # A live run whose genesis config does not name live.mode.
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=_D(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps({"other": 1}),
        created_at=_T0,
    )
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "unknown"
    assert any("does not name a" in f for f in report.failures)


# -- guards ----------------------------------------------------------------


def test_paper_run_is_rejected(tmp_path):
    db = Database(tmp_path / "p.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=_D(1000), schema_version=SCHEMA_VERSION
    )
    with db, pytest.raises(ValueError, match="paper run"):
        validate_live_run(db, run_id="r", now=_T0)


def test_missing_run_is_rejected(tmp_path):
    db = Database(tmp_path / "e.db")
    with db, pytest.raises(ValueError, match="does not exist"):
        validate_live_run(db, run_id="nope", now=_T0)


# -- §21.4 mainnet gate does NOT depend on same-run smoke (decided 2026-07-27) --


def test_mainnet_without_smoke_is_ready(tmp_path):
    # The production mainnet_tiny flow (new run-id, testnet smoke passed on the
    # separate testnet run per §21.3) has an EMPTY live_smoke_tests table — it
    # must still reach live_ready; gating it on smoke would make §21.4 unreachable.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    # deliberately NO _pass_all_smoke
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.execution_mode == "mainnet_tiny"
    assert report.live_ready
    # The *_test_passed booleans are False (no smoke) but do not gate mainnet.
    assert not report.restart_reconciliation_passed


def test_testnet_without_smoke_is_a_shortfall(tmp_path):
    # The mirror of the above: on testnet the same empty table IS a shortfall.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="testnet_live")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("not yet run" in s for s in report.shortfalls)


# -- unprotected-window reconstruction ------------------------------------


def _protect(db, events, *, run_id="r"):
    """events: list of (event_type, symbol, offset_seconds_from_T0[, order_id]).

    The optional 4th element is the protection order the event is about;
    ``stop_loss_repair_blocked`` is the one event whose meaning turns on it
    (present ⇒ a COVERING stop-loss was still resting), so the covering/
    non-covering pairs below need to set it per event.
    """
    with db.transaction() as conn:
        for event_type, symbol, off, *rest in events:
            repo.insert_protection_order_event(
                conn,
                run_id=run_id,
                event_type=event_type,
                symbol=symbol,
                order_id=rest[0] if rest else None,
                timestamp=_T0 + timedelta(seconds=off),
            )


def test_reonset_while_open_keeps_earliest(tmp_path):
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_repair_blocked", "BTC", 5),  # re-onset while open — ignored
            ("stop_loss_placed", "BTC", 20),  # close → window measured from t0
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(20)
    assert r.unprotected_window_count == 1


def test_two_symbols_concurrent_windows_sum(tmp_path):
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_placed", "BTC", 10),
            ("stop_loss_placed", "ETH", 30),
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(40)
    assert r.unprotected_window_count == 2


def test_close_without_open_is_a_noop(tmp_path):
    db = _healthy(tmp_path)
    _protect(db, [("stop_loss_placed", "BTC", 10)])  # close, no prior onset
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == 0
    assert r.unprotected_window_count == 0
    assert r.live_ready


def test_out_of_order_timestamps_clamp_per_window(tmp_path):
    # A BTC close BEFORE its onset (corrupt/skew) clamps that window to 0 and must
    # NOT subtract from ETH's genuine 15s (the per-window clamp, not global).
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 10),
            ("stop_loss_placed", "BTC", 5),  # 5s BEFORE onset → -5, clamped to 0
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_placed", "ETH", 15),
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == Decimal(15)
    assert r.unprotected_window_count == 2
    # The clamped BTC window is STILL a real unprotected window: the count gates
    # even when the seconds clamp away, so this run cannot read as healthy.
    assert not r.live_ready
    assert any("2 window(s)" in f for f in r.failures)


def test_the_production_emergency_close_shape_measures_the_real_seconds(tmp_path):
    """The §17.2 escalation shape the engine actually emits, end to end.

    protection.sync stamps ``stop_loss_repair_exhausted`` from its OWN clock read;
    engine.tick then hands ``_emergency_close`` the EARLIER tick-start instant, so
    the ``emergency_close_triggered`` row precedes its own onset. sync re-fires the
    close every tick until it lands, so the position stays unprotected the whole
    time. Counting the submission as a close measured this — the severe lane — as 0
    seconds and let a run with no stop loss for 10 minutes report live_ready.
    """
    db = _healthy(tmp_path)
    events = []
    # 10 ticks, 60s apart: exhausted at tick+0.5s, the close stamped at tick+0s.
    for tick in range(10):
        events.append(("stop_loss_repair_exhausted", "BTC", tick * 60 + 0.5))
        events.append(("emergency_close_triggered", "BTC", tick * 60))
    # The close finally lands and the position goes flat 10 minutes in.
    events.append(("degraded_protection_cleared", "BTC", 600))
    _protect(db, events)
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    # One window: opened by the FIRST onset (re-onsets while open keep the
    # earliest), closed only when protection's failure line came down.
    assert r.unprotected_position_seconds == Decimal("599.5")
    assert r.unprotected_window_count == 1
    assert not r.unresolved_unprotected_window
    assert not r.live_ready
    assert any("unprotected_position_seconds = 599.5" in f for f in r.failures)


def test_an_emergency_close_alone_does_not_close_an_unprotected_window(tmp_path):
    """Negative control for removing ``emergency_close_triggered`` from the close set.

    An IOC submission is not an end: sync re-fires it until it lands. With nothing
    else in the log the window must stay OPEN and be measured to ``now``.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("emergency_close_triggered", "BTC", 10),
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=45))
    assert r.unprotected_position_seconds == Decimal(45)
    assert r.unresolved_unprotected_window
    assert not r.live_ready


def test_degraded_protection_cleared_closes_a_flat_window_with_nothing_to_cancel(tmp_path):
    """The opposite defect: a window that ended must not fail the run forever.

    ``protection._clear`` emits ``protection_cleared`` only ``if cleared_any`` — so a
    blocked window whose SL FIRED (the order is filled, not active) and flattened
    the position produces no ``protection_cleared`` row. Keying the close set on that
    name alone left the window open to ``now``, i.e. a permanent, uncurable exit 5 on
    a run that is flat and healthy. ``degraded_protection_cleared`` is emitted on that
    same flat tick (``was_failed`` is true, the onset set the failure line), so it
    closes the window.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_blocked", "BTC", 0),  # no order_id ⇒ no covering SL
            ("degraded_protection_cleared", "BTC", 30),  # flat, nothing left to cancel
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0 + timedelta(hours=9))
    assert r.unprotected_position_seconds == Decimal(30)
    assert not r.unresolved_unprotected_window  # NOT measured to `now`, 9h later
    assert r.unprotected_window_count == 1


def test_a_zero_second_window_still_fails_the_gate(tmp_path):
    """A window whose onset and close share one instant is still a real window.

    Keying the gate on seconds alone made this byte-identical to a run that was
    never unprotected. Paired with the control below, which has no onset at all.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_placed", "BTC", 0),  # same instant → 0s
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == 0
    assert r.unprotected_window_count == 1
    assert not r.live_ready
    # The line must not read as the self-contradictory "= 0 ... (want 0)".
    assert any("measured 0" in f and "the window is real" in f for f in r.failures)


def test_a_run_with_no_onset_at_all_is_the_only_clean_zero(tmp_path):
    """Control for the test above: zero WINDOWS, not merely zero seconds."""
    db = _healthy(tmp_path)
    _protect(db, [("stop_loss_placed", "BTC", 0), ("take_profit_placed", "BTC", 1)])
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.unprotected_position_seconds == 0
    assert r.unprotected_window_count == 0
    assert r.live_ready


def test_a_covering_blocked_event_closes_an_open_unprotected_window(tmp_path):
    """A covering ``stop_loss_repair_blocked`` ENDS an open window, not just suppresses.

    protection.py stamps an ``order_id`` on this event only when the resting SL
    still COVERS the position (right closing side AND ``qty >= position``), so
    the event means "covered again" even though the gate flag itself has not
    resolved. Both onset flavours are shown, keyed only on the order_id: BTC's
    is the same event type with NO id (an SL that covered nothing), ETH's is
    ``stop_loss_repair_exhausted``. Before 2026-07-30 the covering event only
    suppressed a NEW onset and left the open one running to the next unrelated
    close event — here, to ``now`` 9 hours later — turning ~30s of genuinely
    unprotected time into a 9-hour, exit-5, non-curable verdict on a healthy run.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_blocked", "BTC", 0),  # no order_id ⇒ genuine onset
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_repair_blocked", "BTC", 20, "r:stop_loss:BTC"),  # covering → closes
            ("stop_loss_repair_blocked", "ETH", 30, "r:stop_loss:ETH"),  # covering → closes
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0 + timedelta(hours=9))
    # Measured to the covering events (20 + 30), NOT to `now`.
    assert r.unprotected_position_seconds == Decimal(50)
    assert r.unprotected_window_count == 2
    assert not r.unresolved_unprotected_window


def test_a_covering_blocked_event_with_no_open_window_opens_or_closes_nothing(tmp_path):
    """Negative control: the close side must never invent a window of its own.

    Two covering events bracket a genuine window — one BEFORE any onset (must
    not pre-close anything, and must leave the later onset free to open) and
    one AFTER the window already closed (must not be counted a second time).
    Only the real 20s / 1 window survives; a close arm written without the
    "is one actually open" guard would either double-count or crash here.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_blocked", "BTC", 0, "r:stop_loss:BTC"),  # nothing open yet
            ("stop_loss_repair_exhausted", "BTC", 10),  # the genuine onset
            ("stop_loss_placed", "BTC", 30),  # the genuine close → 20s
            ("stop_loss_repair_blocked", "BTC", 40, "r:stop_loss:BTC"),  # already closed
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0 + timedelta(hours=9))
    assert r.unprotected_position_seconds == Decimal(20)
    assert r.unprotected_window_count == 1
    assert not r.unresolved_unprotected_window


def test_a_covering_blocked_event_closes_only_its_own_symbols_window(tmp_path):
    """Per-symbol isolation: BTC becoming covered again says nothing about ETH.

    The close arm pops out of the same per-symbol ``open_at`` map the onset
    side fills, so a symbol-blind close would end ETH's window at BTC's event
    and under-report the very metric §20.3 gates real money on.
    """
    db = _healthy(tmp_path)
    _protect(
        db,
        [
            ("stop_loss_repair_exhausted", "BTC", 0),
            ("stop_loss_repair_exhausted", "ETH", 0),
            ("stop_loss_repair_blocked", "BTC", 20, "r:stop_loss:BTC"),  # BTC only
            ("stop_loss_placed", "ETH", 50),  # ETH runs its own full length
        ],
    )
    with db:
        r = validate_live_run(db, run_id="r", now=_T0 + timedelta(hours=9))
    assert r.unprotected_position_seconds == Decimal(70)  # 20 + 50, not 20 + 20
    assert r.unprotected_window_count == 2
    assert not r.unresolved_unprotected_window


# -- kill-switch refresh-rate boundary ------------------------------------


def test_refresh_rate_exactly_99_percent_passes(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    # One 30s outage across a 100-step timeline = exactly 99% available.
    _add_refreshes(db, 100, 1)
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert r.kill_switch_refresh_success_rate == Decimal(99) / Decimal(100)
    assert r.live_ready


def test_refresh_rate_just_under_99_percent_fails(tmp_path):
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 2)  # two outages: 1 - 2/101 < 0.99
    with db:
        r = validate_live_run(db, run_id="r", now=_T0)
    assert not r.live_ready
    assert any("refresh_success_rate" in f for f in r.failures)


# -- LiveValidationReport.__post_init__ invariants ------------------------


# -- the switch actually FIRED ---------------------------------------------


@pytest.mark.parametrize("mode", ["testnet_live", "mainnet_tiny"])
def test_a_fired_kill_switch_fails_the_run_in_both_profiles(tmp_path, mode):
    # The hole this closes: a firing means the exchange cancelled every order on
    # the wallet, SL/TP included, and the engine kept placing against a book it
    # believed was still there. The refresh AVAILABILITY cannot express it — the
    # outage that lets a deadline lapse is a few minutes against a multi-day
    # covered window, well inside the 99% bar. Before this gate a run that
    # demonstrably fired its dead man's switch reported live_ready / exit 0.
    db = _healthy(tmp_path, mode=mode)
    _add_refreshes(db, 0, 4)  # the outage that spanned the deadline
    _kill_switch_event(db, "kill_switch_cancel_triggered")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_fired_count == 1
    # Availability alone sails straight through here — each failure is closed by
    # the refresh at the same instant, so the outage time is ~0 — which is the
    # point: the fired count is what gates, not the rate.
    assert report.kill_switch_refresh_success_rate > Decimal("0.99")
    assert any("kill_switch_fired_count = 1" in f for f in report.failures)
    assert not report.live_ready


def test_a_healthy_run_reports_a_zero_fired_count(tmp_path):
    db = _healthy(tmp_path)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_fired_count == 0
    assert report.live_ready


def test_a_disarm_failure_warns_without_gating(tmp_path):
    # The run is over by then, so it is not a verdict — but §18.2's
    # keep_protective shutdown leaves SL/TP resting and an armed trigger cancels
    # exactly those at its deadline, so it can never be silent either.
    db = _healthy(tmp_path)
    _kill_switch_event(db, "kill_switch_disarm_failed")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_disarm_failed_count == 1
    assert any("disarm failure" in w for w in report.warnings)
    assert report.live_ready  # non-gating


# -- a run latched out of trading ------------------------------------------


def test_a_manually_latched_run_is_not_live_ready(tmp_path):
    # §10.4's three-consecutive-loss guard latches MANUAL safe mode ("a human
    # must confirm") leaving no reconciliation case and no protection event —
    # this scheduler_state row is its only durable trace. Meanwhile §13.1 keeps
    # decision cycles running, so cycle_count and live_order_count climb right
    # past their gates on a run that cannot place a new order.
    db = _healthy(tmp_path)
    _latch_safe_mode(db, mode_type="manual", reason="consecutive_loss")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.safe_mode_active_type == "manual"
    assert report.safe_mode_active_reason == "consecutive_loss"
    assert any("MANUAL safe mode" in f and "consecutive_loss" in f for f in report.failures)
    assert not report.live_ready


def test_a_recoverable_episode_is_not_the_manual_gate(tmp_path):
    # Recoverable episodes release themselves via §13.4; only the manual latch
    # (which needs a human) is a verdict. The daily-loss lane keeps its own,
    # separately-decided treatment.
    db = _healthy(tmp_path)
    _latch_safe_mode(db, mode_type="recoverable", reason="ws_disconnect")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.safe_mode_active_type == "recoverable"
    assert not any("MANUAL safe mode" in f for f in report.failures)


def test_a_log_that_stops_mid_outage_is_flagged_and_does_not_drift(tmp_path):
    """No closing row: the lapse cannot be MEASURED, so it is reported instead.

    The window ends at the last event, never at ``now``. Charging the stretch to
    ``now`` would measure when the operator got round to running ``validate``, and
    the verdict would then get worse the longer they waited — the mirror of the
    drift-toward-pass this rule was written to kill. What is left is the flag.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    last = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "kill_switch_refresh_failed", off=last + _REFRESH_STEP_S)
    with db:
        soon = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=last + 150))
        two_days_later = validate_live_run(db, run_id="r", now=_T0 + timedelta(days=2))
    assert soon.kill_switch_ended_without_clean_shutdown
    # Identical whenever it is asked. This is the property, not an incidental.
    assert two_days_later.kill_switch_outage_seconds == soon.kill_switch_outage_seconds
    assert two_days_later.kill_switch_refresh_success_rate == (
        soon.kill_switch_refresh_success_rate
    )
    assert two_days_later.live_ready == soon.live_ready


def test_the_verdict_does_not_move_with_the_clock(tmp_path):
    """Re-running ``validate`` later must not change what the run scored.

    The window ends at the last EVENT, so the stretch from there to ``now``
    measures when the operator got round to asking, not anything about the run.
    Renamed 2026-08-01: it used to be called ...charged_to_neither_side, after a
    silence-means-downtime rule that has since been deleted — silence longer than
    the cover is now an outage, because a dead process and a fired switch leave
    the same silence. What this pins is the drift-freedom, which still holds.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 1)  # exactly 99%: passes

    # ``now`` is the last event's instant: 100 steps of coverage, one of outage.
    last_event = 100 * _REFRESH_STEP_S
    with db:
        at_stop = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=last_event))
        # Two days later, same database, no new evidence: the same verdict.
        later = validate_live_run(db, run_id="r", now=_T0 + timedelta(days=2))
    assert at_stop.kill_switch_refresh_success_rate == Decimal(99) / Decimal(100)
    assert at_stop.live_ready
    assert later.kill_switch_refresh_success_rate == at_stop.kill_switch_refresh_success_rate
    assert later.live_ready == at_stop.live_ready


def test_a_planned_stop_and_restart_is_not_charged_as_exposure(tmp_path):
    """A clean shutdown RELEASES the cover, so the downtime after it is not a lapse.

    shutdown() clears the wallet-wide trigger, so there is nothing left to fire and
    an operator who stops the daemon overnight has exposed nothing. Without this
    exemption the deadline rule would fail every planned restart.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    stopped_at = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "shutdown_cancel_orders_started", off=stopped_at + 20)
    _kill_switch_event(db, "shutdown_cancel_orders_completed", off=stopped_at + 25)
    # The row that actually proves the trigger is cleared — written AFTER
    # clear_scheduled_cancel() returns. The two rows above bracket the order
    # sweep, which runs while the switch is still armed.
    _kill_switch_event(db, "kill_switch_disarmed", off=stopped_at + 30)
    six_hours = stopped_at + 30 + 6 * 3600
    _kill_switch_event(db, "kill_switch_armed", off=six_hours)
    _kill_switch_event(db, "kill_switch_refreshed", off=six_hours + 30)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=six_hours + 60))
    assert report.kill_switch_outage_seconds == Decimal(0)
    assert report.live_ready


def test_a_kill_between_the_sweep_rows_is_not_granted_the_exemption(tmp_path):
    """``shutdown_cancel_orders_started`` does NOT mean the cover was released.

    Both ``shutdown_cancel_orders_*`` rows bracket the ORDER SWEEP, which runs
    before ``clear_scheduled_cancel()``; at both of them the wallet-wide trigger
    is still armed. Naming them as clean endings handed the exemption to exactly
    the case it must never cover — a process killed mid-sweep, with the switch
    armed and about to fire (2026-08-01 round-13 incremental review).
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    killed_at = 99 * _REFRESH_STEP_S + 30
    _kill_switch_event(db, "shutdown_cancel_orders_started", off=killed_at)
    # ...SIGKILL here. Six hours later someone restarts it.
    back = killed_at + 6 * 3600
    _kill_switch_event(db, "kill_switch_armed", off=back)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=back + 60))
    assert report.kill_switch_outage_seconds >= Decimal(6 * 3600)
    assert not report.live_ready


def test_a_deliberate_stop_cannot_dilute_a_failing_run_into_a_pass(tmp_path):
    """Exempt downtime leaves BOTH terms, not just the numerator.

    Crediting it to ``covered`` alone made downtime a laundering device: a run
    sitting below the bar could be stopped for an hour and restarted, and the
    borrowed denominator carried it over 99% with its exposure unchanged.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    # Two failures -> a real outage that puts the run under the 99% bar.
    base = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "kill_switch_refresh_failed", off=base + 30)
    _kill_switch_event(db, "kill_switch_refreshed", off=base + 90)
    with db:
        before = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=base + 120))
        assert not before.live_ready  # genuinely failing
        # Now stop cleanly and restart an hour later. Nothing about the exposure
        # above changed, so nothing about the verdict may change either.
        _kill_switch_event(db, "kill_switch_disarmed", off=base + 120)
        _kill_switch_event(db, "kill_switch_armed", off=base + 120 + 3600)
        after = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=base + 3800))
    # The hour of chosen downtime is absent from the denominator. (The +30s is
    # the real stretch between the last refresh and the shutdown row, which the
    # switch WAS covering.) Credit the hour and covered becomes 6660s, the rate
    # 99.10%, and this failing run certifies.
    assert after.kill_switch_covered_seconds == before.kill_switch_covered_seconds + 30
    assert after.kill_switch_outage_seconds == before.kill_switch_outage_seconds
    assert not after.live_ready


def test_a_run_that_ends_inside_an_outage_is_not_certified(tmp_path):
    """An unclosed outage at EOF must not read as a clean 100%.

    The window ends at the last event, so an outage still open contributes zero
    seconds — which made the run that failed and NEVER recovered score better
    than the same run recovering two minutes later.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    _kill_switch_event(db, "kill_switch_refresh_failed", off=99 * _REFRESH_STEP_S + 30)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=4000))
    # REPORTED, not gated: an in-flight run's log also ends wherever validate read
    # it, so gating here failed a healthy daemon that blipped 15s ago while the
    # same run 30s later — with MORE measured exposure — passed. The flag informs;
    # it does not vote (2026-08-01 round-13 exit check).
    assert report.kill_switch_ended_in_outage
    assert not any("ends INSIDE an outage" in s for s in report.shortfalls)
    assert "kill_switch_ended_in_outage: yes" in report.summary_lines()


def test_a_clean_shutdown_after_a_blip_does_not_report_an_open_outage(tmp_path):
    """``kill_switch_disarmed`` is positive wire evidence, so it closes an outage.

    Otherwise a run stopped cleanly right after a network blip asserted both
    "clean shutdown: yes" and "ended inside an outage: yes", and the operator's
    only way to clear it was to restart the daemon so an ``armed`` row landed.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    base = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "kill_switch_refresh_failed", off=base + 30)
    _kill_switch_event(db, "shutdown_cancel_orders_started", off=base + 40)
    _kill_switch_event(db, "shutdown_cancel_orders_completed", off=base + 45)
    _kill_switch_event(db, "kill_switch_disarmed", off=base + 50)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=base + 200))
    assert not report.kill_switch_ended_without_clean_shutdown
    assert not report.kill_switch_ended_in_outage
    assert report.live_ready


def test_a_crash_that_outlived_its_deadline_is_charged_on_restart(tmp_path):
    """The case the old silence-means-downtime rule threw away.

    No shutdown row: the process was killed with a schedule standing. 120s later
    the exchange cancelled every order on the wallet — SL and TP included — and it
    stayed that way for six hours. The restart's own ``kill_switch_armed`` is what
    finally bounds the window, which is why the measure has to charge a gap it did
    not see either end of being written.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    died_at = 99 * _REFRESH_STEP_S
    six_hours = died_at + 6 * 3600
    _kill_switch_event(db, "kill_switch_armed", off=six_hours)
    _kill_switch_event(db, "kill_switch_refreshed", off=six_hours + 30)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=six_hours + 60))
    assert report.kill_switch_outage_seconds >= Decimal(6 * 3600)
    assert report.kill_switch_outage_episodes == 1
    assert report.kill_switch_refresh_success_rate < Decimal("0.99")
    assert not report.live_ready


def test_armed_alone_closes_an_outage_inside_the_deadline(tmp_path):
    """Isolates the ``kill_switch_armed`` branch, which nothing else reaches.

    The previous restart test put six hours between the failure and the restart,
    so the downtime rule closed the outage on its own and ``_KILL_SWITCH_ARMED``
    could be deleted from the success set with the suite still green (proved by
    mutation, 2026-08-01). Here the restart lands INSIDE the 120s deadline, so the
    armed row is the only evidence that can close the window.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    failed_at = 99 * _REFRESH_STEP_S + 30
    _kill_switch_event(db, "kill_switch_refresh_failed", off=failed_at)
    # 60s later — well inside the 120s cover, so no lapse, and the ONLY row that
    # can end the outage is the restart's armed.
    _kill_switch_event(db, "kill_switch_armed", off=failed_at + 60)
    _kill_switch_event(db, "kill_switch_refreshed", off=failed_at + 90)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=failed_at + 120))
    # Exactly the 60s between the failure and the armed row, and nothing after it.
    # This is the whole assertion: drop _KILL_SWITCH_ARMED from the success set and
    # the window runs on to the refreshed row at +90 instead, so the number moves.
    assert report.kill_switch_outage_seconds == Decimal(60)
    assert report.kill_switch_outage_episodes == 1


def test_the_deadline_is_taken_from_the_arming_not_from_genesis(tmp_path):
    """A resumed run is judged by the cover it ACTUALLY had, not the one it was born with.

    ``runs.config_json`` is written once, at ``--create``, and a resume that edits
    ``live.kill_switch`` is only a printed WARNING. Sizing every stretch from
    genesis therefore measured a run armed at 120s against a 600s deadline: every
    real lapse in the 121-600s band — stretches in which the exchange has already
    swept the wallet — billed as covered, and a genuinely exposed run reporting
    live_ready (2026-08-01 round-14 review).
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db, schedule_cancel_seconds="600")  # quoted, as the YAML allows
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    resumed_at = 99 * _REFRESH_STEP_S + 30
    # The resume arms at 120s. From here on THAT is the cover, whatever genesis says.
    _kill_switch_event(db, "kill_switch_armed", off=resumed_at, detail="deadline=120s refresh=30s")
    # 300s of silence: comfortably inside genesis's 600s, far outside the 120s the
    # run was actually armed with.
    _kill_switch_event(db, "kill_switch_refreshed", off=resumed_at + 300)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=resumed_at + 400))
    assert report.kill_switch_outage_seconds == Decimal(300)
    assert report.kill_switch_outage_episodes == 1


def test_the_genesis_deadline_sizes_the_cover_until_something_arms(tmp_path):
    """The other half: genesis is the STARTING deadline and it must really be read.

    Same 300s silence, no arming row to override it — so a run created with a 600s
    deadline is covered through it and charged nothing. Pin both directions or the
    per-run read can be replaced by the constant with the suite green.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db, schedule_cancel_seconds="600")
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    quiet_at = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "kill_switch_refreshed", off=quiet_at + 300)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=quiet_at + 400))
    assert report.kill_switch_outage_seconds == Decimal(0)
    assert report.kill_switch_outage_episodes == 0
    # ...and the SAME timeline under the 120s default is charged in full, which is
    # what proves the genesis value did the work rather than the gap being benign.
    other = Database(tmp_path / "default.db")
    _init_live_run(other)
    _pass_all_smoke(other)
    _add_cycles(other, MIN_LIVE_CYCLES)
    _add_orders(other, 30)
    _add_refreshes(other, 100, 0)
    _kill_switch_event(other, "kill_switch_refreshed", off=quiet_at + 300)
    with other:
        strict = validate_live_run(other, run_id="r", now=_T0 + timedelta(seconds=quiet_at + 400))
    assert strict.kill_switch_outage_seconds == Decimal(300)


def test_only_a_row_that_installs_cover_may_state_the_deadline(tmp_path):
    """A row that changed nothing on the exchange cannot re-size the protection.

    ``shutdown_cancel_orders_completed`` dumps arbitrary JSON into the same
    ``detail`` column, so "any row that states a deadline" would let a cloid that
    happened to carry the token decide how long the wallet was covered. Nothing
    pinned the narrowing (2026-08-01 round-15 review).
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    quiet_at = 99 * _REFRESH_STEP_S
    # A non-installing row loudly claiming a 900s cover, then a 300s silence.
    _kill_switch_event(
        db, "shutdown_cancel_orders_completed", off=quiet_at, detail='{"c": "deadline=900s"}'
    )
    _kill_switch_event(db, "kill_switch_refreshed", off=quiet_at + 300)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=quiet_at + 400))
    # Still judged against the 120s default: believing the claim would score 0.
    assert report.kill_switch_outage_seconds == Decimal(300)


def test_suite_authored_refreshes_are_cover_but_not_sample_credit(tmp_path):
    """live-smoke's rows count for exposure and for the deadline, never for the floor.

    A few back-to-back suites clear the floor at 100% with the daemon never
    started, so counting them would let the availability figure describe the smoke
    phase rather than the thing §20.3 certifies for real money (user decision,
    2026-08-01 round-15). This docstring used to name a six-suite total of 114
    while the body below writes 120 rows and asserts on that — a summary that did
    not match its own test (issue #100). The concrete figure is quoted once, in
    RUNBOOK §20.3, pinned to ``smoke.REFRESHES_PER_FULL_SUITE``.
    """
    from contrib.hyperliquid_perp.live.kill_switch import (
        _stamp_suite_authored,
        deadline_detail,
    )

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    for step in range(MIN_KILL_SWITCH_REFRESH_SAMPLES + 20):
        _kill_switch_event(
            db,
            "kill_switch_refreshed",
            off=step * _REFRESH_STEP_S,
            detail=_stamp_suite_authored(deadline_detail(120, "(smoke pre-flight refresh)")),
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    # Plenty of rows, none of them evidence that the DAEMON exercised the switch.
    assert report.kill_switch_refresh_total == 0
    assert not report.live_ready
    # But they DID hold the cover: 30s steps under a 120s deadline, no outage.
    assert report.kill_switch_outage_seconds == Decimal(0)
    assert report.kill_switch_covered_seconds > Decimal(0)
    # The shortfall must not claim there were no refreshes — there are 120 in the
    # table. Saying "no refresh events yet" beside 120 rows and real covered
    # seconds is a report the operator cannot reconcile (2026-08-01 round-16).
    shortfall = _zero_evidence_shortfall(report)
    assert "no kill-switch refresh events yet" not in shortfall
    assert "120" in shortfall and "live-smoke" in shortfall


def test_the_marker_is_a_token_not_merely_the_presence_of_a_detail(tmp_path):
    """A DAEMON refresh that states its deadline still earns sample credit.

    Every non-suite writer of a refresh row passes ``detail=None`` today, which
    made ``is_suite_authored`` behaviourally identical to ``detail is not None``
    — and to a free-text sniff for ``"(smoke "``, the exact thing its own comment
    rejects. All three passed the full suite (2026-08-01 round-16 mutation probe).
    The distinguishing row is the one no test had: a refresh carrying a detail
    that is NOT the suite's. ``_kill_switch_tally``'s own rule invites exactly
    that row, since refreshed rows may state the deadline they installed.
    """
    from contrib.hyperliquid_perp.live.kill_switch import deadline_detail

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    for step in range(MIN_KILL_SWITCH_REFRESH_SAMPLES):
        _kill_switch_event(
            db,
            "kill_switch_refreshed",
            off=step * _REFRESH_STEP_S,
            detail=deadline_detail(120, "refresh=30s"),
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_total == MIN_KILL_SWITCH_REFRESH_SAMPLES


def test_a_suite_authored_failure_opens_an_outage_without_buying_credit(tmp_path):
    """The other half of the split, and the half that was unreachable dead code.

    The only suite writer of ``kill_switch_refresh_failed`` passed ``error=``,
    which lands in ``error_message``; the tally reads ``detail``. So
    ``is_suite_authored`` was always False on failure rows and the exclusion
    branch could never fire — while the RUNBOOK claimed it did
    (2026-08-01 round-16 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _kill_switch_event(db, "kill_switch_armed", off=0, detail=_stamp_suite_authored(None))
    _kill_switch_event(db, "kill_switch_refresh_failed", off=30, detail=_stamp_suite_authored(None))
    # 300s later — past the 120s cover — the suite gets a refresh through.
    _kill_switch_event(db, "kill_switch_refreshed", off=330, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=400))
    # No sample credit from either the failure or the recovery...
    assert report.kill_switch_refresh_total == 0
    # ...but the lapse is charged in full: the wallet really was uncovered.
    assert report.kill_switch_outage_seconds == Decimal(300)
    assert report.kill_switch_outage_episodes == 1


def test_a_smoke_disarm_cannot_make_a_killed_daemon_look_clean(tmp_path):
    """``clean_shutdown`` reports on the DAEMON, so a suite's disarm cannot end it.

    The flag reads the last row against ``_KILL_SWITCH_CLEAN_ENDINGS``. Run the
    daemon, SIGKILL it, then re-run ``live-smoke`` on the same run-id: the suite's
    exit disarm becomes the last row and the report said "clean shutdown: yes" for
    a run whose daemon was killed (2026-08-01 round-16 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_refreshes(db, 100, 0)
    killed_at = 99 * _REFRESH_STEP_S
    # The operator re-runs live-smoke afterwards; it disarms cleanly on exit.
    _kill_switch_event(
        db, "kill_switch_disarmed", off=killed_at + 3600, detail=_stamp_suite_authored(None)
    )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=killed_at + 3700))
    assert report.kill_switch_ended_without_clean_shutdown
    assert "kill_switch_clean_shutdown: no" in report.summary_lines()


def test_a_smoke_rerun_after_a_clean_shutdown_stays_clean(tmp_path):
    """The flag reports on the DAEMON, so a later suite cannot re-open its verdict.

    Reading the run's LAST row rather than its last daemon row condemned two runs
    whose daemon stopped cleanly: this one — the CLI itself tells the operator to
    re-run live-smoke after code/config changes, on the same run-id — and a
    smoke-only run-id (2026-08-01 round-17 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_refreshes(db, 100, 0)
    stopped_at = 99 * _REFRESH_STEP_S
    _kill_switch_event(db, "kill_switch_disarmed", off=stopped_at + 30)  # the daemon's own
    _kill_switch_event(
        db, "kill_switch_armed", off=stopped_at + 3600, detail=_stamp_suite_authored(None)
    )
    _kill_switch_event(
        db, "kill_switch_disarmed", off=stopped_at + 3900, detail=_stamp_suite_authored(None)
    )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=stopped_at + 4000))
    assert report.kill_switch_ended_without_clean_shutdown is False
    assert "kill_switch_clean_shutdown: yes" in report.summary_lines()


def test_a_run_with_no_daemon_rows_says_nothing_about_clean_shutdown(tmp_path):
    # A smoke-only run-id has no daemon to report on. Answering "yes" would claim
    # a clean stop that never happened; "no" sends the operator hunting a kill
    # that never happened either.
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    for step in range(4):
        _kill_switch_event(
            db,
            "kill_switch_refreshed",
            off=step * _REFRESH_STEP_S,
            detail=_stamp_suite_authored("deadline=120s (smoke pre-flight refresh)"),
        )
    _kill_switch_event(db, "kill_switch_disarmed", off=200, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=300))
    assert report.kill_switch_ended_without_clean_shutdown is None
    assert "kill_switch_clean_shutdown: n/a (no daemon rows)" in report.summary_lines()
    # The rate line must name the DAEMON too — four refreshes are in the table.
    assert "kill_switch_refresh_success_rate: n/a (no daemon refresh yet)" in report.summary_lines()


def test_a_suite_whose_refreshes_all_failed_is_still_reported_honestly(tmp_path):
    # suite_refreshed alone could not describe this run: every suite row was a
    # FAILURE, so the "suite rows exist" branch never fired and the shortfall fell
    # back to "no kill-switch refresh events yet" beside real uncovered seconds
    # (2026-08-01 round-17 review).
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _kill_switch_event(db, "kill_switch_armed", off=0, detail=_stamp_suite_authored(None))
    for step in range(3):
        _kill_switch_event(
            db,
            "kill_switch_refresh_failed",
            off=200 * (step + 1),
            detail=_stamp_suite_authored(None),
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=700))
    assert report.kill_switch_refresh_total == 0
    assert report.kill_switch_outage_seconds > Decimal(0)
    # Was `next(s for s in report.shortfalls if "refresh" in s)`, which selected
    # the SMOKE shortfall (it lists the key ``kill_switch_arm_refresh``) — so
    # both assertions passed on an unrelated string and every mutation of the
    # sentence under test survived, including deleting the ``suite_failed`` term
    # this test exists for (2026-08-01 round-18 mutation probe).
    shortfall = _zero_evidence_shortfall(report)
    assert "no kill-switch refresh events yet" not in shortfall
    assert "live-smoke" in shortfall
    # The count is the term that was missing: three failed attempts, named.
    assert "3 refresh attempt(s)" in shortfall
    # And the claim the sentence leads with — what is absent, and whose. Only
    # the tail was pinned, so replacing this clause with a placeholder left the
    # suite green (2026-08-01 round-18 probe).
    assert shortfall.startswith("no DAEMON kill-switch refresh events yet")


def test_the_two_suite_counters_are_not_interchangeable(tmp_path):
    """Successes and failures are tallied apart, not just summed.

    They reach the report only through their sum today, so transposing the two
    fields in the positional tally — or inserting a field between them — was
    invisible to the whole suite. They mean opposite things about the wallet
    (2026-08-01 round-18 mutation probe).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored
    from contrib.hyperliquid_perp.live.validation import _kill_switch_tally

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    for step in range(2):
        _kill_switch_event(
            db,
            "kill_switch_refreshed",
            off=30 * step,
            detail=_stamp_suite_authored(None),
        )
    for step in range(3):
        _kill_switch_event(
            db,
            "kill_switch_refresh_failed",
            off=100 + 30 * step,
            detail=_stamp_suite_authored(None),
        )
    with db.transaction() as conn:
        tally = _kill_switch_tally(conn, "r", None)
    assert (tally.suite_refreshed, tally.suite_failed) == (2, 3)
    # And neither bought daemon sample credit.
    assert (tally.refreshed, tally.failed) == (0, 0)


def test_the_two_end_of_run_flags_are_scoped_differently_on_purpose(tmp_path):
    """``clean_shutdown`` reads the daemon's rows; ``ended_in_outage`` reads the run's.

    Nothing pinned the asymmetry, so re-scoping ``ended_in_outage`` to daemon
    rows — the obvious "consistency" edit — left the suite green. Why the two
    are not nested: see ``_KillSwitchTally.ended_in_outage``. Here a daemon dies
    inside an outage and the operator re-runs live-smoke, as the CLI itself
    instructs (2026-08-01 round-18 review; user decision: keep it run-scoped).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    # Deadline wider than the 3600s gap, so the failed refresh is provably what
    # opens this outage rather than the silence branch (2026-08-01 round-20).
    _init_live_run(db, schedule_cancel_seconds=7200)
    _kill_switch_event(db, "kill_switch_armed", off=0)
    _kill_switch_event(db, "kill_switch_refreshed", off=30)
    # The daemon's last word: a failed refresh, then nothing — it was killed.
    _kill_switch_event(db, "kill_switch_refresh_failed", off=60)
    # The operator's re-run, an hour later, on the same run-id.
    _kill_switch_event(db, "kill_switch_armed", off=3660, detail=_stamp_suite_authored(None))
    _kill_switch_event(db, "kill_switch_disarmed", off=3720, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=3800))
    # Daemon-scoped: the suite's clean disarm cannot launder the kill.
    assert report.kill_switch_ended_without_clean_shutdown is True
    # Run-scoped: the suite's arm DID close that outage, so the tail is measured
    # — and the exposure is in the seconds either way, which is the point.
    assert report.kill_switch_ended_in_outage is False
    # EXACT, not >=: the outage runs from the failed refresh to the suite's arm.
    # Under >= an over-charge of the same stretch read as a pass, and the
    # over-charge is the failure mode that moves the §20.3 verdict.
    assert report.kill_switch_outage_seconds == Decimal(3600)


@pytest.mark.parametrize("closing_event", ["kill_switch_armed", "kill_switch_refreshed"])
def test_a_suite_row_closes_a_daemons_open_outage(tmp_path, closing_event):
    """Suite rows are COVER: they end an outage and stop the meter, whoever wrote them.

    The sample floor and the ``clean_shutdown`` daemon verdict exclude them
    (§20.3, user decision round 15; the second consumer since round 17) — the
    outage machinery must not. Nothing pinned that: scoping the outage-closing
    branches to daemon rows, the same "consistency" edit that is right for the
    floor, left all 1992 tests green while moving a measured run from 87% to
    75% availability — and availability is what decides exit 5. The sibling
    asymmetry test cannot see it either: it has BOTH a suite arm and a suite
    disarm, so either one alone still closes the tail
    (2026-08-01 round-19 mutation probe).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    # A deadline WIDER than the gap, so only the failed refresh can open this
    # outage. Under the 120s default the `gap > deadline` silence branch charges
    # the same 600s, and the test would pass with the branch it names deleted
    # (2026-08-01 round-20 review).
    db = Database(tmp_path / "live.db")
    _init_live_run(db, schedule_cancel_seconds=900)
    _kill_switch_event(db, "kill_switch_armed", off=0)
    _kill_switch_event(db, "kill_switch_refresh_failed", off=30)
    _kill_switch_event(db, closing_event, off=630, detail=_stamp_suite_authored(None))
    _kill_switch_event(db, "kill_switch_refreshed", off=660)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=700))
    # 600s from the failed refresh to the suite row, and NOT the 30s after it.
    assert report.kill_switch_outage_seconds == Decimal(600)
    assert report.kill_switch_outage_episodes == 1
    assert report.kill_switch_ended_in_outage is False


def test_a_suite_disarm_closes_a_daemons_open_outage(tmp_path):
    """The third closing row, on its own — a clean ending is positive wire evidence.

    Separate from the two above because ``kill_switch_disarmed`` closes through
    a different branch (``_KILL_SWITCH_CLEAN_ENDINGS``), and removing THAT
    branch alone was also invisible (2026-08-01 round-19 mutation probe).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db, schedule_cancel_seconds=900)
    _kill_switch_event(db, "kill_switch_armed", off=0)
    _kill_switch_event(db, "kill_switch_refresh_failed", off=30)
    _kill_switch_event(db, "kill_switch_disarmed", off=630, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=700))
    assert report.kill_switch_outage_seconds == Decimal(600)
    assert report.kill_switch_ended_in_outage is False


def test_a_suite_run_that_never_recovers_the_switch_still_ends_in_outage(tmp_path):
    """The inverse: rows that are NOT armed/refreshed/disarmed do not close a tail.

    The RUNBOOK said "any row closes it", which is wider than the code — a
    re-run whose pre-flight refresh AND exit disarm both fail leaves the outage
    open, and the operator must still be told (2026-08-01 round-19 review).

    The suite's own ``armed`` row is in the fixture because a real re-run of the
    suite writes one: pre-flight recovery runs whenever the selection contains
    an order-placing test, and it arms a real manager — and tests 14-17 arm
    even without one (14 drives the switch directly, 15-17 each run a real
    §19.1 recovery). Only ``--dry-run``, or an ``--only`` avoiding both sets,
    writes no rows at all.
    Round 19 left it out, so the fixture was not the
    scenario its own docstring named and its episode count was the synthetic
    one — the real shape opens a SECOND episode, which is the number an
    operator will see (2026-08-01 round-20 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db, schedule_cancel_seconds=900)
    _kill_switch_event(db, "kill_switch_armed", off=0)
    _kill_switch_event(db, "kill_switch_refresh_failed", off=30)
    # The re-run's pre-flight recovery arms: this CLOSES the daemon's outage.
    _kill_switch_event(db, "kill_switch_armed", off=630, detail=_stamp_suite_authored(None))
    # Then its own refresh fails and its exit disarm fails: a second outage that
    # nothing closes.
    _kill_switch_event(
        db, "kill_switch_refresh_failed", off=660, detail=_stamp_suite_authored(None)
    )
    _kill_switch_event(db, "kill_switch_disarm_failed", off=690, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=700))
    assert report.kill_switch_ended_in_outage is True
    assert report.kill_switch_outage_episodes == 2
    # 600s for the daemon's, 30s for the suite's own — the second one is still
    # open, and an open outage is charged only up to the last row.
    assert report.kill_switch_outage_seconds == Decimal(630)


def test_every_daemon_only_count_says_that_live_smoke_rows_were_excluded(tmp_path):
    """All three low-evidence branches print a DAEMON-only number, so all three say so.

    Round 18 fixed the zero-evidence branch alone and the branch one line over
    kept the same defect: 121 live-smoke rows plus ten daemon refreshes reported
    "kill_switch_refresh_total = 10" beside a table holding 131 refresh rows — a
    claim the operator checks against the table and finds false, which is what
    round 18 set out to stop (2026-08-01 round-20 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    # Two successes and two FAILURES: both are attempts the operator can see in
    # the table, and counting only the successes here left the note's
    # ``suite_failed`` term free (2026-08-01 round-20 probe).
    for step in range(2):
        _kill_switch_event(
            db, "kill_switch_refreshed", off=step * 30, detail=_stamp_suite_authored(None)
        )
    for step in range(2):
        _kill_switch_event(
            db,
            "kill_switch_refresh_failed",
            off=60 + step * 30,
            detail=_stamp_suite_authored(None),
        )
    # Below the sample floor, so the "too few to judge" branch fires.
    for step in range(10):
        _kill_switch_event(db, "kill_switch_refreshed", off=200 + step * 30)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=600))
    shortfall = next(s for s in report.shortfalls if "too few to judge" in s)
    assert "kill_switch_refresh_total = 10" in shortfall
    assert "4 refresh attempt(s) on record were written during live-smoke" in shortfall


def test_a_passing_runs_summary_still_says_live_smoke_rows_were_excluded(tmp_path):
    """The one daemon-only count printed on EVERY run, including the ones that pass.

    The three shortfall branches disclose the exclusion, but they fire only
    below the floor. A clean run printed ``kill_switch_refresh_total: 150``
    beside a table holding 271 refresh rows with nothing to explain the gap —
    the same unreconcilable number, on the surface every operator reads
    (2026-08-01 round-21 review).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = _healthy(tmp_path)
    # One of the four is a FAILURE: an attempt is an attempt, and counting only
    # the successes left that term of the sum free — an aborted smoke whose
    # refreshes all failed would print the bare daemon count again
    # (2026-08-01 round-22 mutation probe). It recovers on the next second, so
    # this run stays comfortably above the 99% bar rather than resting on it:
    # a fixture 0.03pp from the gate turns any later edit into a confusing
    # ``live_ready`` failure instead of the assertion under test.
    _kill_switch_event(
        db, "kill_switch_refresh_failed", off=-120, detail=_stamp_suite_authored(None)
    )
    for off in (-119, -60, -30):
        _kill_switch_event(db, "kill_switch_refreshed", off=off, detail=_stamp_suite_authored(None))
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.live_ready
    assert report.kill_switch_suite_refresh_attempts == 4
    line = next(s for s in report.summary_lines() if s.startswith("kill_switch_refresh_total:"))
    assert "(+4 during live-smoke, excluded from the sample floor)" in line


def test_a_run_with_no_live_smoke_rows_says_nothing_extra(tmp_path):
    """No suite rows, no clause — the line stays the bare count it always was."""
    db = _healthy(tmp_path)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    line = next(s for s in report.summary_lines() if s.startswith("kill_switch_refresh_total:"))
    assert "live-smoke" not in line


def test_the_single_instant_branch_also_says_the_suite_rows_were_excluded(tmp_path):
    """The third branch, whose number is just as daemon-only as the other two."""
    from contrib.hyperliquid_perp.live.kill_switch import _stamp_suite_authored

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _kill_switch_event(db, "kill_switch_refreshed", off=0, detail=_stamp_suite_authored(None))
    # Enough daemon rows to clear the floor, all at one instant: no denominator.
    for _ in range(MIN_KILL_SWITCH_REFRESH_SAMPLES):
        _kill_switch_event(db, "kill_switch_refreshed", off=0)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=600))
    shortfall = next(s for s in report.shortfalls if "span no elapsed time" in s)
    assert "1 refresh attempt(s) on record were written during live-smoke" in shortfall


def test_the_row_that_carries_exchange_text_is_the_sweeps_and_stays_the_daemons(tmp_path):
    """The one row that can carry arbitrary external text, in its real shape.

    Round 18's version of this test hung the quoting blob on
    ``kill_switch_disarmed`` and claimed a reachable Critical. It is not
    reachable: that event's detail is one of two fixed literals, and the JSON
    blob — the only place raw exchange and SQLite exception text enters this
    column — is written on ``shutdown_cancel_orders_completed``, which is not a
    clean ending. Walk the two shapes a real run can take and the verdict is the
    same under both predicates, so the anchoring is PREVENTIVE hardening of a
    column with no event-type allowlist, not a fix for a live misreading. What
    is pinned here is the reachable half: the sweep row really does carry
    attacker-shaped text, and it must stay in the daemon subsequence
    (2026-08-01 round-19 review; the predicate itself is pinned in
    test_kill_switch.py, where it discriminates).
    """
    from contrib.hyperliquid_perp.live.kill_switch import _SUITE_AUTHORED_TOKEN

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _kill_switch_event(db, "kill_switch_armed", off=0)
    _kill_switch_event(db, "kill_switch_refreshed", off=30)
    _kill_switch_event(db, "shutdown_cancel_orders_started", off=60)
    _kill_switch_event(
        db,
        "shutdown_cancel_orders_completed",
        off=70,
        detail=json.dumps({"failures": [f"cancel rejected near {_SUITE_AUTHORED_TOKEN}"]}),
    )
    _kill_switch_event(db, "kill_switch_disarmed", off=80, detail="clean shutdown sweep")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=90))
    assert report.kill_switch_ended_without_clean_shutdown is False
    assert "kill_switch_clean_shutdown: yes" in report.summary_lines()
    assert report.kill_switch_refresh_total == 1


def test_the_disarm_failure_warning_names_both_producers(tmp_path):
    # Round 15 rewrote this warning because the count now mixes daemon shutdowns
    # and live-smoke exits; replacing the whole text with a placeholder left the
    # suite green (2026-08-01 round-16 mutation probe).
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_refreshes(db, 100, 0)
    _kill_switch_event(db, "kill_switch_disarm_failed", off=99 * _REFRESH_STEP_S + 30)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=3100))
    warning = next(w for w in report.warnings if "disarm failure" in w)
    assert "live-smoke" in warning and "shutdown" in warning
    assert "cumulative" in warning


def test_the_default_deadline_tracks_the_config_layers_own_default():
    """A drift here silently measures every key-omitting run against the wrong cover.

    ``DEFAULT_SCHEDULE_CANCEL_SECONDS`` exists to answer "what armed a run that
    never said?", so it is only correct while it equals what the config layer
    actually arms with. Nothing else binds the two modules (2026-08-01 round-14).
    """
    from contrib.hyperliquid_perp.live.config import KillSwitchConfig
    from contrib.hyperliquid_perp.live.validation import DEFAULT_SCHEDULE_CANCEL_SECONDS

    assert Decimal(KillSwitchConfig().schedule_cancel_seconds) == DEFAULT_SCHEDULE_CANCEL_SECONDS


def test_silence_one_second_either_side_of_the_deadline(tmp_path):
    """The discontinuity the whole rule turns on, pinned on both sides.

    A gap EQUAL to the cover is still covered; one second more is not. The only
    data points were 60s and six hours against a 120s deadline, so `>` could be
    relaxed to `>=` with the suite green (2026-08-01 round-14 mutation probe).
    """
    from contrib.hyperliquid_perp.live.validation import DEFAULT_SCHEDULE_CANCEL_SECONDS

    deadline = int(DEFAULT_SCHEDULE_CANCEL_SECONDS)

    def _outage(gap: int) -> Decimal:
        db = Database(tmp_path / f"gap{gap}.db")
        _init_live_run(db)
        _pass_all_smoke(db)
        _add_cycles(db, MIN_LIVE_CYCLES)
        _add_orders(db, 30)
        _add_refreshes(db, 100, 0)
        quiet_at = 99 * _REFRESH_STEP_S
        _kill_switch_event(db, "kill_switch_refreshed", off=quiet_at + gap)
        with db:
            return validate_live_run(
                db, run_id="r", now=_T0 + timedelta(seconds=quiet_at + gap + 60)
            ).kill_switch_outage_seconds

    assert _outage(deadline) == Decimal(0)
    assert _outage(deadline + 1) == Decimal(deadline + 1)


def test_one_continuous_lapse_is_one_episode_however_it_is_punctuated(tmp_path):
    """Two shapes of the same fact, because each had its own uncovered guard.

    A wedged process that finally writes a failed refresh at the far end of a long
    silence is ONE lapse, not two — the silence branch latches ``in_outage`` for
    exactly that. And two consecutive failed refreshes are one lapse too, which is
    the failed-refresh branch's own dedup guard. Both were deletable with the
    suite green (2026-08-01 round-14 mutation probe).
    """
    db = Database(tmp_path / "wedged.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 100, 0)
    quiet_at = 99 * _REFRESH_STEP_S
    # 600s of silence, then the wedged process manages to say "I failed".
    _kill_switch_event(db, "kill_switch_refresh_failed", off=quiet_at + 600)
    _kill_switch_event(db, "kill_switch_refreshed", off=quiet_at + 660)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=quiet_at + 700))
    assert report.kill_switch_outage_episodes == 1
    assert report.kill_switch_outage_seconds == Decimal(660)

    twice = Database(tmp_path / "twofails.db")
    _init_live_run(twice)
    _pass_all_smoke(twice)
    _add_cycles(twice, MIN_LIVE_CYCLES)
    _add_orders(twice, 30)
    _add_refreshes(twice, 100, 0)
    _kill_switch_event(twice, "kill_switch_refresh_failed", off=quiet_at + 30)
    _kill_switch_event(twice, "kill_switch_refresh_failed", off=quiet_at + 60)
    _kill_switch_event(twice, "kill_switch_refreshed", off=quiet_at + 90)
    with twice:
        report2 = validate_live_run(twice, run_id="r", now=_T0 + timedelta(seconds=quiet_at + 120))
    assert report2.kill_switch_outage_episodes == 1


def test_rows_at_a_single_instant_cannot_demonstrate_availability(tmp_path):
    """Enough rows to judge, no wall time to judge over: a shortfall, not a pass.

    The branch is a fail-open if it goes missing — with the sample floor cleared
    and zero covered time, nothing was appended and ``live_ready`` stayed True.
    The sample-floor branch is ordered first and hid this one from every existing
    test (2026-08-01 round-14 mutation probe).
    """
    from contrib.hyperliquid_perp.live.validation import MIN_KILL_SWITCH_REFRESH_SAMPLES

    db = Database(tmp_path / "instant.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    with db.transaction() as conn:
        for _ in range(MIN_KILL_SWITCH_REFRESH_SAMPLES):
            repo.insert_kill_switch_event(
                conn, run_id="r", event_type="kill_switch_refreshed", timestamp=_T0
            )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.kill_switch_refresh_success_rate is None
    assert any("span no elapsed time" in s for s in report.shortfalls)
    assert not report.live_ready
    # ...and the summary names THAT cause, not "no refresh yet" over a count of 100.
    lines = report.summary_lines()
    assert "kill_switch_refresh_success_rate: n/a (rows span no elapsed time)" in lines
    assert "kill_switch_refresh_total: 100" in lines


# -- refresh-rate sample floor ---------------------------------------------


def test_one_blip_in_a_young_run_is_a_shortfall_not_a_failure(tmp_path):
    # At the default 30s cadence a five-minute-old run has ten samples, so one
    # network hiccup read as 9/10 = 90% and pinned a healthy run at exit 5 —
    # which RUNBOOK §5 answers with "stop and investigate" and §7 with "start a
    # fresh run-id". Below the sample floor the rate is quantisation noise.
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 9, 1)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    # One 30s outage in a 9-step timeline: the number is noise either way, which
    # is the point of the floor below it.
    assert report.kill_switch_refresh_success_rate < Decimal("0.99")
    assert not any("refresh_success_rate" in f for f in report.failures)
    assert any("too few to judge availability" in s for s in report.shortfalls)


def test_a_single_refresh_is_not_proof_the_switch_worked(tmp_path):
    # The mirror image: 1/1 = 100% used to satisfy "the dead man's switch was
    # refreshing all run" on one sample.
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 1, 0)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    # One row spans no wall time, so availability has no denominator. This used to
    # answer 100% — the strongest possible claim from the weakest possible
    # evidence (2026-08-01 round-13 review).
    assert report.kill_switch_refresh_success_rate is None
    assert not report.live_ready
    assert any("too few to judge availability" in s for s in report.shortfalls)


def test_a_real_rate_failure_prints_counts_not_only_a_rounded_percent(tmp_path):
    # 989/1000 = 98.9% renders as "98.90%", but a genuine 0.98998 renders as
    # "99.00% (need >= 99%)" — a go/no-go line that reads as a self-contradiction
    # to the operator who has to act on it. The raw counts disambiguate.
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _pass_all_smoke(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_orders(db, 30)
    _add_refreshes(db, 989, 11)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    failure = next(f for f in report.failures if "refresh_success_rate" in f)
    # The duration is the fact the operator has to act on: "11 outages totalling
    # 330s" says what happened to this run; a bare percentage does not.
    assert "11 outage(s)" in failure
    assert "330s unrefreshed" in failure


# -- stale covering stamps after a firing ----------------------------------


def test_a_covering_stamp_after_a_firing_does_not_suppress_the_window(tmp_path):
    # Defence in depth behind protection.py's orderStatus confirmation, and the
    # lane that matters for rows written by a build predating it — a 30-cycle
    # acceptance run can span a deploy. Once the switch has fired, an order_id on
    # a blocked event is not evidence: the exchange emptied the book and left
    # every local row saying "open".
    db = _healthy(tmp_path)
    _kill_switch_event(db, "kill_switch_cancel_triggered", off=10)
    _protect(db, [("stop_loss_repair_blocked", "BTC", 20, "sl-1")])
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=60))
    assert report.unprotected_window_count == 1
    assert report.unresolved_unprotected_window


def test_a_covering_stamp_after_the_sl_is_back_on_the_book_suppresses_again(tmp_path):
    # And it must not latch forever: a confirmed stop_loss_placed means the rows
    # agree with the exchange again, so the next blocked-over-covering event is
    # ordinary evidence once more.
    db = _healthy(tmp_path)
    _kill_switch_event(db, "kill_switch_cancel_triggered", off=10)
    _protect(
        db,
        [
            ("stop_loss_placed", "BTC", 20),
            ("stop_loss_repair_blocked", "BTC", 30, "sl-2"),
        ],
    )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=60))
    assert report.unprotected_window_count == 0
    # The run still fails — on the firing itself, which is its own gate — but not
    # on a phantom unprotected window. Matched on the window failure's own
    # wording, not the bare word "unprotected", which the firing failure also
    # uses ("the run traded unprotected").
    assert not any("window(s)" in f for f in report.failures)


def test_a_covering_stamp_with_no_firing_at_all_still_suppresses(tmp_path):
    # The already-decided behaviour, pinned: an ordinary refresh blip that never
    # lapsed a deadline leaves the book intact, and failing a healthy 30-cycle
    # run on it is exactly what the covering carve-out exists to prevent.
    db = _healthy(tmp_path)
    _protect(db, [("stop_loss_repair_blocked", "BTC", 20, "sl-1")])
    with db:
        report = validate_live_run(db, run_id="r", now=_T0 + timedelta(seconds=60))
    assert report.unprotected_window_count == 0
    assert report.live_ready


def _make_report(**overrides) -> LiveValidationReport:
    base = {
        "run_id": "r",
        "execution_mode": "testnet_live",
        "cycle_count": 30,
        "api_failed_count": 0,
        "invalid_output_count": 0,
        "streaks": TrailingFailureStreaks(0, 0, None),
        "live_order_count": 30,
        "fill_count": 0,
        "exchange_fill_dedupe_error_count": 0,
        "orphan_exchange_order_count": 0,
        "orphan_exchange_order_distinct_count": 0,
        "duplicate_fill_apply_count": 0,
        "local_exchange_position_mismatch_count": 0,
        "account_replay_mismatch_count": 0,
        "unprotected_position_seconds": Decimal(0),
        "unprotected_window_count": 0,
        "unresolved_unprotected_window": False,
        "kill_switch_refresh_success_rate": Decimal(1),
        "kill_switch_refresh_total": 100,
        "kill_switch_outage_seconds": Decimal(0),
        "kill_switch_outage_episodes": 0,
        "kill_switch_covered_seconds": Decimal(3000),
        "kill_switch_ended_without_clean_shutdown": False,
        "kill_switch_ended_in_outage": False,
        "kill_switch_fired_count": 0,
        "kill_switch_disarm_failed_count": 0,
        "safe_mode_active_type": None,
        "safe_mode_active_reason": None,
        "restart_reconciliation_passed": True,
        "emergency_close_test_passed": True,
        "startup_with_existing_position_test_passed": True,
        "startup_with_stale_open_order_test_passed": True,
        "unresolved_reconciliation_mismatch_count": 0,
        "daily_loss_breached": False,
        "daily_loss_active": False,
        "emergency_close_event_count": 0,
        "failures": (),
        "shortfalls": (),
    }
    base.update(overrides)
    return LiveValidationReport(**base)


def _insert_raw_fill(db, *, fill_id: str, key: str | None) -> None:
    """Write a fills row directly, the way a corrupt store would."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO fills (fill_id, timestamp, mode, run_id, order_id, symbol, side,"
            " fill_qty, fill_price, fill_notional, fee, fee_rate, realized_pnl_delta,"
            " liquidity_type, exchange_fill_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill_id,
                _T0.isoformat(),
                "live",
                "r",
                "o1",
                "BTC",
                "buy",
                "0.001",
                "100000",
                "100",
                "0.05",
                "0.0005",
                "0",
                "taker",
                key,
            ),
        )


def test_null_keyed_fills_do_not_group_into_a_duplicate(tmp_path):
    """The ``IS NOT NULL`` filter is load-bearing, not defensive noise.

    SQLite's GROUP BY treats NULLs as EQUAL, so without the filter every
    NULL-keyed fill collapses into one group and any run with two of them reports
    a duplicate-apply — failing a healthy run at exit 5. Nothing covered this: the
    count was only ever asserted as the literal 0 in a report fixture
    (mutation-verified, 2026-08-01 round-13 review).
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _insert_raw_fill(db, fill_id="f1", key=None)
    _insert_raw_fill(db, fill_id="f2", key=None)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.duplicate_fill_apply_count == 0


def test_two_fills_sharing_an_exchange_key_fail_the_gate(tmp_path):
    """The other direction: the branch itself must still be able to fire.

    Structurally impossible while the UNIQUE index stands, which is exactly why
    it is a store-integrity assertion — and why deleting the branch was invisible.
    Dropping the index models the corrupt store it exists to catch.
    """
    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    with db.transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_fills_exchange_fill_key")
    _insert_raw_fill(db, fill_id="f1", key="0xdead:1")
    _insert_raw_fill(db, fill_id="f2", key="0xdead:1")
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.duplicate_fill_apply_count == 1
    # Name the failure, do not settle for ``not live_ready``: this fixture is only
    # ``_init_live_run`` — no cycles, no orders, no smoke — so it fails a dozen
    # other gates anyway, and the gate branch this test exists for could be
    # deleted with the assertion still green (2026-08-01 round-14 mutation probe).
    assert any("duplicate_fill_apply_count" in failure for failure in report.failures)
    assert not report.live_ready


@pytest.mark.parametrize(
    "config_json",
    ["5", "[]", '{"live": 5}', '{"live": {"kill_switch": 5}}', "not json at all", None],
)
def test_the_genesis_deadline_reader_degrades_instead_of_crashing(config_json):
    """A corrupt genesis must not crash a read-only reporter, or zero the deadline.

    Zero matters as much as the crash: a 0-second cover would make every gap a
    lapse and turn a garbled config into a guaranteed exit 5.
    """
    from contrib.hyperliquid_perp.live.validation import (
        DEFAULT_SCHEDULE_CANCEL_SECONDS,
        _schedule_cancel_seconds,
    )

    assert _schedule_cancel_seconds(config_json) == DEFAULT_SCHEDULE_CANCEL_SECONDS


# NOT "120": that is a perfectly legal quoted int, which the reader RETURNS — the
# assertion passed only because 120 is also the default, so the case asserted the
# opposite of this test's name and contradicted its sibling below, which uses the
# identical shape to prove quoted values ARE read (2026-08-01 round-14 review).
@pytest.mark.parametrize("raw", [0, -5, True, "abc", 2.5, [120]])
def test_a_nonsensical_deadline_value_falls_back(raw):
    from contrib.hyperliquid_perp.live.validation import (
        DEFAULT_SCHEDULE_CANCEL_SECONDS,
        _schedule_cancel_seconds,
    )

    blob = json.dumps({"live": {"kill_switch": {"schedule_cancel_seconds": raw}}})
    assert _schedule_cancel_seconds(blob) == DEFAULT_SCHEDULE_CANCEL_SECONDS


@pytest.mark.parametrize("raw", [600, 600.0, "600"])
def test_the_genesis_deadline_reader_reads_a_real_value(raw):
    """Including the QUOTED form, which the config layer accepts.

    ``config_json`` stores the ``live:`` block verbatim, BEFORE coercion, so a
    legal ``schedule_cancel_seconds: "600"`` arrives here as a str. Type-testing
    it fell back to 120 and measured the run against a deadline it never had —
    every healthy 121-600s gap charged as a full outage, healthy run at exit 5
    (2026-08-01 round-13 exit check).
    """
    from contrib.hyperliquid_perp.live.validation import _schedule_cancel_seconds

    blob = json.dumps({"live": {"kill_switch": {"schedule_cancel_seconds": raw}}})
    assert _schedule_cancel_seconds(blob) == Decimal(600)


@pytest.mark.parametrize("config_json", ["5", "[]", '{"live": 5}', '{"live": {"mode": 5}}'])
def test_execution_mode_degrades_on_a_corrupt_genesis(config_json):
    # Valid JSON that is not an object has no ``.get``; the docstring promises
    # "unknown, never crash" and nothing tested it.
    from contrib.hyperliquid_perp.live.validation import execution_mode

    assert execution_mode(config_json) == "unknown"


def test_post_init_accepts_a_consistent_report():
    _make_report()  # must not raise


def test_post_init_rejects_rate_present_with_zero_total():
    with pytest.raises(ValueError, match="must be None when there were no"):
        _make_report(kill_switch_refresh_success_rate=Decimal("0.5"), kill_switch_refresh_total=0)


def test_post_init_allows_no_rate_even_when_refreshes_exist():
    # The converse is deliberately NOT enforced: rows spanning no wall time are an
    # absence of evidence, and the invariant used to force them to answer with a
    # number — which the tally could only satisfy with a perfect score.
    _make_report(kill_switch_refresh_success_rate=None, kill_switch_refresh_total=100)


def test_post_init_rejects_rate_out_of_range():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        _make_report(kill_switch_refresh_success_rate=Decimal("1.5"), kill_switch_refresh_total=10)


def test_post_init_rejects_open_window_without_count():
    with pytest.raises(ValueError, match="counted window"):
        _make_report(unresolved_unprotected_window=True, unprotected_window_count=0)


def test_post_init_rejects_negative_seconds():
    with pytest.raises(ValueError, match="must be >= 0"):
        _make_report(unprotected_position_seconds=Decimal(-1))


def test_post_init_rejects_a_negative_suite_attempt_count():
    # Same guard as its two sibling counters, and it was added without one: the
    # summary renders any truthy value, so -3 printed "(+-3 during live-smoke)"
    # (2026-08-01 round-22 review).
    with pytest.raises(ValueError, match="must be >= 0"):
        _make_report(kill_switch_suite_refresh_attempts=-3)


@pytest.mark.parametrize(
    "field",
    [
        "cycle_count",
        "api_failed_count",
        "invalid_output_count",
        "live_order_count",
        "fill_count",
        "exchange_fill_dedupe_error_count",
        "orphan_exchange_order_count",
        "orphan_exchange_order_distinct_count",
        "duplicate_fill_apply_count",
        "local_exchange_position_mismatch_count",
        "account_replay_mismatch_count",
        "unprotected_position_seconds",
        "unprotected_window_count",
        "kill_switch_refresh_total",
        "kill_switch_outage_seconds",
        "kill_switch_outage_episodes",
        "kill_switch_covered_seconds",
        "kill_switch_fired_count",
        "kill_switch_disarm_failed_count",
        "unresolved_reconciliation_mismatch_count",
        "emergency_close_event_count",
        "kill_switch_suite_refresh_attempts",
    ],
)
def test_post_init_rejects_every_negative_count(field):
    # The guard grew one name at a time (fired, disarm_failed, then suite
    # attempts) while seventeen sibling counts stayed unguarded — four of which
    # render raw on the same summary the "(+-3 ...)" fix was about
    # (refresh_total and the three outage figures). One param per field, so
    # dropping a single name from the guard tuple fails exactly that param.
    with pytest.raises(ValueError, match="must be >= 0"):
        _make_report(**{field: -1})


def test_post_init_rejects_no_daemon_rows_beside_daemon_refresh_evidence():
    # Every refresh in the total IS a daemon row, so "no daemon rows" cannot
    # coexist with one. The pair would print "clean shutdown: n/a (no daemon
    # rows)" beside a daemon refresh rate (2026-08-01 round-18 review).
    # ONE refresh, not 100: pinned at a single point, the threshold could move to
    # "> 1" or be re-keyed onto the RATE (which is None whenever covered <= 0)
    # with the suite green — and the one-refresh version of the contradiction is
    # exactly the one a short run produces (2026-08-01 round-19 mutation probe).
    for total in (1, 100):
        with pytest.raises(ValueError, match="no daemon rows"):
            _make_report(
                kill_switch_ended_without_clean_shutdown=None,
                kill_switch_refresh_total=total,
                kill_switch_refresh_success_rate=Decimal(1),
            )
    # And with no rate at all, which is the shape the tally emits when the rows
    # span no wall time: the invariant keys on the EVIDENCE, not on the number.
    with pytest.raises(ValueError, match="no daemon rows"):
        _make_report(
            kill_switch_ended_without_clean_shutdown=None,
            kill_switch_refresh_total=1,
            kill_switch_refresh_success_rate=None,
        )


def test_post_init_allows_no_daemon_rows_when_only_the_suite_wrote():
    # The shape the tally really emits for a smoke-only run-id: no daemon rows,
    # no daemon refresh evidence, and therefore no rate.
    _make_report(
        kill_switch_ended_without_clean_shutdown=None,
        kill_switch_refresh_total=0,
        kill_switch_refresh_success_rate=None,
    )


# -- §21.4 refresh-rate gate + stamped cases + warnings (2026-07-27) --------


def test_mainnet_low_refresh_rate_is_a_failure(tmp_path):
    # The dead man's switch matters MOST on real money: a mainnet_tiny run whose
    # refresh success rate is below 99% must exit 5, exactly as testnet does.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    _add_refreshes(db, 85, 15)  # 15 outages in a 99-step timeline
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert not report.live_ready
    assert any("kill_switch_refresh_success_rate" in f for f in report.failures)


def test_mainnet_no_refresh_events_is_a_shortfall(tmp_path):
    # Zero refresh evidence on mainnet = "keep running", not a 0% failure —
    # mirroring the testnet reading of the same condition.
    db = Database(tmp_path / "live.db")
    _init_live_run(db, mode="mainnet_tiny")
    _add_cycles(db, MIN_LIVE_CYCLES)
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.failures == ()
    # The mainnet half of the same loose-predicate fix: "no kill-switch refresh
    # events" matches the DAEMON branch too ("no DAEMON kill-switch refresh
    # events yet"), so it could not tell the two apart either.
    assert "no kill-switch refresh events yet" in _zero_evidence_shortfall(report)


def test_stamped_reconciliation_case_does_not_fail_the_mainnet_gate(tmp_path):
    # A mismatch case a human already stamped (action_taken set) is RESOLVED:
    # it must drop out of unresolved_reconciliation_mismatch_count and not fail
    # §21.4 — otherwise every historically-resolved case re-fails the gate
    # forever. (equity_mismatch is unresolved-gated only, not a counted
    # integrity failure, so the stamped run stays live_ready.)
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            action_taken="reviewed: exchange settlement lag, values reconverged",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 0
    assert report.live_ready


def test_unstamped_equity_mismatch_still_fails_the_mainnet_gate(tmp_path):
    # The companion boundary: the SAME case without action_taken stays open.
    db = _healthy(tmp_path, mode="mainnet_tiny")
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.unresolved_reconciliation_mismatch_count == 1
    assert any("unresolved_reconciliation_mismatch_count" in f for f in report.failures)


def test_testnet_unresolved_mismatch_warns_but_does_not_gate(tmp_path):
    # On testnet an open manual case is informational (a mainnet gate condition
    # per §21.4) — the report must warn so the operator resolves it before
    # preparing mainnet, without flipping the testnet verdict.
    db = _healthy(tmp_path)
    with db.transaction() as conn:
        repo.insert_exchange_reconciliation_event(
            conn,
            run_id="r",
            trigger="heartbeat",
            case_type="equity_mismatch",
            symbol="BTC",
            exchange_value="200.01",
            timestamp=_T0,
        )
    with db:
        report = validate_live_run(db, run_id="r", now=_T0)
    assert report.live_ready
    assert any("unresolved reconciliation case" in w for w in report.warnings)


def test_mainnet_report_always_reminds_manual_shutdown_item(tmp_path):
    # §21.4's "manual shutdown/restart tested" is operator-confirmed only; the
    # mainnet report carries a fixed warning so exit 0 cannot be read as that
    # checklist item passing. Testnet reports carry no such line.
    mainnet = _healthy(tmp_path, mode="mainnet_tiny")
    with mainnet:
        mainnet_report = validate_live_run(mainnet, run_id="r", now=_T0)
    assert any("manual shutdown/restart" in w for w in mainnet_report.warnings)

    (tmp_path / "t").mkdir()
    testnet = _healthy(tmp_path / "t")
    with testnet:
        testnet_report = validate_live_run(testnet, run_id="r", now=_T0)
    assert not any("manual shutdown/restart" in w for w in testnet_report.warnings)


# --------------------------------------------------------------------------
# stale-feed refusal streak (issue #50) — the live profile
# --------------------------------------------------------------------------


def _add_stale_refusals(db: Database, n: int, *, after: int, run_id: str = "r") -> datetime:
    """``n`` stale refusals after ``after`` cycles; returns the last one's instant."""
    from contrib.hyperliquid_perp.common.constants import STALE_MARKET_DATA_ERROR

    start = _T0 + timedelta(hours=4 * after)
    insert_decision_attempts(
        db,
        [("api_failed", STALE_MARKET_DATA_ERROR)] * n,
        run_id=run_id,
        mode="live",
        start=start,
    )
    return start + timedelta(hours=4 * (n - 1))


def test_live_no_decision_streak_is_a_shortfall_not_a_failure(tmp_path):
    # A live run holding a position on SL/TP alone while no cycle reaches a
    # decision: the store is intact (never exit 5), but the accumulated cycle
    # count says nothing about a run that cannot decide right now — exit 4,
    # with the cause printed. The threshold comparison, the recency window and
    # the wording are pinned once on the paper side (both validators share the
    # constant, the query and the helper); what is live-specific, and only
    # testable here, is that it reaches ``shortfalls``/``live_ready`` rather
    # than ``failures``.
    from contrib.hyperliquid_perp.paper.validation import NO_DECISION_STREAK_THRESHOLD

    db = Database(tmp_path / "live.db")
    _init_live_run(db)
    _add_cycles(db, MIN_LIVE_CYCLES)
    last = _add_stale_refusals(db, NO_DECISION_STREAK_THRESHOLD, after=MIN_LIVE_CYCLES)
    report = validate_live_run(db, run_id="r", now=last + timedelta(hours=1))
    assert report.streaks.no_decision == NO_DECISION_STREAK_THRESHOLD
    assert report.failures == ()
    assert any("no_decision_streak = 3" in s for s in report.shortfalls)
    assert not report.live_ready
    assert "no_decision_streak: 3" in report.summary_lines()
    assert "stale_feed_refusal_streak: 3" in report.summary_lines()
    db.close()
