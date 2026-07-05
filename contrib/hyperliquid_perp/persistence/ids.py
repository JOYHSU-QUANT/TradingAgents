"""Deterministic identifiers for the dedup / exactly-once keys (pure).

Several rows must be reproducible from their logical inputs so a retry or a
restart re-derives the *same* id and the UNIQUE constraints (schema.py) reject a
duplicate instead of double-applying it:

- ``slice_id`` = ``run_id + plan_id + flip_leg + slice_index`` (phase2-data §1)
  — one TWAP slice may produce at most one fill;
- ``funding_event_id`` = ``run_id + symbol + funding_timestamp`` (phase2-data §10)
  — funding posts exactly once per hourly settlement;
- ``decision_attempt_id`` = ``run_id + scheduled_at`` (phase2-spec §3.1) — a
  scheduled cycle's retry state survives restart under one id.

Ids are readable ``|``-joined composites, not hashes, so a row's provenance is
legible in the DB and CSV export. Components are normalised to ``str`` and must
not themselves contain ``|`` (guarded here) so the composite stays unambiguous.

Timestamp components are taken as aware ``datetime`` and canonicalised to UTC
ISO-8601 *here*, so two representations of the same instant (``Z`` vs ``+05:00``
offset) cannot derive different ids and defeat the UNIQUE dedup constraints —
the guarantee must not depend on every caller remembering to normalise.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["decision_attempt_id", "funding_event_id", "slice_id"]

_SEP = "|"


def _canonical_instant(value: datetime, *, name: str) -> str:
    """UTC ISO form of an aware datetime; a naive one is rejected outright."""
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc).isoformat()


def _part(value: object, *, name: str) -> str:
    """One id component as a string, rejecting the separator that would alias ids."""
    text = str(value)
    if _SEP in text:
        raise ValueError(f"id component {name!r} must not contain {_SEP!r}: {text!r}")
    if not text:
        raise ValueError(f"id component {name!r} must not be empty")
    return text


def slice_id(run_id: str, plan_id: str, flip_leg: str | None, slice_index: int) -> str:
    """Unique key for one TWAP slice; ``flip_leg`` is ``None`` for a non-flip plan."""
    return _SEP.join(
        (
            _part(run_id, name="run_id"),
            _part(plan_id, name="plan_id"),
            # A non-flip plan has no leg; normalise None to an empty slot (still a
            # fixed 4-field shape) rather than the string "None".
            "" if flip_leg is None else _part(flip_leg, name="flip_leg"),
            _part(slice_index, name="slice_index"),
        )
    )


def funding_event_id(run_id: str, symbol: str, funding_timestamp: datetime) -> str:
    """Unique key for one hourly funding settlement (exactly-once posting).

    The caller is responsible for the *settlement-hour* semantics (flooring to
    the hour — see ``record_funding``); this derivation only pins the tz form.
    """
    ts = _canonical_instant(funding_timestamp, name="funding_timestamp")
    return _SEP.join(
        (
            _part(run_id, name="run_id"),
            _part(symbol, name="symbol"),
            _part(ts, name="funding_timestamp"),
        )
    )


def decision_attempt_id(run_id: str, scheduled_at: datetime) -> str:
    """Unique key for one scheduled decision cycle (retry state survives restart)."""
    return _SEP.join(
        (
            _part(run_id, name="run_id"),
            _part(_canonical_instant(scheduled_at, name="scheduled_at"), name="scheduled_at"),
        )
    )
