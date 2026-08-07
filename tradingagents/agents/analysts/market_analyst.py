from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_options_market,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.dataflows.interface import is_category_disabled


def create_market_analyst(llm):

    def market_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_stock_data,
            get_indicators,
            get_verified_market_snapshot,
        ]

        # Crypto-only options-volatility tool. Vol regime is a technical read, so
        # it lands on this analyst rather than the news one. Bound only for crypto
        # assets so the stock path's tools and prompt are unchanged, and only when
        # the category is enabled — binding a tool whose category is switched off
        # would just spend a tool call to receive the disabled sentinel.
        crypto_tools_message = ""
        if asset_type == "crypto" and not is_category_disabled(
            "options_data", "get_options_market"
        ):
            tools = tools + [get_options_market]
            crypto_tools_message = (
                "\n\nSince this is a crypto asset, also call get_options_market(asset, curr_date) "
                "for the options-implied volatility regime: the DVOL index with its 30-day "
                "min/max range and its 365-day percentile, ATM (50Δ) implied vol, and the "
                "25-delta risk reversal (RR25). Read DVOL's percentile as where the latest "
                "implied-vol reading sits versus the sample the report names on that line — it "
                "is a trailing year at most, and fewer readings when the feed is short or "
                "stalled, so cite the count rather than implying a full year. Read RR25 as "
                "which wing carries the higher implied vol — negative means the put wing does. "
                "RR25 is not comparable across tenors, so quote it with the expiry tenor the "
                "report prints. DVOL is the only figure in the report carrying a historical "
                "basis: Deribit publishes no options-chain history, so this vendor has no "
                "range and no percentile for ANY chain figure — not RR25, not ATM IV, not the "
                "25Δ wing vols — and none can be inferred from a single print. State RR25's "
                "sign, magnitude and tenor, and state ATM IV and the wing vols as levels; do "
                "NOT describe any of them as elevated, extreme, unusual, stretched or "
                "compressed. "
                "The report's Forward is Deribit's forward price for that "
                "expiry, not spot: it is EXPECTED to differ from the verified snapshot's price "
                "level, so do not reconcile the two and do not flag them as a discrepancy. "
                "DVOL and ATM IV are likewise not the same quantity, even though both are "
                "annualized vol points: DVOL is a model-free 30-day index built across the "
                "whole strike range, while ATM IV is one 50Δ point on a single listed expiry "
                "that may sit anywhere in the report's tenor band. The gap between them is "
                "driven by wing convexity and by that tenor difference, so do not reconcile "
                "those two either, and do not read their gap as a term structure or as a "
                "volatility risk premium. "
                "Report what the figures say rather than restating the tool's own wording, and "
                "respect its caveats: a range or a percentile is omitted when its own window "
                "holds too few readings to support one; the chain half is withheld entirely "
                "for a past analysis date, for an analysis date well ahead of the UTC clock, "
                "and for an asset this vendor reads no chain for, and it can additionally "
                "just fail to be read — in each case the report "
                "says so in place of the ATM/RR25 figures, and you must not substitute the "
                "current chain's or BTC's skew for them; if the report is headed as a "
                "market-wide proxy the DVOL figures are BTC's and no skew is served at all, "
                "so treat them as a crypto-wide volatility signal and never "
                "as that asset's own; and the DVOL level is always printed with an as-of date, "
                "so cite that date wherever you cite the level."
            )

        system_message = (
            """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

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
            + crypto_tools_message
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "market_report": report,
        }

    return market_analyst_node
