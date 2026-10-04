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

What exists so far is the skeleton, the chain reader, the bar store and the
engine's step: the value types, the pool price conversion, the routing and
the ledger (:mod:`.domain`), the ports, the address tables
(:mod:`.constants`), the config loader, the strategy registry, reads of
blocks, pool prices, quotes and the base fee from a node (:mod:`.chain`), an
SQLite store of each pool's reading at every bar boundary and of what each
run decided (:mod:`.store`), the step that decides one bar and an executor
that fills from the bar alone (:mod:`.engine`), and two commands
(:mod:`.cli`): ``backfill`` fills the store from an archive node and
``status`` prints what it holds. No command runs the engine yet, and nothing
here holds a key or signs a transaction.
"""
