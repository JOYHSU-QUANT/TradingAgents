"""``EngineRun.drive``: what it hands ``propagate`` (#392) and what it collects around it (#399)."""

from __future__ import annotations

import logging

from contrib.hyperliquid_perp.domains.perp.target_decision import DecisionConfig
from contrib.hyperliquid_perp.integration.completion_usage import CompletionUsageCollector
from contrib.hyperliquid_perp.integration.engine_drive import EngineRun


class _Graph:
    """Records the ``propagate`` call and answers a final state with no decision text."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def propagate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return ({"final_trade_decision": ""}, "hold")


def test_drive_hands_the_graph_yahoos_spelling_of_the_coin_not_the_bare_coin():
    # A bare ``BTC`` reaches yfinance as the Grayscale ETF; the graph gets
    # ``BTC-USD`` while the caller keeps naming the coin as the perp side does.
    graph = _Graph()
    run = EngineRun(graph, CompletionUsageCollector(), cap=None, analysts=["market"])
    parsed = run.drive(
        coin="BTC", trade_date="2026-11-03", decision_cfg=DecisionConfig(), payload_path=None
    )
    assert graph.calls == [(("BTC-USD", "2026-11-03"), {"asset_type": "crypto"})]
    # An empty decision text fails the contract as an invalid decision, not an error.
    assert not parsed.is_valid
    assert parsed.unavailable_inputs == ()


class _GraphWithoutTwoInputs(_Graph):
    """Answers like ``_Graph`` after the router has logged two optional categories as unavailable."""

    def propagate(self, *args, **kwargs):
        router = logging.getLogger("tradingagents.dataflows.interface")
        for category in ("news_data", "macro_data", "news_data"):
            router.warning(
                "Optional %s unavailable for %s: %s",
                category,
                "get_x",
                "down",
                extra={"unavailable_category": category},
            )
        router.warning("Vendor 'x' failed for get_y: boom")  # no category: not an input
        return super().propagate(*args, **kwargs)


def test_drive_records_the_optional_inputs_the_run_went_without():
    # The categories the router's sentinel lane named during the run ride the
    # parsed decision, sorted and once each (#399); a WARNING without the
    # category attribute is some other lane's and does not count.
    graph = _GraphWithoutTwoInputs()
    run = EngineRun(graph, CompletionUsageCollector(), cap=None, analysts=["market"])
    parsed = run.drive(
        coin="BTC", trade_date="2026-11-03", decision_cfg=DecisionConfig(), payload_path=None
    )
    assert parsed.unavailable_inputs == ("macro_data", "news_data")
    # The collector was detached after the run: nothing of this run's is
    # left on the router's logger to collect the next run's categories.
    router = logging.getLogger("tradingagents.dataflows.interface")
    assert not any(hasattr(h, "categories") for h in router.handlers)
