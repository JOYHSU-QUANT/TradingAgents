"""Tests for TWAP / flip slice-planning math (execution §1.2 / §6.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.paper.twap import (
    MAX_SLICES,
    PlanDisposition,
    build_slice_plan,
    ceil_to_step,
    floor_to_step,
    min_order_qty,
    qty_step_from_sz_decimals,
    rebalance_delta,
    split_flip_budget,
)
from contrib.hyperliquid_perp.persistence.models import Side

D = Decimal


# --------------------------------------------------------------------------
# step helpers
# --------------------------------------------------------------------------


def test_qty_step_from_sz_decimals():
    assert qty_step_from_sz_decimals(0) == D(1)
    assert qty_step_from_sz_decimals(2) == D("0.01")
    assert qty_step_from_sz_decimals(5) == D("0.00001")


def test_qty_step_rejects_negative():
    with pytest.raises(ValueError):
        qty_step_from_sz_decimals(-1)


def test_floor_and_ceil_to_step():
    assert floor_to_step(D("1.037"), D("0.01")) == D("1.03")
    assert ceil_to_step(D("1.031"), D("0.01")) == D("1.04")
    assert floor_to_step(D("1.00"), D("0.01")) == D("1.00")
    assert ceil_to_step(D("1.00"), D("0.01")) == D("1.00")


def test_min_order_qty_rounds_up():
    # 10 USDC / 50000 mid = 0.0002 -> ceil to 0.001 step = 0.001
    assert min_order_qty(D(10), D(50000), D("0.001")) == D("0.001")


# --------------------------------------------------------------------------
# rebalance_delta (execution §6.2 fresh recompute)
# --------------------------------------------------------------------------


def test_rebalance_delta_buy_from_flat():
    # target 1000 notional @ 50000 mark = 0.02 size, from flat -> buy 0.02
    d = rebalance_delta(target_signed_notional=D(1000), mark_price=D(50000), position_size=D(0))
    assert d.side is Side.BUY
    assert d.raw_total_qty == D("0.02")
    assert d.signed_delta_size == D("0.02")


def test_rebalance_delta_sell_to_reduce():
    # target 500 notional @ 50000 = 0.01, currently 0.02 long -> sell 0.01
    d = rebalance_delta(target_signed_notional=D(500), mark_price=D(50000), position_size=D("0.02"))
    assert d.side is Side.SELL
    assert d.raw_total_qty == D("0.01")


def test_rebalance_delta_zero():
    d = rebalance_delta(
        target_signed_notional=D(1000), mark_price=D(50000), position_size=D("0.02")
    )
    assert d.side is None
    assert d.raw_total_qty == D(0)


# --------------------------------------------------------------------------
# slice allocation (execution §1.2 worked example)
# --------------------------------------------------------------------------


def test_slice_math_worked_example():
    # total_qty 1.03, step 0.01, mid 40 so min_order_qty = 10/40 = 0.25 -> 4 slices.
    plan = build_slice_plan(
        D("1.03"), side=Side.BUY, qty_step=D("0.01"), min_notional=D(10), mid=D(40)
    )
    assert plan.disposition is PlanDisposition.TWAP
    assert plan.slice_sizes == (D("0.26"), D("0.26"), D("0.26"), D("0.25"))
    assert sum(plan.slice_sizes) == D("1.03")
    assert plan.rounding_residual_qty == D(0)


def test_rounding_residual_recorded_not_rounded_up():
    plan = build_slice_plan(
        D("1.037"), side=Side.BUY, qty_step=D("0.01"), min_notional=D(10), mid=D(40)
    )
    assert plan.total_qty == D("1.03")
    assert plan.rounding_residual_qty == D("0.007")
    assert sum(plan.slice_sizes) == D("1.03")


def test_zero_slices_rejects():
    # raw below one min_order_qty -> reject, residual, not rounded up.
    plan = build_slice_plan(
        D("0.0005"), side=Side.BUY, qty_step=D("0.001"), min_notional=D(10), mid=D(50000)
    )
    assert plan.disposition is PlanDisposition.REJECT
    assert plan.slice_sizes == ()
    assert not plan.is_executable


def test_one_slice_is_paper_market():
    # total_qty exactly one min_order_qty -> single paper_market fill.
    plan = build_slice_plan(
        D("0.001"), side=Side.BUY, qty_step=D("0.001"), min_notional=D(10), mid=D(50000)
    )
    assert plan.disposition is PlanDisposition.PAPER_MARKET
    assert plan.slice_sizes == (D("0.001"),)


def test_slices_capped_at_120():
    # min_order_qty = 10/10 = 1; max_legal = floor(200/1) = 200 -> capped to 120.
    plan = build_slice_plan(
        D("200"), side=Side.SELL, qty_step=D("0.001"), min_notional=D(10), mid=D(10)
    )
    assert plan.planned_slices == MAX_SLICES
    assert sum(plan.slice_sizes) == plan.total_qty


def test_max_slices_budget_param():
    plan = build_slice_plan(
        D("200"), side=Side.SELL, qty_step=D("0.001"), min_notional=D(10), mid=D(10), max_slices=10
    )
    assert plan.planned_slices == 10


# --------------------------------------------------------------------------
# flip budget split (§1.3)
# --------------------------------------------------------------------------


def test_split_flip_budget_proportional():
    close, open_ = split_flip_budget(D(3), D(1), total_budget=120)
    assert close + open_ == 120
    assert close == 90 and open_ == 30


def test_split_flip_budget_guarantees_min_one_each():
    close, open_ = split_flip_budget(D("0.001"), D(100), total_budget=120)
    assert close >= 1 and open_ >= 1 and close + open_ == 120


def test_split_flip_budget_zero_leg():
    assert split_flip_budget(D(0), D(5)) == (0, MAX_SLICES)
    assert split_flip_budget(D(5), D(0)) == (MAX_SLICES, 0)
