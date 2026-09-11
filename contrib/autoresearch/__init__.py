"""AutoResearch — a BTC timing-hypothesis research radar beside the paper trader.

The research radar is NOT a second trading system: it places no orders, owns
no positions, and never touches ``contrib.hyperliquid_perp``'s store or
schema. It reads BTC history into a store of its OWN
(:mod:`~contrib.autoresearch.store`), and its only eventual output into the
live path is one qualitative block of prompt text, behind a config switch,
much later (plan §7). That sentence is the package's scope test: a change
that needs it softened is out of scope, not a bigger feature.

What this package may borrow from ``contrib.hyperliquid_perp`` is listed —
once, in one place — by :mod:`~contrib.autoresearch.upstream`, and every
other module here imports from THAT rather than reaching upstream itself
(``tests/test_upstream.py`` enforces it by reading the sources). The borrow
is strictly read-only: no module of this package writes to that package's
store, and none of its files change.
"""
