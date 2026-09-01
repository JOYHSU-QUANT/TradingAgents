"""What the run's books say — ONE read behind the prompt's position section and the audit row.

Shared by the paper daemon and the live loop: both keep the same
``current_positions`` / ``current_account_state`` / ``fills`` tables, and the
``cli._provider._EngineDecisionProvider`` that READS these books runs in
both. The read happens once per cycle, before the market fetch: the provider
hands the domain half (:class:`BookPosition`) to the context builder, which
prices the ``Position:`` section, and carries the whole :class:`BookFacts` on
the ``DecisionInput`` so the driver's ``ai_inputs`` prologue writes the same
books the prompt was built from instead of reading the three tables a second
time (issue #134). Reads only; the section is priced by
``domains.perp.context_builder.build_market_context`` and rendered by
``domains.perp.prompt_context``; the pricing itself is
:func:`..domains.perp.marginal_cost.build_position_context`'s.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..common.instants import parse_instant

# The prompt-side DTO lives in ``domains/`` — the context builder assembles the
# position section and must not import ``paper/`` — and is re-exported here,
# beside its only producer, so the import path callers already use keeps
# working.
from ..domains.perp.marginal_cost import BookPosition
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import AccountLedger, PositionState

__all__ = ["BookFacts", "BookPosition", "BookSource", "read_books"]


@dataclass(frozen=True)
class BookFacts:
    """The three books as read together: ledger, position, newest fill stamp.

    Everything the ``ai_inputs`` row needs from the store's account side —
    the ledger's four accumulators, the position row (with its realized PnL
    and the exchange-mirrored liquidation estimate), and the newest fill's
    STORAGE-form timestamp, carried verbatim so ``ai_inputs.last_fill_time``
    keeps the identical bytes the fill was booked with. The prompt needs a
    strict subset, exposed as :attr:`position_facts`. ``position`` is the
    materialized row or ``PositionState.flat(coin)`` — the flat case is a
    ``size = 0`` value, never an absent one.
    """

    ledger: AccountLedger
    position: PositionState
    last_fill_time: str | None

    @property
    def position_facts(self) -> BookPosition:
        """The domain-side view the context builder prices the section from."""
        raw = self.last_fill_time
        return BookPosition(
            size=self.position.size,
            entry_price=self.position.entry_price,
            wallet_balance=self.ledger.wallet_balance,
            last_fill_at=None if raw is None else parse_instant(raw),
        )


# How a wiring hands the daemon provider its books: bound over the run's store
# at construction, called once per cycle (the fresh-run provider is built
# before the ledger is seeded, so the read must be able to answer "no books
# yet"). Named so the seam is a declared type rather than a bare callable
# passed by keyword (issue #134).
BookSource = Callable[[], "BookFacts | None"]


def read_books(db: Database, run_id: str, coin: str) -> BookFacts | None:
    """The run's books, or ``None`` before ``initialize_run`` has run.

    ``None`` is the "no books yet" case only: a fresh run's provider is built
    pre-flight, before the run row exists, and only CALLED after the ledger
    is seeded (``initialize_run`` writes the run row and the ledger in one
    transaction, before the scheduler is built). Should that ordering ever
    change, this read answers ``None`` rather than raising — the provider
    logs it and hands the builder a position-blind context — and the
    driver's audit prologue then refuses the cycle on the same missing
    ledger, so the cycle ends ``api_failed`` rather than silently
    position-blind. Three reads, one per table, and the only place in the
    cycle that makes them.
    """
    ledger = repo.get_current_account_state(db.conn, run_id)
    if ledger is None:
        return None
    position = repo.get_current_position(db.conn, run_id, coin) or PositionState.flat(coin)
    return BookFacts(
        ledger=ledger,
        position=position,
        last_fill_time=repo.last_fill_time(db.conn, run_id),
    )
