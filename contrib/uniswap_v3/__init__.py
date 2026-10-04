"""Uniswap v3 spot execution: one engine for backtest, paper, fork and live.

A strategy answers one question, "what should the portfolio's weights be",
and the engine turns the answer into Uniswap v3 swaps. The same engine step
is meant to run in every :class:`~.domain.types.RunMode`; a mode only chooses
which adapters are wired behind :mod:`.ports`.

The package is isolated: it imports no other package under ``contrib/`` and
none of them imports it (``tests/test_isolation.py`` reads the sources to
hold that). It is also strategy-agnostic: a strategy enters only through
:class:`~.ports.Strategy`, and the one strategy shipped here,
``fixed_weights``, is a placeholder that drives the engine and its tests.

What exists so far is the skeleton and the chain reader: the value types and
the pool price conversion (:mod:`.domain`), the ports, the address tables
(:mod:`.constants`), the config loader, the strategy registry, and reads of
blocks, pool prices, quotes and the base fee from a node (:mod:`.chain`).
There is no engine yet, and nothing here holds a key or signs a transaction.
"""
