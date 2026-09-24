"""Opening the store of a command that OWNS a run, and settling which run it is.

``paper`` and ``live --run-id`` climb the same first rungs on the store:
open it, read the run row, and refuse the two ``--create`` mismatches. What follows differs per lane (the live
sibling-lease check, config drift, genesis, where the lease is taken), so it
stays with the caller; :meth:`OpenedRun.foreign_mode` is the run-mode check
behind each lane's own wording.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..persistence import repository as repo
from ..persistence.db import Database

__all__ = ["OpenedRun", "RunIdentityRefusal", "RunIdentityStage", "open_run"]


class RunIdentityStage(Enum):
    """Which rung of :func:`open_run` refused."""

    MISSING_RUN = "missing_run"  # no run row, and no ``--create``
    RUN_EXISTS = "run_exists"  # a run row, and ``--create``


class RunIdentityRefusal(Exception):
    """A rung of :func:`open_run` refused; nothing here prints, the caller words it.

    Not a ``ValueError``: a caller's ``except ValueError`` around config
    parsing must not swallow it.
    """

    def __init__(self, stage: RunIdentityStage, *, run_id: str, db_path: str | Path) -> None:
        super().__init__(f"{stage.value}: run {run_id!r} in {db_path}")
        self.stage = stage
        self.run_id = run_id
        self.db_path = db_path


@dataclass(frozen=True)
class OpenedRun:
    """The open store and the ``runs`` row read once at open (``None`` for a fresh run).

    The caller closes ``db`` (``with opened.db as db:``).
    """

    db: Database
    existing_run: sqlite3.Row | None

    @property
    def is_restart(self) -> bool:
        return self.existing_run is not None

    def foreign_mode(self, lane: str) -> str | None:
        """The existing run's mode when it is not ``lane``'s, else ``None``.

        A fresh run has no mode yet and never conflicts. Read from the ``runs``
        row's v1 ``mode`` column, so it is answerable before the migration.
        """
        if self.existing_run is None:
            return None
        mode = str(self.existing_run["mode"])
        return None if mode == lane else mode


def open_run(db_path: str | Path, run_id: str, *, create: bool) -> OpenedRun:
    """Open the store for the command that owns ``run_id`` and settle fresh-vs-restart.

    An existing store is opened AS-IS: a running process may own it, and the
    lease that proves otherwise lives inside it, so the schema upgrade is
    deferred to the caller (issue #129). :class:`Database`'s deferred policy
    decides what is built, refused or opened here. A missing file is built in
    full, so a caller refuses a missing path without ``--create`` BEFORE this
    call, or an empty store is left behind. Then the run row is read and the
    ``--create`` flag is checked both ways: a missing run without it is
    :attr:`RunIdentityStage.MISSING_RUN`, an existing run with it
    :attr:`RunIdentityStage.RUN_EXISTS` — a silent resume would append to an
    old run's books when the operator meant a fresh one. On either refusal
    the store is closed before the raise.
    """
    db = Database(db_path, migrate=False, defer_migration=True)
    try:
        existing_run = repo.get_run(db.conn, run_id)
        if existing_run is None and not create:
            raise RunIdentityRefusal(RunIdentityStage.MISSING_RUN, run_id=run_id, db_path=db_path)
        if existing_run is not None and create:
            raise RunIdentityRefusal(RunIdentityStage.RUN_EXISTS, run_id=run_id, db_path=db_path)
    except BaseException:
        db.close()
        raise
    return OpenedRun(db=db, existing_run=existing_run)
