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
    # Non-positive equity: leverage is undefined (None/NULL), never a misleading 0.
    assert acc.effective_leverage(Decimal("1000"), Decimal("0")) is None
    assert acc.effective_leverage(Decimal("1000"), Decimal("-5")) is None
    assert acc.margin_ratio(eq, Decimal("0")) is None  # no maintenance -> undefined
    assert acc.margin_ratio(Decimal("1050"), Decimal("30")) == Decimal("35")


def test_funding_pnl_sign():
    # A long (positive signed notional) pays funding when the rate is positive.
    assert acc.funding_pnl(Decimal("3000"), Decimal("0.0001")) == Decimal("-0.3")
    # A short (negative signed notional) earns it.
    assert acc.funding_pnl(Decimal("-3000"), Decimal("0.0001")) == Decimal("0.3")


def test_summarize_account():
    sched = MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))
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
        mark_price=Decimal("60000"),
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


def test_funding_pending_requires_mark():
    # The pending row is the stored settlement basis; it must be captured complete.
    db = _db()
    _init(db)
    with pytest.raises(ValueError, match="pending funding event must record"):
        acc.record_funding(
            db,
            run_id="r1",
            mode="paper",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.05"),
            funding_rate=None,
        )
    db.close()


def _record_btc_funding(db, ts, *, size="0.05", rate="0.00001", mark="60000"):
    return acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=ts,
        position_size=Decimal(size),
        funding_rate=Decimal(rate),
        mark_price=Decimal(mark),
    )


def test_funding_dedups_same_instant_across_utc_offsets():
    # The same settlement expressed in a +05:00 offset must hit the same
    # exactly-once key — the wallet moves once, not twice.
    db = _db()
    _init(db)
    first = _record_btc_funding(db, _TS)
    assert first.status == "posted"
    wallet_after = repo.get_current_account_state(db.conn, "r1").wallet_balance
    offset_view = _TS.astimezone(timezone(timedelta(hours=5)))
    second = _record_btc_funding(db, offset_view)
    assert second.status == "already_posted"
    assert second.funding_event_id == first.funding_event_id
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == wallet_after
    db.close()


def test_funding_floors_to_settlement_hour():
    # A backfill stamped a few seconds past the hour (fundingHistory ms epochs)
    # is the same settlement as the scheduler's top-of-hour instant.
    db = _db()
    _init(db)
    first = _record_btc_funding(db, _TS)
    skewed = _TS + timedelta(seconds=37, microseconds=250)
    second = _record_btc_funding(db, skewed)
    assert second.status == "already_posted"
    assert second.funding_event_id == first.funding_event_id
    ev = repo.get_funding_event(db.conn, first.funding_event_id)
    assert ev["funding_timestamp"] == _TS.isoformat()  # stored floored
    db.close()


def test_funding_backfill_refuses_legacy_pending_without_mark():
    # A pre-guard pending row (mark NULL) must fail loud on backfill, never
    # settle on a fabricated basis mixing the stored size with a fresh mark.
    # insert_funding_event itself now rejects creating such a row, so the
    # legacy/corrupt store is simulated with raw SQL.
    db = _db()
    _init(db)
    with db.transaction() as conn:
        from contrib.hyperliquid_perp.persistence.ids import funding_event_id

        conn.execute(
            "INSERT INTO funding_events (funding_event_id, recorded_at, updated_at,"
            " funding_timestamp, mode, run_id, symbol, position_size, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                funding_event_id("r1", "BTC", _TS),
                _TS.isoformat(),
                _TS.isoformat(),
                _TS.isoformat(),
                "paper",
                "r1",
                "BTC",
                "0.05",
                "pending",
            ),
        )
    with pytest.raises(ValueError, match="no stored mark_price"):
        _record_btc_funding(db, _TS)
    db.close()


def test_funding_backfill_stamps_updated_at_and_keeps_recorded_at():
    # recorded_at keeps the pending-insert time; updated_at moves to the
    # posting instant, so a live posting and an hours-later backfill stay
    # distinguishable (and backfill latency stays measurable).
    db = _db()
    _init(db)
    pending = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=None,
        mark_price=Decimal("60000"),
        recorded_at=_TS,
    )
    assert pending.status == "pending"
    later = _TS + timedelta(hours=3)
    posted = acc.record_funding(
        db,
        run_id="r1",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_TS,
        position_size=Decimal("0.05"),
        funding_rate=Decimal("0.00001"),
        mark_price=Decimal("61000"),  # ignored: the stored basis settles
        recorded_at=later,
    )
    assert posted.status == "posted"
    ev = repo.get_funding_event(db.conn, posted.funding_event_id)
    assert ev["recorded_at"] == _TS.isoformat()
    assert ev["updated_at"] == later.isoformat()
    db.close()


