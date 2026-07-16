"""Raw exchange payload evidence: one JSON file per round-trip (§16.1 / §16.5).

The live writers — the order submitter, the kill switch's shutdown cancel
sweep, and the fill processor's evidence trail (applied / unmapped / malformed
fills) — persist the untouched exchange payload beside the row that summarises
it, and record that file's path on the row. The two rules that make the evidence
trustworthy live here, once, instead of once per caller:

1. **A failed WRITE must not fail the CALL.** By the time a payload is being
   written the exchange action has already happened — the order is live, the
   cancel landed. Raising because we could not write a souvenir of it would turn
   a successful exchange action into an exception, and the caller would record a
   failure that did not occur.

2. **A failed write must not ERASE an earlier one.** The repository's patch
   writers use the ``_UNSET`` convention: an omitted keyword leaves the column
   alone, an explicit ``None`` CLEARS it. A path we did not write must therefore
   be OMITTED from the patch, never passed through as None — otherwise a
   disk-full retry would delete the pointer to the ORIGINAL ack's payload, for an
   order that retry has just confirmed exists on the exchange, and the only trace
   would be a warning naming the file we failed to write rather than the one we
   destroyed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..persistence.repository import UNSET, Unset

__all__ = ["payload_column", "write_raw_payload"]

logger = logging.getLogger(__name__)

# Anything outside this set is replaced in the FILENAME (not the recorded value).
# A §14.2 fill dedupe key is a ``|``-joined value ("tid|4521"),
# and ``|`` is an illegal path character on Windows — an unsanitised key would
# make every live-fill payload write fail (silently, since the writer is
# fail-soft), losing the very evidence §16.2 exists to keep. The path is only an
# opaque handle stored on the row, so a lossy filename is fine; uniqueness still
# comes from the timestamp suffix.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _safe_key(key: str) -> str:
    return _UNSAFE_FILENAME_CHARS.sub("_", key)


def write_raw_payload(
    *, payload_dir: Path, kind: str, key: str, payload: Any, now: datetime, once: bool = False
) -> str | None:
    """Persist one raw exchange response; return its path, or None on failure.

    ``kind`` names the round-trip ("order", "orderStatus", "cancel") and ``key``
    the cloid it belongs to. Rule 1 above: every failure mode — the directory,
    the disk, or a payload that will not serialise — is caught and warned, never
    raised, because the exchange action this documents has already taken effect.

    ``once`` keeps only the FIRST payload recorded for a ``(kind, key)`` and returns
    that file's path on every later call. It is OFF by default because the order
    round-trips each document a DISTINCT event that merely shares a cloid — a retry's
    ack is not the first ack, and collapsing them would destroy the audit trail. Turn
    it on where the same unchanging fact is re-observed on a schedule: a fill the REST
    backfill keeps re-fetching (an unapplied fill is never inserted, so no cursor ever
    advances past it) would otherwise write one more identical file every pass, without
    bound, until the disk filled.
    """
    safe_key = _safe_key(key)
    stamp = now.strftime("%Y%m%dT%H%M%S_%fZ")
    path = payload_dir / f"{kind}-{safe_key}-{stamp}.json"
    try:
        if once:
            # Inside the guard, with everything else: Rule 1 is that this function never
            # raises, and callers lean on that ("recording the evidence can never turn a
            # skipped fill into a crashed drain"). The glob is I/O like any other here.
            existing = sorted(payload_dir.glob(f"{kind}-{safe_key}-*.json"))
            if existing:
                return str(existing[0])
        payload_dir.mkdir(parents=True, exist_ok=True)
        # default=str keeps Decimal/datetime serialisable; TypeError and
        # ValueError are caught regardless, so an exotic payload degrades to a
        # missing file rather than to an exception raised over a live order.
        path.write_text(json.dumps(payload, default=str, sort_keys=True), encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("failed to write raw exchange payload %s: %s", path, exc)
        return None
    return str(path)


def payload_column(raw_path: str | None) -> str | Unset:
    """The path to record, or UNSET when nothing was written.

    Rule 2 above. Pass THIS to ``raw_exchange_payload_path=`` rather than the
    raw ``str | None``: the writers read an explicit ``None`` as "clear the
    column", so a failed write must arrive as the sentinel — "leave it alone" —
    and not as None, which would erase whatever an earlier successful write
    recorded there.
    """
    return UNSET if raw_path is None else raw_path
