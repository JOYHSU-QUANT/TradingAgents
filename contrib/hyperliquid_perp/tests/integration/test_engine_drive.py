"""``EngineRun.drive``: what it hands ``propagate`` (#392)."""

from __future__ import annotations

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
