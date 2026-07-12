"""Two-layer client order ids for live execution (phase3-spec §8.2, pure).

The system tracks every live order under two ids:

- ``cloid_logical`` — human-readable, ``_``-joined, used ONLY inside this
  system (SQLite, logs, audit). Never sent to the exchange (§8.3 rule 7).
- ``cloid_hex`` — the 128-bit value actually sent as the exchange ``cloid``
  field: ``0x`` + the first 16 bytes of ``SHA-256(cloid_logical)`` as 32 hex
  characters.

The derivation is deterministic: one logical order maps to exactly one wire
id, forever. That is what makes the §8.3 idempotent-retry protocol work — a
retry re-derives the same cloid_hex, so the exchange can recognise the
duplicate instead of accepting a second order. The mapping is persisted in
``cloid_registry`` (repository.py) because the hash is one-way: the exchange
echoes back only cloid_hex, and reverse lookup through the registry is the
one way to decide an order is bot-owned (§19.3).

Segments join with ``_`` and a segment may itself contain ``_`` (the spec's
own example run id does), so cloid_logical is NOT parsed back into fields —
provenance queries go through the registry / orders rows, never through
string-splitting the id.
"""

from __future__ import annotations

import hashlib

from ..domains.perp.enum_guard import check_enum

__all__ = ["LIVE_ORDER_ROLES", "cloid_hex", "cloid_logical"]

# §8.1: every live order carries one of these roles. Phase 2 already uses the
# first four; close / emergency_close / cleanup_cancel are the live-only
# vocabulary. repository._ORDER_ROLES IS this frozenset (aliased, not copied),
# so the id layer and the write boundary cannot drift — do not re-introduce an
# independent set there.
LIVE_ORDER_ROLES = frozenset(
    {
        "entry",
        "rebalance",
        "close",
        "stop_loss",
        "take_profit",
        "emergency_close",
        "cleanup_cancel",
    }
)

# cloid_hex is "0x" + 32 hex chars = 16 bytes = 128 bits (§8.2).
_HEX_BYTES = 16


def _segment(value: object, *, name: str) -> str:
    """One cloid_logical segment as a string; empty or whitespace would corrupt
    the id's legibility (and an empty segment usually means a caller passed a
    field it doesn't actually have — fail loud, don't guess)."""
    text = str(value)
    if not text:
        raise ValueError(f"cloid segment {name!r} must be non-empty")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"cloid segment {name!r} must not contain whitespace: {text!r}")
    return text


def cloid_logical(
    *,
    prefix: str,
    run_id: str,
    symbol: str,
    output_id: str,
    plan_id: str,
    leg: str,
    slice_index: int,
    order_role: str,
) -> str:
    """The §8.2 suggested format, verbatim:

    ``<prefix>_<run_id>_<symbol>_<output_id>_<plan_id>_<leg>_<slice_index>_<order_role>``

    ``slice_index`` is zero-padded to three digits (the §8.2 example's ``000``)
    so ids sort in slice order; a single-shot order (close, SL, TP, emergency
    close) is slice 0 of its one-slice plan. ``leg`` is the flip leg
    (``open`` / ``close``) or a caller-chosen marker like ``na`` for a
    non-flip order — it is a display segment, not parsed vocabulary.
    """
    check_enum(order_role, LIVE_ORDER_ROLES, name="order_role")
    if slice_index < 0:
        raise ValueError(f"slice_index must be >= 0, got {slice_index}")
    segments = (
        _segment(prefix, name="prefix"),
        _segment(run_id, name="run_id"),
        _segment(symbol, name="symbol"),
        _segment(output_id, name="output_id"),
        _segment(plan_id, name="plan_id"),
        _segment(leg, name="leg"),
        f"{slice_index:03d}",
        order_role,
    )
    return "_".join(segments)


def cloid_hex(logical: str) -> str:
    """The wire form: ``0x`` + first 16 bytes of SHA-256(cloid_logical) as hex.

    Deterministic and total — same input, same output, forever (§8.2). The
    exchange sees only this value; §8.3 rule 7 pins every exchange-facing
    query (orderStatus, cancelByCloid) to it.
    """
    if not logical:
        raise ValueError("cloid_logical must be non-empty")
    digest = hashlib.sha256(logical.encode("utf-8")).digest()
    return "0x" + digest[:_HEX_BYTES].hex()
