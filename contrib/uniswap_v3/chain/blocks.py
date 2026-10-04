"""Time to block: the first block whose timestamp is at or after a time.

A bisection over block numbers, since timestamps only grow. Before the
answer is returned, every timestamp the search read is checked to be in
block order; a node that reported two blocks out of order raises. A time
the chain has not reached raises too; no block is extrapolated. So does a
node that lacks a block below its own head. Near the head that is a node
behind the one that reported the head, and worth trying again; deeper, the
node does not keep the history the search needs.

Timestamps read along the way are kept, and narrow the next search. They
are kept only once the search they were read in has passed that check, and
only for blocks at least ``finality_depth`` behind the head: a block that
recent can still be replaced by another with a different timestamp.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from .errors import BlockNotFound, MalformedResponse, RpcConfigError
from .rpc import Rpc

__all__ = ["FINALITY_DEPTH", "ChainBlockLocator", "reading_block"]

# Two epochs of 32 slots, after which a mainnet block is final.
FINALITY_DEPTH: Final = 64


@contextmanager
def reading_block(block: int, final: int) -> Iterator[None]:
    """Around a read of ``block``, where ``final`` is the highest final block number.

    A node that lacks a block answers the same way for one still to come
    and for one it has dropped. Above ``final`` the
    :class:`~.errors.BlockNotFound` stands: behind a load balancer, the node
    asked now may trail the one that reported the head by a few blocks. A
    final block is not one still to come, and waiting will not bring it, so
    there it becomes :class:`~.errors.RpcConfigError`.
    """
    try:
        yield
    except BlockNotFound:
        if block > final:
            raise
        raise RpcConfigError(
            f"the node does not keep block {block}, which is final; "
            f"a node with full block history is needed"
        ) from None


class ChainBlockLocator:
    """A :class:`~..ports.BlockLocator` that asks a node."""

    def __init__(self, rpc: Rpc, *, finality_depth: int = FINALITY_DEPTH) -> None:
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

    def _timestamp(self, block: int, final: int) -> int:
        """The timestamp of ``block``, which is below the head."""
        kept = self._kept_index(block)
        if kept is not None:
            return self._times[kept]
        with reading_block(block, final):
            return self._rpc.header(block).timestamp

    def first_block_at_or_after(self, time: int) -> int:
        """The number of the first block whose timestamp is at or after ``time``."""
        if isinstance(time, bool) or not isinstance(time, int) or time < 0:
            raise ValueError(f"time is epoch seconds, a non-negative integer, got {time!r}")
        head = self._rpc.latest_header()
        if head.timestamp < time:
            raise BlockNotFound(
                f"no block at or after {time} yet: the latest, block {head.number}, "
                f"is at {head.timestamp}"
            )
        seen = {head.number: head.timestamp}
        final = head.number - self._finality_depth

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
            seen[middle] = self._timestamp(middle, final)
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
        for block in ordered:
            if block <= final and self._kept_index(block) is None:
                index = bisect_left(self._numbers, block)
                self._numbers.insert(index, block)
                self._times.insert(index, seen[block])
        return low
