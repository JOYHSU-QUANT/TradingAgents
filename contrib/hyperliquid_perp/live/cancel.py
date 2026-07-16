"""One bot-owned cancel under the §8.3/§16.5 evidence protocol — shared.

Extracted from the PR 2 kill-switch shutdown sweep so the PR 4 startup
reconciliation (§19.3: cancel stale bot-owned orders) runs the IDENTICAL
protocol instead of a near-copy that could drift: attempt row before the wire
(status ``submitted`` — a crash between the two leaves durable evidence the
cancel MAY have landed), outcome patch after, raw ack payload written as
evidence, and a successful cancel settling the local orders row so no phantom
``open`` row survives the sweep that cancelled it.

The §4.1 gate is enforced INSIDE the signed client (``cancel_by_cloid`` calls
``require_exchange_action``), not re-checked here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from ..exchanges.hyperliquid.errors import ExchangeError
from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
from ..paper.clock import Clock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import live_order_attempt_id
from .payloads import payload_column, write_raw_payload

__all__ = ["cancel_bot_order_with_evidence"]

logger = logging.getLogger(__name__)


def cancel_bot_order_with_evidence(
    *,
    db: Database,
    client: HyperliquidSignedClient,
    run_id: str,
    payload_dir: Path,
    clock: Clock,
    coin: str,
    cloid_hex: str,
    cloid_logical: str,
    cancel_reason: str,
) -> None:
    """Cancel one bot-owned order by cloid, leaving the full evidence trail.

    ``clock`` is read twice — once for the attempt's ``requested_at``, once
    after the round-trip for the acknowledgement stamp — so the wire latency
    sits between the two, exactly as the order path records it. Raises the
    underlying failure after recording it: the caller's sweep loop records the
    failure in its own audit event and carries on (§18.2 rule 5 / §19.3).

    ``cancel_reason`` lands on the settled orders row (``shutdown_cancel`` from
    the kill-switch sweep, ``stale_startup_cancel`` from §19.3 startup
    recovery), so a post-mortem can tell WHICH sweep retired the order.
    """
    attempt_index = repo.next_live_attempt_index(
        db.conn, action="cancel_by_cloid", cloid_hex=cloid_hex
    )
    attempt_id = live_order_attempt_id(run_id, "cancel_by_cloid", cloid_hex, attempt_index)
    now: datetime = clock.now()
    local_order = repo.get_order_by_cloid_hex(db.conn, cloid_hex)
    with db.transaction() as conn:
        repo.insert_live_order_attempt(
            conn,
            attempt_id=attempt_id,
            run_id=run_id,
            action="cancel_by_cloid",
            symbol=coin,
            attempt_index=attempt_index,
            order_id=None if local_order is None else local_order["order_id"],
            cloid_logical=cloid_logical,
            cloid_hex=cloid_hex,
            requested_at=now,
        )
    try:
        ack = client.cancel_by_cloid(coin=coin, cloid_hex=cloid_hex)
    except Exception as exc:
        # Record 'failed' WITHOUT letting the record replace `exc`. The
        # original is the diagnosis, and the caller puts it verbatim into its
        # own audit event — a busy DB must not make the permanent record of a
        # failed cancel read "database is locked" instead of the exchange's
        # actual reason. Log first (the live modules' standing ordering rule),
        # then record.
        logger.warning("cancel of cloid %s failed: %s", cloid_hex, exc)
        try:
            with db.transaction() as conn:
                repo.update_live_order_attempt(
                    conn, attempt_id, status="failed", error_message=str(exc)
                )
        except Exception:
            logger.exception(
                "could not record cancel attempt %s as 'failed'; it stays "
                "'submitted' (outcome unknown)",
                attempt_id,
            )
        raise
    done_at = clock.now()
    # The cancel's raw response is evidence too — same protocol the order
    # path follows, and the attempt row has carried the column all along.
    raw_path = write_raw_payload(
        payload_dir=payload_dir,
        kind="cancel",
        key=cloid_hex,
        payload=ack.raw,
        now=done_at,
    )
    with db.transaction() as conn:
        repo.update_live_order_attempt(
            conn,
            attempt_id,
            status="acknowledged" if ack.success else "rejected",
            error_message=ack.error,
            acknowledged_at=done_at,
            raw_exchange_payload_path=payload_column(raw_path),
        )
        if ack.success and local_order is not None:
            repo.update_order(
                conn,
                local_order["order_id"],
                status="canceled",
                exchange_status="canceled",
                canceled_at=done_at,
                cancel_reason=cancel_reason,
                updated_at=done_at,
            )
    if not ack.success:
        raise ExchangeError(f"cancel rejected: {ack.error}")
