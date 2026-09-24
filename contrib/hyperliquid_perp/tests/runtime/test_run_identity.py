"""Opening an owned store and settling which run it is (``runtime.run_identity``)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.persistence.db import Database, SchemaVersionError
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting, run_identity as run_identity_mod
from contrib.hyperliquid_perp.runtime.run_identity import (
    RunIdentityRefusal,
    RunIdentityStage,
    open_run,
)

_T0 = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)


def _seed(path, *, run_id: str = "r", mode: str = "paper") -> None:
    db = Database(path)
    accounting.initialize_run(
        db,
        run_id=run_id,
        mode=mode,
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_T0,
    )
    db.close()


@pytest.fixture
def opened_stores(monkeypatch):
    """Every ``Database`` ``open_run`` constructs, with its kwargs, as ``(db, kwargs)``."""
    stores: list[tuple[Database, dict]] = []

    class _Recording(Database):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            stores.append((self, kwargs))

    monkeypatch.setattr(run_identity_mod, "Database", _Recording)
    return stores


def _is_closed(db: Database) -> bool:
    try:
        db.conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


def test_a_fresh_run_under_create_opens_with_no_row(tmp_path):
    opened = open_run(tmp_path / "s.db", "r", create=True)
    with opened.db:
        assert opened.existing_run is None
        assert opened.is_restart is False
        assert opened.foreign_mode("paper") is None


def test_a_restart_opens_with_the_run_row(tmp_path):
    path = tmp_path / "s.db"
    _seed(path)
    opened = open_run(path, "r", create=False)
    with opened.db:
        assert opened.is_restart is True
        assert opened.existing_run["mode"] == "paper"
        assert opened.existing_run["initial_balance_usdc"] == "100"


def test_the_store_is_opened_deferred_not_migrated(tmp_path, opened_stores):
    # A populated store older than this build is opened AS-IS (issue #129): the
    # caller pays the upgrade once it owns the run. ``Database``'s own tests
    # cover what that policy does to each kind of store; this pins that
    # ``open_run`` asks for it.
    opened = open_run(tmp_path / "s.db", "r", create=True)
    with opened.db:
        assert opened_stores == [(opened.db, {"migrate": False, "defer_migration": True})]


def test_a_missing_run_without_create_is_refused_with_the_store_closed(tmp_path, opened_stores):
    path = tmp_path / "s.db"
    _seed(path, run_id="other")
    with pytest.raises(RunIdentityRefusal) as info:
        open_run(path, "r", create=False)
    refusal = info.value
    assert refusal.stage is RunIdentityStage.MISSING_RUN
    assert (refusal.run_id, refusal.db_path) == ("r", path)
    assert str(refusal) == f"missing_run: run 'r' in {path}"
    ((db, _kwargs),) = opened_stores
    assert _is_closed(db)


def test_an_existing_run_under_create_is_refused_with_the_store_closed(tmp_path, opened_stores):
    path = tmp_path / "s.db"
    _seed(path)
    with pytest.raises(RunIdentityRefusal) as info:
        open_run(path, "r", create=True)
    assert info.value.stage is RunIdentityStage.RUN_EXISTS
    ((db, _kwargs),) = opened_stores
    assert _is_closed(db)


def test_the_refusal_is_not_a_value_error():
    # A caller's ``except ValueError`` around config parsing must not swallow it.
    refusal = RunIdentityRefusal(RunIdentityStage.MISSING_RUN, run_id="r", db_path="s.db")
    assert not isinstance(refusal, ValueError)


@pytest.mark.parametrize(
    ("lane", "expected"),
    [("paper", "live"), ("live", None)],
)
def test_foreign_mode_names_the_other_lanes_mode(tmp_path, lane, expected):
    path = tmp_path / "s.db"
    _seed(path, mode="live")
    opened = open_run(path, "r", create=False)
    with opened.db:
        assert opened.foreign_mode(lane) == expected


def test_a_store_from_a_newer_build_raises_schema_version_error_unwrapped(tmp_path):
    # The at-open refusal ``Database`` makes stays the caller's to word, as it
    # was before the factory: nothing here catches it.
    path = tmp_path / "s.db"
    _seed(path)
    db = Database(path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 1, "2099-01-01T00:00:00+00:00"),
        )
    db.close()
    with pytest.raises(SchemaVersionError, match="NEWER build"):
        open_run(path, "r", create=False)
