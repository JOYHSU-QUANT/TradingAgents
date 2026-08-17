"""Thin wrapper around the official Hyperliquid SDK ``Exchange`` client.

The signed counterpart of :mod:`.sdk_client`: centralises network selection and
the defensive construction pattern so nothing else builds ``Exchange`` directly.
PR 2 adds the §7 exchange actions this phase needs — IOC limit order with a
cloid, cancel (by oid and by cloid), orderStatus queries, scheduleCancel — all
returning structured results (:class:`OrderAck` / :class:`CancelAck`) instead
of raw SDK dicts. The §4.1 order gate
(:class:`~contrib.hyperliquid_perp.ports.OrderGate`) is bound at construction
and judges every signed MUTATION: order placement passes the wire-scoped
condition list (``require_order``, or ``require_protective_order`` for a
protective/de-risking order), cancel/scheduleCancel/updateLeverage the base
subset (§13.1 allows the cancels in safe mode). Every base-subset call states
its symbol or explicitly states ``None``: updateLeverage passes its coin, so
``allowed_symbols`` binds it, while the cancels pass ``None`` by decision
despite naming a coin — see :mod:`~contrib.hyperliquid_perp.live.order_gate`
for both that decision and why updateLeverage is in this subset at all.
The full §4.1 list is a
DECISION question, asked once per cycle through ``check_new_target`` by the
engine, not per order. Queries are read-only and ungated.

This layer is transport only: no persistence, no retry policy. The §8.3
idempotent-retry protocol (registry write before send, query-before-resend on
duplicate) lives in :mod:`contrib.hyperliquid_perp.live.orders`, the intended
caller.

The agent private key is consumed once at construction to build the SDK's
``LocalAccount``; it is never stored on this object, and ``__repr__`` shows
only public addresses (§6 rule 2: the key must not reach logs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from hyperliquid.exchange import Exchange
from hyperliquid.utils.types import Cloid

from ...ports import OrderGate
from .errors import ExchangeError, ExchangeRequestError, MalformedResponseError
from .mapper import hex_identity_matches, require_decimal
from .sdk_client import (
    _BASE_URLS,
    DEFAULT_NETWORK_TIMEOUT_S,
    _init_rejects_kwargs,
    account_from_agent_key,
    call_sdk,
)

logger = logging.getLogger(__name__)

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
    """True when an order ACK's error text signals a duplicate/known cloid.

    Applied to :attr:`OrderAck.error` — the exchange's per-order rejection text —
    never to an orderStatus payload. That distinction is the point of §8.3 rule
    9: this text is a fast-path hint, and orderStatus is the authority on
    rejected-vs-exists.
    """
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

    A top-level ``"err"`` is an ACTION-level failure (bad signature, bad
    payload, invalid nonce) — the order/cancel may never have reached the
    matching engine, so it raises ``ExchangeRequestError`` like any other
    transport failure instead of masquerading as a per-order verdict; the
    caller's attempt row records 'failed' (outcome unknown) and the §8.3
    pre-check resolves it through orderStatus on the same-cloid retry.
    Per-order rejections arrive INSIDE ``statuses`` and stay acks. Anything
    not shaped like the envelope is a malformed response and fails loud.
    """
    if not isinstance(response, dict) or "status" not in response:
        raise MalformedResponseError(
            f"Hyperliquid {action} response is not a status envelope: {response!r}"
        )
    if response["status"] != "ok":
        # The err payload is the message itself (a string) per the SDK.
        raise ExchangeRequestError(f"Hyperliquid {action} rejected: {response.get('response')}")
    return response.get("response")


