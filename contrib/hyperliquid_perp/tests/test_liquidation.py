"""Tests for the paper estimated-liquidation model (execution §6.6.2).

Covers the mandated cases: long, short, no positive liquidation price, currently
liquidatable, cross-tier, fee/funding change, multi-position cross, tick rounding,
plus a comparison against a recorded ``clearinghouseState`` fixture's
``liquidationPx`` with a documented tolerance.

Note on the fixture: ``clearinghouse_state.json`` is a Phase-1 hand-authored
fixture; its ``liquidationPx = 52000`` for a 0.05 BTC long @60000 is *not*
physically consistent with its well-funded ~10k cross account (which would never
liquidate at 52000). We therefore validate the model two ways: the cross
reconstruction correctly reports no liquidation, and — feeding the equity at
which this exact position does liquidate — the model reproduces the recorded
52000 to within one tick (the formula matches Hyperliquid's, not the placeholder
account state).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.mapper import (
    map_margin_schedule,
    map_position,
)
from contrib.hyperliquid_perp.paper.liquidation import (
    LiquidationEstimate,
    MaintenanceSnapshot,
    estimated_liquidation_price,
    maintenance_snapshot,
    price_tick_from_sz_decimals,
)

_TICK = Decimal("0.1")  # BTC: szDecimals 5 -> tick 0.1


def _single(lev="50") -> MarginSchedule:
    return MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(lev)),))


def _make_f(size, entry, wallet, schedule, other_un=Decimal(0), other_maint=Decimal(0)):
    """Independent reimplementation of f(p) for bracketing assertions."""

    def f(p: Decimal) -> Decimal:
        equity = wallet + size * (p - entry) + other_un
        total_maint = schedule.maintenance_margin(abs(size) * p) + other_maint
        return equity - total_maint

    return f


def _assert_long_bracket(price, f, tick):
    # Long liquidation rounds UP: the true root lies in (price - tick, price].
    assert f(price) >= 0
    assert f(price - tick) <= 0


def _assert_short_bracket(price, f, tick):
    # Short liquidation rounds DOWN: the true root lies in [price, price + tick).
    assert f(price) >= 0
    assert f(price + tick) <= 0


# --------------------------------------------------------------------------
# tick derivation
# --------------------------------------------------------------------------


def test_price_tick_from_sz_decimals():
    assert price_tick_from_sz_decimals(5) == Decimal("0.1")
    assert price_tick_from_sz_decimals(4) == Decimal("0.01")


# --------------------------------------------------------------------------
# maintenance snapshot fields (§12.2)
# --------------------------------------------------------------------------


def test_maintenance_snapshot_rejects_invalid_fields():
    # Reproducibility fields a valid tier always yields non-negative; a hand-built
    # or deserialized snapshot must not carry a negative figure or an empty tier id.
    ok = {
        "margin_tier_id": "0",
        "maintenance_margin_rate": Decimal("0.05"),
        "maintenance_deduction": Decimal("0"),
        "maintenance_margin": Decimal("300"),
    }
    MaintenanceSnapshot(**ok)  # valid accepted
    with pytest.raises(ValueError, match="margin_tier_id"):
        MaintenanceSnapshot(**{**ok, "margin_tier_id": ""})
    with pytest.raises(ValueError, match="maintenance_margin_rate"):
        MaintenanceSnapshot(**{**ok, "maintenance_margin_rate": Decimal("-0.01")})
    with pytest.raises(ValueError, match="maintenance_deduction"):
        MaintenanceSnapshot(**{**ok, "maintenance_deduction": Decimal("-1")})
    with pytest.raises(ValueError, match="maintenance_margin"):
        MaintenanceSnapshot(**{**ok, "maintenance_margin": Decimal("-1")})


def test_maintenance_snapshot_rejects_nonpositive_mark():
    # Parity with estimated_liquidation_price: abs() inside position_notional would
    # mask a negative mark (positive notional passes __post_init__) and a zero mark
    # yields margin==0 — both must fail loud, not pass as a silent reproducibility row.
    with pytest.raises(ValueError, match="mark_price must be > 0"):
        maintenance_snapshot(_single(), Decimal("0.05"), Decimal("0"))
    with pytest.raises(ValueError, match="mark_price must be > 0"):
        maintenance_snapshot(_single(), Decimal("0.05"), Decimal("-60000"))


def test_maintenance_snapshot_single_tier():
    snap = maintenance_snapshot(_single(), Decimal("0.05"), Decimal("60000"))
    assert snap.margin_tier_id == "0"
    assert snap.maintenance_margin_rate == Decimal("0.01")
    assert snap.maintenance_deduction == Decimal(0)
    assert snap.maintenance_margin == Decimal("30")  # 3000 * 0.01


def test_maintenance_snapshot_multi_tier():
    sched = MarginSchedule(
        coin="BTC",
        tiers=(MarginTier(Decimal(0), Decimal(50)), MarginTier(Decimal(10_000), Decimal(25))),
    )
    # notional 20000 (0.2 * 100000) -> tier 1
    snap = maintenance_snapshot(sched, Decimal("0.2"), Decimal("100000"))
    assert snap.margin_tier_id == "1"
    assert snap.maintenance_margin_rate == Decimal("0.02")
    assert snap.maintenance_deduction == Decimal("100")


# --------------------------------------------------------------------------
# core liquidation cases (§6.6.2)
# --------------------------------------------------------------------------


def test_long_liquidation_matches_analytical():
    # 3426 - 0.0495 p = 0 ... solved to 52000 exactly (wallet 426).
    est = estimated_liquidation_price(
        size=Decimal("0.05"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("426"),
        schedule=_single(),
        tick_size=_TICK,
    )
    assert not est.already_liquidatable
    assert est.price == Decimal("52000.0")


def test_short_liquidation_rounds_down_to_tick():
    est = estimated_liquidation_price(
        size=Decimal("-0.05"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("426"),
        schedule=_single(),
        tick_size=_TICK,
    )
    assert not est.already_liquidatable
    # 3426 - 0.0505 p = 0 -> p = 67841.584..., floor to 0.1 -> 67841.5
    assert est.price == Decimal("67841.5")
    _assert_short_bracket(
        est.price, _make_f(Decimal("-0.05"), Decimal("60000"), Decimal("426"), _single()), _TICK
    )


def test_long_no_positive_liquidation_price():
    est = estimated_liquidation_price(
        size=Decimal("0.05"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("10000"),
        schedule=_single(),
        tick_size=_TICK,
    )
    assert est.price is None
    assert not est.already_liquidatable  # solvent even at price 0, not liquidatable now


def test_already_liquidatable_reports_no_sl():
    est = estimated_liquidation_price(
        size=Decimal("0.05"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("20"),
        schedule=_single(),
        tick_size=_TICK,
    )
    assert est.already_liquidatable
    assert est.price is None


def test_invalid_tick_size_raises():
    # A zero/negative tick (bad szDecimals lookup) must fail loud, not silently
    # return an unrounded, off-grid price.
    for bad_tick in (Decimal("0"), Decimal("-0.5")):
        with pytest.raises(ValueError, match="tick_size"):
            estimated_liquidation_price(
                size=Decimal("0.05"),
                entry_price=Decimal("60000"),
                mark_price=Decimal("60000"),
                wallet_balance=Decimal("426"),
                schedule=_single(),
                tick_size=bad_tick,
            )


def test_liquidation_estimate_enforces_contract():
    # already_liquidatable  =>  no forward-looking price; the type refuses the
    # illegal fourth combination outright.
    with pytest.raises(ValueError, match="already-liquidatable"):
        LiquidationEstimate(price=Decimal("50000"), already_liquidatable=True)


def test_liquidation_estimate_rejects_non_positive_price():
    # A forward-looking liquidation price is a price: the type sign-guards it like
    # its sibling money-bearing dataclasses, so a hand-built instance can't claim
    # a non-positive liquidation level.
    with pytest.raises(ValueError, match="must be > 0"):
        LiquidationEstimate(price=Decimal("0"), already_liquidatable=False)
    with pytest.raises(ValueError, match="must be > 0"):
        LiquidationEstimate(price=Decimal("-5"), already_liquidatable=False)


def test_cross_tier_liquidation_reselects_tier():
    # Two tiers; the position is large enough that its notional spans a boundary.
    sched = MarginSchedule(
        coin="BTC",
        tiers=(MarginTier(Decimal(0), Decimal(50)), MarginTier(Decimal(2000), Decimal(20))),
    )
    size, entry, wallet = Decimal("0.1"), Decimal("60000"), Decimal("300")
    est = estimated_liquidation_price(
        size=size,
        entry_price=entry,
        mark_price=Decimal("60000"),
        wallet_balance=wallet,
        schedule=sched,
        tick_size=_TICK,
    )
    assert est.price is not None
    _assert_long_bracket(est.price, _make_f(size, entry, wallet, sched), _TICK)


def test_fee_or_funding_change_moves_liquidation_price():
    args = {
        "size": Decimal("0.05"),
        "entry_price": Decimal("60000"),
        "mark_price": Decimal("60000"),
        "schedule": _single(),
        "tick_size": _TICK,
    }
    base = estimated_liquidation_price(wallet_balance=Decimal("426"), **args)
    # A fee/funding cost lowers the wallet -> a long liquidates sooner (higher price).
    after_cost = estimated_liquidation_price(wallet_balance=Decimal("400"), **args)
    assert after_cost.price > base.price
    _assert_long_bracket(
        after_cost.price,
        _make_f(Decimal("0.05"), Decimal("60000"), Decimal("400"), _single()),
        _TICK,
    )


def test_multi_position_cross_folds_in_other_positions():
    size, entry, wallet = Decimal("0.05"), Decimal("60000"), Decimal("426")
    other_un = Decimal("-100")  # another cross position sitting at a loss
    other_maint = Decimal("50")
    est = estimated_liquidation_price(
        size=size,
        entry_price=entry,
        mark_price=Decimal("60000"),
        wallet_balance=wallet,
        schedule=_single(),
        tick_size=_TICK,
        other_positions_unrealized_pnl=other_un,
        other_positions_maintenance_margin=other_maint,
    )
    assert est.price is not None
    _assert_long_bracket(
        est.price, _make_f(size, entry, wallet, _single(), other_un, other_maint), _TICK
    )


def test_tick_rounding_is_conservative_and_on_grid():
    coarse = Decimal("100")
    est = estimated_liquidation_price(
        size=Decimal("0.05"),
        entry_price=Decimal("60000"),
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("426"),
        schedule=_single(),
        tick_size=coarse,
    )
    assert est.price % coarse == 0  # on the tick grid
    # long rounds up: the rounded price is at/above the true 52000 root
    assert est.price >= Decimal("52000")


# --------------------------------------------------------------------------
# recorded fixture comparison (§6.6.2) — see module docstring
# --------------------------------------------------------------------------


def test_fixture_cross_reconstruction_reports_no_liquidation(
    meta_and_asset_ctxs, clearinghouse_state
):
    sched = map_margin_schedule(meta_and_asset_ctxs, "BTC")
    pos = map_position(clearinghouse_state, "BTC")
    assert pos is not None
    account_value = Decimal(clearinghouse_state["marginSummary"]["accountValue"])
    wallet = account_value - pos.unrealized_pnl  # cross wallet backing all positions
    est = estimated_liquidation_price(
        size=pos.size,
        entry_price=pos.entry_price,
        mark_price=Decimal("61050"),
        wallet_balance=wallet,
        schedule=sched,
        tick_size=_TICK,
    )
    # A ~10k cross account backing a 0.05 BTC position does not liquidate by price.
    assert est.price is None


def test_fixture_formula_matches_recorded_liquidation_px_within_tolerance(
    meta_and_asset_ctxs, clearinghouse_state
):
    sched = map_margin_schedule(meta_and_asset_ctxs, "BTC")
    pos = map_position(clearinghouse_state, "BTC")
    recorded = Decimal(clearinghouse_state["assetPositions"][0]["position"]["liquidationPx"])
    tolerance = _TICK  # documented allowable error vs the recorded value
    # Equity at which this exact position liquidates (the isolated-equivalent wallet).
    est = estimated_liquidation_price(
        size=pos.size,
        entry_price=pos.entry_price,
        mark_price=Decimal("60000"),
        wallet_balance=Decimal("426"),
        schedule=sched,
        tick_size=_TICK,
    )
    assert est.price is not None
    assert abs(est.price - recorded) <= tolerance


# --------------------------------------------------------------------------
# input guards + ambient-context immunity
# --------------------------------------------------------------------------


def test_liquidation_input_guards():
    kwargs = {
        "entry_price": Decimal("100"),
        "mark_price": Decimal("100"),
        "wallet_balance": Decimal("1000"),
        "schedule": _single(),
        "tick_size": _TICK,
    }
    with pytest.raises(ValueError, match="flat position"):
        estimated_liquidation_price(size=Decimal("0"), **kwargs)
    with pytest.raises(ValueError, match="must be > 0"):
        estimated_liquidation_price(size=Decimal("1"), **{**kwargs, "entry_price": Decimal("0")})
    with pytest.raises(ValueError, match="must be > 0"):
        estimated_liquidation_price(size=Decimal("1"), **{**kwargs, "mark_price": Decimal("-1")})


def test_liquidation_math_is_immune_to_ambient_decimal_context():
    # The estimate and the §12.2 snapshot fields must be bit-for-bit
    # reproducible regardless of the (mutable, global) ambient context.
    import decimal

    sched = MarginSchedule(
        coin="BTC",
        tiers=(MarginTier(Decimal(0), Decimal(50)), MarginTier(Decimal(10_000), Decimal(25))),
    )
    kwargs = {
        "size": Decimal("0.5"),
        "entry_price": Decimal("60000"),
        "mark_price": Decimal("60000"),
        "wallet_balance": Decimal("2000"),
        "schedule": sched,
        "tick_size": _TICK,
    }
    baseline = estimated_liquidation_price(**kwargs)
    assert baseline.price is not None  # a real root, so the search actually ran
    snap_base = maintenance_snapshot(sched, Decimal("0.5"), Decimal("60000"))
    original = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 4
        assert estimated_liquidation_price(**kwargs) == baseline
        assert maintenance_snapshot(sched, Decimal("0.5"), Decimal("60000")) == snap_base
    finally:
        decimal.getcontext().prec = original
