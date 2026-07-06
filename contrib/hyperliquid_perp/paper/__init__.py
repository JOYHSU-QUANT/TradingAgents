"""Phase 2 paper-trading engine: accounting, funding, liquidation model.

- :mod:`.config` — typed ``paper_trading:`` config (phase2-execution §5.4);
- :mod:`.accounting` — fill / funding posting, account formulas, replay (§6);
- :mod:`.liquidation` — estimated liquidation price over margin tiers (§6.6.1).
"""

from __future__ import annotations
