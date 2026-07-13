"""Tests for the signed ``Exchange`` wrapper (init, health check, §7 actions).

The SDK ``Exchange`` is stubbed at the module seam (its real __init__ fetches
perp meta over the network), so what is under test is the wrapper's contract:
network validation before construction, the spot_meta/timeout defenses, the
key-hygiene rules, the version-mismatch triage mirrored from sdk_client, and
(PR 2) the §4.1 gating + response parsing of the order/cancel/scheduleCancel
actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeRequestError,
    MalformedResponseError,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import (
    CancelAck,
    HyperliquidSignedClient,
    OrderAck,
    is_duplicate_cloid_error,
)
from contrib.hyperliquid_perp.live.authorization import derive_agent_address
from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.live.order_gate import LiveOrderGateRejected, RealOrderGate

_KEY = "0x" + "11" * 32
_WALLET = "0x" + "aa" * 20
_SEAM = "contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client.Exchange"
_CLOID = "0x" + "ab" * 16


def _open_gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
        state_reconciled=True,
        risk_gate_approved=True,
    )


def _closed_gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=False,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
    )


def _client(network="testnet", *, key=_KEY, gate=None, **kwargs) -> HyperliquidSignedClient:
    """A client with the (now construction-bound) §4.1 gate defaulted open."""
    return HyperliquidSignedClient(
        network, key, wallet_address=_WALLET, gate=gate or _open_gate(), **kwargs
    )


def _place(client):
    """The canonical order under test — only the client's response varies."""
    return client.place_ioc_limit(
        coin="BTC",
        is_buy=True,
        size=Decimal("0.01"),
        limit_price=Decimal("100"),
        cloid_hex=_CLOID,
    )


class _FakeInfo:
    def __init__(self):
        self.user_state_calls: list[str] = []
        self.user_state_error: Exception | None = None
        self.query_calls: list = []
        self.query_result = {"status": "unknownOid"}
        self.open_orders_result: list = []

    def user_state(self, address: str):
        self.user_state_calls.append(address)
        if self.user_state_error is not None:
            raise self.user_state_error
        return {"marginSummary": {}}

    def query_order_by_cloid(self, user, cloid):
        self.query_calls.append(("cloid", user, cloid))
        return self.query_result

    def query_order_by_oid(self, user, oid):
        self.query_calls.append(("oid", user, oid))
        return self.query_result

    def frontend_open_orders(self, address):
        self.frontend_open_orders_calls = getattr(self, "frontend_open_orders_calls", [])
        self.frontend_open_orders_calls.append(address)
        return self.open_orders_result