def test_posting_is_immune_to_ambient_decimal_context():
    # The dual of the replay-immunity test: fills/funding *written* under a
    # perturbed ambient context must persist the same pinned arithmetic that
    # replay re-derives — the wallet sum must not round on the way in.
    import decimal

    db = _db()
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 6
        _run_series(db)
    finally:
        decimal.getcontext().prec = original
    assert acc.replay(db, run_id="r1").is_consistent
    db.close()


def test_replay_is_immune_to_ambient_decimal_context():
    # The pinned DECIMAL_CONTEXT means a consumer shrinking the global precision
    # cannot perturb replay's arithmetic and fake a mismatch.
    import decimal

    db = _db()
    _run_series(db)
    baseline = acc.replay(db, run_id="r1")
    assert baseline.is_consistent
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 6
        perturbed = acc.replay(db, run_id="r1")
    finally:
        decimal.getcontext().prec = original
    assert perturbed.is_consistent
    assert perturbed.ledger == baseline.ledger
    db.close()


def test_summarize_account_reports_none_leverage_when_insolvent():
    sched = MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))
    ledger = AccountLedger(wallet_balance=Decimal("-100"))
    val = acc.PositionValuation(
        position=_long("0.05", "60000"), mark_price=Decimal("60000"), schedule=sched
    )
    m = acc.summarize_account(ledger, [val], leverage=Decimal("1"))
    assert m.account_equity < 0
    assert m.effective_leverage is None


def test_fill_effect_enforces_wallet_identity():
    with pytest.raises(ValueError, match="wallet_delta"):
        acc.FillEffect(
            position=_long("0.01", "100"),
            realized_pnl_delta=Decimal("1"),
            fee=Decimal("0.1"),
            fill_notional=Decimal("1"),
            wallet_delta=Decimal("5"),  # != 1 - 0.1
        )


def test_fill_effect_rejects_negative_fee_and_notional():
    # The sibling invariants on the same dataclass (a fee is a cost, a traded
    # notional is a magnitude) reject a hand-built instance that disagrees.
    with pytest.raises(ValueError, match="fee must be >= 0"):
        acc.FillEffect(
            position=_long("0.01", "100"),
            realized_pnl_delta=Decimal("0"),
            fee=Decimal("-0.1"),
            fill_notional=Decimal("1"),
            wallet_delta=Decimal("0.1"),
        )
    with pytest.raises(ValueError, match="fill_notional must be >= 0"):
        acc.FillEffect(
            position=_long("0.01", "100"),
            realized_pnl_delta=Decimal("0"),
            fee=Decimal("0"),
            fill_notional=Decimal("-1"),
            wallet_delta=Decimal("0"),
        )


def test_initial_margin_rejects_non_positive_leverage():
    with pytest.raises(ValueError, match="leverage must be > 0"):
        acc.initial_margin(Decimal("1000"), Decimal("0"))
    with pytest.raises(ValueError, match="leverage must be > 0"):
        acc.initial_margin(Decimal("1000"), Decimal("-5"))


def test_position_state_enforces_invariants():
    with pytest.raises(ValueError, match="coin must be a non-empty"):
        PositionState(coin="", size=Decimal("0"), entry_price=None)
    with pytest.raises(ValueError, match="must carry an entry_price"):
        PositionState(coin="BTC", size=Decimal("1"), entry_price=None)
    with pytest.raises(ValueError, match="entry_price must be > 0"):
        PositionState(coin="BTC", size=Decimal("1"), entry_price=Decimal("0"))


def test_record_funding_requires_initialized_run():
    # record_funding inserts the funding row before reading the ledger; an
    # uninitialized run must raise on the ledger check AND roll the row back.
    db = _db()
    with pytest.raises(ValueError, match="no account state"):
        acc.record_funding(
            db,
            run_id="r1",
            mode="paper",
            symbol="BTC",
            funding_timestamp=_TS,
            position_size=Decimal("0.05"),
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("60000"),
        )
    fe_id = acc.funding_event_id("r1", "BTC", _TS)
    assert repo.get_funding_event(db.conn, fe_id) is None  # rolled back, not committed
    db.close()


