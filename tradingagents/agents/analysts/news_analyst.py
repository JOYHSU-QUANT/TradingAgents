from typing import NamedTuple

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
from tradingagents.dataflows.interface import (
    get_category_for_method,
    is_category_disabled,
)


class _OptionalNewsTool(NamedTuple):
    """One optional-category tool the news analyst may bind and advertise.

    ``scope`` places the hint in the prompt: ``"base"`` joins the always-on
    tool list in the opening sentence, ``"calendar"`` is its own appended
    sentence (asset-agnostic, both paths), ``"crypto"`` joins the
    crypto-only preamble — and ``"crypto"`` also gates *binding* by asset
    type (crypto rows are skipped entirely on a stock run). The category
    gate is the same for all three — binding a tool whose category is
    switched off would just spend a tool call to receive the disabled
    sentinel.

    Hint format follows the scope: ``"base"`` and ``"crypto"`` hints are
    sentence *fragments* (``_join_hints`` supplies the commas and the final
    "and"; the surrounding wrapper adds the framing text), while a
    ``"calendar"`` hint is a *complete sentence* carrying its own leading
    space and final period. Pasting a fragment into a calendar row (or vice
    versa) silently malforms the prompt.
    """

    tool: object
    category: str
    scope: str
    hint: str


# The one registration table: each optional category appears exactly once,
# with its binding gate and its prompt hint side by side, and the news
# ToolNode in trading_graph registers from the same rows — so a new category
# cannot be bound here and forgotten there (or vice versa), and none of the
# entries can end up bound but unadvertised or advertised but gated off.
# Scope note: this table governs the GATED categories only. The always-on
# news tools (get_news / get_global_news here, plus get_insider_transactions
# in the ToolNode) stay hand-maintained in each consumer — the no-divergence
# guarantee does not extend to them.
# Row order is binding order, which the prompt's {tool_names} echoes.
OPTIONAL_NEWS_TOOLS = (
    _OptionalNewsTool(
        tool=get_macro_indicators,
        category="macro_data",
        scope="base",
        hint=(
            "get_macro_indicators(indicator, curr_date, look_back_days) to ground macro "
            "commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', "
            "'fed_funds_rate', '10y_treasury', 'yield_curve')"
        ),
    ),
    _OptionalNewsTool(
        tool=get_prediction_markets,
        category="prediction_markets",
        scope="base",
        hint=(
            "get_prediction_markets(topic, limit, curr_date) for live market-implied "
            "probabilities of forward-looking events (e.g. 'Fed rate cut', "
            "'recession 2026', geopolitical or sector events); always pass today's "
            "date as curr_date so the report can flag prices newer than the "
            "analysis date"
        ),
    ),
    # The macro calendar is asset-agnostic and bound on BOTH paths: it takes
    # no asset argument, and CPI/NFP/PCE/GDP releases are canonical event
    # risk for equities every bit as much as for crypto — the one category
    # here whose description carries no "(crypto)" marker.
    _OptionalNewsTool(
        tool=get_economic_calendar,
        category="economic_calendar",
        scope="calendar",
        hint=(
            " Also use get_economic_calendar(curr_date, look_back_days) for the US "
            "macro calendar — upcoming CPI/NFP/PCE-style releases with forecasts and "
            "the recent prints with surprises; treat event risk as a regime / risk "
            "modifier, not a directional signal (the feed carries no Fed "
            "rate-decision events, so never infer a quiet Fed from it)."
        ),
    ),
    # Crypto-only flows/sentiment/treasury tools, bound only for crypto assets
    # so the stock path's tools and prompt are unchanged.
    _OptionalNewsTool(
        tool=get_etf_flows,
        category="crypto_etf_flows",
        scope="crypto",
        hint=(
            "get_etf_flows(asset, curr_date, look_back_days) for BTC/ETH US "
            "spot-ETF daily net flows (a demand-side signal) — only BTC and ETH "
            "have US spot ETFs, so for another recognized crypto risk asset "
            "(SOL, XRP, ...) it returns BTC flows as a market-wide proxy, which "
            "you should treat as a market signal rather than that asset's own "
            "flows; a stablecoin or unrecognized symbol returns a no-signal note"
        ),
    ),
    _OptionalNewsTool(
        tool=get_fear_greed,
        category="crypto_sentiment",
        scope="crypto",
        hint=(
            "get_fear_greed(curr_date, look_back_days) for the Crypto Fear & "
            "Greed Index (a 0-100 crowd-sentiment gauge)"
        ),
    ),
    _OptionalNewsTool(
        tool=get_btc_treasuries,
        category="btc_treasuries",
        scope="crypto",
        hint=(
            "get_btc_treasuries(asset, curr_date, look_back_days) for corporate "
            "BTC treasury holdings and disclosed buys/disposals of the largest "
            "holders — an announcement-driven demand-side signal (for assets "
            "other than BTC it is a market-wide proxy, not that asset's own "
            "flows)"
        ),
    ),
)


