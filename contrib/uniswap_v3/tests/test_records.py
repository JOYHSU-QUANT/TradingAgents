"""The run records refuse a decision, a step or a run that contradicts itself."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.bars import Finality
from contrib.uniswap_v3.domain.records import (
    BarSeen,
    Decision,
    Outcome,
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
_TARGET = weights("0.5", "0.3", "0.2")
_LEDGER = _ledger("100")
_FILL = Fill(SwapIntent(USDC, (USDC_WETH,), D("30"), D("0")), D("0.01"), D("0"), 7)


def _decision(outcome: Outcome = Outcome.HOLD, **changes) -> Decision:
    fields = {"time": 100, "outcome": outcome, "close_block": 9}
    return Decision(**{**fields, **changes})


def _valuation(time: int = 100) -> Valuation:
    return Valuation(time=time, ledger=_LEDGER, prices=PRICES, total_value=D("100"))


def test_the_outcomes_are_the_five_the_schema_allows():
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
    _decision(Outcome.REJECTED, target=_TARGET, reason="closed")


@pytest.mark.parametrize(
    ("outcome", "changes", "match"),
    [
        (Outcome.HOLD, {"target": _TARGET}, "carries a target exactly when"),
        (Outcome.SKIPPED_SUSPECT, {"target": _TARGET}, "carries a target"),
        (Outcome.FILLED, {}, "carries a target exactly when"),
        (Outcome.NO_TRADE, {"target": {"USDC": D("1")}}, "carries a target exactly when"),
        (Outcome.REJECTED, {"target": _TARGET}, "and no other, carries a reason"),
        (Outcome.FILLED, {"target": _TARGET, "reason": "why"}, "and no other, carries a reason"),
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
    [("", Finality.FINAL, "close_block_hash"), (None, Finality.FINAL, "close_block_hash"),
     ("0xab", "final", "finality must be a Finality")],
)
def test_a_malformed_bar_seen_is_refused(close_block_hash, finality, match):
    with pytest.raises(ValueError, match=match):
        BarSeen(close_block_hash=close_block_hash, finality=finality)


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
