"""The news analyst must bind the crypto-only tools (get_etf_flows,
get_fear_greed, get_btc_treasuries) only when the asset is crypto, and never
for a stock — and the news ToolNode must be able to execute them when bound.
get_economic_calendar is the exception: it takes no asset argument and is
bound on both paths. Each is also gated on its own category being enabled,
so a category disabled with "none" binds nothing.

A fake LLM captures the tools passed to bind_tools so the wiring is asserted
without any network or real model.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.dataflows.config import set_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

_CRYPTO_TOOLS = {"get_etf_flows", "get_fear_greed"}

# The SoSoValue calendar/treasuries categories shipped disabled ("none") and
# were cut over to "sosovalue" on 2026-09-02, so a default-config run binds
# them.
_CUTOVER_TOOLS = {"get_economic_calendar", "get_btc_treasuries"}


class _CapturingLLM:
    """Records the tools bound to it; its bound runnable returns a no-tool-call reply."""

    def __init__(self):
        self.bound_tools = None
        self.prompt = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)

        def _capture(inp):
            # The rendered prompt: binding a tool without advertising it in the
            # system message leaves the model a tool it has no reason to call.
            self.prompt = str(inp)
            return AIMessage(content="ok")

        return RunnableLambda(_capture)


def _bind(asset_type, ticker):
    llm = _CapturingLLM()
    node = create_news_analyst(llm)
    node(
        {
            "trade_date": "2026-07-23",
            "asset_type": asset_type,
            "company_of_interest": ticker,
            "messages": [],
        }
    )
    return llm


def _run(asset_type, ticker):
    return {t.name for t in _bind(asset_type, ticker).bound_tools}


@pytest.mark.unit
def test_crypto_binds_flows_and_sentiment_tools():
    bound = _run("crypto", "BTC-USD")
    assert bound >= _CRYPTO_TOOLS
    # the shared news tools are still present
    assert bound >= {"get_news", "get_global_news"}


@pytest.mark.unit
def test_stock_does_not_bind_crypto_tools():
    bound = _run("stock", "AAPL")
    assert not (_CRYPTO_TOOLS & bound)
    assert bound >= {"get_news", "get_global_news"}


@pytest.mark.unit
def test_disabled_category_is_not_bound():
    # A category switched off with the "none" vendor must not be bound at all:
    # binding it would spend a tool call only to receive the disabled sentinel.
    # The other crypto category must be unaffected.
    set_config({"data_vendors": {"crypto_etf_flows": "none"}})
    try:
        bound = _run("crypto", "BTC-USD")
        assert "get_etf_flows" not in bound
        assert "get_fear_greed" in bound
    finally:
        set_config({"data_vendors": {"crypto_etf_flows": "farside"}})


@pytest.mark.unit
def test_disabling_the_cutover_categories_unbinds_them():
    # The 2026-09-02 cutover is a default flip, not a hard-wire: switching the
    # two categories back to "none" must drop the bindings again on both
    # paths, same as any other disabled category. No restore needed: the
    # autouse ``_isolate_config`` fixture resets the config after every test.
    set_config({"data_vendors": {"economic_calendar": "none", "btc_treasuries": "none"}})
    assert not (_CUTOVER_TOOLS & _run("crypto", "BTC-USD"))
    assert not (_CUTOVER_TOOLS & _run("stock", "AAPL"))


@pytest.mark.unit
def test_the_calendar_binds_on_both_paths_but_treasuries_stays_crypto():
    # Default config since the 2026-09-02 cutover — no YAML edit involved.
    # The macro calendar takes no asset argument and CPI/NFP/PCE releases are
    # event risk for equities too, so it is the one category here bound outside
    # the crypto block. Treasuries is a BTC-holdings feed and stays crypto-only:
    # asserting both in one test is what stops the split being widened by
    # accident in either direction.
    assert _run("crypto", "BTC-USD") >= _CUTOVER_TOOLS
    stock = _run("stock", "AAPL")
    assert "get_economic_calendar" in stock
    assert "get_btc_treasuries" not in stock


@pytest.mark.unit
def test_the_stock_path_is_told_about_the_calendar_it_now_carries():
    # The hint moved out of the crypto-only block along with the binding, and
    # both halves have to land: a bound-but-unadvertised tool is one the model
    # never calls, which would read exactly like the category still being off.
    llm = _bind("stock", "AAPL")
    assert "get_economic_calendar" in {t.name for t in llm.bound_tools}
    assert "get_economic_calendar(curr_date, look_back_days)" in llm.prompt
    # The stock path must not pick up the crypto preamble along with it —
    # with a positive counterpart on the same needle, or rewording the
    # preamble would make the negative assertion vacuously true.
    assert "Since this is a crypto asset" not in llm.prompt
    assert "Since this is a crypto asset" in _bind("crypto", "BTC-USD").prompt


@pytest.mark.unit
def test_disabled_base_category_is_neither_bound_nor_advertised():
    # macro_data and prediction_markets used to be bound unconditionally, with
    # their descriptions welded into the opening sentence — switching either
    # category to "none" left the model a bound, advertised tool that could
    # only return the disabled sentinel. The table-driven registration closed
    # that: the gate now drops both the binding and the hint together.
    set_config({"data_vendors": {"macro_data": "none"}})
    try:
        llm = _bind("stock", "AAPL")
        assert "get_macro_indicators" not in {t.name for t in llm.bound_tools}
        assert "get_macro_indicators(indicator, curr_date, look_back_days)" not in llm.prompt
        # its sibling in the opening sentence is untouched
        assert "get_prediction_markets(topic, limit, curr_date)" in llm.prompt
    finally:
        set_config({"data_vendors": {"macro_data": "fred"}})
    # Positive control on the same needles: with the category back on, the
    # binding and the hint both return — so the negative assertions above
    # cannot go vacuously true if the hint wording is ever rewritten.
    llm = _bind("stock", "AAPL")
    assert "get_macro_indicators" in {t.name for t in llm.bound_tools}
    assert "get_macro_indicators(indicator, curr_date, look_back_days)" in llm.prompt


@pytest.mark.unit
def test_every_optional_table_row_is_registered_in_the_news_toolnode():
    # The invariant the shared table exists to enforce: a category added to
    # OPTIONAL_NEWS_TOOLS is thereby registered in the news ToolNode, so the
    # analyst can never bind a tool whose call is not executable.
    from tradingagents.agents.analysts.news_analyst import OPTIONAL_NEWS_TOOLS

    nodes = TradingAgentsGraph._create_tool_nodes(None)
    news_tools = set(nodes["news"].tools_by_name)
    assert news_tools >= {entry.tool.name for entry in OPTIONAL_NEWS_TOOLS}


@pytest.mark.unit
def test_table_validation_rejects_a_wrong_category_and_a_wrong_scope():
    # A typo'd category fails OPEN at the gate (an unknown category is never
    # "disabled"), so the import-time validator must be the thing that
    # catches it — and the current table must pass the same validator.
    from tradingagents.agents.analysts.news_analyst import (
        OPTIONAL_NEWS_TOOLS,
        _validate_table,
    )

    _validate_table(OPTIONAL_NEWS_TOOLS)  # the shipped table passes
    good = OPTIONAL_NEWS_TOOLS[0]
    with pytest.raises(ValueError, match="names category"):
        _validate_table([good._replace(category="macro_dta")])
    with pytest.raises(ValueError, match="scope"):
        _validate_table([good._replace(scope="bsae")])


@pytest.mark.unit
def test_news_toolnode_can_execute_crypto_tools():
    # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    news_tools = set(nodes["news"].tools_by_name)
    # The cutover tools are registered here too: the default-config binding
    # must be executable, and re-disabling a category never unregisters it.
    assert news_tools >= _CRYPTO_TOOLS | _CUTOVER_TOOLS, (
        "crypto flows/sentiment/calendar/treasury tools are bound to the news "
        "analyst for crypto assets but not registered in the news ToolNode, so "
        "the model's call fails."
    )


@pytest.mark.unit
def test_each_cutover_category_gates_only_its_own_tool():
    # Each flag must gate its own tool alone. Disabling one category at a time
    # is what exposes a guard that reads the sibling category's key: that
    # mutation drops both tools from one flip, or neither.
    for category, tool in (
        ("economic_calendar", "get_economic_calendar"),
        ("btc_treasuries", "get_btc_treasuries"),
    ):
        set_config({"data_vendors": {category: "none"}})
        try:
            bound = _run("crypto", "BTC-USD")
            assert tool not in bound, f"{category} disabled but {tool} was still bound"
            assert (_CUTOVER_TOOLS - {tool}) <= bound, (
                f"only {category} was disabled, but the sibling cutover tool dropped too"
            )
        finally:
            # Intra-test restore: the autouse config-reset fixture only runs
            # between tests, and the next iteration needs this category back
            # at its default for the sibling assertion to hold.
            set_config({"data_vendors": {category: DEFAULT_CONFIG["data_vendors"][category]}})
