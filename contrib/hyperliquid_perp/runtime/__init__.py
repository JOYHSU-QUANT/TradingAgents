"""The execution kernel the paper and live lanes share (refactor plan v2, T1).

Sits above ``persistence`` and below ``paper`` / ``live``: these modules read
the store (the run lease, the books, the no-decision policy) and so cannot
live in ``common``, and none of them is owned by either lane.

- :mod:`.clock` — the two clocks behind the :class:`~..ports.Clock` seam;
- :mod:`.market_feed` — market-data snapshots with freshness accounting, the
  providers behind :class:`~..ports.SnapshotProvider`;
- :mod:`.asset_spec` — the per-asset metadata an engine needs, and the two
  precision steps derived from ``szDecimals``;
- :mod:`.decision` — what one AI call sees (``DecisionInput``) and the
  retryable-failure type of the :class:`~..ports.DecisionProvider` seam;
- :mod:`.position_facts` — the one read of the books behind the prompt's
  position section and the ``ai_inputs`` row;
- :mod:`.run_lock` — the single-instance lease per run;
- :mod:`.no_decision` — the no-decision escalation policy both validators
  and both running loops apply.

This ``__init__`` imports nothing: the modules are imported by name, and a
package-level import would put every one of them in every importer's
load-time closure (the layering tests pin those closures).
"""