class _FakeExchange:
    """Mirrors the SDK signature we rely on; records construction args."""

    def __init__(self, wallet, base_url=None, account_address=None, spot_meta=None, timeout=None):
        self.wallet = wallet
        self.base_url = base_url
        self.account_address = account_address
        self.spot_meta = spot_meta
        self.timeout = timeout
        self.info = _FakeInfo()
        self.order_calls: list[tuple] = []
        self.order_result = {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 111}}]}},
        }
        self.cancel_calls: list[tuple] = []
        self.cancel_result = {
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": ["success"]}},
        }
        self.schedule_calls: list[int] = []
        self.schedule_result = {"status": "ok", "response": {"type": "default"}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.order_calls.append((name, is_buy, sz, limit_px, order_type, reduce_only, cloid))
        return self.order_result

    def cancel(self, name, oid):
        self.cancel_calls.append(("oid", name, oid))
        return self.cancel_result

    def cancel_by_cloid(self, name, cloid):
        self.cancel_calls.append(("cloid", name, cloid))
        return self.cancel_result

    def schedule_cancel(self, time):
        self.schedule_calls.append(time)
        return self.schedule_result


@pytest.fixture
def fake_exchange(monkeypatch):
    monkeypatch.setattr(_SEAM, _FakeExchange)


def test_unknown_network_raises_before_construction():
    with pytest.raises(ValueError, match="network must be one of"):
        _client("prod")


def test_construction_pins_base_url_to_live_network(fake_exchange):
    testnet = _client(timeout=7.0)
    mainnet = _client("mainnet")
    assert "testnet" in testnet._exchange.base_url
    assert testnet._exchange.base_url != mainnet._exchange.base_url
    assert testnet._exchange.timeout == 7.0
    # No timeout kwarg -> the bounded default, never an unbounded-hang None
    # on the signing path (PR 2's order methods land on this class).
    assert mainnet._exchange.timeout == 30.0
    # The agent key signs; the MAIN wallet is the account being traded (§6).
    assert testnet._exchange.account_address == _WALLET
    # Same 0.22.0 mainnet-spot-meta crash defense as the read-only client.
    assert testnet._exchange.spot_meta == {"tokens": [], "universe": []}


def test_wallet_is_derived_from_the_agent_key(fake_exchange):
    client = _client()
    assert client.agent_address == derive_agent_address(_KEY)


def test_malformed_key_is_named_and_never_echoed(fake_exchange):
    bad_key = "0xdeadbeef"
    with pytest.raises(ExchangeError) as excinfo:
        _client(key=bad_key)
    message = str(excinfo.value)
    assert "malformed" in message
    assert bad_key not in message
    assert excinfo.value.__cause__ is None


# ---- §7 exchange actions (PR 2: they arrived WITH the §4.1 gate) ----------


def test_place_ioc_limit_sends_ioc_with_cloid_and_parses_resting(fake_exchange):
    client = _client()
    ack = client.place_ioc_limit(
        coin="BTC",
        is_buy=True,
        size=Decimal("0.01"),
        limit_price=Decimal("100.5"),
        cloid_hex=_CLOID,
    )
    assert ack.status == "resting" and ack.accepted
    assert ack.exchange_order_id == "111"
    (name, is_buy, sz, px, order_type, reduce_only, cloid) = client._exchange.order_calls[0]
    assert (name, is_buy, reduce_only) == ("BTC", True, False)
    assert order_type == {"limit": {"tif": "Ioc"}}  # §9: every slice is IOC
    assert sz == 0.01 and px == 100.5  # Decimal→float at the wire boundary
    assert cloid.to_raw() == _CLOID  # the wire id is the cloid_hex (§8.3 rule 7)


def test_place_ioc_limit_parses_filled_and_error_statuses(fake_exchange):
    client = _client()
    client._exchange.order_result = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"oid": 7, "totalSz": "0.01", "avgPx": "99.5"}}]},
        },
    }
    ack = _place(client)
    assert ack.status == "filled"
    assert ack.filled_size == Decimal("0.01") and ack.average_price == Decimal("99.5")
    client._exchange.order_result = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "Duplicate cloid"}]}},
    }
    ack = _place(client)
    assert ack.status == "error" and not ack.accepted and ack.is_duplicate


def test_top_level_err_envelope_raises_request_error(fake_exchange):
    # §8.3 rule 11: a top-level err is an ACTION-level failure (signature /
    # payload / nonce) — the order may never have reached the matching engine,
    # so it must raise like a transport failure, never become a per-order
    # 'error' ack whose REJECTED verdict would consume the cloid.
    client = _client()
    client._exchange.order_result = {"status": "err", "response": "User or API Wallet invalid"}
    with pytest.raises(ExchangeRequestError, match="invalid"):
        _place(client)


def test_cancel_top_level_err_envelope_raises_request_error(fake_exchange):
    # Same action-level semantics on both cancel paths: an envelope err is not
    # a per-cancel rejection verdict.
    client = _client()
    client._exchange.cancel_result = {"status": "err", "response": "Invalid nonce"}
    with pytest.raises(ExchangeRequestError, match="nonce"):
        client.cancel_by_cloid(coin="BTC", cloid_hex=_CLOID)
    with pytest.raises(ExchangeRequestError, match="nonce"):
        client.cancel_by_oid(coin="BTC", exchange_order_id="7")


def test_multi_status_order_response_is_malformed(fake_exchange):
    client = _client()
    client._exchange.order_result = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": 1}}, {"resting": {"oid": 2}}]},
        },
    }
    with pytest.raises(MalformedResponseError, match="expected exactly 1"):
        _place(client)


def test_zero_status_order_response_is_malformed(fake_exchange):
    # The empty-list twin of the multi-status case: one order out, one status
    # back. Zero statuses means the ack cannot be attributed to our order, and
    # guessing (e.g. reading it as "not accepted") would let a resting order be
    # reported as rejected — which licenses the caller to mint a NEW cloid.
    client = _client()
    client._exchange.order_result = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": []}},
    }
    with pytest.raises(MalformedResponseError, match="expected exactly 1"):
        _place(client)


def test_unrecognised_order_status_is_malformed(fake_exchange):
    # The order-side twin of test_unrecognised_cancel_status_is_malformed: a
    # status shape outside resting/filled/error is never guessed at.
    client = _client()
    client._exchange.order_result = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"teleported": {"oid": 1}}]}},
    }
    with pytest.raises(MalformedResponseError, match="order status not recognised"):
        _place(client)


def test_a_response_that_is_not_a_status_envelope_is_malformed(fake_exchange):
    # Not a status envelope at all — a proxy error page, a truncated body. Fail
    # loud rather than read the missing fields as absent evidence.
    client = _client()
    client._exchange.order_result = ["unexpected", "shape"]
    with pytest.raises(MalformedResponseError):
        _place(client)


