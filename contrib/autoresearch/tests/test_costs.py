"""The cost model: which fee a fill pays, what a fill costs, and the record it leaves."""

from __future__ import annotations

import pytest

from contrib.autoresearch.costs import (
    LIVE_LEVERAGE,
    LIVE_SLIPPAGE_BPS,
    LIVE_TAKER_FEE_RATE,
    VENUE_BASE_MAKER_FEE_RATE,
    CostModel,
    FillRole,
)


def test_the_defaults_are_the_live_paper_run_s_assumptions():
    model = CostModel()
    assert model.fill_role is FillRole.TAKER
    assert model.fee_rate == LIVE_TAKER_FEE_RATE
    assert model.slippage_bps == LIVE_SLIPPAGE_BPS
    assert model.leverage == LIVE_LEVERAGE


def test_the_fill_role_picks_the_fee_and_changes_nothing_else():
    """A maker assumption changes the fee; slippage stays whatever was set.

    A resting order that fills has no adverse slippage, and it also may not
    fill at all. Zeroing slippage would price in the fill and ignore the
    miss, so the model leaves that number to the operator.
    """
    maker = CostModel(fill_role="maker")
    assert maker.fee_rate == VENUE_BASE_MAKER_FEE_RATE
    assert maker.slippage_bps == LIVE_SLIPPAGE_BPS
    assert maker.fill_role is FillRole.MAKER


def test_a_fill_costs_its_fee_and_its_slippage_on_the_mid_notional():
    model = CostModel(taker_fee_rate=0.001, slippage_bps=10)
    fee, slippage = model.fill_cost(2000.0)
    assert fee == pytest.approx(2.0)
    assert slippage == pytest.approx(2.0)
    # A short's notional is negative on the way in; the cost is not.
    assert model.fill_cost(-2000.0) == (fee, slippage)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("taker_fee_rate", -0.0001),
        ("maker_fee_rate", True),
        ("slippage_bps", "5"),
        ("leverage", 0),
        ("leverage", -1),
        ("taker_fee_rate", float("nan")),
        ("slippage_bps", float("inf")),
        ("leverage", 10**400),
    ],
)
def test_a_cost_that_is_not_a_finite_non_negative_number_is_refused(field, value):
    """Through the vocabulary's one numeric guard: a NaN fee is a NaN Sharpe on a
    trial filed as measured, and ``nan < 0`` is False."""
    with pytest.raises(ValueError, match=f"CostModel.{field}"):
        CostModel(**{field: value})


def test_an_unknown_fill_role_is_refused_by_the_vocabulary():
    with pytest.raises(ValueError, match="fill role"):
        CostModel(fill_role="limit")


def test_the_record_round_trips_and_names_the_role_by_value():
    """What the ledger writes (plan §3.3 ``cost_params_json``) reads back as the same model."""
    model = CostModel(fill_role=FillRole.MAKER, slippage_bps=2, leverage=3)
    record = model.to_dict()
    assert record["fill_role"] == "maker"
    assert set(record) == set(CostModel.__dataclass_fields__)
    assert CostModel.from_dict(record) == model
    with pytest.raises(ValueError, match="does not have"):
        CostModel.from_dict({**record, "stop_loss": 0.02})


def test_the_description_states_every_number_the_metrics_depend_on():
    line = CostModel(fill_role="maker", slippage_bps=2, leverage=2).describe()
    assert line == (
        "costs: maker fills at 0.00015 fee + 2 bps slippage, funding settled hourly, leverage 2"
    )
