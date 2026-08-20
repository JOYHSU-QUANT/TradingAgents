"""Encode/decode helpers and the ``UNSET`` sentinel shared by every table module.

Values cross this seam as native Python types (``Decimal``, ``datetime``,
``bool``) and are encoded to the schema's storage form here — Decimals to TEXT
(no REAL float), datetimes to ISO-8601 UTC, bools to ``0`` / ``1`` — so callers
never hand-serialize.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..ids import _canonical_instant

__all__ = ["UNSET", "Unset"]


def _iso_utc(value: datetime) -> str:
    """Canonical ISO-8601 UTC string for a datetime; rejects naive datetimes.

    All stored timestamps must be UTC and comparable as strings: the funding
    dedup key ``(run_id, symbol, funding_timestamp)`` and the deterministic
    ``funding_event_id`` both derive from the stored/serialized timestamp, so a
    naive-vs-aware or non-UTC-offset representation of the *same* instant would
    produce different keys and let a settlement post twice. Delegates to the one
    canonicalization rule in :mod:`..ids` so storage and id derivation can never
    drift apart.
    """
    return _canonical_instant(value, name="datetime value")


def _encode(value: Any) -> Any:
    """Map a native value to its SQLite storage form (see module docstring)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _iso_utc(value)
    return value


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    """Encoded ``INSERT INTO table (cols) VALUES (?, ...)`` — the one write path.

    Column names come from ``values`` keys (all module-internal, never user
    input), so the interpolated identifier list is not an injection surface;
    every *value* is bound as a parameter.
    """
    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    columns = ", ".join(cols)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        [_encode(values[c]) for c in cols],
    )


def _dec_or_none(value: Any) -> Decimal | None:
    """Decode a stored TEXT value back to Decimal; ``None`` stays ``None``."""
    return None if value is None else Decimal(value)


class Unset:
    """Sentinel type: a keyword the caller did not supply (vs an explicit ``None``)."""


# Every patch-style writer uses this as a default argument value — defaults are
# bound at function-definition (import) time, so the sentinel must exist before
# those sibling table modules import it.
#
# PUBLIC, unlike most of this module's helpers: a caller sometimes has to say
# "leave this column ALONE" as a *value*, and the distinction matters. The live
# payload path is the case — a failed payload write must omit the column, not
# clear it (an explicit None CLEARS), so live.payloads hands this back instead of
# None. Without a name for "unset", that caller can only pass None and silently
# erase whatever an earlier successful write recorded.
UNSET = Unset()

# Internal spellings kept so the many `x: T | _Unset = _UNSET` signatures in the
# sibling table modules read unchanged; they are the same object/type.
_Unset = Unset
_UNSET = UNSET
