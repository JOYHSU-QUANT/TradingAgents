"""Single-instance lease for a run (one driving process per run_id).

Two processes driving the same run would destroy each other: the
second's restart reconciliation cancels the first's *live* plans (it assumes
the previous process is dead), and both loops would then spend AI budget on
the same deterministic ``decision_attempt_id`` and interleave fills that a
later replay flags as corruption. The lease makes the assumption explicit:
startup refuses while another holder's heartbeat is fresh.

The lease lives in ``scheduler_state`` (``lock_pid`` / ``lock_heartbeat_at``)
rather than an OS file lock: SQLite's ``BEGIN IMMEDIATE`` serializes two
simultaneous acquirers portably (Windows + POSIX), and a hard-killed holder
needs no cleanup — its heartbeat simply goes stale. Freshness is the signal;
the pid is a diagnostic breadcrumb (pids recycle). The driving loop must beat
at least once per :data:`LOCK_STALE_SECONDS`; a crashed run is takeable after
that window, and :func:`release_run_lock` clears the lease early on a clean
shutdown. Both the per-iteration refresh and the release are guarded by pid,
so a frozen-then-resumed process can neither clear nor silently reclaim a
successor's lease — its next heartbeat raises instead.
"""

from __future__ import annotations

from datetime import datetime

from ..common.instants import delta_ms, gap_label, parse_instant
from ..persistence import repository as repo
from ..persistence.db import Database

__all__ = [
    "LOCK_STALE_SECONDS",
    "RunLockError",
    "acquire_run_lock",
    "heartbeat_run_lock",
    "lease_age_label",
    "peek_run_lock",
    "release_run_lock",
]

# The loop heartbeats once per iteration, and one iteration can legitimately
# hold the loop for many minutes: poll() runs one full AI engine call (deep
# multi-agent reasoning; the §3.1 retries each land on a *later* poll, but a
# single call can still block for minutes) before control returns. The window
# must outlast the slowest realistic iteration — a takeover mid-AI-call would
# recreate exactly the two-writer corruption this lease exists to prevent.
# The cost of the margin is only how long a crashed run stays untakeable,
# trivial against a 4h decision cycle.
LOCK_STALE_SECONDS = 900


class RunLockError(Exception):
    """Another live process already holds this run's lease."""


def lease_age_label(heartbeat_at: datetime, *, now: datetime) -> str:
    """How long ago a lease was heartbeated, as a refusal message states it.

    Shared by this module's refusal and the CLI's migration refusal
    (``cli._common``), which are one message family and must not drift apart.
    Rendered through :func:`gap_label` rather than a fixed ``%.0fs``: the
    holder heartbeats once per loop iteration, so a lease read just after a
    write is a fraction of a second old, and ``%.0fs`` printed that as
    "heartbeat 0s ago" (issue #290). A stamp AHEAD of ``now`` — the writer's
    clock ahead of this host's — is a fresh lease by definition and reads as
    no age at all, not as a negative one.
    """
    return gap_label(max(0, delta_ms(now, heartbeat_at)))


# The lease bound as the refusal states it, in the same unit ladder as the age
# beside it — an age in "ms" against a bound in "900s" would make the operator
# convert to know how long to wait.
_STALE_LABEL = gap_label(LOCK_STALE_SECONDS * 1000)


def _holder(state, now: datetime) -> tuple[int, datetime] | None:
    """The (pid, heartbeat instant) of a *fresh* lease, else ``None``."""
    if state is None or state["lock_pid"] is None or state["lock_heartbeat_at"] is None:
        return None
    heartbeat_at = parse_instant(state["lock_heartbeat_at"])
    if (now - heartbeat_at).total_seconds() >= LOCK_STALE_SECONDS:
        return None
    return int(state["lock_pid"]), heartbeat_at


def _refuse_if_held(state, run_id: str, *, pid: int | None, now: datetime) -> None:
    """Raise :class:`RunLockError` when a FRESH lease belongs to another pid.

    ``pid=None`` exempts nobody — for a caller that has not taken the lease
    and so cannot legitimately be its holder.
    """
    holder = _holder(state, now)
    if holder is not None and holder[0] != pid:
        raise RunLockError(
            f"run {run_id!r} is already being driven by pid {holder[0]} "
            f"(heartbeat {lease_age_label(holder[1], now=now)} ago). Two processes "
            "on one run would cancel each other's live orders and double the AI "
            f"spend. If that process is truly gone, retry after {_STALE_LABEL}."
        )


def acquire_run_lock(db: Database, run_id: str, *, pid: int, now: datetime) -> None:
    """Take the run's lease, or raise :class:`RunLockError` while it is held.

    Runs read-check and write in one ``BEGIN IMMEDIATE`` transaction so two
    processes starting simultaneously cannot both see "unlocked" and proceed.
    A stale lease (holder crashed) is taken over silently.
    """
    with db.transaction() as conn:
        _refuse_if_held(repo.get_scheduler_state(conn, run_id), run_id, pid=pid, now=now)
        repo.upsert_scheduler_state(
            conn, run_id, lock_pid=pid, lock_heartbeat_at=now, updated_at=now
        )


def peek_run_lock(db: Database, run_id: str, *, now: datetime) -> None:
    """:func:`acquire_run_lock`'s refusal without its write.

    For a command that must know "does a live process own this run?" BEFORE it
    is ready to take the lease itself — ``live`` migrates the store ahead of
    reads and writes that the lease cannot precede (issue #129). Any fresh
    holder refuses (a peeker holds nothing, so there is no own pid to exempt).
    Read-only, so it proves nothing against a process starting concurrently:
    the caller still takes the real lease later, and the loser of that race
    exits there.
    """
    _refuse_if_held(repo.get_scheduler_state(db.conn, run_id), run_id, pid=None, now=now)


def heartbeat_run_lock(db: Database, run_id: str, *, pid: int, now: datetime) -> None:
    """Refresh the lease — only while ``pid`` is still the recorded holder.

    Raises :class:`RunLockError` when the lease has been taken over: a process
    frozen past :data:`LOCK_STALE_SECONDS` may have been legitimately superseded,
    and an unguarded refresh would silently stamp its own pid back over the
    successor's lease — the two processes would then flip-flop the lock every
    iteration while BOTH keep driving the run. The check-and-write runs in one
    ``BEGIN IMMEDIATE`` transaction, mirroring :func:`acquire_run_lock`; the
    caller must treat the raise as fatal (stop the loop without touching the
    store — the successor owns it now).
    """
    with db.transaction() as conn:
        # Narrow read: this runs every loop iteration, and the full row carries
        # ever-growing breadcrumb text columns the check doesn't need.
        row = conn.execute(
            "SELECT lock_pid FROM scheduler_state WHERE run_id = ?", (run_id,)
        ).fetchone()
        holder = None if row is None else row["lock_pid"]
        if holder != pid:
            raise RunLockError(
                f"run {run_id!r} lease is no longer held by pid {pid} (current "
                f"holder: {holder}) — this process was superseded while stalled "
                "and must stop driving the run."
            )
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
