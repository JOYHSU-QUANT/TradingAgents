"""The run genesis both lanes write through ``runtime.genesis.write_genesis``."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import PerpPosition
from contrib.hyperliquid_perp.paper.config import InitialPosition
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting
from contrib.hyperliquid_perp.runtime.genesis import write_genesis

D = Decimal
_T0 = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def _exchange_position(coin: str, size: Decimal, entry_price: Decimal) -> PerpPosition:
    """What the live lane seeds from: the mapped exchange position, extra fields and all."""
    return PerpPosition(coin=coin, size=size, entry_price=entry_price, unrealized_pnl=D(0))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "g.db")
    yield database
    database.close()


def test_write_genesis_records_the_row_the_seeds_and_the_config_subset(db):
    write_genesis(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=D("1000.5"),
        seeds=[
            _exchange_position("ETH", D("-2"), D("3000")),
            _exchange_position("BTC", D("0.5"), D("100")),
        ],
        config_subset={"coin": "BTC", "risk": {"cap": D("1.5")}, "note": "中文"},
        created_at=_T0,
    )
    row = repo.get_run(db.conn, "r")
    assert row["mode"] == "live"
    assert row["initial_balance_usdc"] == "1000.5"
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["created_at"] == _T0.isoformat()
    # Decimals through ``default=str``, non-ASCII kept (``ensure_ascii=False``).
    assert row["config_json"] == '{"coin": "BTC", "risk": {"cap": "1.5"}, "note": "中文"}'
    assert repo.get_run_seed_positions(db.conn, "r") == [
        PositionState(coin="BTC", size=D("0.5"), entry_price=D("100")),
        PositionState(coin="ETH", size=D("-2"), entry_price=D("3000")),
    ]


def test_write_genesis_with_no_seeds_opens_a_flat_run(db):
    write_genesis(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(100),
        seeds=(),
        config_subset={"coin": "BTC"},
        created_at=_T0,
    )
    assert repo.get_run_seed_positions(db.conn, "r") == []
    assert repo.get_run(db.conn, "r")["config_json"] == '{"coin": "BTC"}'


def test_write_genesis_goes_through_the_accounting_module_attribute(db, monkeypatch):
    # A cli test stops a fresh run by patching ``accounting.initialize_run``;
    # the seam must read that name at call time, not bind it at import. The
    # seed is the paper lane's config shape (the live lane's is above).
    calls = []
    monkeypatch.setattr(accounting, "initialize_run", lambda *a, **kw: calls.append((a, kw)))
    write_genesis(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1),
        seeds=[InitialPosition(coin="BTC", size=D(1), entry_price=D(2))],
        config_subset={},
        created_at=_T0,
    )
    ((positional, keywords),) = calls
    assert positional == (db,)
    assert keywords == {
        "run_id": "r",
        "mode": "paper",
        "initial_balance_usdc": D(1),
        "schema_version": SCHEMA_VERSION,
        "initial_positions": [PositionState(coin="BTC", size=D(1), entry_price=D(2))],
        "config_json": "{}",
        "created_at": _T0,
    }


def test_write_genesis_refuses_a_second_genesis_for_the_same_run(db):
    kwargs = {
        "run_id": "r",
        "mode": "paper",
        "initial_balance_usdc": D(100),
        "seeds": (),
        "config_subset": {},
        "created_at": _T0,
    }
    write_genesis(db, **kwargs)
    with pytest.raises(ValueError, match="already"):
        write_genesis(db, **kwargs)
