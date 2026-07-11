"""Tests for HyperliquidClient construction that need no network.

Only the network-validation guard is exercised here: it raises *before* any
``Info`` instance (and therefore any HTTP call) is constructed, so it is safe to
assert offline. The happy path auto-fetches perp meta over the network and is not
unit-tested.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeRequestError,
    MalformedResponseError,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client import (
    HyperliquidClient,
    call_sdk,
)


def test_call_sdk_wraps_non_domain_error_as_request_error():
    # The single seam where opaque SDK/network errors become a domain error: a
    # raw ConnectionError must surface as ExchangeRequestError so main.py's
    # `except ExchangeError` catches it (exit 1), not as an unhandled exit 2.
    def _boom():
        raise ConnectionError("timeout")

    with pytest.raises(ExchangeRequestError):
        call_sdk(_boom)


def test_call_sdk_reraises_domain_error_unchanged():
    # An ExchangeError raised inside the call (e.g. by a mapper) must pass through
    # untouched, not be re-wrapped as a generic ExchangeRequestError.
    def _malformed():
        raise MalformedResponseError("bad json")

    with pytest.raises(MalformedResponseError):
        call_sdk(_malformed)


def test_call_sdk_passes_args_through_on_success():
    assert call_sdk(lambda a, b: a + b, 2, 3) == 5


@pytest.mark.parametrize("network", ["badnet", "main", "", "prod"])
def test_unknown_network_raises_before_constructing_info(network):
    # A misconfigured network must fail loudly at construction rather than silently
    # routing reads to the default endpoint. (Casing/whitespace are normalised via
    # strip().lower(), so the cases here are genuinely unknown keys.)
    with pytest.raises(ValueError, match="network must be one of"):
        HyperliquidClient(network)


def test_from_config_unknown_network_raises():
    # from_config is the production entrypoint; a bad YAML `network:` value must
    # surface the same guard, not a default-endpoint fallback.
    with pytest.raises(ValueError, match="network must be one of"):
        HyperliquidClient.from_config({"network": "prod"})


def _capture_info_kwargs(monkeypatch) -> dict:
    """Stub the SDK ``Info`` so construction records kwargs without a network call."""
    captured: dict = {}

    class _StubInfo:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client.Info", _StubInfo)
    return captured


def test_from_config_defaults_timeout_to_30s(monkeypatch):
    # A None timeout makes requests block forever on a stalled response; from_config
    # must default to a finite cap so a hung exchange read fails instead of wedging.
    captured = _capture_info_kwargs(monkeypatch)
    HyperliquidClient.from_config({"network": "mainnet"})
    assert captured["timeout"] == 30.0


def test_from_config_reads_network_timeout_s(monkeypatch):
    captured = _capture_info_kwargs(monkeypatch)
    HyperliquidClient.from_config({"network": "mainnet", "network_timeout_s": 5})
    assert captured["timeout"] == 5.0


def test_from_config_explicit_timeout_overrides_config(monkeypatch):
    # An explicit kwarg wins over the config key.
    captured = _capture_info_kwargs(monkeypatch)
    HyperliquidClient.from_config({"network_timeout_s": 5}, timeout=12.0)
    assert captured["timeout"] == 12.0


def test_from_config_network_override_wins_over_config(monkeypatch):
    # _cmd_live pins the client to live.network via the explicit kwarg; a stale
    # top-level `network: mainnet` in the same YAML must NOT win for a
    # testnet_live run — the exact §3.1 "wrong network by accident" class.
    _capture_info_kwargs(monkeypatch)
    client = HyperliquidClient.from_config({"network": "mainnet"}, network="testnet")
    assert client.network == "testnet"


def test_from_config_without_override_reads_config_network(monkeypatch):
    _capture_info_kwargs(monkeypatch)
    client = HyperliquidClient.from_config({"network": "testnet"})
    assert client.network == "testnet"


def test_from_config_defaults_to_mainnet(monkeypatch):
    # No override, no config key: the Phase 1/2 default stands.
    _capture_info_kwargs(monkeypatch)
    client = HyperliquidClient.from_config({})
    assert client.network == "mainnet"


def test_from_config_null_network_timeout_falls_back_to_default(monkeypatch):
    # A present-but-null value (YAML `network_timeout_s:` left blank) must fall back
    # to the default, not crash on float(None).
    captured = _capture_info_kwargs(monkeypatch)
    HyperliquidClient.from_config({"network": "mainnet", "network_timeout_s": None})
    assert captured["timeout"] == 30.0


def test_info_typeerror_is_translated_to_exchange_error(monkeypatch):
    # An SDK-shape mismatch (an older sdk whose Info() signature lacks spot_meta/timeout)
    # surfaces as a clear ExchangeError naming the incompatibility, not a bare TypeError
    # that main.py would report as a generic "unexpected error". The stub mirrors a real
    # old SDK: its __init__ signature genuinely omits the kwargs we pass, so calling it
    # raises TypeError — and the signature check identifies that as a version mismatch.
    class _OldInfo:
        def __init__(self, *, base_url, skip_ws):  # no spot_meta / timeout
            pass

    monkeypatch.setattr("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client.Info", _OldInfo)
    with pytest.raises(ExchangeError, match="incompatible"):
        HyperliquidClient("mainnet")


def test_construction_network_failure_is_wrapped_as_request_error(monkeypatch):
    # Info() auto-fetches perp meta at construction — a network failure there
    # must reach callers as ExchangeRequestError (their named exit-1 lane), not
    # as a raw requests exception that lands in the generic exit-2 bucket.
    class _NetBoom:
        def __init__(self, *, base_url, skip_ws, spot_meta, timeout):
            raise ConnectionError("dns down")

    monkeypatch.setattr("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client.Info", _NetBoom)
    with pytest.raises(ExchangeRequestError, match="dns down"):
        HyperliquidClient("mainnet")


def test_info_internal_typeerror_is_not_mislabeled_as_version_mismatch(monkeypatch):
    # A TypeError raised *inside* a compatible Info.__init__ (a data fault, not a
    # signature rejection) must surface unchanged — relabeling it "incompatible SDK
    # version?" would send the operator to upgrade the SDK while the real cause hides.
    # The signature check, not the message text, decides this: Info accepts every kwarg
    # we pass, so the wrapper must re-raise the original TypeError, not an ExchangeError.
    class _InternalBoom:
        def __init__(self, *, base_url, skip_ws, spot_meta, timeout):
            raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(
        "contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client.Info", _InternalBoom
    )
    with pytest.raises(TypeError, match="not subscriptable"):
        HyperliquidClient("mainnet")
