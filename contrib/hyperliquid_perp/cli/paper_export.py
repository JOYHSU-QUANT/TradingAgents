"""The paper daemon’s export/breadcrumb lane (phase2-data §1.1).

Replay-verify + CSV export at every cycle boundary and on shutdown, the durable
``scheduler_state`` breadcrumbs those outcomes leave behind, the in-band
``REPLAY_UNVERIFIED.json`` marker, and the best-effort pending-funding retry the
protection-only and shutdown lanes use.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _stamp_breadcrumb(db, run_id: str, kind: str, status: str, error: str | None) -> None:
    """Durably stamp a ``scheduler_state`` breadcrumb trio (``last_<kind>_*``).

    Export failures, replay outcomes, and config drift on resume are
    warn-and-carry-on (the mid-run replay halt only lives in process memory) —
    without this record none would leave any trace once the process exits.
    ``kind`` is a code-owned literal ("export" / "replay" / "config_drift");
    the column vocabulary stays validated by ``upsert_scheduler_state``'s
    fixed keyword signature.
    """
    from ..persistence import repository as repo

    stamp = datetime.now(timezone.utc)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            run_id,
            updated_at=stamp,
            **{
                f"last_{kind}_status": status,
                f"last_{kind}_error": error,
                f"last_{kind}_at": stamp,
            },
        )


_UNVERIFIED_MARKER = "REPLAY_UNVERIFIED.json"


def _mark_export_verification(
    export_dir: Path, run_id: str, replay_ok: bool, reason: str | None
) -> None:
    """Record, in-band next to the CSVs, whether the exported set passed replay.

    ``scheduler_state`` (which carries the ``last_replay_*`` breadcrumb) is NOT
    one of the exported tables, so a consumer reading the CSVs alone cannot tell
    a mismatch cycle's export from a healthy one. When replay did not verify we
    drop ``REPLAY_UNVERIFIED.json`` beside the CSVs; when it verifies we remove
    any stale marker a previous bad cycle left behind (the export dir is reused
    every cycle). Best-effort: a marker failure is logged, never raised — the
    loop must survive, and the ``last_replay_*`` breadcrumb remains authoritative.
    """
    marker = Path(export_dir) / _UNVERIFIED_MARKER
    try:
        if replay_ok:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(
                json.dumps(
                    {"run_id": run_id, "replay_verified": False, "reason": reason},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    except OSError as exc:
        logger.error("failed to update replay-verification marker for %s: %s", run_id, exc)


def _post_cycle_export(db, run_id: str, export_dir: Path) -> bool:
    """Replay-verify then export (phase2-data §1.1); returns whether the books verified.

    An export failure never stops trading (spec: record ``export_failed`` and
    carry on) — but it IS durably recorded on ``scheduler_state``
    (``last_export_status`` / ``last_export_error`` / ``last_export_at``), so a
    post-mortem can tell how long the CSV view had been stale even when stderr
    was not captured. A replay mismatch or replay *failure* returns ``False``
    so the caller can stop opening new positions on unverifiable books; both
    outcomes (and healthy verifications) stamp the ``last_replay_*`` breadcrumb.
    """
    from ..paper.reconcile import classify_replay
    from ..persistence.export import ExportError, export_run

    # classify_replay contains a raising replay ("failed" — unverifiable books
    # are treated like inconsistent ones), so this lane cannot kill the loop.
    replay_status, replay_detail, _cause = classify_replay(db, run_id=run_id)
    replay_ok = replay_status == "ok"
    if replay_status == "mismatch":
        logger.error("accounting replay mismatch for %s: %s", run_id, replay_detail)
        print(
            f"WARNING: accounting replay mismatch for {run_id!r}: {replay_detail} — "
            "investigate before trusting this run's results.",
            file=sys.stderr,
        )
    elif replay_status == "failed":
        logger.error("accounting replay failed for %s: %s", run_id, replay_detail)
        print(f"WARNING: accounting replay failed: {replay_detail}", file=sys.stderr)
    _stamp_breadcrumb(db, run_id, "replay", replay_status, replay_detail)
    export_ok = False
    try:
        export_run(db, run_id=run_id, output_dir=export_dir)
        export_ok = True
        _stamp_breadcrumb(db, run_id, "export", "ok", None)
        print(f"exported CSVs to {export_dir}", file=sys.stderr)
    except ExportError as exc:
        # §1.1: record export_failed, keep the monitor and protections running.
        logger.error("export_failed for %s: %s", run_id, exc)
        print(f"WARNING: export_failed — {exc}", file=sys.stderr)
        _stamp_breadcrumb(db, run_id, "export", "failed", str(exc))
    # Mark the freshly written set as unverified when replay didn't pass, so a
    # consumer reading the CSVs alone isn't misled (see _mark_export_verification).
    # Only when a full set was actually (re)written — a failed export leaves the
    # previous set and its marker untouched.
    if export_ok:
        _mark_export_verification(export_dir, run_id, replay_ok, replay_detail)
    return replay_ok


# Protection-only mode never reaches the cycle-terminal funding retry (the
# scheduler is never polled), so the loop retries on this wall-clock cadence
# instead. Hour-grained to match funding's own settlement granularity; when
# nothing is pending the pass is a single cheap SQLite query.
_HALTED_FUNDING_RETRY_SECONDS = 3600


def _retry_pending_funding(db, run_id: str, *, now: datetime, funding_source) -> None:
    """Best-effort pending-funding retry for the protection and shutdown lanes.

    The cycle-terminal lane calls :func:`backfill_pending_funding` directly and
    stays fail-loud (a store-level error there must kill the loop — pinned by
    test). These lanes exist to keep SL/TP alive (the halted-mode timer) or to
    flush the most complete final CSVs the store allows (settle-exit and
    Ctrl-C/SIGTERM exports) — a raising retry must not take either down, so any
    failure is contained to an ERROR log and the hourly timer (or the export
    itself) carries on. ``record_funding`` posts exactly-once, so repeated
    passes are safe.
    """
    from ..paper.reconcile import backfill_pending_funding

    try:
        backfill_pending_funding(db, run_id=run_id, now=now, funding_source=funding_source)
    except Exception:
        logger.exception("pending-funding retry failed for %s (best-effort lane)", run_id)