def test_post_fill_warns_when_equity_underwater_but_wallet_positive(caplog):
    import logging

    db = _db()
    # A tiny reduce at a crushed price keeps the wallet positive but leaves the
    # residual position deeply underwater at that price — exercises the
    # equity_at_fill_price <= 0 disjunct, distinct from the wallet < 0 one.
    _init(db, balance="10", positions=[_long("1", "100")])
    with caplog.at_level(logging.WARNING):
        acc.post_fill(
            db,
            run_id="r1",
            mode="paper",
            fill_id="f1",
            order_id="o1",
            symbol="BTC",
            side="sell",
            qty=Decimal("0.01"),
            price=Decimal("5"),
            fee_rate=Decimal("0"),
        )
    # Wallet stays >= 0 (the wallet<0 disjunct did NOT fire) yet the warning fired.
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance >= 0
    assert "insolvent" in caplog.text
    db.close()


def test_post_fill_warns_when_wallet_goes_negative(caplog):
    import logging

    db = _db()
    _init(db, balance="0.1")  # fee 0.27 on the fill below exceeds the wallet
    with caplog.at_level(logging.WARNING):
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
    assert "insolvent" in caplog.text
    # Warned, not blocked: the fill still posted.
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance < 0
    db.close()


def test_initialize_run_warns_on_nonpositive_balance(caplog):
    import logging

    db = _db()
    with caplog.at_level(logging.WARNING):
        acc.initialize_run(
            db,
            run_id="r0",
            mode="paper",
            initial_balance_usdc=Decimal("0"),
            schema_version=1,
        )
    assert "non-positive initial balance" in caplog.text
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
    r1 = acc.replay(db, run_id="r1")
    r2 = acc.replay(db, run_id="r1")
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
    result = acc.replay(db, run_id="r1")
    assert not result.account_matches
    assert not result.is_consistent
    db.close()


def test_replay_detects_corrupted_position():
    db = _db()
    _run_series(db)
    with db.transaction() as conn:
        conn.execute("UPDATE current_positions SET size = '99' WHERE run_id = 'r1'")
    result = acc.replay(db, run_id="r1")
    assert "BTC" in result.position_mismatches
    db.close()


def test_replay_with_initial_positions():
    db = _db()
    seed = _long("0.01", "50000")
    _init(db, positions=[seed])
    # No fills: replay rebuilds the seed position and opening balance from the
    # run's own committed genesis rows — no caller-supplied config involved.
    result = acc.replay(db, run_id="r1")
    assert result.is_consistent
    assert result.positions["BTC"] == seed
    db.close()


def test_replay_missing_run_raises():
    db = _db()
    with pytest.raises(ValueError, match="does not exist"):
        acc.replay(db, run_id="ghost")
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


# --------------------------------------------------------------------------
# construction-time invariants on the result/value dataclasses
# --------------------------------------------------------------------------


def test_funding_result_couples_pnl_to_posted_status():
    acc.FundingResult("posted", "fe", Decimal("1"))  # valid pairings accepted
    acc.FundingResult("already_posted", "fe", None)
    with pytest.raises(ValueError, match="exactly when status"):
        acc.FundingResult("pending", "fe", Decimal("1"))
    with pytest.raises(ValueError, match="exactly when status"):
        acc.FundingResult("posted", "fe", None)
    with pytest.raises(ValueError, match="status must be one of"):
        acc.FundingResult("settled", "fe", None)


def test_account_metrics_invariants_enforced():
    kwargs = {
        "wallet_balance": Decimal("1000"),
        "account_equity": Decimal("1000"),  # == wallet + upnl
        "available_balance": Decimal("900"),  # == equity - used_im
        "unrealized_pnl": Decimal("0"),
        "total_position_notional": Decimal("500"),
        "used_initial_margin": Decimal("100"),
        "total_maintenance_margin": Decimal("10"),
        "effective_leverage": Decimal("0.5"),
        "margin_ratio": Decimal("100"),
    }
    acc.AccountMetrics(**kwargs)  # valid pairing accepted
    # None-couplings
    with pytest.raises(ValueError, match="effective_leverage"):
        acc.AccountMetrics(**{**kwargs, "account_equity": Decimal("-5")})
    with pytest.raises(ValueError, match="effective_leverage"):
        acc.AccountMetrics(**{**kwargs, "effective_leverage": None})
    with pytest.raises(ValueError, match="margin_ratio"):
        acc.AccountMetrics(**{**kwargs, "total_maintenance_margin": Decimal("0")})
    with pytest.raises(ValueError, match="margin_ratio"):
        acc.AccountMetrics(**{**kwargs, "margin_ratio": None})
    # Arithmetic identities: equity = wallet + upnl, available = equity - used_im.
    # (equity kept > 0 and maint != 0 so the None-couplings still pass and it is
    # the identity check that fires.)
    with pytest.raises(ValueError, match="account_equity"):
        acc.AccountMetrics(
            **{**kwargs, "account_equity": Decimal("1234"), "available_balance": Decimal("1134")}
        )
    with pytest.raises(ValueError, match="available_balance"):
        acc.AccountMetrics(**{**kwargs, "available_balance": Decimal("777")})


