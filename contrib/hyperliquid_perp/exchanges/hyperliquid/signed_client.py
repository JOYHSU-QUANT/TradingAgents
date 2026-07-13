"""Thin wrapper around the official Hyperliquid SDK ``Exchange`` client.

The signed counterpart of :mod:`.sdk_client`: centralises network selection and
the defensive construction pattern so nothing else builds ``Exchange`` directly.
PR 2 adds the §7 exchange actions this phase needs — IOC limit order with a
cloid, cancel (by oid and by cloid), orderStatus queries, scheduleCancel — all
returning structured results (:class:`OrderAck` / :class:`CancelAck`) instead
of raw SDK dicts. The §4.1
:class:`~contrib.hyperliquid_perp.live.order_gate.RealOrderGate` is bound at
construction and judges every signed MUTATION: order placement passes the full
condition list, cancel/scheduleCancel the base subset (§13.1 allows those in
safe mode). Queries are read-only and ungated.

This layer is transport only: no persistence, no retry policy. The §8.3
idempotent-retry protocol (registry write before send, query-before-resend on
duplicate) lives in :mod:`contrib.hyperliquid_perp.live.orders`, the intended
caller.

The agent private key is consumed once at construction to build the SDK's
``LocalAccount``; it is never stored on this object, and ``__repr__`` shows
only public addresses (§6 rule 2: the key must not reach logs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid

from .errors import ExchangeError, ExchangeRequestError, MalformedResponseError
from .sdk_client import (
    _BASE_URLS,
    DEFAULT_NETWORK_TIMEOUT_S,
    _init_rejects_kwargs,
    account_from_agent_key,
    call_sdk,
)

if TYPE_CHECKING:
    from ...live.order_gate import RealOrderGate

# The keyword arguments we pass to ``Exchange(...)`` in __init__, for the same
# signature-based version-mismatch triage sdk_client applies to ``Info``
# (shared logic: ``sdk_client._init_rejects_kwargs``).
_EXCHANGE_KWARGS = ("base_url", "account_address", "spot_meta", "timeout")

# §8.3 rule 2 triage: the exchange rejects a resend of a still-known cloid with
# an error mentioning the duplicate. Matched on markers, not exact text (the
# wire message is not a versioned contract); a false negative merely fails the
# retry loudly, while a false positive routes into the query-before-resend path
# — which is safe by construction (it trusts orderStatus, not this string).
_DUPLICATE_CLOID_MARKERS = ("duplicate", "already exists", "already used")


def is_duplicate_cloid_error(message: str | None) -> bool:
    """True when an order-status error text signals a duplicate/known cloid."""
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in _DUPLICATE_CLOID_MARKERS)


@dataclass(frozen=True)
class OrderAck:
    """One order action's parsed outcome (§7 ``order`` / the per-order status).

    ``status`` is the exchange's per-order verdict: ``"resting"`` (accepted,
    on the book), ``"filled"`` (IOC executed immediately), or ``"error"``
    (rejected — ``error`` carries the exchange text, and :attr:`is_duplicate`
    routes the §8.3 retry protocol). ``raw`` is the untouched SDK response for
    the audit trail (raw_exchange_payload_path).
    """

    status: str
    exchange_order_id: str | None = None
    filled_size: Decimal | None = None
    average_price: Decimal | None = None
    error: str | None = None
    raw: Any = None

    def __post_init__(self) -> None:
        # The docstring's status↔field contract, enforced: a verdict whose
        # evidence fields disagree with it cannot exist (parser or test double).
        if self.status not in ("resting", "filled", "error"):
            raise ValueError(f"OrderAck.status must be resting/filled/error, got {self.status!r}")
        if self.status == "error":
            ok = (
                self.error is not None
                and self.exchange_order_id is None
                and self.filled_size is None
                and self.average_price is None
            )
        elif self.status == "filled":
            ok = (
                self.error is None
                and self.exchange_order_id is not None
                and self.filled_size is not None
                and self.average_price is not None
            )
        else:  # resting — on the book, nothing filled yet.
            ok = (
                self.error is None
                and self.exchange_order_id is not None
                and self.filled_size is None
                and self.average_price is None
            )
        if not ok:
            raise ValueError(f"OrderAck fields do not satisfy the {self.status!r} contract")

    @property
    def accepted(self) -> bool:
        return self.status in ("resting", "filled")

    @property
    def is_duplicate(self) -> bool:
        return self.status == "error" and is_duplicate_cloid_error(self.error)


@dataclass(frozen=True)
class CancelAck:
    """One cancel action's parsed outcome: ``success`` or the exchange's error."""

    success: bool
    error: str | None = None
    raw: Any = None

    def __post_init__(self) -> None:
        # "success or the error" is exclusive: a refusal must carry its reason
        # and a success must not smuggle one.
        if self.success == (self.error is not None):
            raise ValueError(
                f"CancelAck requires error exactly when success is False, "
                f"got success={self.success} error={self.error!r}"
            )


def _response_payload(response: Any, *, action: str) -> Any:
    """Unwrap the SDK's ``{"status": "ok"/"err", "response": ...}`` envelope.

    A top-level ``"err"`` is an exchange-level rejection (bad signature, bad
    payload) — surfaced as a normal error string by callers, not an exception,
    so the §8.3 protocol can inspect it. Anything not shaped like the envelope
    is a malformed response and fails loud.
    """
    if not isinstance(response, dict) or "status" not in response:
        raise MalformedResponseError(
            f"Hyperliquid {action} response is not a status envelope: {response!r}"
        )
    if response["status"] != "ok":
        # The err payload is the message itself (a string) per the SDK.
        return {"error": str(response.get("response"))}
    return response.get("response")


def _raise_on_top_level_error(response: Any, *, action: str) -> None:
    """Raise when an action's response is a top-level error envelope.

    For statusless actions (scheduleCancel) this IS the whole verdict: no
    per-order statuses exist, so a top-level error is the only failure shape.
    """
    payload = _response_payload(response, action=action)
    if isinstance(payload, dict) and "error" in payload and "data" not in payload:
        raise ExchangeRequestError(f"Hyperliquid {action} rejected: {payload['error']}")


def _single_status(response: Any, *, action: str) -> Any:
    """The one per-order status out of a single-order action's response.

    This wrapper only ever submits one order/cancel per request, so exactly
    one status must come back — zero or several means the response does not
    match the request and nothing can be safely assumed about what the
    exchange did.
    """
    payload = _response_payload(response, action=action)
    if isinstance(payload, dict) and "error" in payload and "data" not in payload:
        return payload  # top-level err envelope, already normalised
    data = payload.get("data") if isinstance(payload, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise MalformedResponseError(
            f"Hyperliquid {action} response carries {0 if not statuses else len(statuses)}"
            f" statuses (expected exactly 1): {response!r}"
        )
    return statuses[0]


def _parse_order_ack(response: Any) -> OrderAck:
    status = _single_status(response, action="order")
    if isinstance(status, dict) and "resting" in status:
        resting = status["resting"]
        return OrderAck(
            status="resting",
            exchange_order_id=str(resting["oid"]),
            raw=response,
        )
    if isinstance(status, dict) and "filled" in status:
        filled = status["filled"]
        return OrderAck(
            status="filled",
            exchange_order_id=str(filled["oid"]),
            filled_size=Decimal(str(filled["totalSz"])),
            average_price=Decimal(str(filled["avgPx"])),
            raw=response,
        )
    if isinstance(status, dict) and "error" in status:
        return OrderAck(status="error", error=str(status["error"]), raw=response)
    raise MalformedResponseError(f"Hyperliquid order status not recognised: {status!r}")


def _parse_cancel_ack(response: Any) -> CancelAck:
    status = _single_status(response, action="cancel")
    if status == "success":
        return CancelAck(success=True, raw=response)
    if isinstance(status, dict) and "error" in status:
        return CancelAck(success=False, error=str(status["error"]), raw=response)
    raise MalformedResponseError(f"Hyperliquid cancel status not recognised: {status!r}")


class HyperliquidSignedClient:
    """Owns the SDK ``Exchange`` instance for the agent wallet.

    ``wallet_address`` is the MAIN wallet the agent trades on behalf of (the
    SDK's ``account_address``); the agent key only signs. The §4.1
    :class:`RealOrderGate` is bound at construction — one client, one gate —
    so every mutating method is judged by the same gate whose flags the kill
    switch manager and (PR 4/5) state machines maintain; a call site cannot
    substitute a permissive gate of its own. Decimal quantities/prices cross
    to the SDK's floats only here, at the wire boundary (callers' math stays
    all-Decimal).
    """

    # Same bounded timeout default and rationale as HyperliquidClient.__init__
    # — PR 2's order methods land on this class, so the signing path must
    # never inherit an unbounded hang by default.
    def __init__(
        self,
        network: str,
        agent_key: str,
        *,
        wallet_address: str,
        gate: RealOrderGate,
        timeout: float | None = DEFAULT_NETWORK_TIMEOUT_S,
    ) -> None:
        key = network.strip().lower()
        if key not in _BASE_URLS:
            raise ValueError(f"network must be one of {sorted(_BASE_URLS)}, got {network!r}")
        self.network = key
        self.wallet_address = wallet_address
        self._gate = gate
        # Leak-safe key handling lives in one shared home (§6 rule 2).
        account = account_from_agent_key(agent_key, error_cls=ExchangeError)
        # spot_meta stub: same 0.22.0 mainnet-spot-meta crash defense as
        # sdk_client — Exchange builds its own Info internally and we only
        # trade perps. Perp meta still auto-fetches.
        try:
            self._exchange = Exchange(
                account,
                base_url=_BASE_URLS[key],
                account_address=wallet_address,
                spot_meta={"tokens": [], "universe": []},
                timeout=timeout,
            )
        except TypeError as exc:
            # Same signature-based triage as sdk_client: only relabel as a
            # version mismatch when a kwarg we pass is genuinely unsupported.
            if not _init_rejects_kwargs(Exchange.__init__, _EXCHANGE_KWARGS):
                raise
            raise ExchangeError(
                f"Hyperliquid SDK Exchange() rejected its arguments — incompatible "
                f"hyperliquid-python-sdk version? ({type(exc).__name__}: {exc})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — construction meta-fetch is network I/O
            # Exchange() builds its own Info, whose construction auto-fetches
            # perp meta — same translation rule as sdk_client's Info guard so a
            # network failure stays in callers' named ExchangeError lanes.
            raise ExchangeRequestError(
                f"Hyperliquid request failed: {type(exc).__name__}: {exc}"
            ) from exc

    @property
    def agent_address(self) -> str:
        """The agent wallet's public address (safe to log)."""
        return self._exchange.wallet.address

    def health_check(self) -> None:
        """Prove the signed client can reach its network (read-only, no order).

        Round-trips a ``user_state`` read for the main wallet through the
        ``Exchange``'s own Info transport — the same base URL and timeout every
        signed action would use — so a bad network choice or unreachable
        endpoint fails here, at startup, instead of on the first live order.
        Raises ``ExchangeRequestError`` on failure.
        """
        call_sdk(self._exchange.info.user_state, self.wallet_address)

    # ---- §7 exchange actions (PR 2) -------------------------------------

    def place_ioc_limit(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: Decimal,
        limit_price: Decimal,
        cloid_hex: str,
        reduce_only: bool = False,
    ) -> OrderAck:
        """Submit one IOC limit order carrying its cloid (§7 ``order``, §9).

        The FULL §4.1 gate (bound at construction) runs first — a rejection
        raises ``LiveOrderGateRejected`` before any network traffic. The cloid
        is mandatory: every order this system sends must be attributable
        through the registry (§8.2), and an anonymous order would be judged
        non-bot-owned by our own reconciliation (§19.3). Returns the parsed
        :class:`OrderAck`; an exchange-side rejection is an ``error`` ack, not
        an exception (the §8.3 retry protocol needs to inspect it).
        """
        self._gate.require_order(coin)
        response = call_sdk(
            self._exchange.order,
            coin,
            is_buy,
            float(size),
            float(limit_price),
            {"limit": {"tif": "Ioc"}},
            reduce_only,
            Cloid.from_str(cloid_hex),
        )
        return _parse_order_ack(response)

    def cancel_by_oid(self, *, coin: str, exchange_order_id: str) -> CancelAck:
        """Cancel one order by exchange order id (§7 ``cancel``).

        Base gate only: §13.1 allows cancelling bot-owned orders in safe mode.
        """
        self._gate.require_exchange_action()
        response = call_sdk(self._exchange.cancel, coin, int(exchange_order_id))
        return _parse_cancel_ack(response)

    def cancel_by_cloid(self, *, coin: str, cloid_hex: str) -> CancelAck:
        """Cancel one order by client order id (§7 ``cancelByCloid``).

        §8.3 rule 7: the exchange-facing identifier is always cloid_hex.
        """
        self._gate.require_exchange_action()
        response = call_sdk(self._exchange.cancel_by_cloid, coin, Cloid.from_str(cloid_hex))
        return _parse_cancel_ack(response)

    def query_order_by_cloid(self, cloid_hex: str) -> Any:
        """The exchange's order status for a cloid (§7 ``orderStatus``; read-only).

        Returns the raw orderStatus payload — the §8.3 duplicate protocol and
        PR 4's reconciliation interpret it against their own vocabularies; the
        transport does not guess at a shared shape for both.
        """
        return call_sdk(
            self._exchange.info.query_order_by_cloid,
            self.wallet_address,
            Cloid.from_str(cloid_hex),
        )

    def query_order_by_oid(self, exchange_order_id: str) -> Any:
        """The exchange's order status for an exchange order id (read-only)."""
        return call_sdk(
            self._exchange.info.query_order_by_oid,
            self.wallet_address,
            int(exchange_order_id),
        )

    def open_orders(self) -> Any:
        """The main wallet's open orders (read-only; §18 shutdown / §19.3 startup).

        Uses ``frontendOpenOrders``, not the basic ``openOrders`` endpoint:
        only the frontend variant is documented to carry order metadata
        including the ``cloid`` field, and the §19.3 bot-owned decision is a
        cloid reverse-lookup — without it every bot order would silently
        classify as non-bot-owned.
        """
        return call_sdk(self._exchange.info.frontend_open_orders, self.wallet_address)

    def schedule_cancel(self, *, cancel_at: datetime) -> None:
        """Arm/refresh the dead man's switch (§7 ``scheduleCancel``, §18).

        ``cancel_at`` is the instant the exchange should cancel this wallet's
        open orders if no refresh lands first; converted to the epoch-ms form
        the API takes here, at the wire boundary. Failure raises
        ``ExchangeRequestError`` — the kill switch manager turns that into a
        ``kill_switch_refresh_failed`` event, so this method never half-hides
        an unarmed switch behind a return code.
        """
        self._gate.require_exchange_action()
        if cancel_at.tzinfo is None:
            raise ValueError("cancel_at must be timezone-aware (UTC)")
        response = call_sdk(self._exchange.schedule_cancel, int(cancel_at.timestamp() * 1000))
        _raise_on_top_level_error(response, action="scheduleCancel")

    def clear_scheduled_cancel(self) -> None:
        """Disarm the dead man's switch (§7 ``scheduleCancel`` unset, §18.2 rule 6).

        The exchange-side trigger is wallet-wide — at the deadline it cancels
        EVERY open order on the wallet, including non-bot-owned orders the
        shutdown sweep deliberately left alone (§19.3). After a fully clean
        sweep there is no bot order left for the backstop to protect, so the
        kill switch manager unsets the trigger. Failure raises — the caller
        records it and leaves the switch armed (the fail-safe direction).
        """
        self._gate.require_exchange_action()
        response = call_sdk(self._exchange.schedule_cancel, None)
        _raise_on_top_level_error(response, action="scheduleCancel")

    def __repr__(self) -> str:  # never the key — addresses only (§6 rule 2)
        return (
            f"{type(self).__name__}(network={self.network!r}, "
            f"agent_address={self.agent_address!r}, "
            f"wallet_address={self.wallet_address!r})"
        )
