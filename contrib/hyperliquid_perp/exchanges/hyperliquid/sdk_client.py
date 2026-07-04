"""Thin wrapper around the official Hyperliquid SDK ``Info`` client.

Centralises mainnet/testnet selection so nothing else constructs ``Info``
directly. Phase 1 only ever reads public/info endpoints (``skip_ws=True``); the
signed ``Exchange`` client and WebSockets arrive in Phase 3.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils import constants

from .errors import ExchangeError, ExchangeRequestError

_BASE_URLS = {
    "mainnet": constants.MAINNET_API_URL,
    "testnet": constants.TESTNET_API_URL,
}

# The keyword arguments we pass to ``Info(...)`` in __init__. The construction guard
# uses these to tell a genuine SDK-version signature mismatch from an internal error.
_INFO_KWARGS = ("base_url", "skip_ws", "spot_meta", "timeout")


def _info_rejects_kwargs() -> bool:
    """True if ``Info.__init__`` cannot accept the kwargs we pass it.

    A signature mismatch means an incompatible installed SDK (actionable: upgrade). A
    ``TypeError`` raised from *inside* a compatible ``__init__`` (a data fault) returns
    ``False`` so it is re-raised unchanged rather than mislabeled as a version problem.
    Decided via the signature, not the error-message text (which varies by Python
    version and locale).
    """
    try:
        params = inspect.signature(Info.__init__).parameters.values()
    except (TypeError, ValueError):
        # Can't introspect (e.g. a C-extension or a test stub) — preserve the prior
        # behavior and treat the TypeError as a version mismatch.
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return False  # **kwargs accepts anything; the TypeError came from within
    accepted = {p.name for p in params}
    return any(name not in accepted for name in _INFO_KWARGS)


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
            # A TypeError here is ambiguous: either the installed SDK's Info() does not
            # accept one of our kwargs (an older/newer incompatible version — actionable
            # as "upgrade"), or Info.__init__ raised TypeError *internally* for a data
            # reason ("upgrade the SDK" would be wrong advice, and the real cause must
            # surface). Decide by the signature, not the message text: only relabel as a
            # version mismatch when a kwarg we pass is genuinely unsupported; otherwise
            # re-raise the original so a data fault is not masked as a version problem.
            if not _info_rejects_kwargs():
                raise
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
            # (Configs from load_config can no longer carry that null — it drops
            # blank top-level keys — so this branch is standalone-caller defense.)
            raw = config.get("network_timeout_s")
            timeout = float(raw) if raw is not None else 30.0
        return cls(network=config.get("network", "mainnet"), timeout=timeout)
