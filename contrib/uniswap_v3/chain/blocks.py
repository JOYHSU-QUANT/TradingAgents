"""Time to block: the first block whose timestamp is at or after a time.

A bisection over block numbers, since timestamps only grow. Before the
answer is returned, every timestamp the search read is checked to be in
block order; a node that reported two blocks out of order raises. A time
the chain has not reached raises too; no block is extrapolated.

Timestamps read along the way are kept, and narrow the next search. They
are kept only once the search they were read in has passed that check, and
only for blocks at least ``finality_depth`` behind the head: a block that
recent can still be replaced by another with a different timestamp.
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Final

from .errors import BlockNotFound, MalformedResponse
from .rpc import Rpc

__all__ = ["ChainBlockLocator"]

# Two epochs of 32 slots, after which a mainnet block is final.
_FINALITY_DEPTH: Final = 64


class ChainBlockLocator:
    """A :class:`~..ports.BlockLocator` that asks a node."""

    def __init__(self, rpc: Rpc, *, finality_depth: int = _FINALITY_DEPTH) -> None:
        if isinstance(finality_depth, bool) or not isinstance(finality_depth, int):
            raise ValueError(f"finality_depth must be an integer, got {finality_depth!r}")
        if finality_depth < 0:
            raise ValueError(f"finality_depth must not be negative, got {finality_depth}")
        self._rpc = rpc
        self._finality_depth = finality_depth
        # Parallel and sorted: the kept block numbers and their timestamps.
        self._numbers: list[int] = []
        self._times: list[int] = []

    def _kept_index(self, block: int) -> int | None:
        index = bisect_left(self._numbers, block)
        return index if index < len(self._numbers) and self._numbers[index] == block else None

    def first_block_at_or_after(self, time: int) -> int:
        """The number of the first block whose timestamp is at or after ``time``."""
        if isinstance(time, bool) or not isinstance(time, int) or time < 0:
            raise ValueError(f"time is epoch seconds, a non-negative integer, got {time!r}")
        head = self._rpc.header()
        if head.timestamp < time:
            raise BlockNotFound(
                f"no block at or after {time} yet: the latest, block {head.number}, "
                f"is at {head.timestamp}"
            )
        seen = {head.number: head.timestamp}

        # The answer is in [low, high], and block ``high`` is at or after ``time``.
        low, high = 0, head.number
        index = bisect_left(self._times, time)
        # The kept blocks that bound the search join ``seen``, so the check
        # below holds them against whatever this search reads.
        if index < len(self._numbers) and self._numbers[index] < high:
            high = self._numbers[index]
            seen[high] = self._times[index]
        if index > 0 and self._numbers[index - 1] < high:
            low = self._numbers[index - 1] + 1
            seen[low - 1] = self._times[index - 1]
        while low < high:
            middle = (low + high) // 2
            # A kept block lies between the bounds only when the head has
            # moved back since it was kept.
            kept = self._kept_index(middle)
            seen[middle] = (
                self._times[kept] if kept is not None else self._rpc.header(middle).timestamp
            )
            if seen[middle] >= time:
                high = middle
            else:
                low = middle + 1

        # The bisection trusted every timestamp it read to be in order.
        ordered = sorted(seen)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if seen[earlier] >= seen[later]:
                raise MalformedResponse(
                    f"block {earlier} is at {seen[earlier]} and block {later} at {seen[later]}: "
                    f"block times must increase"
                )
        final = head.number - self._finality_depth
        for block in ordered:
            if block <= final and self._kept_index(block) is None:
                index = bisect_left(self._numbers, block)
                self._numbers.insert(index, block)
                self._times.insert(index, seen[block])
        return low
