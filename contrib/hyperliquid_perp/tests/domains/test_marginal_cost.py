"""Tests for the position section's pricer (``domains/perp/marginal_cost``).

Every expected figure below is HAND-COMPUTED and written out — none is
derived by calling the function under test with different arguments (the
PR #95 / #101 lesson: a test that re-derives its expectation from the code
proves only that the code agrees with itself).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.marginal_cost import (
    MAX_COST_ROWS,
    BookPosition,
    PositionInputs,
    PositionPricing,
    build_position_context,
    display_targets,
)
from contrib.hyperliquid_perp.domains.perp.schema import PositionSide, derive_round_trip_rate

D = Decimal
_FILL_AT = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

# The worked example from the plan, one number changed: equity 1,000 USDC at
# 1x, a long of 0.003125 BTC entered at 80,000 and marked there (so no
# unrealized PnL and the wallet IS the equity), i.e. notional 250 = 25% of
# equity committed. Moving to 35% trades 100 USDC of notional.
_BASE = {
    "size": D("0.003125"),
    "entry_price": D(80000),
    "wallet_balance": D(1000),
    "mark": D(80000),
    "leverage": D(1),
    "funding_rate": D("0.0001"),
    "grid_min": 0,
    "grid_max": 60,
    "grid_step": 1,
    "taker_fee_rate": D("0.00045"),
    "slippage_bps": D(5),
    "last_fill_at": _FILL_AT,
}


def _build(**overrides):
    """Price ``_BASE`` (with overrides) through the DTOs the builder hands over.

    Kept flat here so every case below still reads as "the worked example with
    ONE number changed"; the split into books / pricing / this cycle's market
    is the pricer's, and doing it in one place is what keeps the cases from
    restating it fifteen times.
    """
    kw = {**_BASE, **overrides}
    inputs = PositionInputs(
        book=BookPosition(
            size=kw["size"],
            entry_price=kw["entry_price"],
            wallet_balance=kw["wallet_balance"],
            last_fill_at=kw["last_fill_at"],
        ),
        pricing=PositionPricing(
            leverage=kw["leverage"],
            grid_min=kw["grid_min"],
            grid_max=kw["grid_max"],
            grid_step=kw["grid_step"],
            taker_fee_rate=kw["taker_fee_rate"],
            slippage_bps=kw["slippage_bps"],
        ),
    )
    return build_position_context(inputs, mark=kw["mark"], funding_rate=kw["funding_rate"])


def _row(pos, target):
    return next(r for r in pos.cost_rows if r.target_margin_pct == target)


# --------------------------------------------------------------------------
# The round-trip rate: slippage on BOTH legs, because that is what the paper
# fill model charges — the plan's draft formula counted it once (14 bps).
# --------------------------------------------------------------------------


def test_the_round_trip_rate_counts_fee_and_slippage_on_both_legs():
    # 2 * (0.045% + 5 bps) = 2 * 9.5 bps = 19 bps, as a fraction 0.0019.
    assert derive_round_trip_rate(D("0.00045"), D(5)) == D("0.0019")
    # NOT 0.0014 (fee twice, slippage once): the fill model moves EVERY fill
    # adversely by slippage_bps, the reversing leg included.
    assert derive_round_trip_rate(D("0.00045"), D(5)) != D("0.0014")


# --------------------------------------------------------------------------
# The worked example, digit by digit
# --------------------------------------------------------------------------


def test_worked_example_prices_a_25_to_35_move_by_hand():
    pos = _build()
    assert pos.side is PositionSide.LONG
    assert pos.equity == D(1000)
    assert pos.notional == D(250)
    assert pos.margin_pct == D(25)
    assert pos.unrealized_pnl == D(0)
    row = _row(pos, 35)
    # |35 - 25| / 100 * 1000 * 1 = 100 USDC of notional changes hands.
    assert row.trade_notional == D(100)
    # 100 * 0.0019 = 0.19 USDC for the round trip ...
    assert row.round_trip_cost == D("0.19")
    # ... which is 19 bps of the notional traded: the move must be right by
    # more than that to pay for itself (the renderer prints the rate).
    assert row.round_trip_cost / row.trade_notional * 10_000 == D(19)


def test_breakeven_is_the_same_rate_on_every_row_and_cost_is_linear_in_distance():
    pos = _build()
    assert {r.round_trip_cost / r.trade_notional * 10_000 for r in pos.cost_rows} == {D(19)}
    # Flat (0%) from 25% trades 250 = the whole notional; 60% trades 350.
    assert _row(pos, 0).trade_notional == D(250)
    assert _row(pos, 0).round_trip_cost == D("0.475")
    assert _row(pos, 60).trade_notional == D(350)
    assert _row(pos, 60).round_trip_cost == D("0.665")


def test_the_row_at_the_current_margin_is_skipped_not_priced_at_zero():
    # 25% is on the sampled grid (0, 5, ..., 60) and IS the current margin:
    # nothing to trade, no breakeven to state — no row, rather than a row
    # claiming a free move.
    pos = _build()
    targets = [r.target_margin_pct for r in pos.cost_rows]
    assert 25 not in targets
    assert targets == [0, 5, 10, 15, 20, 30, 35, 40, 45, 50, 55, 60]


def test_leverage_scales_the_notional_traded_but_not_the_rate():
    # At 2x the same 250 notional commits only 12.5% of equity; moving to 35%
    # trades (35 - 12.5) / 100 * 1000 * 2 = 450 USDC, still at 19 bps.
    pos = _build(leverage=D(2))
    assert pos.margin_pct == D("12.5")
    row = _row(pos, 35)
    assert row.trade_notional == D(450)
    assert row.round_trip_cost == D("0.855")


def test_unrealized_pnl_flows_into_equity_and_therefore_margin_pct():
    # Marked 8,000 above entry: uPnL = 0.003125 * 8000 = 25; equity 1,025;
    # notional 0.003125 * 88000 = 275; margin = 275 / 1025 * 100.
    pos = _build(mark=D(88000))
    assert pos.unrealized_pnl == D(25)
    assert pos.equity == D(1025)
    assert pos.notional == D(275)
    assert pos.margin_pct == D(275) / D(1025) * 100


# --------------------------------------------------------------------------
# Holding cost: signed by who pays
# --------------------------------------------------------------------------


def test_holding_cost_is_what_a_long_pays_at_positive_funding():
    # 0.0001 / hour * 8 hours * 250 notional = 0.2 USDC per 8h, paid.
    assert _build().holding_cost_8h == D("0.2")


def test_holding_cost_flips_sign_for_a_short_at_positive_funding():
    # A short of the same size RECEIVES the positive rate.
    pos = _build(size=D("-0.003125"))
    assert pos.side is PositionSide.SHORT
    assert pos.holding_cost_8h == D("-0.2")
    # and its margin/notional are magnitudes, unchanged by the sign
    assert pos.notional == D(250)
    assert pos.margin_pct == D(25)


def test_holding_cost_flips_sign_for_a_long_at_negative_funding():
    assert _build(funding_rate=D("-0.0001")).holding_cost_8h == D("-0.2")


# --------------------------------------------------------------------------
# Flat and fail-closed
# --------------------------------------------------------------------------


def test_flat_carries_equity_and_the_fill_stamp_and_nothing_to_price():
    pos = _build(size=D(0), entry_price=None)
    assert pos.side is None
    assert pos.equity == D(1000)  # wallet: no unrealized on a flat account
    assert pos.cost_rows == ()
    assert pos.holding_cost_8h is None
    assert pos.margin_pct is None
    assert pos.last_fill_at == _FILL_AT


def test_non_positive_equity_omits_the_section_with_a_warning(caplog):
    # Wallet 100, long 0.003125 marked 40,000 below entry: uPnL -125,
    # equity -25. Nothing to price a move against; the gate refuses every
    # directional target on such an account anyway (no_account_equity).
    with caplog.at_level(logging.WARNING):
        pos = _build(wallet_balance=D(100), mark=D(40000))
    assert pos is None
    assert "position section omitted" in caplog.text
    assert "-25" in caplog.text


def test_zero_equity_is_also_omitted():
    assert _build(wallet_balance=D(125), mark=D(40000)) is None


def test_an_open_position_without_an_entry_is_rejected():
    with pytest.raises(ValueError, match="entry_price"):
        _build(entry_price=None)


@pytest.mark.parametrize("field", ["mark", "leverage"])
def test_non_positive_mark_or_leverage_is_rejected(field):
    with pytest.raises(ValueError, match=field):
        _build(**{field: D(0)})


@pytest.mark.parametrize(
    ("size", "entry_price"),
    [
        # A flat row that kept a stale entry: the pricer's flat branch hardcodes
        # entry_price=None, so this used to be dropped in silence — no log, no
        # symptom, a corrupt store fact simply gone.
        (D(0), D(50000)),
        # An open position with no entry at all, and one entered at zero: both
        # reach unrealized_pnl and produce garbage equity, which either surfaces
        # two modules later in PositionContext or comes out non-positive and
        # gets REPORTED AS INSOLVENCY, which it is not.
        (D("0.01"), None),
        (D("0.01"), D(0)),
        (D("0.01"), D(-1)),
    ],
)
def test_the_books_size_and_entry_must_agree_before_anything_is_priced(size, entry_price):
    # The same pairing persistence.models.PositionState enforces on the row
    # this is read from, restated on the type that leaves the store.
    with pytest.raises(ValueError, match="BookPosition"):
        BookPosition(size=size, entry_price=entry_price, wallet_balance=D(1000), last_fill_at=None)


def test_a_flat_book_without_an_entry_and_an_open_one_with_a_positive_entry_are_accepted():
    # The negative cases above are only meaningful if the legal pair passes.
    assert BookPosition(
        size=D(0), entry_price=None, wallet_balance=D(1000), last_fill_at=None
    ).size == D(0)
    assert BookPosition(
        size=D("0.01"), entry_price=D(50000), wallet_balance=D(1000), last_fill_at=None
    ).entry_price == D(50000)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # An inverted grid: display_targets returns no points, so an OPEN
        # position reaches PositionContext with no cost rows and is refused
        # there — at pricing time, deep in a cycle, naming a DTO instead of the
        # config key. Rejected here it names grid_min/grid_max at construction.
        ({"grid_min": 60, "grid_max": 30}, "grid_min"),
        # Above the percent-of-equity range every grid bound upstream lives in.
        ({"grid_max": 150}, "grid_max"),
        ({"grid_min": -1}, "grid_min"),
        # A zero step makes display_targets raise mid-pricing instead.
        ({"grid_step": 0}, "grid_step"),
    ],
)
def test_an_unusable_target_grid_is_rejected_when_the_rules_are_built(overrides, expected):
    base = {
        "leverage": D(1),
        "grid_min": 0,
        "grid_max": 60,
        "grid_step": 1,
        "taker_fee_rate": D("0.00045"),
        "slippage_bps": D(5),
    }
    with pytest.raises(ValueError, match=expected):
        PositionPricing(**{**base, **overrides})


def test_a_grid_whose_ceiling_sits_on_its_floor_is_legal():
    # ``<=``, not ``<``: the effective ceiling is min(ai_target_margin_max_pct,
    # risk.max_target_margin_pct), and a legal pair (grid min 60, cap 60) drives
    # it onto the floor. A stricter guard would reject a config the loader accepts.
    pricing = PositionPricing(
        leverage=D(1),
        grid_min=60,
        grid_max=60,
        grid_step=1,
        taker_fee_rate=D("0.00045"),
        slippage_bps=D(5),
    )
    assert pricing.grid_min == pricing.grid_max == 60


@pytest.mark.parametrize("field", ["taker_fee_rate", "slippage_bps"])
def test_a_negative_fill_cost_is_rejected_when_the_rules_are_built(field):
    # On PositionPricing, not inside the pricing loop: a negative cost still
    # carries the name of the config field it came from here, where a row
    # promising the account is PAID to trade would just be a smaller number.
    with pytest.raises(ValueError, match=field):
        PositionPricing(
            leverage=D(1),
            grid_min=0,
            grid_max=60,
            grid_step=1,
            **{
                "taker_fee_rate": D("0.00045"),
                "slippage_bps": D(5),
                field: D(-1),
            },
        )


# --------------------------------------------------------------------------
# The bounded grid
# --------------------------------------------------------------------------


def test_a_small_grid_is_printed_whole():
    assert display_targets(0, 40, 20) == [0, 20, 40]
    assert display_targets(0, 12, 1) == list(range(13))  # exactly MAX_COST_ROWS


def test_the_paper_grid_samples_to_every_fifth_point():
    # 61 legal targets (0..60 step 1) -> stride ceil(60 / 12) = 5 -> 13 rows,
    # and the ceiling is hit exactly.
    assert display_targets(0, 60, 1) == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60]


def test_the_ceiling_is_appended_when_the_stride_skips_it():
    # 0..100 step 1: stride ceil(100 / 12) = 9 lands on 99; 100 is a legal
    # target the format block advertises, so its row is added.
    sampled = display_targets(0, 100, 1)
    assert sampled == [0, 9, 18, 27, 36, 45, 54, 63, 72, 81, 90, 99, 100]
    assert len(sampled) == MAX_COST_ROWS


def test_rows_never_exceed_the_effective_ceiling():
    # grid_max is the EFFECTIVE ceiling (cap 60 under a grid max of 100):
    # no row may name a margin the gate would clamp.
    assert max(r.target_margin_pct for r in _build().cost_rows) == 60


def test_the_first_row_is_the_grid_floor():
    assert display_targets(10, 30, 5)[0] == 10
    assert _build(grid_min=10, grid_step=5).cost_rows[0].target_margin_pct == 10


@pytest.mark.parametrize(("step", "max_rows"), [(0, 13), (1, 1)])
def test_display_targets_rejects_a_degenerate_grid(step, max_rows):
    with pytest.raises(ValueError):
        display_targets(0, 60, step, max_rows)