def test_every_mutation_is_gated(fake_exchange):
    # The §4.1 contract the PR 1 placeholder test existed to force: no order
    # or cancel unless the construction-bound gate allows it.
    client = _client(gate=_closed_gate())
    with pytest.raises(LiveOrderGateRejected):
        _place(client)
    with pytest.raises(LiveOrderGateRejected):
        client.cancel_by_oid(coin="BTC", exchange_order_id="1")
    with pytest.raises(LiveOrderGateRejected):
        client.cancel_by_cloid(coin="BTC", cloid_hex=_CLOID)
    with pytest.raises(LiveOrderGateRejected):
        client.schedule_cancel(cancel_at=datetime.now(timezone.utc))
    with pytest.raises(LiveOrderGateRejected):
        client.clear_scheduled_cancel()
    assert client._exchange.order_calls == []
    assert client._exchange.cancel_calls == []
    assert client._exchange.schedule_calls == []


def test_gate_flags_flipped_after_construction_are_honoured(fake_exchange):
    # The bound gate is judged live at each call, not snapshotted: the kill
    # switch manager flips flags on the SAME instance the client holds.
    gate = _open_gate()
    client = _client(gate=gate)
    gate.kill_switch_active = False
    with pytest.raises(LiveOrderGateRejected, match="kill switch"):
        _place(client)
    assert client._exchange.order_calls == []


def test_order_gate_blocks_full_list_but_cancel_needs_only_the_base(fake_exchange):
    # §13.1: cancels stay allowed in safe-mode-like states.
    gate = _open_gate()
    gate.manual_safe_mode = True
    client = _client(gate=gate)
    with pytest.raises(LiveOrderGateRejected):
        _place(client)
    ack = client.cancel_by_cloid(coin="BTC", cloid_hex=_CLOID)
    assert ack.success


def test_cancel_parses_success_and_error(fake_exchange):
    client = _client()
    ack = client.cancel_by_oid(coin="BTC", exchange_order_id="42")
    assert ack.success
    assert client._exchange.cancel_calls[0] == ("oid", "BTC", 42)
    client._exchange.cancel_result = {
        "status": "ok",
        "response": {
            "type": "cancel",
            "data": {"statuses": [{"error": "Order already canceled"}]},
        },
    }
    ack = client.cancel_by_cloid(coin="BTC", cloid_hex=_CLOID)
    assert not ack.success and "already canceled" in ack.error


def test_unrecognised_cancel_status_is_malformed(fake_exchange):
    client = _client()
    client._exchange.cancel_result = {
        "status": "ok",
        "response": {"type": "cancel", "data": {"statuses": [123]}},
    }
    with pytest.raises(MalformedResponseError, match="cancel status not recognised"):
        client.cancel_by_oid(coin="BTC", exchange_order_id="42")


def test_schedule_cancel_sends_epoch_ms_and_rejects_naive(fake_exchange):
    client = _client()
    when = datetime(2026, 7, 12, 8, 2, tzinfo=timezone.utc)
    client.schedule_cancel(cancel_at=when)
    assert client._exchange.schedule_calls == [int(when.timestamp() * 1000)]
    with pytest.raises(ValueError, match="timezone-aware"):
        client.schedule_cancel(cancel_at=datetime(2026, 7, 12, 8, 2))


def test_schedule_cancel_err_envelope_raises_request_error(fake_exchange):
    client = _client()
    client._exchange.schedule_result = {"status": "err", "response": "too many triggers"}
    with pytest.raises(ExchangeRequestError, match="too many triggers"):
        client.schedule_cancel(cancel_at=datetime.now(timezone.utc))


def test_clear_scheduled_cancel_sends_none(fake_exchange):
    # §18.2 rule 6: disarm goes out as scheduleCancel with no time (SDK unset).
    client = _client()
    client.clear_scheduled_cancel()
    assert client._exchange.schedule_calls == [None]


def test_clear_scheduled_cancel_err_envelope_raises_request_error(fake_exchange):
    client = _client()
    client._exchange.schedule_result = {"status": "err", "response": "cannot unset"}
    with pytest.raises(ExchangeRequestError, match="cannot unset"):
        client.clear_scheduled_cancel()


