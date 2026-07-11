"""Tests for the signed ``Exchange`` wrapper (PR 1: init + health check only).

The SDK ``Exchange`` is stubbed at the module seam (its real __init__ fetches
perp meta over the network), so what is under test is the wrapper's contract:
network validation before construction, the spot_meta/timeout defenses, the
key-hygiene rules, and the version-mismatch triage mirrored from sdk_client.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeRequestError,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import (
    HyperliquidSignedClient,
)
from contrib.hyperliquid_perp.live.authorization import derive_agent_address

_KEY = "0x" + "11" * 32
_WALLET = "0x" + "aa" * 20
_SEAM = "contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client.Exchange"


class _FakeInfo:
    def __init__(self):
        self.user_state_calls: list[str] = []
        self.user_state_error: Exception | None = None

    def user_state(self, address: str):
        self.user_state_calls.append(address)
        if self.user_state_error is not None:
            raise self.user_state_error
        return {"marginSummary": {}}


class _FakeExchange:
    """Mirrors the SDK signature we rely on; records construction args."""

    def __init__(self, wallet, base_url=None, account_address=None, spot_meta=None, timeout=None):
        self.wallet = wallet
        self.base_url = base_url
        self.account_address = account_address
        self.spot_meta = spot_meta
        self.timeout = timeout
        self.info = _FakeInfo()


@pytest.fixture
def fake_exchange(monkeypatch):
    monkeypatch.setattr(_SEAM, _FakeExchange)


def test_unknown_network_raises_before_construction():
    with pytest.raises(ValueError, match="network must be one of"):
        HyperliquidSignedClient("prod", _KEY, wallet_address=_WALLET)


def test_construction_pins_base_url_to_live_network(fake_exchange):
    testnet = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET, timeout=7.0)
    mainnet = HyperliquidSignedClient("mainnet", _KEY, wallet_address=_WALLET)
    assert "testnet" in testnet._exchange.base_url
    assert testnet._exchange.base_url != mainnet._exchange.base_url
    assert testnet._exchange.timeout == 7.0
    # The agent key signs; the MAIN wallet is the account being traded (§6).
    assert testnet._exchange.account_address == _WALLET
    # Same 0.22.0 mainnet-spot-meta crash defense as the read-only client.
    assert testnet._exchange.spot_meta == {"tokens": [], "universe": []}


def test_wallet_is_derived_from_the_agent_key(fake_exchange):
    client = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
    assert client.agent_address == derive_agent_address(_KEY)


def test_malformed_key_is_named_and_never_echoed(fake_exchange):
    bad_key = "0xdeadbeef"
    with pytest.raises(ExchangeError) as excinfo:
        HyperliquidSignedClient("testnet", bad_key, wallet_address=_WALLET)
    message = str(excinfo.value)
    assert "malformed" in message
    assert bad_key not in message
    assert excinfo.value.__cause__ is None


def test_no_order_methods_exist_in_pr1(fake_exchange):
    # The PR 1 contract: this wrapper cannot place or cancel anything. If an
    # order method lands, it must arrive with the §4.1 gate — this test forces
    # that conversation.
    client = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
    for forbidden in ("order", "cancel", "cancel_by_cloid", "modify_order", "market_open"):
        assert not hasattr(client, forbidden)


def test_health_check_reads_user_state_via_exchange_transport(fake_exchange):
    client = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
    client.health_check()
    assert client._exchange.info.user_state_calls == [_WALLET]


def test_health_check_wraps_sdk_errors(fake_exchange):
    client = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
    client._exchange.info.user_state_error = ConnectionError("down")
    with pytest.raises(ExchangeRequestError):
        client.health_check()


def test_repr_shows_addresses_never_the_key(fake_exchange):
    client = HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
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
        HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)


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
        HyperliquidSignedClient("testnet", _KEY, wallet_address=_WALLET)
