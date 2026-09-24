"""The execution kernel the paper and live lanes share (refactor plan v2, T1).

Sits above ``persistence`` and below ``paper`` / ``live``: these modules read
the store (the run lease, the books, the no-decision policy) and so cannot
live in ``common``, and none of them is owned by either lane. Single-venue
by design: the kernel may name the Hyperliquid adapter's error family
(``exchanges.hyperliquid.errors``) directly, as the snapshot provider does to
sort a raise into ERROR or DEFECT; a second venue is not in scope.

- :mod:`.accounting` — the §6 account formulas, the two fill effects (modelled
  and exchange-basis), the run genesis and the spec §5 accounting replay;
- :mod:`.clock` — the two clocks behind the :class:`~..ports.Clock` seam;
- :mod:`.market_feed` — market-data snapshots with freshness accounting, the
  providers behind :class:`~..ports.SnapshotProvider`;
- :mod:`.asset_spec` — the per-asset metadata an engine needs, the two
  precision steps derived from ``szDecimals``, and the venue read that
  builds it;
- :mod:`.decision` — what one AI call sees (``DecisionInput``) and the
  retryable-failure type of the :class:`~..ports.DecisionProvider` seam;
- :mod:`.position_facts` — the one read of the books behind the prompt's
  position section and the ``ai_inputs`` row;
- :mod:`.run_identity` — opening the store a daemon owns and settling which
  run it is (fresh under ``--create``, or a restart);
- :mod:`.genesis` — the run row, opening ledger and seed positions a
  ``--create`` writes once;
- :mod:`.run_lock` — the single-instance lease per run;
- :mod:`.no_decision` — the no-decision escalation policy both validators
  and both running loops apply.

This ``__init__`` imports nothing: the modules are imported by name, and a
package-level import would put every one of them in every importer's
load-time closure (the layering tests pin those closures).
"""
