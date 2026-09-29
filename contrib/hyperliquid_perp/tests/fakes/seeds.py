"""Seeds for the accounting tests: a BTC position and a paper run's genesis rows."""

from __future__ import annotations

from decimal import Decimal

from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.runtime import accounting


def long_position(size, entry, realized="0") -> PositionState:
    return PositionState(
        coin="BTC", size=Decimal(size), entry_price=Decimal(entry), realized_pnl=Decimal(realized)
    )


def init_paper_run(db, balance="1000", positions=()):
    accounting.initialize_run(
        db,
        run_id="r1",
        mode="paper",
        initial_balance_usdc=Decimal(balance),
        schema_version=1,
        initial_positions=positions,
    )