def _validate_table(table):
    """Reject a mis-authored table row at import time.

    A typo'd category would fail OPEN — an unknown category is never
    disabled by category-level config (the vendor lookup falls back to
    "default"; only a tool-level "none" override would still catch it), so
    the tool would stay bound while the user's real config said none — and a
    typo'd scope would only surface on the first node run. Fail where the
    table edit happens instead.
    """
    for entry in table:
        if entry.scope not in ("base", "calendar", "crypto"):
            raise ValueError(f"unknown OPTIONAL_NEWS_TOOLS scope {entry.scope!r}")
        mapped = get_category_for_method(entry.tool.name)
        if mapped != entry.category:
            raise ValueError(
                f"OPTIONAL_NEWS_TOOLS row {entry.tool.name!r} names category "
                f"{entry.category!r} but the vendor config maps that method to "
                f"{mapped!r}"
            )


_validate_table(OPTIONAL_NEWS_TOOLS)


def _join_hints(hints: list[str]) -> str:
    """Join tool hints with "and" before the LAST hint only.

    A plain ", and ".join reads "A, and B, and C, and D" once more than two
    hints are present, and each hint already carries its own commas,
    semicolons and em-dashes — the repeated conjunction makes the boundaries
    between hints unreadable. Two hints read "A, and B" (the general branch
    already produces that; only the single-hint case needs its own return).
    """
    if len(hints) == 1:
        return hints[0]
    return ", ".join(hints[:-1]) + ", and " + hints[-1]


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        tools = [get_news, get_global_news]
        base_hints = [
            f"get_news(query, start_date, end_date) for {asset_label}-specific or "
            f"targeted news searches",
            "get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news",
        ]
        calendar_messages = []
        crypto_hints = []
        for entry in OPTIONAL_NEWS_TOOLS:
            if entry.scope == "crypto" and asset_type != "crypto":
                continue
            # entry.tool.name IS the vendor-config method name — one identity,
            # not a hand-synced pair, so the gate cannot consult a method the
            # row's tool does not implement.
            if is_category_disabled(entry.category, entry.tool.name):
                continue
            tools.append(entry.tool)
            if entry.scope == "base":
                base_hints.append(entry.hint)
            elif entry.scope == "calendar":
                calendar_messages.append(entry.hint)
            elif entry.scope == "crypto":
                crypto_hints.append(entry.hint)
            else:
                # Unreachable for a table _validate_table accepted; kept so a
                # scope added to that allowlist without a branch here fails
                # loud instead of silently dropping the hint while the tool
                # still binds.
                raise ValueError(f"unknown OPTIONAL_NEWS_TOOLS scope {entry.scope!r}")
        crypto_tools_message = (
            " Since this is a crypto asset, also use " + _join_hints(crypto_hints) + "."
            if crypto_hints
            else ""
        )

        system_message = (
            "You are a news researcher tasked with analyzing recent news and trends over "
            "the past week. Please write a comprehensive report of the current state of "
            "the world that is relevant for trading and macroeconomics. Use the "
            "available tools: " + _join_hints(base_hints) + ". Provide specific, "
            "actionable insights with supporting evidence to help traders make informed "
            "decisions."
            + "".join(calendar_messages)
            + crypto_tools_message
            + " Make sure to append a Markdown table at the end of the report to "
            "organize key points in the report, organized and easy to read."
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
