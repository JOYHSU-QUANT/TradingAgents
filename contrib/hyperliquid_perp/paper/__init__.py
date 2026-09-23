"""Phase 2 paper-trading engine: accounting, execution, SL/TP, liquidation model.

Persistence / accounting (PR2):

- :mod:`.config` — typed ``paper_trading:`` config (phase2-execution §5.4);
- :mod:`.accounting` — fill / funding posting, account formulas, replay (§6);
- :mod:`.liquidation` — estimated liquidation price over margin tiers (§6.6.1).

Execution engine (PR3):

- :mod:`.fill_model` — simulated taker fill price off mid ± slippage (§5.2 / §6.4)
  and the §5.2.1 maker post / fill rule;
- :mod:`.twap` — TWAP / flip slice-planning math (§1.2 / §6.2);
- :mod:`.stops` — stop-loss / take-profit price + gate math (§2–§4);
- :mod:`.engine` — the tick-driven orchestrator that composes them all (§1–§5).

Scheduler / acceptance (PR4):

- :mod:`.scheduler` — rolling 4h decision cycles + §3.1 retry (phase2-spec §3);
- :mod:`.reconcile` — restart reconciliation (phase2-execution §1.2);
- :mod:`.validation` — the spec §5 acceptance report and Phase-3 verdict.

The injectable clock, the snapshot provider, the run lease and the books
read are the live lane's too, and moved to :mod:`..runtime` (refactor plan
v2, T1); ``clock``, ``market_feed``, ``run_lock`` and ``position_facts``
here re-export them until plan PR 3 deletes the paths.
"""

from __future__ import annotations
