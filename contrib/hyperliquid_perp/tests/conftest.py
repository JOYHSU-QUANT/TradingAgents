"""Shared pytest helpers for the Hyperliquid perp tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id as derive_attempt_id

_FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    with (_FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def meta_and_asset_ctxs():
    return _load("meta_and_asset_ctxs.json")


@pytest.fixture
def candle_snapshot():
    return _load("candle_snapshot.json")


@pytest.fixture
def funding_history():
    return _load("funding_history.json")


@pytest.fixture
def clearinghouse_state():
    return _load("clearinghouse_state.json")


def insert_decision_attempts(db, statuses, *, run_id="r", start: datetime, mode="paper"):
    """Insert one terminal decision_attempts row per status, on the 4h grid."""
    with db.transaction() as conn:
        for i, status in enumerate(statuses):
            scheduled = start + timedelta(hours=4 * i)
            repo.insert_decision_attempt(
                conn,
                decision_attempt_id=derive_attempt_id(run_id, scheduled),
                timestamp=scheduled,
                mode=mode,
                run_id=run_id,
                scheduled_at=scheduled,
                attempt_count=1,
                status=status,
            )
