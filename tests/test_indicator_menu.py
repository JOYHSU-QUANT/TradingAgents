"""The market analyst's indicator menu is rendered from the shared table (#187).

The system prompt used to carry the indicator descriptions as a third verbatim
copy of ``utils.INDICATOR_DESCRIPTIONS`` (the two report lanes were the first
two, #137), editable apart from them. The prompt is the analyst's INPUT, and a
changed input is a segmentation point for every running paper run, so the
derivation is held to the literal it replaced byte for byte — through the move
of the grouping up to the agent layer, which changed no text (#219).
"""

import pytest

import tradingagents.dataflows.utils as utils
from tradingagents.agents.analysts.market_analyst import _system_message_head
from tradingagents.agents.utils.indicator_menu import (
    INDICATOR_MENU,
    INDICATOR_MENU_OMITS,
    indicator_menu,
)
from tradingagents.dataflows.utils import INDICATOR_DESCRIPTIONS

# The system message head as market_analyst.py carried it, verbatim, before
# the menu was derived (the literal at PR #216's HEAD). This is the pin: it is
# NOT to be regenerated from the code it pins. Changing what the analyst reads
# is a decision — made here, in the open, by editing this literal alongside
# the table — never a side effect of editing a description.
_HEAD_BEFORE_DERIVATION = """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names.

Before writing the final report, call get_verified_market_snapshot for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices.

Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""


def _menu_slice(head: str) -> str:
    start = head.index("Moving Averages:")
    end = head.index("\n\n- Select indicators")
    return head[start:end]


@pytest.mark.unit
def test_the_prompt_head_is_byte_identical_to_the_literal_it_replaced():
    assert _system_message_head() == _HEAD_BEFORE_DERIVATION


@pytest.mark.unit
def test_the_menu_is_the_literal_menu():
    # The slice, so a failure names the menu rather than the whole prompt.
    assert indicator_menu() == _menu_slice(_HEAD_BEFORE_DERIVATION)


@pytest.mark.unit
def test_the_menu_follows_the_shared_table(monkeypatch):
    # Alter one sentence through the module-level name and both the menu and
    # the prompt built from it must carry the alteration — a re-inlined copy
    # (the pre-#187 shape) would keep rendering its own sentence.
    marked = dict(INDICATOR_DESCRIPTIONS)
    marked["rsi"] = "MARKER: the shared table was read for this line"
    monkeypatch.setattr(utils, "INDICATOR_DESCRIPTIONS", marked)
    assert "- rsi: MARKER: the shared table was read for this line" in indicator_menu()
    assert "- rsi: MARKER: the shared table was read for this line" in _system_message_head()
    assert _system_message_head() != _HEAD_BEFORE_DERIVATION


@pytest.mark.unit
def test_the_menu_and_the_declared_omissions_partition_the_table():
    # A newly described indicator must be placed — in a category, or in the
    # omissions — by whoever adds it: joining the menu silently would change
    # the analyst's input, missing it silently would hide a servable one.
    listed = [key for _, keys in INDICATOR_MENU for key in keys]
    assert len(listed) == len(set(listed))
    assert set(listed) | INDICATOR_MENU_OMITS == set(INDICATOR_DESCRIPTIONS)
    assert not set(listed) & INDICATOR_MENU_OMITS
    assert {"mfi"} == INDICATOR_MENU_OMITS  # the one indicator the prompt has never offered


@pytest.mark.unit
def test_every_listed_description_is_one_line():
    # The menu is line-shaped ("- name: description"); a description with a
    # line break would open a new bullet the prompt never wrote.
    for _, keys in INDICATOR_MENU:
        for key in keys:
            assert "\n" not in INDICATOR_DESCRIPTIONS[key], key
