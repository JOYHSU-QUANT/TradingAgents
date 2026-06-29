"""Account / position reads — implements the ``ExchangeAccount`` port.

Needs the (public, read-only) wallet address. Not used by ``--context-only``,
which builds market context without any wallet.
"""

from __future__ import annotations

from ...domains.perp.schema import AccountSnapshot, PerpPosition
from . import mapper
from .sdk_client import HyperliquidClient, call_sdk


class HyperliquidAccount:
    """Read-only account state for a given wallet address."""

    def __init__(self, client: HyperliquidClient) -> None:
        self._info = client.info

    def get_account_snapshot(self, wallet_address: str) -> AccountSnapshot:
        raw = call_sdk(self._info.user_state, wallet_address)
        return mapper.map_account_snapshot(raw)

    def get_position(self, wallet_address: str, coin: str) -> PerpPosition | None:
        # Reuse the snapshot read rather than issuing a second user_state call.
        return self.get_account_snapshot(wallet_address).position_for(coin)
