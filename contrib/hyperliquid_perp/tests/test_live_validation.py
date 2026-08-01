"""Live acceptance validator (§20.3 / §21.4) — metric computation over fake stores."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live import smoke
from contrib.hyperliquid_perp.live.fills import ExchangeFill, post_live_fill
from contrib.hyperliquid_perp.live.validation import (
    MIN_LIVE_CYCLES,
    MIN_LIVE_ORDERS,
    LiveValidationReport,
    validate_live_run,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

from .conftest import insert_decision_attempts

_T0 = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
_D = Decimal


def _init_live_run(db: Database, *, mode: str = "testnet_live", run_id: str = "r") -> None:
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode="live",
        initial_balance_usdc=_D(200),
        schema_version=SCHEMA_VERSION,
        config_json=json.dumps({"live": {"mode": mode}}),
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


def _kill_switch_event(db, event_type: str, *, off: int = 0, run_id: str = "r") -> None:
    with db.transaction() as conn:
        repo.insert_kill_switch_event(
            conn,
            run_id=run_id,
            event_type=event_type,
            timestamp=_T0 + timedelta(seconds=off),
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
    assert report.kill_switch_covered_seconds > Decimal(0)


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
    assert any("orphan" in f for f in report.failures)


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
    ``tests/test_live_fills.py``'s own fill-construction pattern (an
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
    assert any("refresh events" in s for s in report.shortfalls)


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
    assert report.kill_switch_ended_in_outage
    assert not report.live_ready
    assert any("ends INSIDE an outage" in s for s in report.shortfalls)


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
        "live_order_count": 30,
        "fill_count": 0,
        "exchange_fill_dedupe_error_count": 0,
        "orphan_exchange_order_count": 0,
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


@pytest.mark.parametrize("raw", [0, -5, True, "120"])
def test_a_nonsensical_deadline_value_falls_back(raw):
    from contrib.hyperliquid_perp.live.validation import (
        DEFAULT_SCHEDULE_CANCEL_SECONDS,
        _schedule_cancel_seconds,
    )

    blob = json.dumps({"live": {"kill_switch": {"schedule_cancel_seconds": raw}}})
    assert _schedule_cancel_seconds(blob) == DEFAULT_SCHEDULE_CANCEL_SECONDS


def test_the_genesis_deadline_reader_reads_a_real_value():
    from contrib.hyperliquid_perp.live.validation import _schedule_cancel_seconds

    blob = json.dumps({"live": {"kill_switch": {"schedule_cancel_seconds": 600}}})
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
    assert any("no kill-switch refresh events" in s for s in report.shortfalls)


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
