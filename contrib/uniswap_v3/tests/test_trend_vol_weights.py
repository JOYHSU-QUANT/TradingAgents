"""The rule strategy: a 3-bar trend, a 2-return volatility, sized to a per-bar target.

``bars_per_year`` is 1, so the annualised volatility is the per-bar one and
the numbers can be worked out by hand. The sample standard deviation of two
returns ``a`` and ``b`` is ``|a - b| / sqrt(2)``.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

import pytest

from contrib.uniswap_v3.domain.types import Bar, Hold, MarketView, Portfolio
from contrib.uniswap_v3.strategies.trend_vol_weights import TrendVolWeights, _recent
from contrib.uniswap_v3.tests.fakes.engine import FIRST_DAY, bar, ledger, weights

PARAMS = {
    "trend_window": 3,
    "vol_window": 2,
    "bars_per_year": 1,
    "target_vol": "0.05",
    "max_weight": "0.5",
    "band": "0.05",
}
STRATEGY = TrendVolWeights.from_params(PARAMS)
ALL_QUOTE = weights("1", "0", "0")
HALF_WETH = weights("0.5000", "0.5000", "0")
FOUR_PLACES = Decimal("0.0001")


def _view(weth: list[str], wbtc: list[str] | None = None, *, suspect: set[int] = frozenset()):
    """Bars one a day, WETH closing at ``weth`` and WBTC at ``wbtc`` (flat by default)."""
    if wbtc is None:
        wbtc = ["50000"] * len(weth)
    return MarketView(
        tuple(
            bar(day, weth=eth, wbtc=btc, suspect=day in suspect)
            for day, (eth, btc) in enumerate(zip(weth, wbtc, strict=True))
        )
    )


def _portfolio(view: MarketView, usdc: str, weth: str, wbtc: str) -> Portfolio:
    return ledger(usdc, weth, wbtc).portfolio("USDC", view.latest.prices)


def test_from_params_reads_quoted_decimals_and_integer_windows():
    by_hand = TrendVolWeights(
        trend_window=3,
        vol_window=2,
        bars_per_year=1,
        target_vol=Decimal("0.05"),
        max_weight=Decimal("0.5"),
        band=Decimal("0.05"),
    )
    assert by_hand == STRATEGY
    assert STRATEGY.bars_needed == 3


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({}, "takes exactly the params"),
        ({key: value for key, value in PARAMS.items() if key != "band"}, "takes exactly"),
        ({**PARAMS, "lookback": 5}, "takes exactly the params"),
        (["trend_window"], "takes exactly the params"),
        (None, "takes exactly the params"),
        ({**PARAMS, "trend_window": 1}, "trend_window must be an integer of at least 2"),
        ({**PARAMS, "trend_window": "3"}, "trend_window must be an integer"),
        ({**PARAMS, "trend_window": 3.0}, "trend_window must be an integer"),
        ({**PARAMS, "trend_window": True}, "trend_window must be an integer"),
        ({**PARAMS, "vol_window": 1}, "vol_window must be an integer of at least 2"),
        ({**PARAMS, "bars_per_year": 0}, "bars_per_year must be an integer of at least 1"),
        ({**PARAMS, "bars_per_year": True}, "bars_per_year must be an integer"),
        ({**PARAMS, "target_vol": 0.05}, "quoted decimal"),
        ({**PARAMS, "target_vol": "0"}, "target_vol must be a Decimal above 0"),
        ({**PARAMS, "target_vol": "-0.1"}, "target_vol must be a Decimal above 0"),
        ({**PARAMS, "max_weight": "0"}, r"max_weight must be a Decimal in \(0, 1\]"),
        ({**PARAMS, "max_weight": "1.5"}, r"max_weight must be a Decimal in \(0, 1\]"),
        ({**PARAMS, "max_weight": None}, "quoted decimal"),
        ({**PARAMS, "band": "1"}, r"band must be a Decimal in \[0, 1\)"),
        ({**PARAMS, "band": "-0"}, r"band must be a Decimal in \[0, 1\)"),
        ({**PARAMS, "band": "5e-2"}, "quoted decimal"),
    ],
)
def test_malformed_params_are_refused(params, match):
    with pytest.raises(ValueError, match=match):
        TrendVolWeights.from_params(params)


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"target_vol": Decimal("Infinity")}, "target_vol must be a Decimal above 0"),
        ({"max_weight": 1}, "max_weight must be a Decimal"),
        ({"bars_per_year": 1.0}, "bars_per_year must be an integer"),
        ({"band": Decimal("NaN")}, r"band must be a Decimal in \[0, 1\)"),
    ],
)
def test_direct_construction_is_checked_too(fields, match):
    by_hand = {
        "trend_window": 3,
        "vol_window": 2,
        "bars_per_year": 1,
        "target_vol": Decimal("0.05"),
        "max_weight": Decimal("0.5"),
        "band": Decimal("0.05"),
    }
    with pytest.raises(ValueError, match=match):
        TrendVolWeights(**{**by_hand, **fields})


def test_the_bars_needed_are_the_longer_of_the_trend_window_and_the_returns_window():
    long_vol = TrendVolWeights.from_params({**PARAMS, "trend_window": 2, "vol_window": 4})
    assert long_vol.bars_needed == 5  # four returns need five closes
    rising = ["100", "110", "121", "133"]
    assert long_vol.target(_view(rising), "USDC") == ALL_QUOTE
    assert long_vol.target(_view([*rising, "146"]), "USDC") == HALF_WETH


def test_a_max_weight_of_one_lets_a_token_take_the_whole_portfolio():
    whole = TrendVolWeights.from_params({**PARAMS, "max_weight": "1"})
    # Doubling every bar: two equal returns, so no volatility, so the cap.
    view = _view(["100", "200", "400"])
    assert whole.target(view, "USDC") == weights("0", "1.0000", "0")


def test_too_few_bars_confirm_no_trend_and_the_target_is_all_quote():
    view = _view(["100", "110"])
    assert STRATEGY.target(view, "USDC") == ALL_QUOTE
    # Holding WETH then is out of band, and the answer sells it.
    assert STRATEGY.decide(view, _portfolio(view, "500", "4.5", "0")) == ALL_QUOTE
    assert STRATEGY.decide(view, _portfolio(view, "1000", "0", "0")) == Hold()


def test_a_token_closing_at_or_below_its_average_is_out_of_trend():
    assert STRATEGY.target(_view(["121", "110", "100"]), "USDC") == ALL_QUOTE
    # 100, 130, 110 average 113.33; 110 is below it.
    assert STRATEGY.target(_view(["100", "130", "110"]), "USDC") == ALL_QUOTE
    # 90, 110, 100 average 100: at the average is not above it.
    assert STRATEGY.target(_view(["90", "110", "100"]), "USDC") == ALL_QUOTE
    # The average is over the last three closes only: 100, 90, 95 average 95, and the
    # earlier 10 would have pulled a four-close average below the latest close.
    assert STRATEGY.target(_view(["10", "100", "90", "95"]), "USDC") == ALL_QUOTE


def test_a_trending_token_with_no_volatility_takes_the_cap():
    # Doubling every bar: the returns are exactly equal and the variance exactly zero.
    assert STRATEGY.target(_view(["100", "200", "400"]), "USDC") == HALF_WETH


def test_a_trending_token_with_next_to_no_volatility_is_capped_too():
    # Growing a tenth a bar: the returns agree to 28 digits, and the size is enormous.
    assert STRATEGY.target(_view(["100", "110", "121"]), "USDC") == HALF_WETH


def test_a_trending_token_is_sized_to_the_volatility_target():
    # Returns ln(1) and ln(1.21): a volatility of ln(1.21) / sqrt(2), about 0.1348.
    view = _view(["100", "100", "121"])
    expected = Decimal("0.05") / Decimal(repr(math.log(1.21) / math.sqrt(2)))
    target = STRATEGY.target(view, "USDC")
    weight = target.weights["WETH"]
    assert abs(weight - expected) < FOUR_PLACES
    assert weight == weight.quantize(FOUR_PLACES)
    assert target.weights["USDC"] == Decimal(1) - weight
    assert target.weights["WBTC"] == 0


def test_annualising_scales_the_volatility_by_the_root_of_the_bars_per_year():
    yearly = TrendVolWeights.from_params({**PARAMS, "bars_per_year": 4})
    view = _view(["100", "100", "121"])
    # Twice the volatility, half the weight, both cut to four places.
    halved = (STRATEGY.target(view, "USDC").weights["WETH"] / 2).quantize(
        FOUR_PLACES, rounding=ROUND_DOWN
    )
    assert yearly.target(view, "USDC").weights["WETH"] == halved


def test_the_trend_and_volatility_windows_read_only_their_last_bars():
    # Three bars or ten: the last three closes decide the trend, the last two returns the size.
    short = _view(["100", "110", "121"])
    long = _view(["500", "1", "900", "7", "60", "3", "8", "100", "110", "121"])
    assert STRATEGY.target(long, "USDC") == STRATEGY.target(short, "USDC")


def test_weights_in_trend_that_add_up_to_more_than_one_are_scaled_down():
    wide = TrendVolWeights.from_params({**PARAMS, "max_weight": "0.8"})
    view = _view(["100", "110", "121"], ["1000", "1100", "1210"])
    # Both at the 0.8 cap, 1.6 together, scaled to 0.5 each: nothing is left for the quote.
    assert wide.target(view, "USDC") == weights("0", "0.5000", "0.5000")


def test_scaling_down_keeps_the_proportions_and_cutting_leaves_the_rest_to_the_quote():
    wide = TrendVolWeights.from_params({**PARAMS, "max_weight": "0.8"})
    # WETH at the cap, WBTC sized by its volatility: 0.8 + 0.3709 scaled to 1 and cut
    # to four places each, so the quote keeps the 0.0001 the cuts leave.
    view = _view(["100", "110", "121"], ["1000", "1000", "1210"])
    assert wide.target(view, "USDC") == weights("0.0001", "0.6832", "0.3167")


def test_two_tokens_in_trend_within_the_cap_are_both_held():
    view = _view(["100", "110", "121"], ["1000", "1100", "1210"])
    assert STRATEGY.target(view, "USDC") == weights("0", "0.5000", "0.5000")


def test_a_portfolio_inside_the_band_is_held_and_one_outside_is_sent_to_the_target():
    view = _view(["100", "110", "121"])  # WETH 50%, USDC 50%, WBTC 0
    # Worth about 1000 USDC at WETH = 121: 4.1322 WETH is 500 USDC.
    assert STRATEGY.decide(view, _portfolio(view, "500", "4.1322", "0")) == Hold()
    # 54% WETH: inside the band.
    assert STRATEGY.decide(view, _portfolio(view, "460", "4.4628", "0")) == Hold()
    # 60% WETH: outside it.
    assert STRATEGY.decide(view, _portfolio(view, "400", "4.9587", "0")) == HALF_WETH
    # Holding WBTC, which is out of trend, is drift too.
    assert STRATEGY.decide(view, _portfolio(view, "500", "3.3", "0.002")) == HALF_WETH


def test_suspect_bars_are_left_out_of_the_series():
    clean = _view(["100", "110", "121"])
    # A wild reading in the middle, marked suspect, changes nothing.
    with_suspect = _view(["100", "110", "9", "121"], suspect={2})
    assert STRATEGY.target(with_suspect, "USDC") == STRATEGY.target(clean, "USDC")
    # Suspect bars do not count towards the bars the windows need.
    assert STRATEGY.target(_view(["100", "110", "121"], suspect={0}), "USDC") == ALL_QUOTE


def test_the_recent_bars_are_the_last_unsuspect_ones_and_no_more():
    view = _view(["1", "2", "3", "4", "5"], suspect={0, 3})
    assert _recent(view, 2) == [view.bars[2], view.bars[4]]
    assert _recent(view, 3) == [view.bars[1], view.bars[2], view.bars[4]]
    assert _recent(view, 9) == [view.bars[1], view.bars[2], view.bars[4]]


def test_a_portfolio_worth_nothing_is_held():
    view = _view(["100", "110", "121"])
    assert STRATEGY.decide(view, _portfolio(view, "0", "0", "0")) == Hold()


def test_a_bar_in_the_window_without_a_price_for_a_token_raises():
    without_wbtc = Bar(time=FIRST_DAY, close_block=1, prices={"WETH": Decimal(100)}, base_fee_wei=1)
    view = MarketView((without_wbtc, bar(1, weth="110"), bar(2, weth="121")))
    with pytest.raises(ValueError, match=f"the bar at {FIRST_DAY} has no price for 'WBTC'"):
        STRATEGY.target(view, "USDC")


def test_the_same_view_and_portfolio_give_the_same_answer_whatever_was_decided_before():
    view = _view(["100", "100", "121"], ["1000", "900", "1300"])
    portfolio = _portfolio(view, "1000", "0", "0")
    first = STRATEGY.decide(view, portfolio)
    STRATEGY.decide(_view(["121", "110", "100"]), _portfolio(view, "0", "8", "0.01"))
    assert STRATEGY.decide(view, portfolio) == first
    assert TrendVolWeights.from_params(PARAMS).decide(view, portfolio) == first
