"""The news analyst must bind the crypto-only flows/sentiment tools
(get_etf_flows, get_fear_greed) only when the asset is crypto, and never for a
stock — and the news ToolNode must be able to execute them when bound.

A fake LLM captures the tools passed to bind_tools so the wiring is asserted
without any network or real model.
"""

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.dataflows.config import set_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

_CRYPTO_TOOLS = {"get_etf_flows", "get_fear_greed"}


class _CapturingLLM:
    """Records the tools bound to it; its bound runnable returns a no-tool-call reply."""

    def __init__(self):
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)
        return RunnableLambda(lambda _inp: AIMessage(content="ok"))


def _run(asset_type, ticker):
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
    return {t.name for t in llm.bound_tools}


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
def test_news_toolnode_can_execute_crypto_tools():
    # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    news_tools = set(nodes["news"].tools_by_name)
    assert news_tools >= _CRYPTO_TOOLS, (
        "crypto flows/sentiment tools are bound to the news analyst for crypto "
        "assets but not registered in the news ToolNode, so the model's call fails."
    )
