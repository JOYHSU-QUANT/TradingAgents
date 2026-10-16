"""AutoResearch — a BTC timing-hypothesis research radar beside the paper trader.

The research radar is NOT a second trading system: it places no orders, owns
no positions, and never touches ``contrib.hyperliquid_perp``'s store or
schema. It reads BTC history into a store of its OWN
(:mod:`~contrib.autoresearch.store`), and its outputs into the live path are
two small JSON documents the trading package reads behind config switches
defaulting to off: one qualitative block of prompt text
(:mod:`~contrib.autoresearch.signal`, plan §7) and, since 2026-10-16, the
side of one fixed rule for the risk gate (:mod:`~contrib.autoresearch.guardrail`,
the trend guardrail decided 2026-10-02). Nothing numeric crosses either way.
That sentence is the package's scope test: a change that needs it softened
is out of scope, not a bigger feature.

What this package may borrow from outside itself (``contrib.hyperliquid_perp``,
the shared floor ``contrib.core`` and ``tradingagents``) is listed —
once, in one place — by :mod:`~contrib.autoresearch.upstream`, and every
other module here imports from THAT rather than reaching upstream itself
(``tests/test_upstream.py`` enforces it by reading the sources). The borrow
is strictly read-only: no module of this package writes to that package's
store, and none of its files change.
"""
