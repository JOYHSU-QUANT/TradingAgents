"""What the run's books say about its position — the input to the prompt's position section.

One read shared by the paper daemon and the live loop: both keep the same
``current_positions`` / ``current_account_state`` / ``fills`` tables, and the
``cli._provider._EngineDecisionProvider`` that READS these books runs in
both. Reads only; the section is priced by
``domains.perp.context_builder.build_market_context`` and rendered by
``domains.perp.prompt_context``; the pricing itself is
:func:`..domains.perp.marginal_cost.build_position_context`'s.
"""

from __future__ import annotations

from ..common.instants import parse_instant

# The DTO itself moved to ``domains/`` — the context builder assembles the
# position section and must not import ``paper/`` — and is re-exported here,
# beside its only reader, so the import path callers already use keeps working
# (the same thin-shim shape as ``domains/perp/config_coercion.py``).
from ..domains.perp.marginal_cost import BookPosition
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import PositionState

__all__ = ["BookPosition", "read_book_position"]


def read_book_position(db: Database, run_id: str, coin: str) -> BookPosition | None:
    """The run's position facts, or ``None`` before ``initialize_run`` has run.

    ``None`` is the "no books yet" case only: a fresh run's provider is built
    pre-flight, before the run row exists, and only CALLED after the ledger
    is seeded — but the section must degrade to "omitted" rather than raise
    if that ordering ever changes.
    """
    ledger = repo.get_current_account_state(db.conn, run_id)
    if ledger is None:
        return None
    position = repo.get_current_position(db.conn, run_id, coin) or PositionState.flat(coin)
    raw = repo.last_fill_time(db.conn, run_id)
    return BookPosition(
        size=position.size,
        entry_price=position.entry_price,
        wallet_balance=ledger.wallet_balance,
        last_fill_at=None if raw is None else parse_instant(raw),
    )
