"""The run records refuse a decision, a step or a run that contradicts itself."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.bars import Finality
from contrib.uniswap_v3.domain.records import (
    BarSeen,
    Decision,
    FillRecord,
    Outcome,
    RejectionCode,
    RunRecord,
    StepRecord,
    Valuation,
)
from contrib.uniswap_v3.domain.types import Fill, RunMode, SwapIntent
from contrib.uniswap_v3.tests.fakes.engine import (
    PRICES,
    USDC,
    USDC_WETH,
    ledger as _ledger,
    weights,
)

D = Decimal
_HASH = "0x" + "ab" * 32
_TARGET = weights("0.5", "0.3", "0.2")
_LEDGER = _ledger("100")
_FILL = Fill(SwapIntent(USDC, (USDC_WETH,), D("30"), D("0")), D("0.01"), D("0"), 7)


def _decision(outcome: Outcome = Outcome.HOLD, **changes) -> Decision:
    fields = {"time": 100, "outcome": outcome, "close_block": 9}
    return Decision(**{**fields, **changes})


def _valuation(time: int = 100) -> Valuation:
    return Valuation(time=time, ledger=_LEDGER, prices=PRICES, total_value=D("100"))


def test_the_outcomes_are_the_five_a_step_can_end_in():
    assert {outcome.value for outcome in Outcome} == {
        "skipped_suspect",
        "hold",
        "no_trade",
        "filled",
        "rejected",
    }


def test_each_outcome_has_one_well_formed_decision():
    _decision(Outcome.HOLD)
    assert _decision(Outcome.SKIPPED_SUSPECT).suspect
    assert not _decision(Outcome.HOLD).suspect
    _decision(Outcome.NO_TRADE, target=_TARGET)
    _decision(Outcome.FILLED, target=_TARGET)
    _decision(
        Outcome.REJECTED, target=_TARGET, reason="closed", reason_code=RejectionCode.EXECUTOR
    )


@pytest.mark.parametrize(
    ("outcome", "changes", "match"),
    [
        (Outcome.HOLD, {"target": _TARGET}, "carries a target exactly when"),
        (Outcome.SKIPPED_SUSPECT, {"target": _TARGET}, "carries a target"),
        (Outcome.FILLED, {}, "carries a target exactly when"),
        (Outcome.NO_TRADE, {"target": {"USDC": D("1")}}, "carries a target exactly when"),
        (Outcome.REJECTED, {"target": _TARGET}, "and no other, carries a reason"),
        (
            Outcome.REJECTED,
            {"target": _TARGET, "reason": "why"},
            "carries a reason and a reason code",
        ),
        (
            Outcome.REJECTED,
            {"target": _TARGET, "reason_code": RejectionCode.GAS},
            "carries a reason and a reason code",
        ),
        (
            Outcome.REJECTED,
            {"target": _TARGET, "reason": " ", "reason_code": RejectionCode.GAS},
            "a reason is a non-empty string",
        ),
        (
            Outcome.REJECTED,
            {"target": _TARGET, "reason": "why", "reason_code": "gas"},
            "a reason code a RejectionCode",
        ),
        (Outcome.FILLED, {"target": _TARGET, "reason": "why"}, "and no other, carries a reason"),
        (Outcome.HOLD, {"reason_code": RejectionCode.GAS}, "and no other, carries a reason"),
        (
            Outcome.HOLD,
            {"seen": BarSeen(close_block=8, close_block_hash=_HASH, finality=Finality.FINAL)},
            "seen describes block 8, and the bar closed on 9",
        ),
        (Outcome.HOLD, {"time": -1}, "non-negative integers"),
        (Outcome.HOLD, {"close_block": True}, "non-negative integers"),
        ("hold", {}, "outcome must be an Outcome"),
        (Outcome.HOLD, {"seen": ("0xab", "final")}, "seen must be a BarSeen"),
    ],
)
def test_a_decision_that_contradicts_itself_is_refused(outcome, changes, match):
    with pytest.raises(ValueError, match=match):
        _decision(outcome, **changes)


@pytest.mark.parametrize(
    ("close_block_hash", "finality", "match"),
    [
        ("", Finality.FINAL, "64 lowercase hex digits"),
        (None, Finality.FINAL, "64 lowercase hex digits"),
        ("0xab", Finality.FINAL, "64 lowercase hex digits"),
        (_HASH.upper(), Finality.FINAL, "64 lowercase hex digits"),
        (_HASH, "final", "finality must be a Finality"),
    ],
)
def test_a_malformed_bar_seen_is_refused(close_block_hash, finality, match):
    with pytest.raises(ValueError, match=match):
        BarSeen(close_block=9, close_block_hash=close_block_hash, finality=finality)


def test_a_bar_seen_names_a_block_that_can_exist():
    with pytest.raises(ValueError, match="close_block must be a non-negative integer"):
        BarSeen(close_block=-1, close_block_hash=_HASH, finality=Finality.FINAL)


def test_a_valuation_keeps_its_own_copy_of_the_prices():
    prices = dict(PRICES)
    valuation = Valuation(time=1, ledger=_LEDGER, prices=prices, total_value=D("100"))
    prices["WETH"] = D("1")
    assert valuation.prices["WETH"] == D("2000")
    for changes, match in (
        ({"time": -1}, "time must be a non-negative integer"),
        ({"ledger": {"USDC": D("1")}}, "ledger must be a Ledger"),
        ({"total_value": 100}, "total_value must be a finite Decimal"),
        ({"total_value": D("NaN")}, "total_value must be a finite Decimal"),
        ({"prices": [("WETH", D("1"))]}, "prices must map token symbol to price"),
        ({"prices": {"WETH": D("0")}}, r"prices\['WETH'\] must be a finite, positive Decimal"),
        ({"prices": {"WETH": 2000.0}}, r"prices\['WETH'\] must be a finite, positive Decimal"),
    ):
        fields = {"time": 1, "ledger": _LEDGER, "prices": PRICES, "total_value": D("100")}
        with pytest.raises(ValueError, match=match):
            Valuation(**{**fields, **changes})


def test_a_step_carries_fills_exactly_when_its_decision_is_filled():
    filled = _decision(Outcome.FILLED, target=_TARGET)
    assert StepRecord(decision=filled, valuation=_valuation(), fills=(_FILL,)).fills == (_FILL,)
    assert StepRecord(decision=_decision(), valuation=_valuation()).fills == ()
    for decision, fills in ((filled, ()), (_decision(), (_FILL,)), (filled, [_FILL])):
        with pytest.raises(ValueError, match="carries fills exactly when"):
            StepRecord(decision=decision, valuation=_valuation(), fills=fills)
    with pytest.raises(ValueError, match="the valuation at 200 is not of the decision at 100"):
        StepRecord(decision=_decision(), valuation=_valuation(200))
    with pytest.raises(ValueError, match="a step's fills are Fill values"):
        StepRecord(decision=filled, valuation=_valuation(), fills=("fill",))
    with pytest.raises(ValueError, match="a step holds a Decision and a Valuation"):
        StepRecord(decision=_decision(), valuation=_LEDGER)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"run_id": ""}, "run_id must be a non-empty string"),
        ({"strategy": None}, "strategy must be a non-empty string"),
        ({"config": " "}, "config must be a non-empty string"),
        ({"mode": "paper"}, "mode must be a RunMode"),
        ({"chain_id": -1}, "non-negative integers"),
        ({"created_at": 1.5}, "non-negative integers"),
        ({"ledger": None}, "ledger must be a Ledger"),
        ({"quote": "DAI"}, "must include the quote token 'DAI'"),
    ],
)
def test_a_malformed_run_is_refused(changes, match):
    fields = {
        "run_id": "run-1",
        "mode": RunMode.BACKTEST,
        "chain_id": 1,
        "quote": "USDC",
        "strategy": "fixed_weights",
        "config": "{}",
        "ledger": _LEDGER,
        "created_at": 0,
    }
    assert RunRecord(**fields).run_id == "run-1"
    with pytest.raises(ValueError, match=match):
        RunRecord(**{**fields, **changes})


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"leg": -1}, "leg must be a non-negative integer"),
        ({"block": True}, "block must be a non-negative integer"),
        ({"token_in": ""}, "token_in must be a token symbol"),
        ({"token_out": "USDC"}, "two different tokens"),
        ({"route": ()}, "route must be a non-empty tuple of pool addresses"),
        # What tuple() makes of a route stored as one string.
        ({"route": tuple(USDC_WETH.address)}, "route must be a non-empty tuple of pool addresses"),
        ({"route": [USDC_WETH.address]}, "route must be a non-empty tuple of pool addresses"),
        ({"amount_in": D("0")}, "amount_in is not an amount a fill can have"),
        ({"amount_out": D("NaN")}, "amount_out is not an amount a fill can have"),
        ({"gas_cost_eth": D("-1")}, "gas_cost_eth is not an amount a fill can have"),
        ({"min_amount_out": 1}, "min_amount_out is not an amount a fill can have"),
        ({"min_amount_out": D("2")}, "amount_out 1.5 is below min_amount_out 2"),
    ],
)
def test_a_malformed_fill_record_is_refused(changes, match):
    fields = {
        "time": 100,
        "leg": 0,
        "token_in": "USDC",
        "token_out": "WETH",
        "route": (USDC_WETH.address,),
        "amount_in": D("3000"),
        "min_amount_out": D("1.49"),
        "amount_out": D("1.5"),
        "gas_cost_eth": D("0"),
        "block": 7,
    }
    assert FillRecord(**fields).route == (USDC_WETH.address,)
    with pytest.raises(ValueError, match=match):
        FillRecord(**{**fields, **changes})
