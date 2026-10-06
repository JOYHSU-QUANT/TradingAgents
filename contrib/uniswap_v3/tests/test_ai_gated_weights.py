"""The AI-gated strategy: the rule's target, cut by each token's verdict, or to what is held without one.

The rule is the 3-bar trend, 2-return volatility one of
``test_trend_vol_weights``: closes rising 100, 110, 121 put a token in trend
with no measured volatility, so at the 0.5 cap.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.config import StrategySpec
from contrib.uniswap_v3.domain.records import Outcome
from contrib.uniswap_v3.domain.types import Hold, MarketView, Portfolio
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.engine.backtest import run_backtest
from contrib.uniswap_v3.engine.step import EngineError
from contrib.uniswap_v3.ports import Strategy
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.strategies.ai_gated_weights import AiGatedWeights
from contrib.uniswap_v3.strategies.trend_vol_weights import TrendVolWeights
from contrib.uniswap_v3.tests.fakes.engine import (
    FIRST_DAY,
    bar,
    config as _plain_config,
    ledger,
    weights,
)
from contrib.uniswap_v3.tests.fakes.node import DEFAULT_TICK, UP_HALF_TICK, put_day
from contrib.uniswap_v3.tests.fakes.verdicts import (
    SOURCE,
    config as _verdict_config,
    record,
    verdict,
)

RULE = {
    "trend_window": 3,
    "vol_window": 2,
    "bars_per_year": 1,
    "target_vol": "0.05",
    "max_weight": "0.5",
    "band": "0.05",
}
MULTIPLIERS = {"Buy": "1", "Overweight": "0.75", "Hold": "0.5", "Underweight": "0.25", "Sell": "0"}
PARAMS = {"rule": RULE, "multipliers": MULTIPLIERS}
STRATEGY = AiGatedWeights.from_params(PARAMS)
RISING = ["100", "110", "121"]
FALLING = ["121", "110", "100"]
ALL_QUOTE = weights("1", "0", "0")
HALF_WETH = weights("0.5", "0.5", "0")


def _view(
    weth: list[str],
    wbtc: list[str] | None = None,
    *,
    said: dict[str, Rating] | None = None,
    source: str | None = SOURCE,
) -> MarketView:
    """Bars one a day, WETH closing at ``weth`` and WBTC at ``wbtc`` (flat by default), ``said`` at the last."""
    if wbtc is None:
        wbtc = ["50000"] * len(weth)
    bars = tuple(
        bar(day, weth=eth, wbtc=btc) for day, (eth, btc) in enumerate(zip(weth, wbtc, strict=True))
    )
    last = len(bars) - 1
    verdicts = (
        {bars[-1].time: {symbol: verdict(symbol, last, rating) for symbol, rating in said.items()}}
        if said
        else {}
    )
    return MarketView(bars, verdicts=verdicts, verdict_source=source)


def _portfolio(view: MarketView, usdc: str, weth: str, wbtc: str) -> Portfolio:
    return ledger(usdc, weth, wbtc).portfolio("USDC", view.latest.prices)


# --- params --------------------------------------------------------------------


def test_from_params_builds_the_rule_and_the_multipliers():
    assert STRATEGY.rule == TrendVolWeights.from_params(RULE)
    assert STRATEGY.band == Decimal("0.05")
    assert dict(STRATEGY.multipliers) == {
        Rating.BUY: Decimal(1),
        Rating.OVERWEIGHT: Decimal("0.75"),
        Rating.HOLD: Decimal("0.5"),
        Rating.UNDERWEIGHT: Decimal("0.25"),
        Rating.SELL: Decimal(0),
    }
    assert isinstance(STRATEGY, Strategy)
    # Ties are allowed, and a whole number needs no quotes.
    flat = AiGatedWeights.from_params({**PARAMS, "multipliers": dict.fromkeys(MULTIPLIERS, 1)})
    assert set(flat.multipliers.values()) == {Decimal(1)}


def _without(key: str) -> dict[str, str]:
    return {name: value for name, value in MULTIPLIERS.items() if name != key}


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({}, "ai_gated_weights takes exactly the params"),
        ({**PARAMS, "band": "0.05"}, "takes exactly the params"),
        ({**PARAMS, "rule": None}, "rule: trend_vol_weights takes exactly the params"),
        ({**PARAMS, "rule": {**RULE, "trend_window": 1}}, "rule: trend_window must be an integer"),
        ({**PARAMS, "multipliers": ["Buy"]}, "multipliers must map each rating"),
        ({**PARAMS, "multipliers": _without("Sell")}, "multipliers must name exactly the ratings"),
        ({**PARAMS, "multipliers": {**MULTIPLIERS, "REVIEW": "0"}}, "multipliers names 'REVIEW'"),
        ({**PARAMS, "multipliers": {**MULTIPLIERS, "buy": "1"}}, "multipliers names 'buy'"),
        ({**PARAMS, "multipliers": {**MULTIPLIERS, "Buy": 1.0}}, "quoted decimal"),
        (
            {**PARAMS, "multipliers": {**MULTIPLIERS, "Buy": "1.5"}},
            r"multipliers\['Buy'\] must be a Decimal in \[0, 1\]",
        ),
        (
            {**PARAMS, "multipliers": {**MULTIPLIERS, "Sell": "-0.1"}},
            r"multipliers\['Sell'\] must be a Decimal in \[0, 1\]",
        ),
        (
            {**PARAMS, "multipliers": {**MULTIPLIERS, "Hold": "0.8"}},
            "multipliers must not rise from Buy to Sell: Hold 0.8 is above Overweight 0.75",
        ),
    ],
)
def test_malformed_params_are_refused(params, match):
    with pytest.raises(ValueError, match=match):
        AiGatedWeights.from_params(params)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"rule": RULE}, "rule must be a TrendVolWeights"),
        (
            {"multipliers": {**STRATEGY.multipliers, Rating.REVIEW: Decimal(0)}},
            "multipliers must name exactly the ratings",
        ),
        (
            {"multipliers": {**STRATEGY.multipliers, Rating.HOLD: Decimal("NaN")}},
            r"multipliers\['Hold'\] must be a Decimal in \[0, 1\]",
        ),
        (
            {"multipliers": {**STRATEGY.multipliers, Rating.HOLD: 0.5}},
            r"multipliers\['Hold'\] must be a Decimal",
        ),
    ],
)
def test_direct_construction_is_checked_too(fields, match):
    by_hand = {"rule": STRATEGY.rule, "multipliers": dict(STRATEGY.multipliers)}
    with pytest.raises(ValueError, match=match):
        AiGatedWeights(**{**by_hand, **fields})


# --- the gate ------------------------------------------------------------------


def test_a_view_without_a_verdict_source_is_refused():
    view = _view(RISING, source=None)
    with pytest.raises(
        ValueError, match="ai_gated_weights reads verdicts, and the view carries none"
    ):
        STRATEGY.decide(view, _portfolio(view, "1000", "0", "0"))


@pytest.mark.parametrize(
    ("rating", "weth"),
    [
        (Rating.BUY, "0.5"),
        (Rating.OVERWEIGHT, "0.375"),
        (Rating.HOLD, "0.25"),
        (Rating.UNDERWEIGHT, "0.125"),
        (Rating.SELL, "0"),
    ],
)
def test_a_verdict_cuts_the_rules_weight_by_its_ratings_multiplier(rating, weth):
    view = _view(RISING, said={"WETH": rating})
    assert STRATEGY.rule.target_for(view, "USDC") == HALF_WETH
    expected = weights(str(Decimal(1) - Decimal(weth)), weth, "0")
    assert STRATEGY.target_for(view, _portfolio(view, "1000", "0", "0")) == expected
    # Whatever is held: with a verdict, the verdict sets the target, not the holding.
    assert STRATEGY.target_for(view, _portfolio(view, "0", "8", "0")) == expected


def test_a_review_verdict_is_handled_as_no_verdict():
    reviewed = _view(RISING, said={"WETH": Rating.REVIEW})
    unsaid = _view(RISING)
    for holdings in (("1000", "0", "0"), ("700", "2.4793", "0"), ("0", "8", "0")):
        assert STRATEGY.target_for(
            reviewed, _portfolio(reviewed, *holdings)
        ) == STRATEGY.target_for(unsaid, _portfolio(unsaid, *holdings))


def test_without_a_verdict_a_token_in_trend_is_held_at_its_share_up_to_the_rules_weight():
    view = _view(RISING)  # the rule: WETH 0.5
    # Nothing held: nothing is bought; nor from a portfolio worth nothing, whose shares are zero.
    assert STRATEGY.target_for(view, _portfolio(view, "1000", "0", "0")) == ALL_QUOTE
    assert STRATEGY.target_for(view, _portfolio(view, "0", "0", "0")) == ALL_QUOTE
    # 30% held (2.4793 WETH at 121 is 299.99): kept, not added to; the share is cut to four places.
    assert STRATEGY.target_for(view, _portfolio(view, "700", "2.4793", "0")) == weights(
        "0.7001", "0.2999", "0"
    )
    # 80% held: sold down to the rule's weight.
    assert STRATEGY.target_for(view, _portfolio(view, "200", "6.6116", "0")) == HALF_WETH


def test_without_a_verdict_a_token_out_of_trend_is_sold_whatever_is_held():
    view = _view(FALLING)
    assert STRATEGY.target_for(view, _portfolio(view, "500", "5", "0")) == ALL_QUOTE


def test_a_verdict_cannot_put_a_token_in_against_its_trend():
    view = _view(FALLING, said={"WETH": Rating.BUY})
    assert STRATEGY.target_for(view, _portfolio(view, "1000", "0", "0")) == ALL_QUOTE
    # Nor with too few bars for the rule to confirm a trend.
    short = _view(RISING[:2], said={"WETH": Rating.BUY})
    assert STRATEGY.target_for(short, _portfolio(short, "1000", "0", "0")) == ALL_QUOTE


def test_each_token_is_gated_on_its_own():
    view = _view(RISING, ["1000", "1100", "1210"], said={"WETH": Rating.BUY})
    assert STRATEGY.rule.target_for(view, "USDC") == weights("0", "0.5", "0.5")
    # WBTC, with no verdict, keeps its 20% (0.1653 WBTC at 1210 is 200.01); 0.3 is left to the quote.
    portfolio = _portfolio(view, "800", "0", "0.1653")
    assert STRATEGY.target_for(view, portfolio) == weights("0.3", "0.5", "0.2")


@pytest.mark.parametrize("rating", [Rating.BUY, Rating.SELL, Rating.REVIEW])
@pytest.mark.parametrize("holdings", [("1000", "0", "0"), ("0", "4", "0.01"), ("0", "0", "0")])
def test_no_token_is_ever_targeted_above_the_rules_weight(rating, holdings):
    view = _view(RISING, ["1000", "1000", "1210"], said={"WETH": rating, "WBTC": rating})
    allowed = STRATEGY.rule.target_for(view, "USDC").weights
    target = STRATEGY.target_for(view, _portfolio(view, *holdings)).weights
    assert all(target[symbol] <= allowed[symbol] for symbol in ("WETH", "WBTC"))
    assert target["USDC"] >= allowed["USDC"]


def test_a_cut_weight_is_cut_to_four_places_and_the_rest_goes_to_the_quote():
    wide = AiGatedWeights.from_params({**PARAMS, "rule": {**RULE, "max_weight": "0.8"}})
    view = _view(
        RISING,
        ["1000", "1000", "1210"],
        said={"WETH": Rating.OVERWEIGHT, "WBTC": Rating.OVERWEIGHT},
    )
    # The rule: 0.6832 and 0.3167 (test_trend_vol_weights). Three quarters, cut: 0.5124 and 0.2375.
    assert wide.rule.target_for(view, "USDC") == weights("0.0001", "0.6832", "0.3167")
    assert wide.target_for(view, _portfolio(view, "1000", "0", "0")) == weights(
        "0.2501", "0.5124", "0.2375"
    )


# --- the trigger ---------------------------------------------------------------


def test_the_band_is_the_rules_and_a_portfolio_inside_it_is_held():
    view = _view(RISING, said={"WETH": Rating.HOLD})  # the target: WETH 0.25
    # 25% WETH (2.0661 at 121 is 249.998): nothing drifts.
    assert STRATEGY.decide(view, _portfolio(view, "750", "2.0661", "0")) == Hold()
    # 29%: inside the band.
    assert STRATEGY.decide(view, _portfolio(view, "710", "2.3967", "0")) == Hold()
    # 31%: outside it.
    assert STRATEGY.decide(view, _portfolio(view, "690", "2.5620", "0")) == weights(
        "0.75", "0.25", "0"
    )


def test_without_a_verdict_a_holding_below_the_rules_weight_is_not_traded():
    view = _view(RISING)
    held = _portfolio(view, "700", "2.4793", "0")  # 30% WETH
    # The rule would raise it to 50%; the gate's target is the share itself, so nothing drifts.
    assert STRATEGY.rule.decide(view, held) == HALF_WETH
    assert STRATEGY.decide(view, held) == Hold()
    # Above the rule's weight, the sale the rule asks for goes through.
    assert STRATEGY.decide(view, _portfolio(view, "200", "6.6116", "0")) == HALF_WETH


def test_a_portfolio_worth_nothing_is_held():
    view = _view(RISING, said={"WETH": Rating.BUY})
    assert STRATEGY.decide(view, _portfolio(view, "0", "0", "0")) == Hold()


def test_the_same_view_and_portfolio_give_the_same_answer_whatever_was_decided_before():
    view = _view(RISING, said={"WETH": Rating.OVERWEIGHT})
    portfolio = _portfolio(view, "1000", "0", "0")
    first = STRATEGY.decide(view, portfolio)
    STRATEGY.decide(_view(FALLING), _portfolio(view, "0", "8", "0.01"))
    assert STRATEGY.decide(view, portfolio) == first
    assert AiGatedWeights.from_params(PARAMS).decide(view, portfolio) == first


# --- through the engine --------------------------------------------------------


def test_a_backtest_replays_the_stored_verdicts_through_the_gate(tmp_path):
    """Four days rising by half: the rule holds both tokens from the third, and the verdicts gate them."""
    config = replace(
        _verdict_config(),
        strategy=StrategySpec(
            name="ai_gated_weights",
            params={"rule": {**RULE, "trend_window": 2}, "multipliers": MULTIPLIERS},
        ),
    )
    with open_store(tmp_path / "store.db") as store:
        for day in range(4):
            put_day(store, day, eth_tick=DEFAULT_TICK - day * (DEFAULT_TICK - UP_HALF_TICK))
        store.insert_verdict(record("WETH", 2, Rating.BUY))
        store.insert_verdict(record("WBTC", 2, Rating.SELL))
        summary = run_backtest(
            store, config, run_id="gated", start=FIRST_DAY, opening=ledger(), now=FIRST_DAY
        )
        decisions = store.decisions("gated")

    assert summary.decided == 4
    assert [decision.outcome for decision in decisions] == [
        Outcome.HOLD,
        Outcome.HOLD,
        Outcome.FILLED,
        Outcome.FILLED,
    ]
    # Day 2: both in trend at the cap; Buy keeps WETH's half, Sell cuts WBTC's to nothing.
    assert decisions[2].target == weights("0.5", "0.5", "0")
    assert decisions[2].verdicts == {
        "WETH": verdict("WETH", 2, Rating.BUY).digest,
        "WBTC": verdict("WBTC", 2, Rating.SELL).digest,
    }
    # Day 3, no verdict: WETH, risen past half the portfolio, is sold down to the rule's
    # weight, and WBTC, which nothing said to hold, is not bought.
    assert decisions[3].verdicts == {}
    assert decisions[3].target == weights("0.5", "0.5", "0")


def test_a_config_without_a_verdicts_section_fails_the_first_bar_and_decides_nothing(tmp_path):
    config = replace(
        _plain_config(),
        strategy=StrategySpec(name="ai_gated_weights", params=PARAMS),
    )
    with open_store(tmp_path / "store.db") as store:
        put_day(store, 0)
        with pytest.raises(
            EngineError,
            match=r"the strategy refused the bar at \d+ \(ai_gated_weights reads verdicts, and "
            r"the view carries none: the config needs a verdicts section",
        ):
            run_backtest(
                store, config, run_id="unjudged", start=FIRST_DAY, opening=ledger(), now=FIRST_DAY
            )
        assert store.decisions("unjudged") == []
