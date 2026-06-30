"""Hyperliquid perpetuals contrib module.

Drives the *unmodified* TradingAgents engine against Hyperliquid perps
(Direction 2 — plugin, zero edits to ``tradingagents/``). See ``docs/`` for the
full design. Phase 1 / Milestone A builds the data layer and the
``PerpMarketContext`` consumed by ``--context-only``.
"""
