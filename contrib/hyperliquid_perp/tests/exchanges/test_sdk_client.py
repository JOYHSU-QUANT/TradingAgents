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
    ExchangeThrottledError,
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


def test_direct_construction_defaults_timeout_to_30s(monkeypatch):
    # The same hazard guards the raw __init__: skipping from_config must not
    # silently buy an unbounded hang.
    captured = _capture_info_kwargs(monkeypatch)
    HyperliquidClient("mainnet")
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


# -- throttle classification (the §17.2 escalation seam) ----------------------
#
# This is where "the venue would not serve us" is told apart from "the venue
# rejected this order". Get it wrong one way and a rate limit market-closes a
# healthy position; wrong the other way and a permanently refused stop-loss
# holds forever instead of escalating. Neither direction had any coverage.


class _SdkClientError(Exception):
    """The shape hyperliquid's SDK actually raises.

    Deliberately faithful in the detail that caused the bug: ClientError does
    not call super().__init__(), so every constructor argument lands in ``args``
    — including the response HEADERS — and str(exc) renders the whole tuple.
    """

    def __init__(self, status_code, code, message, headers, data=None):
        self.status_code = status_code
        self.error_code = code
        self.error_message = message
        self.header = headers
        self.error_data = data


def _raise(exc):
    def _fn():
        raise exc

    return _fn


def test_a_429_is_classified_as_throttled():
    err = _SdkClientError(429, "TooManyRequests", "slow down", {})
    with pytest.raises(ExchangeThrottledError):
        call_sdk(_raise(err))


def test_a_503_is_classified_as_throttled():
    with pytest.raises(ExchangeThrottledError):
        call_sdk(_raise(_SdkClientError(503, "Unavailable", "shedding load", {})))


def test_a_rejection_carrying_ratelimit_headers_is_not_a_throttle():
    # The regression that made this a Critical. A 4xx from a Cloudflare-fronted
    # endpoint routinely carries x-ratelimit-* headers, and those headers land
    # in str(exc) — so matching text after seeing the status turned "422 Order
    # has invalid price" into a throttle. A throttled verdict suppresses §17.2,
    # so a permanently rejected stop-loss would hold forever over a live
    # position. A status code present is authoritative.
    err = _SdkClientError(
        422,
        "BadRequest",
        "Order has invalid price",
        {"x-ratelimit-remaining": "812", "x-ratelimit-limit": "1200"},
    )
    with pytest.raises(ExchangeRequestError) as caught:
        call_sdk(_raise(err))
    assert not isinstance(caught.value, ExchangeThrottledError)


def test_a_5xx_gateway_error_is_not_a_throttle():
    # 502/504 mean a gateway never reached the backend — an ordinary transport
    # failure, whose unknown outcome still deserves the §8.3 recovery probe that
    # the throttle lane deliberately skips.
    for status in (502, 504):
        with pytest.raises(ExchangeRequestError) as caught:
            call_sdk(_raise(_SdkClientError(status, "Gateway", "bad gateway", {})))
        assert not isinstance(caught.value, ExchangeThrottledError)


def test_a_status_less_transport_error_falls_back_to_the_message():
    # Non-SDK exceptions (a bare urllib/requests wrapper) carry no status, so
    # the marker words are the only signal left.
    with pytest.raises(ExchangeThrottledError):
        call_sdk(_raise(RuntimeError("HTTP 429 Too Many Requests")))


def test_the_loaders_network_vocabulary_matches_the_sdk_clients():
    # ``common.constants.LEGAL_NETWORKS`` is a deliberate copy of ``_BASE_URLS``'s
    # keys, kept so config loading never imports the SDK. Deliberate is not
    # drift-proof: nothing else tied the two, and the config layer would keep
    # admitting a network the client cannot resolve (or refusing one it can)
    # with the suite green. The test may import the SDK; only the loader may
    # not (issue #102).
    from contrib.hyperliquid_perp.common.constants import LEGAL_NETWORKS
    from contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client import _BASE_URLS

    assert set(LEGAL_NETWORKS) == set(_BASE_URLS)


def test_digits_inside_a_larger_number_are_not_a_throttle():
    # The first draft matched a bare "429" substring, so an oid, an epoch-ms
    # timestamp or a price containing those digits read as a rate limit.
    for message in ("order 184296 rejected: insufficient margin", "ts 1785429011234 stale"):
        with pytest.raises(ExchangeRequestError) as caught:
            call_sdk(_raise(ValueError(message)))
        assert not isinstance(caught.value, ExchangeThrottledError)
