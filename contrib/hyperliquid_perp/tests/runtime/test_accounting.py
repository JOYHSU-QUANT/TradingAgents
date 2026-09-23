"""Tests for the shared account math: fill effects, formulas, genesis, replay."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import AccountLedger, PositionState
from contrib.hyperliquid_perp.runtime import accounting as acc

_FEE = Decimal("0.00045")


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
# genesis, replay and the dataclass invariants
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
