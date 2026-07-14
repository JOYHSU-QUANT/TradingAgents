"""Deterministic identifiers for the dedup / exactly-once keys (pure).

Several rows must be reproducible from their logical inputs so a retry or a
restart re-derives the *same* id and the UNIQUE constraints (schema.py) reject a
duplicate instead of double-applying it:

- ``slice_id`` = ``run_id + plan_id + flip_leg + slice_index`` (phase2-data §1)
  — one TWAP slice may produce at most one fill;
- ``fill_id`` = ``run_id + order_id + fill_index`` — a ``paper_market`` / SL /
  TP fill carries no ``slice_id``, so the ``fills`` PRIMARY KEY is its only
  exactly-once guard; a deterministic derivation keeps that guarantee uniform
  instead of depending on the caller minting a fresh id per retry;
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

__all__ = [
    "decision_attempt_id",
    "fill_id",
    "funding_event_id",
    "live_order_attempt_id",
    "slice_id",
]

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


def fill_id(run_id: str, order_id: str, fill_index: int = 0) -> str:
    """Unique key for one simulated fill of an order (exactly-once on retry).

    TWAP slice fills are already deduplicated by their ``slice_id`` UNIQUE
    constraint; ``paper_market`` / SL / TP fills have no slice, so their only
    guard is the ``fills`` PRIMARY KEY itself. Deriving the id from the logical
    inputs means a crash-retry re-derives the *same* id and the PK rejects the
    double-post. ``fill_index`` is ``0`` for the single-fill simulation paths
    and counts up if a future model ever splits one order into several fills.
    """
    return _SEP.join(
        (
            _part(run_id, name="run_id"),
            _part(order_id, name="order_id"),
            _part(fill_index, name="fill_index"),
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


def live_order_attempt_id(run_id: str, action: str, target: str, attempt_index: int) -> str:
    """Unique key for one live exchange round-trip (phase3-spec §8.3).

    ``target`` is the cloid_hex for place / cancel_by_cloid attempts and the
    exchange order id for a by-oid cancel — whichever identifier the exchange
    action itself is addressed by. Deterministic for the same reason as
    ``fill_id``: a crash-retry that re-derives the same (action, target,
    attempt_index) collides on the PRIMARY KEY instead of silently minting a
    second row for the same round-trip.

    CAVEAT for the by-oid ``cancel`` action (no writer yet — PR 4/5 will be the
    first): the attempt_index such a row needs cannot come from
    ``next_live_attempt_index``, which keys on cloid_hex alone, and the table's
    ``UNIQUE (cloid_hex, action, attempt_index)`` does not constrain it either
    (its cloid_hex is NULL, and NULLs stay distinct). This PRIMARY KEY is its
    ONLY guard, so whoever writes the first by-oid cancel owns deriving its
    index — do not assume the cloid-based helper already covers it.
    """
    return _SEP.join(
        (
            _part(run_id, name="run_id"),
            _part(action, name="action"),
            _part(target, name="target"),
            _part(attempt_index, name="attempt_index"),
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
