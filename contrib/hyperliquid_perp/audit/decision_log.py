"""Write each :class:`PerpTradeDecision` to a durable JSON audit record.

One file per decision under ``<results_dir>/perp_decisions/``. The record pins
the four things a post-mortem needs (phase1-spec build order 9): the **prompt
hash** the engine reasoned over, the **models** used, the full **decision**, and
a **timestamp**.

:func:`build_log_record` is pure (timestamp injected) so it is unit-tested
without touching the filesystem; :func:`write_decision_log` does the I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..domains.perp.decision import PerpTradeDecision

if TYPE_CHECKING:
    # Type-only import (annotations are strings under ``from __future__``) so the
    # audit layer documents the constrained value set without taking a runtime
    # dependency on the integration layer.
    from ..integration.decision_adapter import RatingSource

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# The valid ``rating_source`` tags, mirroring ``RatingSource`` in the integration
# layer. Duplicated as plain strings (not the enum) because that import is
# TYPE_CHECKING-only here — a runtime ``isinstance`` would force a circular import.
_VALID_RATING_SOURCES = frozenset({"explicit", "parse_fallback", "default"})


def prompt_hash(prompt: str) -> str:
    """``sha256:<hex>`` of the exact prompt text the engine reasoned over."""
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_log_record(
    *,
    coin: str,
    decision: PerpTradeDecision,
    prompt: str,
    models: dict[str, str],
    rating: str,
    timestamp: datetime,
    rating_source: str | RatingSource = "explicit",
) -> dict[str, Any]:
    """Assemble the JSON-ready audit record (pure — no clock, no I/O)."""
    if timestamp.tzinfo is None:
        # A naive datetime makes ``timestamp.timestamp()`` interpret the value in the
        # host's local zone, so ``timestamp_ms`` would be silently off by the UTC
        # offset (e.g. 8h on a UTC+8 box) while the ISO ``timestamp`` string looks
        # fine — a corrupt audit record that no test on a UTC machine would catch.
        raise ValueError("build_log_record requires a timezone-aware timestamp")
    # ``rating_source`` is the one record field not derived from a validated domain
    # object — a caller passing a wrong-case ("EXPLICIT") or unknown ("fallback")
    # tag would otherwise write a silently corrupt audit field. A ``RatingSource``
    # (a ``str`` enum) compares/serialises as its value, so check the string form.
    rs_value = rating_source.value if isinstance(rating_source, Enum) else rating_source
    if rs_value not in _VALID_RATING_SOURCES:
        raise ValueError(
            f"build_log_record: unknown rating_source {rating_source!r}; "
            f"must be one of {sorted(_VALID_RATING_SOURCES)}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "coin": coin,
        "timestamp": timestamp.isoformat(),
        "timestamp_ms": int(timestamp.timestamp() * 1000),
        "prompt_hash": prompt_hash(prompt),
        "models": models,
        "rating": rating,
        "rating_source": rs_value,
        "decision": decision.to_dict(),
    }


def _filename(coin: str, timestamp: datetime) -> str:
    """Sortable, filesystem-safe name: ``BTC_20260627_174500_000.json``.

    The millisecond suffix keeps two decisions for the same coin in the same
    second from silently overwriting each other.
    """
    stamp = timestamp.strftime("%Y%m%d_%H%M%S") + f"_{timestamp.microsecond // 1000:03d}"
    safe_coin = "".join(c for c in coin if c.isalnum()) or "UNKNOWN"
    return f"{safe_coin}_{stamp}.json"


def _parse_timestamp(ts_raw: str | None) -> datetime:
    """Parse the record's ISO timestamp for the filename stamp; robust to junk.

    ``datetime.fromisoformat`` only learned to accept a trailing ``Z`` in 3.11, and
    the ``timestamp`` field may carry a value built elsewhere — so normalise a ``Z``
    suffix and fall back to now(UTC) on anything unparseable. This affects **only**
    the filename stamp; the record's own ``timestamp`` field is written verbatim, so
    a degraded parse never corrupts the audit content, just its filename.
    """
    if not ts_raw:
        return datetime.now(timezone.utc)
    normalized = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
    try:
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        logger.warning(
            "could not parse record timestamp %r — using now() for the filename stamp", ts_raw
        )
        return datetime.now(timezone.utc)


def _unique_path(directory: Path, coin: str, timestamp: datetime) -> Path:
    """A not-yet-existing path for this coin/timestamp under ``directory``.

    The millisecond-stamped name from :func:`_filename` normally suffices, but two
    decisions for the same coin in the same millisecond (e.g. a retry loop or a
    batch run on a low-resolution clock) would collide — and the atomic rename in
    :func:`write_decision_log` would then silently overwrite, destroying the earlier
    audit record. Append a ``_1``/``_2``/... counter on collision so no record is
    ever lost, warning so the (unexpected) collision is visible.
    """
    base = _filename(coin, timestamp)
    path = directory / base
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while (candidate := directory / f"{stem}_{n}{suffix}").exists():
        n += 1
    logger.warning(
        "audit filename %s already exists — writing %s instead to avoid overwriting "
        "an earlier decision record",
        base,
        candidate.name,
    )
    return candidate


def write_decision_log(
    record: dict[str, Any],
    results_dir: str | Path,
    *,
    coin: str | None = None,
    timestamp: datetime | None = None,
) -> Path:
    """Write ``record`` as pretty JSON under ``<results_dir>/perp_decisions/``.

    ``coin``/``timestamp`` default to the values inside ``record`` so callers can
    just pass the record. Returns the path written.
    """
    coin = coin or record.get("coin", "UNKNOWN")
    if timestamp is None:
        timestamp = _parse_timestamp(record.get("timestamp"))

    directory = Path(results_dir) / "perp_decisions"
    directory.mkdir(parents=True, exist_ok=True)
    path = _unique_path(directory, coin, timestamp)
    # Write to a sibling temp file then atomically rename into place. A crash or a
    # serialization error mid-write (e.g. a non-JSON-able value in ``record``) would
    # otherwise leave a truncated/zero-length file at ``path``, silently corrupting
    # the audit trail; the rename only happens once the full record is on disk.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def log_decision(
    *,
    coin: str,
    decision: PerpTradeDecision,
    prompt: str,
    models: dict[str, str],
    rating: str,
    results_dir: str | Path,
    timestamp: datetime | None = None,
    rating_source: str | RatingSource = "explicit",
) -> tuple[dict[str, Any], Path]:
    """Build the record and write it; returns ``(record, path)``.

    Convenience wrapper for :func:`build_log_record` + :func:`write_decision_log`
    used by ``main.py``. ``timestamp`` defaults to now (UTC).
    """
    timestamp = timestamp or datetime.now(timezone.utc)
    record = build_log_record(
        coin=coin,
        decision=decision,
        prompt=prompt,
        models=models,
        rating=rating,
        timestamp=timestamp,
        rating_source=rating_source,
    )
    path = write_decision_log(record, results_dir, coin=coin, timestamp=timestamp)
    return record, path
