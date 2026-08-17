"""Shared pytest helpers for the Hyperliquid perp tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id as derive_attempt_id

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Keep the whole suite immune to a developer's real repo-root ``.env``.

    Both CLI entry points call ``load_dotenv_files()`` as their first act; under
    pytest that would inject every variable of a real ``.env`` into the process,
    and ``load_dotenv``'s ``os.environ`` writes happen outside monkeypatch's
    snapshot, so they outlive the test that triggered them. Neutralize the
    entry-point bindings suite-wide; the loader itself stays testable through
    ``config.load_dotenv_files`` (untouched), and the entry-point ordering tests
    opt back in by re-binding the real function.
    """
    from contrib.hyperliquid_perp import cli, main

    monkeypatch.setattr(cli, "load_dotenv_files", lambda: None)
    monkeypatch.setattr(main, "load_dotenv_files", lambda: None)


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


def synthetic_bar(**over):
    """A minimal well-formed candleSnapshot bar: BTC/1m, t/T in the past."""
    base = {
        "t": 1000,
        "T": 1999,
        "s": "BTC",
        "i": "1m",
        "o": "1",
        "h": "2",
        "l": "1",
        "c": "1",
        "v": "1",
    }
    base.update(over)
    return base


def echo_order_status_cloid(payload, cloid_hex):
    """Echo the queried cloid into a canned orderStatus payload, like the venue.

    ``parse_order_status`` requires the inner order to echo the cloid it was
    queried by (identity check, 2026-08-17). The real Info endpoint always
    echoes it; the fakes' canned payloads predate the check and are often
    served against cloids the test never sees (a manager-derived hex), so each
    fake routes its answer through here to model the well-behaved venue. A
    payload that ALREADY carries a cloid is returned untouched — that is how a
    test scripts a mismatch. Copies rather than mutates: several canned
    payloads are shared module constants.
    """
    if (
        isinstance(payload, dict)
        and payload.get("status") == "order"
        and isinstance(payload.get("order"), dict)
        and isinstance(payload["order"].get("order"), dict)
        and "cloid" not in payload["order"]["order"]
    ):
        inner = {**payload["order"]["order"], "cloid": cloid_hex}
        return {**payload, "order": {**payload["order"], "order": inner}}
    return payload


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
