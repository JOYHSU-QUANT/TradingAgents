"""Compatibility path: the run lease moved to :mod:`..runtime.run_lock` (refactor plan v2, T1).

Every in-package importer already uses that path — this module exists for
one PR, until the accounting split (plan PR 3) deletes it.
"""

from __future__ import annotations

from ..runtime.run_lock import (
    LOCK_STALE_SECONDS,
    RunLockError,
    acquire_run_lock,
    heartbeat_run_lock,
    lease_age_label,
    peek_run_lock,
    release_run_lock,
)

__all__ = [
    "LOCK_STALE_SECONDS",
    "RunLockError",
    "acquire_run_lock",
    "heartbeat_run_lock",
    "lease_age_label",
    "peek_run_lock",
    "release_run_lock",
]
