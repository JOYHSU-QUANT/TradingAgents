"""Tests for paper accounting: fill math, account formulas, funding, replay."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.paper import accounting as acc
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.ids import slice_id
from contrib.hyperliquid_perp.persistence.models import AccountLedger, PositionState

_FEE = Decimal("0.00045")
_TS = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _flat(coin="BTC") -> PositionState:
    return PositionState.flat(coin)


def _long(size, entry, realized="0") -> PositionState:
    return PositionState(
        coin="BTC", size=Decimal(size), entry_price=Decimal(entry), realized_pnl=Decimal(realized)
    )


def _db() -> Database:
    return Database(":memory:")


# --------------------------------------------------------------------------
# compute_fill_effect (pure §6.3 / §6.5)
# --------------------------------------------------------------------------


def test_open_long_from_flat():
    e = acc.compute_fill_effect(
        _flat(), side="buy", qty=Decimal("0.01"), price=Decimal("60000"), fee_rate=_FEE
    )
    assert e.position.size == Decimal("0.01")
    assert e.position.entry_price == Decimal("60000")
    assert e.realized_pnl_delta == 0
    assert e.fee == Decimal("0.27")  # 600 * 0.00045
    assert e.wallet_delta == Decimal("-0.27")


def test_add_to_long_averages_entry():
    e = acc.compute_fill_effect(
        _long("0.02", "100"),
        side="buy",
        qty=Decimal("0.02"),
        price=Decimal("110"),
        fee_rate=Decimal(0),
    )
    assert e.position.size == Decimal("0.04")
    assert e.position.entry_price == Decimal("105")  # (2 + 2.2)/0.04
    assert e.realized_pnl_delta == 0


def test_reduce_long_realizes_and_keeps_entry():
    e = acc.compute_fill_effect(
        _long("0.05", "100"),
        side="sell",
        qty=Decimal("0.02"),
        price=Decimal("110"),
        fee_rate=Decimal(0),
    )
    assert e.position.size == Decimal("0.03")
    assert e.position.entry_price == Decimal("100")  # entry unchanged on reduce
    assert e.realized_pnl_delta == Decimal("0.2")  # (110-100)*0.02


def test_close_long_goes_flat():
    e = acc.compute_fill_effect(
        _long("0.05", "100"),
        side="sell",
        qty=Decimal("0.05"),
        price=Decimal("90"),
        fee_rate=Decimal(0),
    )
    assert e.position.is_flat
    assert e.position.entry_price is None
    assert e.realized_pnl_delta == Decimal("-0.5")  # (90-100)*0.05


def test_open_short_and_reduce():
    opened = acc.compute_fill_effect(
        _flat(), side="sell", qty=Decimal("0.05"), price=Decimal("100"), fee_rate=Decimal(0)
    )
    assert opened.position.size == Decimal("-0.05")
    assert opened.position.entry_price == Decimal("100")
    reduced = acc.compute_fill_effect(
        opened.position, side="buy", qty=Decimal("0.02"), price=Decimal("90"), fee_rate=Decimal(0)
    )
    assert reduced.position.size == Decimal("-0.03")
    assert reduced.realized_pnl_delta == Decimal("0.2")  # short profit: (entry-exit)*qty


def test_flip_in_one_fill_opens_remainder_at_fill_price():
    e = acc.compute_fill_effect(
        _long("0.02", "100"),
        side="sell",
        qty=Decimal("0.05"),
        price=Decimal("120"),
        fee_rate=Decimal(0),
    )
    assert e.position.size == Decimal("-0.03")  # crossed zero
    assert e.position.entry_price == Decimal("120")  # remainder opens fresh
    assert e.realized_pnl_delta == Decimal("0.4")  # only the closed 0.02 realizes


def test_fill_effect_validates_inputs():
    with pytest.raises(ValueError, match="side"):
        acc.compute_fill_effect(
            _flat(), side="long", qty=Decimal("1"), price=Decimal("1"), fee_rate=_FEE
        )
    with pytest.raises(ValueError, match="qty"):
        acc.compute_fill_effect(
            _flat(), side="buy", qty=Decimal("0"), price=Decimal("1"), fee_rate=_FEE
        )
    with pytest.raises(ValueError, match="price"):
        acc.compute_fill_effect(
            _flat(), side="buy", qty=Decimal("1"), price=Decimal("0"), fee_rate=_FEE
        )


# --------------------------------------------------------------------------
# account formulas (§6.1 / §6.2 / §6.6)
# --------------------------------------------------------------------------


def test_account_formulas_and_62_example():
    # §6.2 worked example: 1000 equity, 20% margin, 5x -> 200 margin, 1000 notional.
    assert acc.initial_margin(Decimal("1000"), Decimal("5")) == Decimal("200")
    eq = acc.account_equity(Decimal("1000"), Decimal("50"))
    assert eq == Decimal("1050")
    assert acc.available_balance(eq, Decimal("200")) == Decimal("850")
    assert acc.effective_leverage(Decimal("1000"), eq) == Decimal("1000") / Decimal("1050")
    assert acc.effective_leverage(Decimal("1000"), Decimal("0")) == 0  # non-positive equity
    assert acc.margin_ratio(eq, Decimal("0")) is None  # no maintenance -> undefined
    assert acc.margin_ratio(Decimal("1050"), Decimal("30")) == Decimal("35")


def test_funding_pnl_sign():
    # A long (positive signed notional) pays funding when the rate is positive.
    assert acc.funding_pnl(Decimal("3000"), Decimal("0.0001")) == Decimal("-0.3")
    # A short (negative signed notional) earns it.
    assert acc.funding_pnl(Decimal("-3000"), Decimal("0.0001")) == Decimal("0.3")


def test_summarize_account():
    sched = MarginSchedule(tiers=(MarginTier(Decimal(0), Decimal(50)),))
    ledger = AccountLedger(wallet_balance=Decimal("1000"))
    val = acc.PositionValuation(
        position=_long("0.05", "60000"), mark_price=Decimal("61000"), schedule=sched
    )
    m = acc.summarize_account(ledger, [val], leverage=Decimal("1"))
    assert m.total_position_notional == Decimal("3050")  # 0.05 * 61000
    assert m.unrealized_pnl == Decimal("50")  # 0.05 * (61000-60000)
    assert m.account_equity == Decimal("1050")
    assert m.used_initial_margin == Decimal("3050")  # notional / 1x
    assert m.total_maintenance_margin == Decimal("30.5")  # 3050 * 0.01


# --------------------------------------------------------------------------
# transactional posting + replay
# --------------------------------------------------------------------------


def _init(db, balance="1000", positions=()):
    acc.initialize_run(
        db,
        run_id="r1",
        mode="paper",
        initial_balance_usdc=Decimal(balance),
        schema_version=1,
        initial_positions=positions,
    )


def test_initialize_run_seeds_ledger_and_positions():
    db = _db()
    _init(db, "1000", positions=[_long("0.01", "60000")])
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == Decimal("1000")
    assert repo.get_current_position(db.conn, "r1", "BTC") == _long("0.01", "60000")
    db.close()


def test_post_fill_updates_position_and_ledger():
    db = _db()
    _init(db)
    acc.post_fill(
        db,
        run_id="r1",
        mode="paper",
        fill_id="f1",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=Decimal("0.01"),
        price=Decimal("60000"),
        fee_rate=_FEE,
    )
    acc.post_fill(
        db,
        run_id="r1",
        mode="paper",
        fill_id="f2",
        order_id="o2",
        symbol="BTC",
        side="sell",
        qty=Decimal("0.01"),
        price=Decimal("61000"),
        fee_rate=_FEE,
    )
    ledger = repo.get_current_account_state(db.conn, "r1")
    # realized 10, fees 0.27 + 0.2745, funding 0
    assert ledger.realized_pnl == Decimal("10")
    assert ledger.total_fees == Decimal("0.5445")
    assert ledger.wallet_balance == Decimal("1009.4555")
    assert repo.get_current_position(db.conn, "r1", "BTC").is_flat
    db.close()


def test_post_fill_requires_initialized_run():
    db = _db()
    with pytest.raises(ValueError, match="no account state"):
        acc.post_fill(
            db,
            run_id="r1",
            mode="paper",
            fill_id="f1",
            order_id="o1",
            symbol="BTC",
            side="buy",
            qty=Decimal("0.01"),
            price=Decimal("60000"),
            fee_rate=_FEE,
        )
    db.close()


def test_duplicate_slice_fill_rolls_back_ledger():
    db = _db()
    _init(db)
    sid = slice_id("r1", "plan1", None, 0)
    acc.post_fill(
        db,
        run_id="r1",
        mode="paper",
        fill_id="f1",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=Decimal("0.01"),
        price=Decimal("60000"),
        fee_rate=_FEE,
        slice_id=sid,
    )
    before = repo.get_current_account_state(db.conn, "r1")
    with pytest.raises(sqlite3.IntegrityError):
        acc.post_fill(
            db,
            run_id="r1",
            mode="paper",
            fill_id="f2",
            order_id="o2",
            symbol="BTC",
            side="buy",
            qty=Decimal("0.01"),
            price=Decimal("60000"),
            fee_rate=_FEE,
            slice_id=sid,
        )
    # the retried slice changed nothing (exactly-once)
    assert repo.get_current_account_state(db.conn, "r1") == before
    assert len(repo.iter_fills(db.conn, "r1")) == 1
    db.close()


# --------------------------------------------------------------------------
# funding exactly-once (§6.5)
# --------------------------------------------------------------------------


def test_funding_posts_once_and_retry_is_noop():
    db = _db()
    _init(db)
    r1 = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    assert r1.status == "posted" and r1.funding_pnl == Decimal("-0.03")
    ledger_after = repo.get_current_account_state(db.conn, "r1")
    assert ledger_after.wallet_balance == Decimal("999.97")
    assert ledger_after.net_funding_pnl == Decimal("-0.03")
    r2 = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    assert r2.status == "already_posted"
    assert repo.get_current_account_state(db.conn, "r1") == ledger_after  # not double-posted
    db.close()


def test_funding_pending_then_backfilled_posts_once():
    db = _db()
    _init(db)
    pend = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=None,
    )
    assert pend.status == "pending"
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == Decimal(
        "1000"
    )  # untouched
    posted = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    assert posted.status == "posted"
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == Decimal("999.97")
    # a further retry after backfill is a no-op
    again = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    assert again.status == "already_posted"
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == Decimal("999.97")
    db.close()


def test_funding_requires_mark_when_rate_known():
    db = _db()
    _init(db)
    with pytest.raises(ValueError, match="mark_price is required"):
        acc.record_funding(
            db,
            run_id="r1",
            mode="paper",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.05"),
            funding_rate=Decimal("0.00001"),
        )
    db.close()


def test_funding_backfill_uses_pending_row_basis():
    db = _db()
    _init(db)
    # Settlement captured pending: long 0.05 @ mark 60000.
    acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=None,
        mark_price=Decimal("60000"),
    )
    # The position later flips; the backfill passes the *current* (wrong) size/mark.
    result = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("-0.04"),
        funding_rate=Decimal("0.0001"),
        mark_price=Decimal("70000"),
    )
    # Funding posts on the stored settlement basis (0.05 @ 60000 -> 3000), not the new one.
    assert result.status == "posted"
    assert result.funding_pnl == Decimal("-0.3")  # -3000 * 0.0001
    ev = repo.get_funding_event(db.conn, result.funding_event_id)
    assert Decimal(ev["position_size"]) == Decimal("0.05")
    assert Decimal(ev["signed_position_notional"]) == Decimal("3000")
    db.close()


def test_funding_rejects_naive_timestamp():
    db = _db()
    _init(db)
    with pytest.raises(ValueError, match="timezone-aware"):
        acc.record_funding(
            db,
            run_id="r1",
            mode="paper",
            symbol="BTC",
            funding_timestamp=datetime(2026, 7, 1),
            position_size=Decimal("0.05"),
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("60000"),
        )
    db.close()


# --------------------------------------------------------------------------
# accounting replay (spec §5)
# --------------------------------------------------------------------------


def _run_series(db):
    _init(db)
    acc.post_fill(
        db,
        run_id="r1",
        mode="paper",
        fill_id="f1",
        order_id="o1",
        symbol="BTC",
        side="buy",
        qty=Decimal("0.02"),
        price=Decimal("60000"),
        fee_rate=_FEE,
    )
    acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.02"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60500"),
    )
    acc.post_fill(
        db,
        run_id="r1",
        mode="paper",
        fill_id="f2",
        order_id="o2",
        symbol="BTC",
        side="sell",
        qty=Decimal("0.01"),
        price=Decimal("61000"),
        fee_rate=_FEE,
    )


def test_replay_is_consistent_and_deterministic():
    db = _db()
    _run_series(db)
    r1 = acc.replay(db, run_id="r1", initial_balance=Decimal("1000"))
    r2 = acc.replay(db, run_id="r1", initial_balance=Decimal("1000"))
    assert r1.is_consistent
    assert r1.ledger == r2.ledger and r1.positions == r2.positions  # deterministic
    # replayed ledger equals the materialized one
    assert r1.ledger == repo.get_current_account_state(db.conn, "r1")
    db.close()


def test_replay_detects_corrupted_materialized_state():
    db = _db()
    _run_series(db)
    # Corrupt the materialized wallet out of band.
    with db.transaction() as conn:
        conn.execute("UPDATE current_account_state SET wallet_balance = '0' WHERE run_id = 'r1'")
    result = acc.replay(db, run_id="r1", initial_balance=Decimal("1000"))
    assert not result.account_matches
    assert not result.is_consistent
    db.close()


def test_replay_detects_corrupted_position():
    db = _db()
    _run_series(db)
    with db.transaction() as conn:
        conn.execute("UPDATE current_positions SET size = '99' WHERE run_id = 'r1'")
    result = acc.replay(db, run_id="r1", initial_balance=Decimal("1000"))
    assert "BTC" in result.position_mismatches
    db.close()


def test_replay_with_initial_positions():
    db = _db()
    seed = _long("0.01", "50000")
    _init(db, positions=[seed])
    # No fills: replay should reproduce the seed position and opening balance.
    result = acc.replay(db, run_id="r1", initial_balance=Decimal("1000"), initial_positions=[seed])
    assert result.is_consistent
    assert result.positions["BTC"] == seed
    db.close()


def test_funding_events_across_timestamps_are_distinct():
    db = _db()
    _init(db)
    later = _TS + timedelta(hours=1)
    a = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    b = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=later,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("60000"),
    )
    assert a.funding_event_id != b.funding_event_id
    assert len(repo.iter_funding_events(db.conn, "r1", status="posted")) == 2
    db.close()
