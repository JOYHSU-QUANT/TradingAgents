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
    "accounting_adjustment_id",
    "decision_attempt_id",
    "exchange_fill_key",
    "fill_id",
    "funding_event_id",
    "live_fill_id",
    "live_order_attempt_id",
    "slice_id",
    "usable_fill_tid",
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


def usable_fill_tid(tid: object) -> bool:
    """Whether a raw ``tid`` can serve as the §14.2 dedupe identity.

    THE single definition of a usable fill tid, shared by the ingest side
    (``live/fills.py``, which raises ``MalformedResponseError`` on failure)
    and the reconciliation cross-check (``live/reconcile.py``, which
    withholds invalid-fill verdicts) — one vocabulary, so the two legs that
    together decide MANUAL safe mode can never drift. Usable means the
    exchange's documented wire types (str/int; ``bool`` is an int subclass,
    not an id) and non-empty under the ``str()`` coercion
    :func:`exchange_fill_key` applies.
    """
    return isinstance(tid, (str, int)) and not isinstance(tid, bool) and str(tid) != ""


def exchange_fill_key(*, tid: object) -> str:
    """The §14.2 dedupe key for one live fill — exactly-once across all sources.

    The key is the exchange's own stable fill id: ``tid|<value>``. Hyperliquid
    supplies ``tid`` on EVERY fill — the SDK's ``Fill`` type declares it a
    required ``int``, on both the ``userFills`` socket stream and the
    ``userFillsByTime`` REST endpoint — so the same fill derives the same key
    from every source, which is exactly what §14.3 rule 5 demands.

    §14.2's composite fallback (``symbol + oid + fill_time + side + price +
    size``) is deliberately NOT implemented. Its precondition — "the exchange has
    no stable fill id" — is false for Hyperliquid, and the composite is not a
    safe substitute where it does apply:

    - it can COLLIDE two genuinely distinct fills (one order matching two resting
      orders at the same price, side, size and millisecond derives one key), so
      the second real fill is silently dropped as a duplicate — an undercount
      that replay cannot detect, because replay only re-derives what was recorded;
    - it can DIVERGE from the tid form for the same fill if any source ever
      omitted ``tid``, double-posting the money.

    So a live fill without a ``tid`` is treated as MALFORMED (see
    ``ExchangeFill.parse``): its raw payload is recorded and it is not applied
    (§11.3), surfacing for reconciliation rather than being keyed on something
    that can silently miscount. If a future venue genuinely lacks a stable fill
    id, the composite belongs here — with its collision risk confronted, not
    inherited.
    """
    if tid is None or str(tid) == "":
        raise ValueError("a live fill dedupe key requires the exchange's tid (§14.2)")
    return _SEP.join((_part("tid", name="tag"), _part(tid, name="tid")))


def live_fill_id(run_id: str, exchange_fill_key: str) -> str:
    """Unique key for one live fill row (exactly-once on re-ingest).

    Derived from the §14.2 dedupe key, so the same exchange fill re-arriving from
    any source re-derives the same ``fill_id`` and the ``fills`` PRIMARY KEY
    rejects it — a second, independent guard beside the UNIQUE ``exchange_fill_key``
    index. Run-prefixed so the id reads like the paper engine's ids in the store,
    though the exchange key alone is already unique.

    ``exchange_fill_key`` is itself a ``|``-joined composite, so it is embedded
    VERBATIM rather than through ``_part`` (which forbids the separator): a
    fill_id is a PRIMARY KEY, never parsed back into fields, and the leading
    ``<run_id>|livefill|`` tag keeps it unambiguous against every other id shape.
    """
    return (
        f"{_part(run_id, name='run_id')}{_SEP}livefill{_SEP}"
        f"{_part_nonempty(exchange_fill_key, name='exchange_fill_key')}"
    )


def accounting_adjustment_id(
    run_id: str, adjustment_type: str, target_id: str, seq: int = 0
) -> str:
    """Unique key for one §15 accounting correction (exactly-once backfill).

    Keyed on the target being corrected (a fill id, a funding event id), the
    correction TYPE, and WHICH correction of that type it is: ``seq`` counts the
    corrections already posted for that (target, type) — the first is ``0``, a
    later and genuinely different one is ``1``, ``2``, …

    The sequence exists because a target can legitimately be corrected more than
    once: a referral discount, a staking-tier rebate applied after the fact, an
    exchange fee correction. Keyed on (target, type) alone, that second correction
    would collide with the first on the PRIMARY KEY and be refused — which does not
    merely lose the correction, it WEDGES the reconciliation job, which re-hits the
    same target and fails again on every pass, forever.

    Exactly-once is therefore no longer the id's job alone. The caller derives
    ``seq`` from the corrections already recorded (see ``backfill_fill_fee``, which
    counts them inside the posting transaction) and must treat a re-learned but
    UNCHANGED amount as a no-op rather than as a new correction. What the id still
    guarantees is that two writers racing to post the same sequence collide instead
    of double-posting.

    ``target_id`` may itself be a ``|``-joined composite (a live ``fill_id`` is), so
    — as in ``live_fill_id`` — it is embedded VERBATIM as the TRAILING segment rather
    than through ``_part``: the id is a PRIMARY KEY, never parsed back into fields,
    and the fixed ``<run_id>|<type>|<seq>|`` prefix keeps it unambiguous. That is why
    ``seq`` sits before it, not after.
    """
    return (
        f"{_part(run_id, name='run_id')}{_SEP}"
        f"{_part(adjustment_type, name='adjustment_type')}{_SEP}"
        f"{_part(seq, name='seq')}{_SEP}"
        f"{_part_nonempty(target_id, name='target_id')}"
    )


def _part_nonempty(value: object, *, name: str) -> str:
    """Like :func:`_part` but permits the ``|`` separator (a trailing composite id).

    Only safe for the LAST segment of a composite that is never parsed back into
    fields — the fixed prefix ahead of it is what keeps the whole id unambiguous.
    """
    text = str(value)
    if not text:
        raise ValueError(f"id component {name!r} must not be empty")
    return text
