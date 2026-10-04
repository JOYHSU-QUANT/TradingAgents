"""The base fee of a block, read off its header."""

from __future__ import annotations

from .errors import MalformedResponse
from .rpc import Rpc

__all__ = ["ChainGasOracle"]


class ChainGasOracle:
    """A :class:`~..ports.GasOracle` that asks a node."""

    def __init__(self, rpc: Rpc) -> None:
        self._rpc = rpc

    def base_fee_wei(self, block: int) -> int:
        """The base fee per gas of ``block``, in wei."""
        base_fee = self._rpc.header(block).base_fee_wei
        if base_fee is None:
            # Before London (mainnet block 12,965,000) a block has none.
            raise MalformedResponse(f"block {block} has no base fee")
        return base_fee
