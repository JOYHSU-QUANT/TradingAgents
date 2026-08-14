"""The ``cloid_registry`` table (phase3-spec §8.2 / §19.3) — the bot-owned lookup.

Not to be confused with :mod:`..cloid` one level up, the id-derivation layer
whose logical↔hex pairs this registry persists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ...domains.perp.enum_guard import check_enum
from ._base import _insert
from ._vocab import _ORDER_ROLES

__all__ = ["get_cloid_by_hex", "get_cloid_by_logical", "insert_cloid_mapping"]


# --------------------------------------------------------------------------
# cloid_registry (phase3-spec §8.2 / §19.3) — the bot-owned lookup table
# --------------------------------------------------------------------------


def insert_cloid_mapping(
    conn: sqlite3.Connection,
    *,
    cloid_logical: str,
    cloid_hex: str,
    run_id: str,
    symbol: str,
    order_role: str,
    created_at: datetime | None = None,
) -> None:
    """Register one logical↔wire id pair; idempotent for the exact same pair.

    §8.3 rule 1: a retry reuses the same cloid, and the registry write happens
    before every send attempt — so re-registering an identical mapping is a
    no-op, while the same hex arriving with a DIFFERENT logical id (or vice
    versa) is a real corruption of the bot-owned lookup and fails loud.
    """
    check_enum(order_role, _ORDER_ROLES, name="order_role")
    existing = get_cloid_by_hex(conn, cloid_hex)
    if existing is not None:
        # A retry legitimately re-registers the exact same pair; the same hex
        # with a different logical id would re-point the §19.3 lookup.
        if existing["cloid_logical"] != cloid_logical:
            raise ValueError(
                f"cloid_registry conflict: ({cloid_logical!r}, {cloid_hex!r}) collides "
                f"with existing ({existing['cloid_logical']!r}, {existing['cloid_hex']!r})"
            )
        return
    try:
        _insert(
            conn,
            "cloid_registry",
            {
                "cloid_hex": cloid_hex,
                "cloid_logical": cloid_logical,
                "run_id": run_id,
                "symbol": symbol,
                "order_role": order_role,
                "created_at": created_at or datetime.now(timezone.utc),
            },
        )
    except sqlite3.IntegrityError:
        # The hex was free, so this hit the cloid_logical UNIQUE index: the
        # same logical id already maps to a DIFFERENT hex. Name the collision
        # instead of leaking the raw constraint error.
        row = get_cloid_by_logical(conn, cloid_logical)
        existing_pair = (
            "<row vanished mid-transaction>"
            if row is None
            else f"({row['cloid_logical']!r}, {row['cloid_hex']!r})"
        )
        raise ValueError(
            f"cloid_registry conflict: ({cloid_logical!r}, {cloid_hex!r}) collides "
            f"with existing {existing_pair}"
        ) from None


def get_cloid_by_hex(conn: sqlite3.Connection, cloid_hex: str) -> sqlite3.Row | None:
    """§19.3 reverse lookup: the one way to decide an exchange order is bot-owned."""
    return conn.execute("SELECT * FROM cloid_registry WHERE cloid_hex = ?", (cloid_hex,)).fetchone()


def get_cloid_by_logical(conn: sqlite3.Connection, cloid_logical: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cloid_registry WHERE cloid_logical = ?", (cloid_logical,)
    ).fetchone()
