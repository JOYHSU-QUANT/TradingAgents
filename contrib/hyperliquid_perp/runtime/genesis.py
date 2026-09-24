"""The run genesis a ``--create`` writes once, the one way both lanes write it.

:func:`write_genesis` is the seam between a lane's genesis INPUTS — the
opening balance, the positions to seed, the config subset to record — and
:func:`~.accounting.initialize_run`, which owns the transaction. The paper
lane seeds from ``paper_trading.account``; the live lane seeds from the
exchange snapshot and records the ``live:`` block beside the shared subset.
Neither shape is known here: a seed is anything with the three fields
:class:`PositionSeed` names.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from ..persistence.db import Database
from ..persistence.models import PositionState
from ..persistence.schema import SCHEMA_VERSION
from . import accounting

__all__ = ["PositionSeed", "write_genesis"]


class PositionSeed(Protocol):
    """A position to seed at genesis: signed size at an entry price."""

    @property
    def coin(self) -> str: ...

    @property
    def size(self) -> Decimal: ...

    @property
    def entry_price(self) -> Decimal: ...


def write_genesis(
    db: Database,
    *,
    run_id: str,
    mode: str,
    initial_balance_usdc: Decimal,
    seeds: Iterable[PositionSeed],
    config_subset: Mapping[str, Any],
    created_at: datetime,
) -> None:
    """Create ``run_id``'s row, opening ledger and seed positions (one transaction).

    ``config_subset`` is recorded as JSON with ``ensure_ascii=False`` and
    ``default=str`` — the form the resume drift check parses back — and the
    row is stamped with this build's ``SCHEMA_VERSION``. Everything else,
    including the refusal of a run that already exists, is
    :func:`~.accounting.initialize_run`'s.
    """
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode=mode,
        initial_balance_usdc=initial_balance_usdc,
        schema_version=SCHEMA_VERSION,
        initial_positions=[
            PositionState(coin=p.coin, size=p.size, entry_price=p.entry_price) for p in seeds
        ],
        config_json=json.dumps(config_subset, ensure_ascii=False, default=str),
        created_at=created_at,
    )
