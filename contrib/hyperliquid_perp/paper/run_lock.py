"""Single-instance lease for a paper run (one live process per run_id).

Two ``paper`` processes driving the same run would destroy each other: the
second's restart reconciliation cancels the first's *live* plans (it assumes
the previous process is dead), and both loops would then spend AI budget on
the same deterministic ``decision_attempt_id`` and interleave fills that a
later replay flags as corruption. The lease makes the assumption explicit:
startup refuses while another holder's heartbeat is fresh.

The lease lives in ``scheduler_state`` (``lock_pid`` / ``lock_heartbeat_at``)
rather than an OS file lock: SQLite's ``BEGIN IMMEDIATE`` serializes two
simultaneous acquirers portably (Windows + POSIX), and a hard-killed holder
needs no cleanup — its heartbeat simply goes stale. Freshness is the signal;
the pid is a diagnostic breadcrumb (pids recycle). The paper loop must beat
at least once per :data:`LOCK_STALE_SECONDS`; a crashed run is takeable after
that window, and :func:`release_run_lock` clears the lease early on a clean
shutdown (guarded by pid so a frozen-then-resumed process can never clear a
successor's lease).
"""

from __future__ import annotations

from datetime import datetime

from ..persistence import repository as repo
from ..persistence.db import Database
from .scheduler import parse_instant

__all__ = [
    "LOCK_STALE_SECONDS",
    "RunLockError",
    "acquire_run_lock",
    "heartbeat_run_lock",
    "release_run_lock",
]

# The loop heartbeats once per iteration, and one iteration can legitimately
# hold the loop for many minutes: poll() runs the full AI engine call (deep
# reasoning + up to two in-call retries) before control returns. The window
# must outlast the slowest realistic iteration — a takeover mid-AI-call would
# recreate exactly the two-writer corruption this lease exists to prevent.
# The cost of the margin is only how long a crashed run stays untakeable,
# trivial against a 4h decision cycle.
LOCK_STALE_SECONDS = 900


class RunLockError(Exception):
    """Another live process already holds this run's lease."""


def _holder(state, now: datetime) -> tuple[int, float] | None:
    """The (pid, heartbeat age in seconds) of a *fresh* lease, else ``None``."""
    if state is None or state["lock_pid"] is None or state["lock_heartbeat_at"] is None:
        return None
    age = (now - parse_instant(state["lock_heartbeat_at"])).total_seconds()
    if age >= LOCK_STALE_SECONDS:
        return None
    return int(state["lock_pid"]), age


def acquire_run_lock(db: Database, run_id: str, *, pid: int, now: datetime) -> None:
    """Take the run's lease, or raise :class:`RunLockError` while it is held.

    Runs read-check and write in one ``BEGIN IMMEDIATE`` transaction so two
    processes starting simultaneously cannot both see "unlocked" and proceed.
    A stale lease (holder crashed) is taken over silently.
    """
    with db.transaction() as conn:
        holder = _holder(repo.get_scheduler_state(conn, run_id), now)
        if holder is not None and holder[0] != pid:
            raise RunLockError(
                f"run {run_id!r} is already being driven by pid {holder[0]} "
                f"(heartbeat {holder[1]:.0f}s ago). Two processes on one run would "
                "cancel each other's live orders and double the AI spend. If that "
                f"process is truly gone, retry after {LOCK_STALE_SECONDS}s."
            )
        repo.upsert_scheduler_state(
            conn, run_id, lock_pid=pid, lock_heartbeat_at=now, updated_at=now
        )


def heartbeat_run_lock(db: Database, run_id: str, *, pid: int, now: datetime) -> None:
    """Refresh the lease. The caller must already hold it (acquire succeeded)."""
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn, run_id, lock_pid=pid, lock_heartbeat_at=now, updated_at=now
        )


def release_run_lock(db: Database, run_id: str, *, pid: int, now: datetime) -> None:
    """Clear the lease on clean shutdown — only if ``pid`` still holds it.

    A process frozen past :data:`LOCK_STALE_SECONDS` may have been superseded;
    clearing unconditionally would drop the successor's live lease.
    """
    with db.transaction() as conn:
        state = repo.get_scheduler_state(conn, run_id)
        if state is None or state["lock_pid"] != pid:
            return
        repo.upsert_scheduler_state(
            conn, run_id, lock_pid=None, lock_heartbeat_at=None, updated_at=now
        )
