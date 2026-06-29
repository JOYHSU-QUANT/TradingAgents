"""Thin wrapper around the official Hyperliquid SDK ``Info`` client.

Centralises mainnet/testnet selection so nothing else constructs ``Info``
directly. Phase 1 only ever reads public/info endpoints (``skip_ws=True``); the
signed ``Exchange`` client and WebSockets arrive in Phase 3.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .errors import ExchangeError, ExchangeRequestError

_BASE_URLS = {
    "mainnet": constants.MAINNET_API_URL,
    "testnet": constants.TESTNET_API_URL,
}


def call_sdk(fn: Callable[..., Any], *args: Any) -> Any:
    """Invoke an SDK call, translating any non-domain error to ExchangeRequestError.

    Shared by the market-data and account adapters so SDK/network errors are
    wrapped in one place and callers never import SDK exception types.
    """
    try:
        return fn(*args)
    except ExchangeError:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK/network errors are opaque
        # Some SDK/network exceptions (e.g. a bare ConnectionError) stringify to "",
        # which would leave the top-level message — the one main.py prints — hollow.
        # Prefix the type name so the error is always diagnostic even when str(exc) is empty.
        raise ExchangeRequestError(
            f"Hyperliquid request failed: {type(exc).__name__}: {exc}"
        ) from exc


class HyperliquidClient:
    """Owns the SDK ``Info`` instance used for all read-only calls."""

    def __init__(self, network: str = "mainnet", *, timeout: float | None = None) -> None:
        key = network.strip().lower()
        if key not in _BASE_URLS:
            raise ValueError(f"network must be one of {sorted(_BASE_URLS)}, got {network!r}")
        self.network = key
        # skip_ws: Phase 1 is request/response only — no live subscriptions.
        # spot_meta override: the SDK's init-time parse of mainnet *spot* meta
        # crashes in 0.22.0 (IndexError on spot_meta["tokens"]). We only trade
        # perps, so stub spot meta out; perp meta still auto-fetches and
        # populates the name->coin map that candle/funding calls need.
        try:
            self.info = Info(
                base_url=_BASE_URLS[key],
                skip_ws=True,
                spot_meta={"tokens": [], "universe": []},
                timeout=timeout,
            )
        except TypeError as exc:
            # A TypeError here is an SDK-shape mismatch (e.g. an older hyperliquid-sdk
            # whose Info() does not accept ``timeout``/``spot_meta``), not a runtime
            # data error. Re-raise as a clear ExchangeError naming the incompatibility
            # so it surfaces as an actionable message rather than a bare TypeError that
            # main.py reports as a generic "unexpected error".
            raise ExchangeError(
                f"Hyperliquid SDK Info() rejected its arguments — incompatible "
                f"hyperliquid-python-sdk version? ({type(exc).__name__}: {exc})"
            ) from exc

    @classmethod
    def from_config(cls, config: dict, *, timeout: float | None = None) -> HyperliquidClient:
        # A None timeout makes the SDK's requests block forever on a stalled
        # response — no error, no output, the process just hangs. Default to a
        # finite cap (overridable via the explicit kwarg or ``network_timeout_s``)
        # so a hung exchange read fails loud instead of silently wedging the run.
        if timeout is None:
            # ``dict.get(key, default)`` only falls back when the key is absent; a
            # present-but-null value (YAML ``network_timeout_s:`` left blank) returns
            # None, so treat null like absent rather than crashing on ``float(None)``.
            raw = config.get("network_timeout_s")
            timeout = float(raw) if raw is not None else 30.0
        return cls(network=config.get("network", "mainnet"), timeout=timeout)
