"""Integration layer: drive the unmodified TradingAgents engine.

The *entire* integration surface is two files (``docs/INTEGRATION.md``):

- :mod:`.trading_graph` — subclass that injects perp context *in*.
- :mod:`.decision_adapter` — maps the engine's 5-tier rating *out* into a
  :class:`~..domains.perp.decision.PerpTradeDecision`.

Nothing under ``tradingagents/`` is touched (Direction 2).
"""