def test_position_valuation_rejects_non_positive_mark():
    sched = MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))
    with pytest.raises(ValueError, match="mark_price must be > 0"):
        acc.PositionValuation(_long("0.01", "60000"), Decimal("0"), sched)


def test_position_valuation_rejects_coin_mismatch():
    # _long is a BTC position; pairing it with an ETH schedule would value it
    # against the wrong asset's tier table — rejected at construction.
    sched = MarginSchedule(coin="ETH", tiers=(MarginTier(Decimal(0), Decimal(50)),))
    with pytest.raises(ValueError, match="must be the same asset"):
        acc.PositionValuation(_long("0.01", "60000"), Decimal("60000"), sched)


def test_replay_result_positions_mapping_is_immutable():
    db = _db()
    _run_series(db)
    result = acc.replay(db, run_id="r1")
    with pytest.raises(TypeError):
        result.positions["BTC"] = PositionState.flat("BTC")  # type: ignore[index]
    db.close()


# --------------------------------------------------------------------------
# summarize_account ambient-context immunity (matches the sibling money paths)
# --------------------------------------------------------------------------


def test_summarize_account_is_immune_to_ambient_decimal_context():
    import decimal

    sched = MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))
    ledger = AccountLedger(wallet_balance=Decimal("1234.56789"))
    vals = [acc.PositionValuation(_long("0.0123", "61234.5"), Decimal("61999.875"), sched)]
    baseline = acc.summarize_account(ledger, vals, leverage=Decimal("3"))
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 4
        perturbed = acc.summarize_account(ledger, vals, leverage=Decimal("3"))
    finally:
        decimal.getcontext().prec = original
    assert perturbed == baseline


# --------------------------------------------------------------------------
# initialize_run: the genesis must apply exactly once, atomically
# --------------------------------------------------------------------------


def test_initialize_run_rejects_duplicate_run_id_and_rolls_back():
    db = _db()
    _init(db, "1000", positions=[_long("0.01", "60000")])
    # Re-initializing an existing run is a lifecycle error surfaced as a clean
    # domain error (not a raw sqlite3.IntegrityError), matching the missing-run path.
    with pytest.raises(ValueError, match="already initialized"):
        acc.initialize_run(
            db,
            run_id="r1",
            mode="paper",
            initial_balance_usdc=Decimal("999"),
            schema_version=1,
        )
    # First genesis intact: the retry must not have moved the opening state.
    assert repo.get_current_account_state(db.conn, "r1").wallet_balance == Decimal("1000")
    assert len(repo.get_run_seed_positions(db.conn, "r1")) == 1
    db.close()


def test_initialize_run_rejects_duplicate_seed_coins_atomically():
    db = _db()
    dup = [_long("0.01", "60000"), _long("0.02", "50000")]  # both BTC
    with pytest.raises(sqlite3.IntegrityError):
        acc.initialize_run(
            db,
            run_id="r1",
            mode="paper",
            initial_balance_usdc=Decimal("1000"),
            schema_version=1,
            initial_positions=dup,
        )
    # The whole genesis rolled back: no run row, no partial seed/current rows.
    assert repo.get_run(db.conn, "r1") is None
    assert len(repo.get_run_seed_positions(db.conn, "r1")) == 0
    assert repo.get_current_account_state(db.conn, "r1") is None
    db.close()


# --------------------------------------------------------------------------
# replay rebuilds funding from the stored basis, not the derived column
# --------------------------------------------------------------------------


def test_replay_recomputes_funding_from_stored_basis():
    db = _db()
    _run_series(db)
    assert acc.replay(db, run_id="r1").is_consistent  # sanity before corruption
    # Corrupt the derived column behind the API's back: replay must rebuild the
    # ledger from (position_size, mark_price, funding_rate) and still match the
    # materialized state — symmetric with how fills are recomputed from basis.
    db.conn.execute("UPDATE funding_events SET funding_pnl = '999999'")
    result = acc.replay(db, run_id="r1")
    assert result.account_matches
    assert result.is_consistent
    db.close()
