"""Integration layer: drive the unmodified TradingAgents engine.

The *entire* integration surface is one file (``docs/INTEGRATION.md``):

- :mod:`.trading_graph` — subclass that injects the perp context *and* the
  Phase 2 structured-output contract *in*; the engine's ``final_trade_decision``
  is parsed back *out* by :mod:`..domains.perp.target_decision` (the Phase 1
  rating adapter was retired with the Phase 2 contract migration).
- :mod:`.completion_usage` — the per-run completion measurement and its
  ``<payload>.usage.json`` sidecar (issue #182).
- :mod:`.decision_provider` — the daemons' :class:`~..ports.DecisionProvider`:
  builds each cycle's input payload and drives the engine to a parsed target.
- :mod:`.engine_drive` — one engine run, from the assembled prompt to a parsed
  target; the daemons' provider and the one-shot ``main.run_engine`` both run
  the engine through it.
- :mod:`.decision_reports` — the ``<payload>.reports.json`` sidecar: the
  analyst reports, debates and trader plan the engine produced on the way to
  ``final_trade_decision``, kept so a decision can be replayed later.

Nothing under ``tradingagents/`` is touched (Direction 2).
"""
