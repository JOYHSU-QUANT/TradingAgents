from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_btc_treasuries,
    get_economic_calendar,
    get_etf_flows,
    get_fear_greed,
    get_global_news,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.dataflows.interface import is_category_disabled


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_news,
            get_global_news,
            # macro_data and prediction_markets are also OPTIONAL_CATEGORIES, so
            # setting either to the "none" vendor would leave a bound tool that can
            # only return the disabled sentinel. They stay unguarded here because
            # their descriptions are welded into the single system_message string
            # below; un-advertising them means restructuring that prompt, which is
            # out of scope for this change. Folding all four into one table-driven
            # registration is the follow-up — worth doing before further optional
            # crypto tools are added to this block.
            get_macro_indicators,
            get_prediction_markets,
        ]

        # Crypto-only flows/sentiment/calendar/treasury tools. Bound only for
        # crypto assets so the stock path's tools and prompt are unchanged, and
        # only when the category is actually enabled — binding a tool whose
        # category is switched off would just spend a tool call to receive the
        # disabled sentinel.
        crypto_tools_message = ""
        if asset_type == "crypto":
            crypto_tools = []
            crypto_hints = []
            if not is_category_disabled("crypto_etf_flows", "get_etf_flows"):
                crypto_tools.append(get_etf_flows)
                crypto_hints.append(
                    "get_etf_flows(asset, curr_date, look_back_days) for BTC/ETH US "
                    "spot-ETF daily net flows (a demand-side signal) — only BTC and ETH "
                    "have US spot ETFs, so for another recognized crypto risk asset "
                    "(SOL, XRP, ...) it returns BTC flows as a market-wide proxy, which "
                    "you should treat as a market signal rather than that asset's own "
                    "flows; a stablecoin or unrecognized symbol returns a no-signal note"
                )
            if not is_category_disabled("crypto_sentiment", "get_fear_greed"):
                crypto_tools.append(get_fear_greed)
                crypto_hints.append(
                    "get_fear_greed(curr_date, look_back_days) for the Crypto Fear & "
                    "Greed Index (a 0-100 crowd-sentiment gauge)"
                )
            if not is_category_disabled("economic_calendar", "get_economic_calendar"):
                crypto_tools.append(get_economic_calendar)
                crypto_hints.append(
                    "get_economic_calendar(curr_date, look_back_days) for the US macro "
                    "calendar — upcoming CPI/NFP/PCE-style releases with forecasts and "
                    "the recent prints with surprises; treat event risk as a regime / "
                    "risk modifier, not a directional signal (the feed carries no Fed "
                    "rate-decision events, so never infer a quiet Fed from it)"
                )
            if not is_category_disabled("btc_treasuries", "get_btc_treasuries"):
                crypto_tools.append(get_btc_treasuries)
                crypto_hints.append(
                    "get_btc_treasuries(asset, curr_date, look_back_days) for corporate "
                    "BTC treasury holdings and disclosed buys/disposals of the largest "
                    "holders — an announcement-driven demand-side signal (for assets "
                    "other than BTC it is a market-wide proxy, not that asset's own "
                    "flows)"
                )
            if crypto_tools:
                tools = tools + crypto_tools
                crypto_tools_message = (
                    " Since this is a crypto asset, also use " + ", and ".join(crypto_hints) + "."
                )

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(query, start_date, end_date) for {asset_label}-specific or targeted news searches, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
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
            "news_report": report,
        }

    return news_analyst_node
