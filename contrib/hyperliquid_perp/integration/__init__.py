"""Integration layer: drive the unmodified TradingAgents engine.

The *entire* integration surface is one file (``docs/INTEGRATION.md``):

- :mod:`.trading_graph` — subclass that injects the perp context *and* the
  Phase 2 structured-output contract *in*; the engine's ``final_trade_decision``
  is parsed back *out* by :mod:`..domains.perp.target_decision` (the Phase 1
  rating adapter was retired with the Phase 2 contract migration).

Nothing under ``tradingagents/`` is touched (Direction 2).
"""
