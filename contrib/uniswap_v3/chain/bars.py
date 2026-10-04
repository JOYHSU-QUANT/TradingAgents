"""Pool readings at a bar boundary, read from a node.

The close of the boundary ``T`` is the state at the end of the last block
before the first block at or after ``T``. :func:`read_pool_bars` finds that
block, reads its header once for the hash, the time and the base fee, and
reads each pool's ``slot0`` and TWAP at it.

A reading names the hash of its close block. One read from a block that
could still be replaced is ``PENDING``; :func:`confirm_pool_bars` checks it
once the block number is final. The pool reads themselves are made by block
number, so a reading is taken to describe the block whose hash the header
read returned beside it.

Every read here is of a named block, under :func:`~.blocks.reading_block`: a
final block the node lacks raises :class:`~.errors.RpcConfigError`, not a
:class:`~.errors.BlockNotFound` that a caller would wait on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ..domain.bars import BarSettings, Finality, PoolBar
from ..domain.types import Pool
from ..ports import BlockLocator
from .blocks import reading_block
from .errors import CallReverted, MalformedResponse
from .pool_price import read_slot0, read_twap_tick
from .rpc import BlockHeader, Rpc

__all__ = ["confirm_pool_bars", "read_pool_bars"]


def read_pool_bars(
    rpc: Rpc,
    locator: BlockLocator,
    pools: Sequence[Pool],
    time: int,
    *,
    settings: BarSettings,
    final_block: int,
) -> tuple[PoolBar, ...]:
    """The reading of each of ``pools`` at the boundary ``time``, in their order.

    ``final_block`` is the highest block number that is final. A reading
    whose boundary block is at or below it is ``FINAL`` as read; a later one
    is ``PENDING``.

    A :class:`~.errors.CallReverted` raised here is the TWAP read's: a pool
    whose oracle does not reach back the window has no TWAP at the block.
    ``slot0`` is a plain view of an initialised pool, so a revert from it is
    a node's fault and raises :class:`~.errors.MalformedResponse`.
    """
    boundary_block = locator.first_block_at_or_after(time)
    if boundary_block == 0:
        raise MalformedResponse(f"no block comes before the boundary {time}")
    close_block = boundary_block - 1
    with reading_block(close_block, final_block):
        header = rpc.header(close_block)
    if header.timestamp >= time:
        raise MalformedResponse(
            f"block {close_block} is at {header.timestamp}, and it should be the last "
            f"before the boundary {time}"
        )
    if header.base_fee_wei is None:
        # Before London (mainnet block 12,965,000) a block has none.
        raise MalformedResponse(f"block {close_block} has no base fee")
    finality = Finality.FINAL if boundary_block <= final_block else Finality.PENDING
    readings = []
    for pool in pools:
        with reading_block(close_block, final_block):
            try:
                slot0 = read_slot0(rpc, pool, close_block)
            except CallReverted as exc:
                raise MalformedResponse(
                    f"slot0 of {pool.address} reverted at block {close_block}, which a "
                    f"pool's slot0 does not do ({exc})"
                ) from None
            twap_tick = read_twap_tick(
                rpc, pool, close_block, window_seconds=settings.twap_window_seconds
            )
        readings.append(
            PoolBar(
                chain_id=rpc.chain_id,
                pool=pool.address,
                interval_seconds=settings.interval_seconds,
                time=time,
                close_block=close_block,
                close_block_hash=header.hash,
                close_block_time=header.timestamp,
                sqrt_price_x96=slot0.sqrt_price_x96,
                tick=slot0.tick,
                twap_tick=twap_tick,
                twap_window_seconds=settings.twap_window_seconds,
                base_fee_wei=header.base_fee_wei,
                finality=finality,
            )
        )
    return tuple(readings)


def confirm_pool_bars(
    rpc: Rpc, pending: Sequence[PoolBar], *, final_block: int
) -> tuple[PoolBar, ...]:
    """The ``pending`` readings whose blocks are final by now, each with its verdict.

    A reading is ``FINAL`` when the final chain has, at its close block's
    number, the block it was read from, and the block after that one is at
    or after the boundary. Otherwise it is ``REORGED``. A reading whose
    boundary block is above ``final_block`` is left out: it is still
    pending.
    """
    headers: dict[int, BlockHeader] = {}

    def header(block: int) -> BlockHeader:
        if block not in headers:
            with reading_block(block, final_block):
                headers[block] = rpc.header(block)
        return headers[block]

    confirmed = []
    for reading in pending:
        if reading.close_block + 1 > final_block:
            continue
        close, boundary = header(reading.close_block), header(reading.close_block + 1)
        same = (
            close.hash == reading.close_block_hash
            and close.timestamp == reading.close_block_time
            and boundary.timestamp >= reading.time
        )
        confirmed.append(replace(reading, finality=Finality.FINAL if same else Finality.REORGED))
    return tuple(confirmed)