def _single_status(response: Any, *, action: str) -> Any:
    """The one per-order status out of a single-order action's response.

    This wrapper only ever submits one order/cancel per request, so exactly
    one status must come back — zero or several means the response does not
    match the request and nothing can be safely assumed about what the
    exchange did.
    """
    payload = _response_payload(response, action=action)
    data = payload.get("data") if isinstance(payload, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        # `statuses` has already FAILED the list test here, so it may be any
        # shape at all — len() on a truthy non-sized value (say `"statuses": 3`)
        # would raise TypeError while building the very message meant to report
        # the malformation, and the named-error contract this layer promises
        # would be broken by its own error path.
        count = len(statuses) if isinstance(statuses, list) else "non-list"
        raise MalformedResponseError(
            f"Hyperliquid {action} response carries {count}"
            f" statuses (expected exactly 1): {response!r}"
        )
    return statuses[0]


def _field(container: Any, key: str, *, kind: str) -> Any:
    """One required field out of an exchange status body, or fail loud.

    The SDK hands back plain dicts, so a field the exchange stops sending (or
    renames) would otherwise surface as a bare KeyError from inside a parser
    that documents itself as raising MalformedResponseError — and the caller's
    `except ExchangeError` lane would not catch it.
    """
    if not isinstance(container, dict) or key not in container:
        raise MalformedResponseError(f"Hyperliquid {kind} status is missing {key!r}: {container!r}")
    return container[key]


def _decimal_field(container: Any, key: str, *, kind: str) -> Decimal:
    """A required numeric field as Decimal, via the mapper's shared guard.

    ``require_decimal`` is used rather than a local ``Decimal(str(...))``
    because it also rejects NON-FINITE values: ``Decimal("NaN")`` parses without
    complaint, and a NaN ``avgPx`` here would reach OrderAck, the orders row, and
    every PnL derived from it — poisoning the accounting rather than failing.
    """
    return require_decimal(_field(container, key, kind=kind), field=f"{kind} {key}")


def _check_ack_cloid(body: Any, *, expected_cloid_hex: str, kind: str) -> None:
    """The accepted ack must echo the submitted cloid, or nothing is booked.

    Same identity discipline — and the same strict stance on a MISSING echo —
    as ``live.orders.parse_order_status``; see there for the full rationale.
    The stake specific to this site: booking a mismatched ack would bind
    another order's oid to this cloid in the orders row, and every later
    cancel/modify/fill-attach would act on the wrong order, whereas raising
    lands on the caller's malformed-response lane (attempt 'failed', outcome
    unknown), which the §8.3 same-cloid retry resolves through orderStatus.
    An ``error`` verdict has no identity to check (nothing booked, no oid to
    misbind).
    """
    echoed = body.get("cloid") if isinstance(body, dict) else None
    if not hex_identity_matches(echoed, expected_cloid_hex):
        raise MalformedResponseError(
            f"Hyperliquid {kind} status for cloid {expected_cloid_hex} answered with "
            f"cloid {echoed!r} — refusing to book another order's ack"
        )


def _parse_order_ack(response: Any, *, expected_cloid_hex: str) -> OrderAck:
    status = _single_status(response, action="order")
    if isinstance(status, dict) and "resting" in status:
        resting = status["resting"]
        _check_ack_cloid(resting, expected_cloid_hex=expected_cloid_hex, kind="resting order")
        return OrderAck(
            status="resting",
            exchange_order_id=str(_field(resting, "oid", kind="resting order")),
            raw=response,
        )
    if isinstance(status, dict) and "filled" in status:
        filled = status["filled"]
        _check_ack_cloid(filled, expected_cloid_hex=expected_cloid_hex, kind="filled order")
        return OrderAck(
            status="filled",
            exchange_order_id=str(_field(filled, "oid", kind="filled order")),
            filled_size=_decimal_field(filled, "totalSz", kind="filled order"),
            average_price=_decimal_field(filled, "avgPx", kind="filled order"),
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
    SDK's ``account_address``); the agent key only signs. The §4.1 order gate
    (:class:`~contrib.hyperliquid_perp.ports.OrderGate`) is bound at construction
    — one client, one gate — so every mutating method is judged by the same gate
    whose flags the kill switch manager and (PR 4/5) state machines maintain; a
    call site cannot substitute a permissive gate of its own. Decimal
    quantities/prices cross to the SDK's floats only here, at the wire boundary
    (callers' math stays all-Decimal).
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
        gate: OrderGate,
        timeout: float | None = DEFAULT_NETWORK_TIMEOUT_S,
    ) -> None:
        key = network.strip().lower()
        if key not in _BASE_URLS:
            raise ValueError(f"network must be one of {sorted(_BASE_URLS)}, got {network!r}")
        self.network = key
        self.wallet_address = wallet_address
        # Exposed for the same reason HyperliquidClient exposes it (sdk_client),
        # plus one this class owns alone: the kill switch's timing invariant
        # counts the failed attempt's own wall time as a term, and this is the
        # client every KillSwitchManager is built on. Forwarding ``timeout`` into
        # ``Exchange`` without keeping it here left that term unreadable, so the
        # constructor silently checked four of its five terms on EVERY production
        # manager while the CLI preflight checked all five — the two halves of one
        # invariant disagreeing, with only the preflight live (2026-08-01 round-14
        # review).
        self.timeout = timeout
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

    def update_leverage(self, *, coin: str, leverage: int, is_cross: bool = True) -> None:
        """Set the account's leverage for ``coin`` (§7 ``updateLeverage``, PR 6).

        The one signed exchange action the PR 1–5 order path never needed
        (testnet_live / mainnet_tiny both pin ``leverage: 1`` and the exchange
        default already sits there), but the §20.2 smoke suite must prove the
        action reaches the exchange before the first real cycle — so PR 6 wraps
        it. Rides ``require_exchange_action`` (the same wire gate as
        ``schedule_cancel``: a signed account-config change, not an order that
        opens exposure) — but passes ``coin``, so ``allowed_symbols`` binds it:
        this run may only change the leverage of the coin it was configured to
        trade. Its coin is this run's own configured symbol — not a registry row
        that may predate the config, nor one read back from the exchange — so
        the reason the cancels pass ``None`` does not apply here (2026-08-17,
        issue #28). ``updateLeverage`` is statusless —
        the envelope is the
        whole verdict, and ``_response_payload`` raises ``ExchangeRequestError``
        on a top-level ``err`` (a rejected leverage change never half-hides
        behind a return code).
        """
        if leverage < 1:
            raise ValueError(f"leverage must be >= 1 (§20.1 pins 1), got {leverage!r}")
        self._gate.require_exchange_action(coin)
        response = call_sdk(self._exchange.update_leverage, leverage, coin, is_cross)
        _response_payload(response, action="updateLeverage")

    def place_ioc_limit(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: Decimal,
        limit_price: Decimal,
        cloid_hex: str,
        reduce_only: bool = False,
        protective: bool = False,
    ) -> OrderAck:
        """Submit one IOC limit order carrying its cloid (§7 ``order``, §9).

        The §4.1 gate bound at construction runs first — a rejection raises
        ``LiveOrderGateRejected`` before any network traffic. It is the
        WIRE-SCOPED subset (``require_order``), the conditions that must hold for
        any order to be sent at all; the three decision-scoped ones live on
        ``check_new_target``, which the engine asks once per cycle (§9.3 allows
        SL repair and emergency close while a slice plan runs). The cloid
        is mandatory: every order this system sends must be attributable
        through the registry (§8.2), and an anonymous order would be judged
        non-bot-owned by our own reconciliation (§19.3). Returns the parsed
        :class:`OrderAck`; a PER-ORDER rejection is an ``error`` ack, not an
        exception (the §8.3 retry protocol needs to inspect it), while a
        top-level ``err`` envelope (action-level: signature/payload/nonce)
        raises ``ExchangeRequestError`` — the order may never have reached
        the matching engine, so it must not consume the cloid as 'rejected'.

        ``protective`` routes the wire-side gate backstop through
        :meth:`~...ports.OrderGate.require_protective_order` for a §17.2
        emergency close (a de-risking IOC that must clear in safe mode); the
        caller (:class:`~...live.orders.LiveOrderSubmitter`) sets it from the
        order role so this backstop and its own pre-check agree.
        """
        if protective:
            self._gate.require_protective_order(coin)
        else:
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
        return _parse_order_ack(response, expected_cloid_hex=cloid_hex)

    def place_trigger_order(
        self,
        *,
        coin: str,
        is_buy: bool,
        size: Decimal,
        limit_price: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        cloid_hex: str,
        is_market: bool = False,
        reduce_only: bool = True,
    ) -> OrderAck:
        """Place one resting reduce-only trigger order — SL/TP protection (§17).

        The §17 protection order: a resting stop-loss (``tpsl="sl"``) or
        take-profit (``tpsl="tp"``) that the exchange fills when the mark
        crosses ``trigger_price``. It defaults to reduce-only (a protection
        order may only shrink the position) and to a LIMIT trigger
        (``is_market=False``): §9.2 rule 1 forbids an unprotected market-like
        live order, so the caller sets ``limit_price`` to an aggressive but
        bounded price past the trigger (marketable on fire, capped slippage) —
        the "aggressive IOC, price-protected" shape §9.4 pins for stop_loss /
        take_profit. The bound §4.1 gate runs first via
        ``require_protective_order`` (§13.1: a reduce-only SL/TP must stay
        placeable while a safe mode is active); like ``place_ioc_limit`` it is
        a wire-scoped check, not the per-cycle list, so SL repair is allowed
        while a slice plan runs (§9.3). ``tpsl`` is validated here; the cloid
        is mandatory (§8.2/§19.3).
        """
        if tpsl not in ("sl", "tp"):
            raise ValueError(f"tpsl must be 'sl' or 'tp' (§17 trigger order), got {tpsl!r}")
        # A reduce-only SL/TP is always protective (§13.1): it must be placeable
        # while a safe mode is active, so it rides the protective gate.
        self._gate.require_protective_order(coin)
        response = call_sdk(
            self._exchange.order,
            coin,
            is_buy,
            float(size),
            float(limit_price),
            {"trigger": {"triggerPx": float(trigger_price), "isMarket": is_market, "tpsl": tpsl}},
            reduce_only,
            Cloid.from_str(cloid_hex),
        )
        return _parse_order_ack(response, expected_cloid_hex=cloid_hex)

    def modify_trigger_order(
        self,
        *,
        target: str,
        coin: str,
        is_buy: bool,
        size: Decimal,
        limit_price: Decimal,
        trigger_price: Decimal,
        tpsl: str,
        cloid_hex: str,
        is_market: bool = False,
        reduce_only: bool = True,
    ) -> OrderAck:
        """Modify a resting protection order in place (§17.4 modify-before-cancel).

        §17.4 requires updating an existing SL/TP through ``modify`` rather than
        cancel-then-create, to shrink the window in which the position has no
        protection. ``target`` is the exchange order id of the order being
        replaced; the modified order carries a fresh ``cloid_hex`` (its new
        logical identity — trigger price / size changed), so the registry and
        the §8.3 retry protocol still map one logical order to one wire cloid.
        Same reduce-only trigger shape and gate as :meth:`place_trigger_order`.
        """
        if tpsl not in ("sl", "tp"):
            raise ValueError(f"tpsl must be 'sl' or 'tp' (§17 trigger order), got {tpsl!r}")
        # Moving a resting SL/TP is protective (§13.1/§17.4): sendable in safe mode.
        self._gate.require_protective_order(coin)
        response = call_sdk(
            self._exchange.modify_order,
            int(target),
            coin,
            is_buy,
            float(size),
            float(limit_price),
            {"trigger": {"triggerPx": float(trigger_price), "isMarket": is_market, "tpsl": tpsl}},
            reduce_only,
            Cloid.from_str(cloid_hex),
        )
        return _parse_order_ack(response, expected_cloid_hex=cloid_hex)

    def cancel_by_oid(self, *, coin: str, exchange_order_id: str) -> CancelAck:
        """Cancel one order by exchange order id (§7 ``cancel``).

        Base gate only: §13.1 allows cancelling bot-owned orders in safe mode.
        ``None`` for the gate's symbol although ``coin`` is right there — the
        cancels' coin never comes from this run's config, so the allowlist would
        block the very orders a sweep exists to clear (order_gate module
        docstring). No production caller: §8.3 rule 7 keeps every
        exchange-facing lookup on the cloid, so :meth:`cancel_by_cloid` is what
        both sweeps use and this is the smoke suite's path.
        """
        self._gate.require_exchange_action(None)
        response = call_sdk(self._exchange.cancel, coin, int(exchange_order_id))
        return _parse_cancel_ack(response)

    def cancel_by_cloid(self, *, coin: str, cloid_hex: str) -> CancelAck:
        """Cancel one order by client order id (§7 ``cancelByCloid``).

        §8.3 rule 7: the exchange-facing identifier is always cloid_hex. The
        cancel path both sweeps take, and account-wide for gate purposes — see
        the order_gate module docstring for why the coin it carries does not
        bind the allowlist.
        """
        self._gate.require_exchange_action(None)
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

    def user_fills_by_time(self, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        """The main wallet's fills in ``[start_time_ms, end_time_ms]`` (read-only; §14.1).

        The REST backfill source (:class:`~...live.fill_backfill.FillBackfiller`):
        it catches fills the WebSocket missed while it was down or before it
        subscribed — and in the v1 live loop, which attaches no socket yet, it
        is the sole fills source. Times are epoch-ms, the form the API takes; the raw fill list
        is returned untouched for the ingester to parse and dedupe (§14.2/§14.3).
        Ungated — a read never places or moves anything.
        """
        return call_sdk(
            self._exchange.info.user_fills_by_time,
            self.wallet_address,
            int(start_time_ms),
            None if end_time_ms is None else int(end_time_ms),
        )

    def exchange_time(self) -> datetime | None:
        """The exchange's own clock, or None if this response does not carry it.

        ``clearinghouseState`` stamps its answer with a ``time`` field (epoch
        ms). It is the only exchange-side clock this transport can reach without
        a new endpoint, and the kill switch needs it: ``scheduleCancel`` takes an
        ABSOLUTE deadline computed from OUR clock, so host clock drift silently
        changes the real protection window (see KillSwitchManager.arm).

        Returns None — rather than raising — when the field is absent or
        unparseable: the field is not load-bearing for any other caller, and a
        missing timestamp must degrade the skew CHECK, never block a startup
        that is otherwise healthy. Read-only, so ungated.
        """
        state = call_sdk(self._exchange.info.user_state, self.wallet_address)
        stamp = state.get("time") if isinstance(state, dict) else None
        if stamp is None:
            return None
        try:
            return datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            # Same discipline as mapper._opt_dec: a PRESENT-but-unparseable
            # value is a data-quality signal, not an omitted field. Still
            # None (the degradation stands), but logged — the skew check's
            # own message says "returned no timestamp", which would mislabel
            # this corruption as absence with no trace of the real cause.
            logger.warning(
                "clearinghouseState 'time' is present but unparseable as epoch "
                "ms: %r — clock-skew check degrades as if it were absent",
                stamp,
            )
            return None

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
        self._gate.require_exchange_action(None)  # arms the whole wallet
        if cancel_at.tzinfo is None:
            raise ValueError("cancel_at must be timezone-aware (UTC)")
        response = call_sdk(self._exchange.schedule_cancel, int(cancel_at.timestamp() * 1000))
        # scheduleCancel is statusless: the envelope IS the whole verdict, and
        # _response_payload raises on a top-level err.
        _response_payload(response, action="scheduleCancel")

    def clear_scheduled_cancel(self) -> None:
        """Disarm the dead man's switch (§7 ``scheduleCancel`` unset, §18.2 rule 6).

        The exchange-side trigger is wallet-wide — at the deadline it cancels
        EVERY open order on the wallet, including non-bot-owned orders the
        shutdown sweep deliberately left alone (§19.3). After a fully clean
        sweep there is no bot order left for the backstop to protect, so the
        kill switch manager unsets the trigger. Failure raises — the caller
        records it and leaves the switch armed (the fail-safe direction).
        """
        self._gate.require_exchange_action(None)  # wallet-wide, like the arm
        response = call_sdk(self._exchange.schedule_cancel, None)
        _response_payload(response, action="scheduleCancel")

    def __repr__(self) -> str:  # never the key — addresses only (§6 rule 2)
        return (
            f"{type(self).__name__}(network={self.network!r}, "
            f"agent_address={self.agent_address!r}, "
            f"wallet_address={self.wallet_address!r})"
        )
