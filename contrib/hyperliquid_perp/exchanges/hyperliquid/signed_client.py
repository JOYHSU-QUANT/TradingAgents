"""Thin wrapper around the official Hyperliquid SDK ``Exchange`` client (PR 1).

The signed counterpart of :mod:`.sdk_client`: centralises network selection and
the defensive construction pattern so nothing else builds ``Exchange`` directly.
PR 1 exposes initialization and a read-only health check ONLY — no order
methods exist on this class yet; they arrive in PR 2 behind the §4.1 real-order
gate.

The agent private key is consumed once at construction to build the SDK's
``LocalAccount``; it is never stored on this object, and ``__repr__`` shows
only public addresses (§6 rule 2: the key must not reach logs).
"""

from __future__ import annotations

from hyperliquid.exchange import Exchange

from .errors import ExchangeError, ExchangeRequestError
from .sdk_client import (
    _BASE_URLS,
    DEFAULT_NETWORK_TIMEOUT_S,
    _init_rejects_kwargs,
    account_from_agent_key,
    call_sdk,
)

# The keyword arguments we pass to ``Exchange(...)`` in __init__, for the same
# signature-based version-mismatch triage sdk_client applies to ``Info``
# (shared logic: ``sdk_client._init_rejects_kwargs``).
_EXCHANGE_KWARGS = ("base_url", "account_address", "spot_meta", "timeout")


class HyperliquidSignedClient:
    """Owns the SDK ``Exchange`` instance for the agent wallet.

    ``wallet_address`` is the MAIN wallet the agent trades on behalf of (the
    SDK's ``account_address``); the agent key only signs. PR 1 scope: construct
    + :meth:`health_check`. Deliberately NO order/cancel methods yet.
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
        timeout: float | None = DEFAULT_NETWORK_TIMEOUT_S,
    ) -> None:
        key = network.strip().lower()
        if key not in _BASE_URLS:
            raise ValueError(f"network must be one of {sorted(_BASE_URLS)}, got {network!r}")
        self.network = key
        self.wallet_address = wallet_address
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

    def __repr__(self) -> str:  # never the key — addresses only (§6 rule 2)
        return (
            f"{type(self).__name__}(network={self.network!r}, "
            f"agent_address={self.agent_address!r}, "
            f"wallet_address={self.wallet_address!r})"
        )