def test_queries_are_read_only_and_ungated(fake_exchange):
    client = _client()
    client._exchange.info.query_result = {"status": "unknownOid"}
    assert client.query_order_by_cloid(_CLOID) == {"status": "unknownOid"}
    assert client.query_order_by_oid("42") == {"status": "unknownOid"}
    kinds = [c[0] for c in client._exchange.info.query_calls]
    assert kinds == ["cloid", "oid"]
    # Queried against the MAIN wallet, and the cloid goes out in wire form.
    assert client._exchange.info.query_calls[0][1] == _WALLET
    assert client._exchange.info.query_calls[0][2].to_raw() == _CLOID
    assert client._exchange.info.query_calls[1][2] == 42


def test_open_orders_uses_the_frontend_endpoint(fake_exchange):
    # §19.3: the basic openOrders response is not documented to echo cloid;
    # only frontendOpenOrders carries the metadata the bot-owned lookup needs.
    client = _client()
    client._exchange.info.open_orders_result = [{"oid": 1, "coin": "BTC", "cloid": _CLOID}]
    assert client.open_orders() == [{"oid": 1, "coin": "BTC", "cloid": _CLOID}]
    assert client._exchange.info.frontend_open_orders_calls == [_WALLET]


def test_duplicate_marker_heuristic():
    assert is_duplicate_cloid_error("Duplicate cloid")
    assert is_duplicate_cloid_error("order already exists")
    assert not is_duplicate_cloid_error("Insufficient margin")
    assert not is_duplicate_cloid_error(None)
    assert not is_duplicate_cloid_error("")


def test_health_check_reads_user_state_via_exchange_transport(fake_exchange):
    client = _client()
    client.health_check()
    assert client._exchange.info.user_state_calls == [_WALLET]


def test_health_check_wraps_sdk_errors(fake_exchange):
    client = _client()
    client._exchange.info.user_state_error = ConnectionError("down")
    with pytest.raises(ExchangeRequestError):
        client.health_check()


def test_repr_shows_addresses_never_the_key(fake_exchange):
    client = _client()
    text = repr(client)
    assert _WALLET in text
    assert client.agent_address in text
    assert _KEY not in text
    assert "11" * 32 not in text


def test_old_sdk_signature_is_translated_to_exchange_error(monkeypatch):
    # An Exchange() whose signature genuinely lacks our kwargs is a version
    # mismatch — actionable as "upgrade the SDK".
    class _OldExchange:
        def __init__(self, wallet, base_url=None):
            pass

    monkeypatch.setattr(_SEAM, _OldExchange)
    with pytest.raises(ExchangeError, match="incompatible"):
        _client()


def test_construction_network_failure_is_wrapped_as_request_error(monkeypatch):
    # Exchange() builds its own Info (perp-meta auto-fetch) — a network failure
    # at construction must reach callers as ExchangeRequestError, same rule as
    # the read-only client.
    class _NetBoom:
        def __init__(
            self, wallet, base_url=None, account_address=None, spot_meta=None, timeout=None
        ):
            raise ConnectionError("dns down")

    monkeypatch.setattr(_SEAM, _NetBoom)
    with pytest.raises(ExchangeRequestError, match="dns down"):
        _client()


def test_internal_typeerror_is_not_mislabeled_as_version_mismatch(monkeypatch):
    # A TypeError raised *inside* a compatible __init__ is a data fault and
    # must surface unchanged (same triage rule as sdk_client's Info guard).
    class _InternalBoom:
        def __init__(
            self, wallet, base_url=None, account_address=None, spot_meta=None, timeout=None
        ):
            raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(_SEAM, _InternalBoom)
    with pytest.raises(TypeError, match="not subscriptable"):
        _client()


def test_order_ack_enforces_its_status_contract():
    # The docstring's status↔field contract is enforced at construction, so
    # a parser bug or a sloppy test double cannot mint an impossible verdict.
    with pytest.raises(ValueError, match="resting/filled/error"):
        OrderAck(status="restnig", exchange_order_id="1")
    with pytest.raises(ValueError, match="'error'"):
        OrderAck(status="error")  # a rejection must carry the exchange text
    with pytest.raises(ValueError, match="'resting'"):
        OrderAck(status="resting")  # accepted must carry the oid
    with pytest.raises(ValueError, match="'filled'"):
        OrderAck(status="filled", exchange_order_id="1")  # fills need size+price
    with pytest.raises(ValueError, match="'resting'"):
        OrderAck(  # resting cannot smuggle fill evidence
            status="resting",
            exchange_order_id="1",
            filled_size=Decimal("1"),
            average_price=Decimal("2"),
        )


def test_cancel_ack_requires_error_exactly_on_failure():
    with pytest.raises(ValueError, match="exactly when"):
        CancelAck(success=False)  # a refusal must say why
    with pytest.raises(ValueError, match="exactly when"):
        CancelAck(success=True, error="but it worked?")  # success cannot smuggle one
