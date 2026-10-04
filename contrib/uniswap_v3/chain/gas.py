"""The base fee of a block, read off its header."""

from __future__ import annotations

from .errors import MalformedResponse
from .rpc import Rpc

__all__ = ["ChainGasOracle"]


class ChainGasOracle:
    """A :class:`~..ports.GasOracle` that asks a node."""

    def __init__(self, rpc: Rpc) -> None:
        self._rpc = rpc
        # The last block asked about: the swaps of one bar are all priced at one block.
        self._last: tuple[int, int] | None = None

    def base_fee_wei(self, block: int) -> int:
        """The base fee per gas of ``block``, in wei."""
        if self._last is not None and self._last[0] == block:
            return self._last[1]
        base_fee = self._rpc.header(block).base_fee_wei
        if base_fee is None:
            # Before London (mainnet block 12,965,000) a block has none.
            raise MalformedResponse(f"block {block} has no base fee")
        self._last = (block, base_fee)
        return base_fee
