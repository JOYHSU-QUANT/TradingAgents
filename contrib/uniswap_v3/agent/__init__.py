"""The agent layer: asks the TradingAgents graph for a verdict on each traded token, and records it.

The one place in the package's code, its tests aside, that may import the
``tradingagents`` engine (``tests/test_isolation.py``), and it does so lazily, in :mod:`.graph`, so
that a command which asks no judge waits on none of the engine's
dependencies. Nothing here reaches ``domain/`` or ``ports.py`` with an
engine type: a verdict leaves this layer as the stdlib
:class:`~..domain.verdicts.VerdictRecord` the store takes.

- :mod:`.settings` — which judge: provider, models, analysts, completion cap
  (the config's ``agent`` section).
- :mod:`.tickers` — the token symbol to the ticker the graph analyses.
- :mod:`.context` — the spot context the graph is handed with the ticker:
  the recent closes from the store, and the role of the rating.
- :mod:`.graph` — the judge itself: the graph built over the config's
  settings, and the fake that answers without a model.
- :mod:`.record` — the sidecar beside the store, and the stored row.
- :mod:`.verdicts` — one visit's asking: which tokens, in what order,
  what is kept when a later one fails.
- :mod:`.errors` — what the layer raises, by what the caller should do.
"""
