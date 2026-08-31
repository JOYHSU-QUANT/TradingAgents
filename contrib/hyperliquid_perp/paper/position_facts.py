"""What the run's books say about its position — the input to the prompt's position section.

One read shared by the paper daemon and the live loop: both keep the same
``current_positions`` / ``current_account_state`` / ``fills`` tables, and the
``cli._provider._EngineDecisionProvider`` that renders the section runs in
both. Reads only; pricing is
:func:`..domains.perp.marginal_cost.build_position_context`'s.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..common.instants import parse_instant
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import PositionState

__all__ = ["BookPosition", "read_book_position"]


@dataclass(frozen=True)
class BookPosition:
    """The books' position facts: signed size, entry, wallet balance, newest fill.

    ``size == 0`` is flat (``entry_price`` then ``None``, as on
    :class:`~..persistence.models.PositionState`). ``wallet_balance`` is the
    ledger's — realized PnL, fees and funding already posted — so the caller
    adds only unrealized PnL at its own mark to reach equity.
    ``last_fill_at`` is the run's newest fill of ANY kind (the same
    ``fills.timestamp`` maximum ``ai_inputs.last_fill_time`` records), not
    the position's opening time — the books do not keep one, and "when did
    this position last change" is the honest fact for a churn-aware prompt.
    """

    size: Decimal
    entry_price: Decimal | None
    wallet_balance: Decimal
    last_fill_at: datetime | None


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
