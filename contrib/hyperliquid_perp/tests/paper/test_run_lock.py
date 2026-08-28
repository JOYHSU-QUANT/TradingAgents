"""Tests for the single-instance run lease (scheduler_state lock columns)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.paper.run_lock import (
    LOCK_STALE_SECONDS,
    RunLockError,
    acquire_run_lock,
    heartbeat_run_lock,
    peek_run_lock,
    release_run_lock,
)
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "l.db")
    yield database
    database.close()


def _state(db, run_id="r"):
    return repo.get_scheduler_state(db.conn, run_id)


def test_acquire_on_fresh_state_writes_lease(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    row = _state(db)
    assert row["lock_pid"] == 101
    assert row["lock_heartbeat_at"] == _T0.isoformat()


def test_second_process_with_fresh_heartbeat_is_refused(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    # One second inside the staleness window: still held.
    with pytest.raises(RunLockError, match="pid 101"):
        acquire_run_lock(db, "r", pid=202, now=_T0 + timedelta(seconds=LOCK_STALE_SECONDS - 1))
    assert _state(db)["lock_pid"] == 101  # holder's lease untouched


def test_stale_heartbeat_is_taken_over(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    # age == LOCK_STALE_SECONDS is the boundary: stale, takeable.
    takeover_at = _T0 + timedelta(seconds=LOCK_STALE_SECONDS)
    acquire_run_lock(db, "r", pid=202, now=takeover_at)
    row = _state(db)
    assert row["lock_pid"] == 202
    assert row["lock_heartbeat_at"] == takeover_at.isoformat()


def test_peek_refuses_exactly_when_acquire_would_and_never_writes(db):
    # Issue #129: `live` needs acquire's verdict before it is ready to take the
    # lease. Same message, same staleness boundary, no own-pid exemption (a
    # peeker holds nothing) — and the store is untouched on every outcome (no
    # lease row for a free run, the holder's row intact for a held one).
    peek_run_lock(db, "r", now=_T0)
    assert _state(db) is None
    acquire_run_lock(db, "r", pid=101, now=_T0)
    with pytest.raises(RunLockError, match="pid 101"):
        peek_run_lock(db, "r", now=_T0 + timedelta(seconds=LOCK_STALE_SECONDS - 1))
    peek_run_lock(db, "r", now=_T0 + timedelta(seconds=LOCK_STALE_SECONDS))  # stale
    row = _state(db)
    assert (row["lock_pid"], row["lock_heartbeat_at"]) == (101, _T0.isoformat())
    # The exemption acquire grants its own pid is not a peek's to claim: a
    # fresh lease stamped with THIS process's pid (a pid recycled after a
    # crash) still refuses, where acquire would silently re-take it.
    import os

    acquire_run_lock(db, "r", pid=os.getpid(), now=_T0 + timedelta(seconds=LOCK_STALE_SECONDS))
    with pytest.raises(RunLockError, match=f"pid {os.getpid()}"):
        peek_run_lock(db, "r", now=_T0 + timedelta(seconds=LOCK_STALE_SECONDS + 1))


def test_same_pid_reacquire_succeeds(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    later = _T0 + timedelta(seconds=5)
    acquire_run_lock(db, "r", pid=101, now=later)  # own lease: no error
    row = _state(db)
    assert row["lock_pid"] == 101
    assert row["lock_heartbeat_at"] == later.isoformat()


def test_heartbeat_refreshes_lease(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    beat = _T0 + timedelta(seconds=60)
    heartbeat_run_lock(db, "r", pid=101, now=beat)
    row = _state(db)
    assert row["lock_pid"] == 101
    assert row["lock_heartbeat_at"] == beat.isoformat()
    # The refreshed beat keeps a rival out past the original acquire's window.
    with pytest.raises(RunLockError):
        acquire_run_lock(db, "r", pid=202, now=beat + timedelta(seconds=LOCK_STALE_SECONDS - 1))


def test_heartbeat_by_superseded_pid_raises_and_leaves_successor_lease(db):
    # A froze past the staleness window; B legitimately took over.
    acquire_run_lock(db, "r", pid=101, now=_T0)
    takeover_at = _T0 + timedelta(seconds=LOCK_STALE_SECONDS)
    acquire_run_lock(db, "r", pid=202, now=takeover_at)
    # A resumes and heartbeats: it must NOT steal the lease back silently —
    # both processes driving one run is the corruption the lease prevents.
    with pytest.raises(RunLockError, match="superseded"):
        heartbeat_run_lock(db, "r", pid=101, now=takeover_at + timedelta(seconds=30))
    row = _state(db)
    assert row["lock_pid"] == 202  # successor's lease untouched
    assert row["lock_heartbeat_at"] == takeover_at.isoformat()


def test_heartbeat_after_release_raises(db):
    # A cleanly released (or never held) — a late heartbeat must not recreate
    # ownership out of thin air.
    acquire_run_lock(db, "r", pid=101, now=_T0)
    release_run_lock(db, "r", pid=101, now=_T0)
    with pytest.raises(RunLockError):
        heartbeat_run_lock(db, "r", pid=101, now=_T0 + timedelta(seconds=30))
    assert _state(db)["lock_pid"] is None


def test_heartbeat_on_missing_state_raises(db):
    with pytest.raises(RunLockError):
        heartbeat_run_lock(db, "never-locked", pid=101, now=_T0)
    assert _state(db, "never-locked") is None  # and must not create a row


def test_release_clears_only_when_pid_matches(db):
    acquire_run_lock(db, "r", pid=101, now=_T0)
    release_run_lock(db, "r", pid=202, now=_T0)  # wrong pid: no-op
    assert _state(db)["lock_pid"] == 101
    release_run_lock(db, "r", pid=101, now=_T0)  # holder: clears the lease
    row = _state(db)
    assert row["lock_pid"] is None
    assert row["lock_heartbeat_at"] is None
    # Cleared lease is immediately acquirable, no staleness wait.
    acquire_run_lock(db, "r", pid=202, now=_T0)
    assert _state(db)["lock_pid"] == 202


def test_release_on_missing_state_is_noop(db):
    release_run_lock(db, "never-locked", pid=101, now=_T0)  # must not raise
    assert _state(db, "never-locked") is None  # and must not create a row
