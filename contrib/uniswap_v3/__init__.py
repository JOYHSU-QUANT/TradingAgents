"""Uniswap v3 spot execution: one engine for backtest, paper, fork and live.

A strategy answers one question, "what should the portfolio's weights be",
and the engine turns the answer into Uniswap v3 swaps. The same engine step
is meant to run in every :class:`~.domain.types.RunMode`; a mode only chooses
which adapters are wired behind :mod:`.ports`.

The package is isolated: it imports no other package under ``contrib/`` and
none of them imports it (``tests/test_isolation.py`` reads the sources to
hold that). It is also strategy-agnostic: a strategy enters only through
:class:`~.ports.Strategy`. Two ship here: ``fixed_weights``, a placeholder
that drives the engine and its tests, and ``trend_vol_weights``, a rule
strategy that holds a token while it trends, sized to a volatility target.

What exists so far is the skeleton, the chain reader, the bar store, the
engine's step, the backtest and the paper run: the value types, the pool price conversion,
the routing, the ledger and the run metrics (:mod:`.domain`), the ports,
the address tables
(:mod:`.constants`), the config loader, the strategy registry, reads of
blocks, pool prices, quotes and the base fee from a node (:mod:`.chain`), an
SQLite store of each pool's reading at every bar boundary, of what an
outside judge said of each token there (verdicts, which a config may have
the strategy's view carry) and of what each run decided (:mod:`.store`),
the step that decides one bar, an executor
that fills from the bar alone, one that fills from the pools' quotes, and
the loop that replays stored bars through the step (:mod:`.engine`), a
paper run's visit (:mod:`.paper`), and six commands (:mod:`.cli`):
``backfill`` fills the store from an archive node, ``status`` prints what
it holds, ``backtest`` replays stored bars through the engine, ``paper``
reads and decides the bars the chain has closed since a run's last,
``fork`` replays stored bars signing each swap on a local anvil fork, and
``report`` prints a run's return, drawdown and costs. Only a fork run
signs, and only with anvil's development accounts on a fork it has
checked (:mod:`.chain`); nothing here holds a key of its own.
"""
