"""The external-weights strategy: the coordinator's weights when it set them, a hold when it did not, and what it refuses."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.config import StrategySpec
from contrib.uniswap_v3.domain.records import Outcome
from contrib.uniswap_v3.domain.types import Hold, MarketView, Portfolio
from contrib.uniswap_v3.engine.backtest import run_backtest
from contrib.uniswap_v3.engine.step import EngineError
from contrib.uniswap_v3.ports import Strategy
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.strategies.external_weights import ExternalWeights
from contrib.uniswap_v3.tests.fakes.engine import (
    FIRST_DAY,
    bar,
    config as _plain_config,
    ledger,
    weights,
)
from contrib.uniswap_v3.tests.fakes.node import put_day
from contrib.uniswap_v3.tests.fakes.targets import (
    SOURCE,
    config as _target_config,
    record,
    target,
)

PARAMS = {"band": "0.05"}
STRATEGY = ExternalWeights.from_params(PARAMS)
ALL_QUOTE = weights("1", "0", "0")


def _view(aimed: dict[str, str] | None = None, *, source: str | None = SOURCE) -> MarketView:
    """Two daily bars, with ``aimed`` (token -> weight) set at the last."""
    bars = (bar(0), bar(1))
    targets = (
        {bars[-1].time: {symbol: target(symbol, 1, weight) for symbol, weight in aimed.items()}}
        if aimed
        else {}
    )
    return MarketView(bars, targets=targets, target_source=source)


def _portfolio(view: MarketView, usdc: str, weth: str, wbtc: str) -> Portfolio:
    return ledger(usdc, weth, wbtc).portfolio("USDC", view.latest.prices)


# --- params --------------------------------------------------------------------


def test_from_params_builds_the_band():
    assert STRATEGY.band == Decimal("0.05")
    assert isinstance(STRATEGY, Strategy)


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({}, "external_weights takes exactly the params"),
        ({**PARAMS, "weights": {}}, "takes exactly the params"),
        ({"band": 0.05}, "quoted decimal"),
        ({"band": "1"}, r"band must be a Decimal in \[0, 1\)"),
        ({"band": "-0.1"}, r"band must be a Decimal in \[0, 1\)"),
    ],
)
def test_malformed_params_are_refused(params, match):
    with pytest.raises(ValueError, match=match):
        ExternalWeights.from_params(params)


def test_direct_construction_is_checked_too():
    with pytest.raises(ValueError, match="band must be a Decimal"):
        ExternalWeights(band=0.05)  # type: ignore[arg-type]


# --- the target ----------------------------------------------------------------


def test_a_view_without_a_target_source_is_refused():
    view = _view(source=None)
    with pytest.raises(
        ValueError, match="external_weights reads targets, and the view carries none"
    ):
        STRATEGY.decide(view, _portfolio(view, "1000", "0", "0"))


def test_the_coordinators_weights_are_the_target_and_the_rest_goes_to_the_quote():
    view = _view({"WETH": "0.3", "WBTC": "0.2"})
    assert STRATEGY.target_for(view, "USDC") == weights("0.5", "0.3", "0.2")
    # Whatever is held: the coordinator sets the target, not the holding.
    assert STRATEGY.decide(view, _portfolio(view, "1000", "0", "0")) == weights("0.5", "0.3", "0.2")
    assert STRATEGY.decide(view, _portfolio(view, "0", "0.5", "0")) == weights("0.5", "0.3", "0.2")


def test_a_weight_is_cut_to_four_places():
    view = _view({"WETH": "0.33333", "WBTC": "0"})
    assert STRATEGY.target_for(view, "USDC") == weights("0.6667", "0.3333", "0")


def test_weights_that_fill_the_portfolio_leave_nothing_to_the_quote():
    view = _view({"WETH": "1", "WBTC": "0"})
    assert STRATEGY.target_for(view, "USDC") == weights("0", "1", "0")


def test_without_a_target_the_portfolio_is_held_whatever_it_holds():
    view = _view()
    assert STRATEGY.target_for(view, "USDC") is None
    for holdings in (("1000", "0", "0"), ("0", "8", "0"), ("0", "0", "0")):
        assert STRATEGY.decide(view, _portfolio(view, *holdings)) == Hold()
    # A target at an earlier bar is history, not the bar being decided.
    earlier = MarketView(
        (bar(0), bar(1)),
        targets={FIRST_DAY: {"WETH": target("WETH", 0, "0.5")}},
        target_source=SOURCE,
    )
    assert STRATEGY.decide(earlier, _portfolio(earlier, "1000", "0", "0")) == Hold()


def test_a_target_on_some_traded_tokens_and_not_others_is_refused():
    view = _view({"WETH": "0.3"})
    with pytest.raises(
        ValueError,
        match=r"the targets of 'test-coordinator' at \d+ cover \['WETH'\] and not \['WBTC'\]; "
        r"external_weights takes a target on every traded token at a bar, or none",
    ):
        STRATEGY.decide(view, _portfolio(view, "1000", "0", "0"))


def test_targets_that_add_up_to_more_than_the_portfolio_are_refused():
    view = _view({"WETH": "0.6", "WBTC": "0.6"})
    with pytest.raises(ValueError, match="add up to 1.2, more than the whole portfolio"):
        STRATEGY.decide(view, _portfolio(view, "1000", "0", "0"))


# --- the trigger ---------------------------------------------------------------


def test_a_portfolio_inside_the_band_is_held():
    view = _view({"WETH": "0.3", "WBTC": "0"})  # WETH at 2000 (fakes.engine.bar)
    # 30% WETH: nothing drifts.
    assert STRATEGY.decide(view, _portfolio(view, "700", "0.15", "0")) == Hold()
    # 34%: inside the band.
    assert STRATEGY.decide(view, _portfolio(view, "660", "0.17", "0")) == Hold()
    # 36%: outside it.
    assert STRATEGY.decide(view, _portfolio(view, "640", "0.18", "0")) == weights("0.7", "0.3", "0")


def test_a_portfolio_worth_nothing_is_held():
    view = _view({"WETH": "0.3", "WBTC": "0"})
    assert STRATEGY.decide(view, _portfolio(view, "0", "0", "0")) == Hold()


def test_the_same_view_and_portfolio_give_the_same_answer_whatever_was_decided_before():
    view = _view({"WETH": "0.3", "WBTC": "0.2"})
    portfolio = _portfolio(view, "1000", "0", "0")
    first = STRATEGY.decide(view, portfolio)
    STRATEGY.decide(_view(), _portfolio(view, "0", "8", "0.01"))
    assert STRATEGY.decide(view, portfolio) == first
    assert ExternalWeights.from_params(PARAMS).decide(view, portfolio) == first


# --- through the engine --------------------------------------------------------


def test_a_backtest_holds_until_a_target_is_set_and_then_follows_it(tmp_path):
    config = replace(
        _target_config(), strategy=StrategySpec(name="external_weights", params=PARAMS)
    )
    with open_store(tmp_path / "store.db") as store:
        for day in range(4):
            put_day(store, day)
        # No target on day 0; both tokens on day 1; day 2 none again; day 3 all out.
        store.insert_target(record("WETH", 1, "0.3"))
        store.insert_target(record("WBTC", 1, "0.2"))
        store.insert_target(record("WETH", 3, "0"))
        store.insert_target(record("WBTC", 3, "0"))
        summary = run_backtest(
            store, config, run_id="carry", start=FIRST_DAY, opening=ledger(), now=FIRST_DAY
        )
        decisions = store.decisions("carry")

    assert summary.decided == 4
    assert summary.untargeted == (FIRST_DAY, FIRST_DAY + 2 * 86_400)
    assert [decision.outcome for decision in decisions] == [
        Outcome.HOLD,
        Outcome.FILLED,
        Outcome.HOLD,
        Outcome.FILLED,
    ]
    assert decisions[1].target == weights("0.5", "0.3", "0.2")
    assert decisions[3].target == ALL_QUOTE
    # A run that reads targets keeps no verdict digests: it reads no verdicts.
    assert [decision.verdicts for decision in decisions] == [None] * 4


def test_a_config_without_a_targets_section_fails_the_first_bar_and_decides_nothing(tmp_path):
    config = replace(_plain_config(), strategy=StrategySpec(name="external_weights", params=PARAMS))
    with open_store(tmp_path / "store.db") as store:
        put_day(store, 0)
        with pytest.raises(
            EngineError,
            match=r"the strategy refused the bar at \d+ \(external_weights reads targets, and "
            r"the view carries none: the config needs a targets section",
        ):
            run_backtest(
                store, config, run_id="untargeted", start=FIRST_DAY, opening=ledger(), now=FIRST_DAY
            )
        assert store.decisions("untargeted") == []
