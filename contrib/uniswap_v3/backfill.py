"""Fills the store's ``bars`` from an archive node, one boundary at a time.

A run can be repeated: a boundary every configured pool already has a
reading at is skipped without a read, and a boundary that has some of them
gets only the missing ones. The readings of one boundary are written in one
transaction, so a run that stops leaves whole boundaries behind and the
next run carries on from there.

A run refuses a store whose readings of the configured pools were taken
over another TWAP window than the config's: a series holds one window.

Before reading anything new, a run checks the stored readings that were
``PENDING`` and whose blocks are final by now
(:func:`~.chain.bars.confirm_pool_bars`).

What a failed read does to the run:

- :class:`~.chain.errors.CallReverted`: the boundary has no answer (a pool's
  oracle that does not reach back the TWAP window reverts). Nothing is
  written for it, it is reported, and the run goes on. A later run asks
  again.
- Any other :class:`~.chain.errors.ChainError` ends the run. A node that
  refuses or garbles one read is likely to do the same to the next, and
  going on would leave a run of gaps that says nothing about the chain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .chain.bars import confirm_pool_bars, read_pool_bars
from .chain.blocks import FINALITY_DEPTH, ChainBlockLocator
from .chain.errors import CallReverted
from .chain.rpc import Rpc
from .config import UniswapConfig
from .constants import EARLIEST_BAR_TIME, pool_key
from .domain.bars import Finality
from .domain.types import Pool
from .store.repository import Store, StoreError

__all__ = [
    "BackfillPlan",
    "BackfillRangeError",
    "BackfillSummary",
    "backfill",
    "check_range",
    "plan_backfill",
]


# Roughly what reading one boundary of a one-day bar costs once a run is
# under way: about fourteen header reads (the head, the block search and
# the close block; the first search of a run reads about twice as many,
# and a shorter bar's fewer), and two calls per pool.
_SEARCH_REQUESTS = 14
_REQUESTS_PER_POOL = 2


class BackfillRangeError(ValueError):
    """The range asked for is not one that can be backfilled."""


@dataclass(frozen=True)
class BackfillPlan:
    """The boundaries of a range, and which pools each still lacks a reading of.

    ``missing`` holds only the boundaries that lack something, oldest first.
    """

    boundaries: int
    missing: tuple[tuple[int, tuple[Pool, ...]], ...]

    @property
    def requests(self) -> int:
        """About how many requests reading what is missing takes."""
        return sum(_SEARCH_REQUESTS + _REQUESTS_PER_POOL * len(pools) for _, pools in self.missing)


@dataclass(frozen=True)
class BackfillSummary:
    """What a run did. ``unanswered`` and ``not_reached`` are boundaries, oldest first.

    ``not_reached`` are the boundaries the chain had not come to when the
    run started.
    """

    written: int
    already_stored: int
    unanswered: tuple[int, ...]
    not_reached: tuple[int, ...]
    confirmed: int
    reorged: int


def check_range(config: UniswapConfig, *, start: int, end: int) -> None:
    """Refuse a range that cannot be backfilled.

    ``start`` must be a boundary, and not before the chain's earliest bar;
    the range ends at the last boundary at or before ``end``.
    """
    interval = config.bars.interval_seconds
    earliest = EARLIEST_BAR_TIME[config.chain_id]
    if start % interval:
        raise BackfillRangeError(
            f"the range must start on a bar boundary: {start} is not a multiple of {interval}"
        )
    if start < earliest:
        raise BackfillRangeError(
            f"bars on chain {config.chain_id} start at {earliest}, and the range starts at {start}"
        )
    if end < start:
        raise BackfillRangeError(f"the range ends at {end}, before it starts at {start}")


def plan_backfill(store: Store, config: UniswapConfig, *, start: int, end: int) -> BackfillPlan:
    """What the store lacks between ``start`` and ``end``, both included. Reads no chain.

    The range is checked by :func:`check_range`.
    """
    check_range(config, start=start, end=end)
    interval = config.bars.interval_seconds
    stored = {
        pool: store.bar_times(config.chain_id, pool.address, interval, start=start, end=end)
        for pool in config.pools
    }
    missing = []
    boundaries = range(start, end + 1, interval)
    for time in boundaries:
        lacking = tuple(pool for pool in config.pools if time not in stored[pool])
        if lacking:
            missing.append((time, lacking))
    return BackfillPlan(boundaries=len(boundaries), missing=tuple(missing))


def _require_one_twap_window(store: Store, config: UniswapConfig) -> None:
    """Refuse to add readings to a series that was taken over another TWAP window.

    A series holds one window: a bar is checked against a limit set for the
    config's, and :func:`~.store.bar_source.load_bar` refuses a reading of
    another. Writing the config's window beside an older one would leave a
    store that can be read under neither.
    """
    window = config.bars.twap_window_seconds
    for pool in config.pools:
        others = store.twap_windows(
            config.chain_id, pool.address, config.bars.interval_seconds
        ) - {window}
        if others:
            raise StoreError(
                f"the store holds readings of {pool_key(pool)} with a TWAP over "
                f"{sorted(others)} seconds, and the config asks for {window}; backfill "
                f"that window into a new store, or set bars.twap_window_seconds back"
            )


def _confirm_pending(rpc: Rpc, store: Store, *, final_block: int) -> tuple[int, int]:
    """Check the pending readings that are final by now; how many held and how many did not."""
    checked = confirm_pool_bars(rpc, store.pending_bars(rpc.chain_id), final_block=final_block)
    final = [reading for reading in checked if reading.finality is Finality.FINAL]
    reorged = [reading for reading in checked if reading.finality is Finality.REORGED]
    if final:
        store.set_finality(final, Finality.FINAL)
    if reorged:
        store.set_finality(reorged, Finality.REORGED)
    return len(final), len(reorged)


def backfill(
    rpc: Rpc,
    store: Store,
    config: UniswapConfig,
    *,
    start: int,
    end: int,
    report: Callable[[int, str], None] = lambda time, text: None,
) -> BackfillSummary:
    """Read and store what :func:`plan_backfill` finds missing, oldest first.

    ``report`` is handed each boundary that was read and what came of it. A
    chain error other than a revert is raised as it is; what was stored
    before it stays.
    """
    if rpc.chain_id != config.chain_id:
        raise ValueError(
            f"the connection is to chain {rpc.chain_id} and the config is for {config.chain_id}"
        )
    plan = plan_backfill(store, config, start=start, end=end)
    _require_one_twap_window(store, config)
    head = rpc.latest_header()
    final_block = head.number - FINALITY_DEPTH
    confirmed, reorged = _confirm_pending(rpc, store, final_block=final_block)

    locator = ChainBlockLocator(rpc)
    written = 0
    unanswered: list[int] = []
    not_reached: list[int] = []
    for time, pools in plan.missing:
        if time > head.timestamp:
            not_reached.append(time)
            continue
        try:
            readings = read_pool_bars(
                rpc, locator, pools, time, settings=config.bars, final_block=final_block
            )
        except CallReverted as exc:
            unanswered.append(time)
            report(time, f"no answer ({exc})")
            continue
        store.insert_bars(readings)
        written += 1
        report(time, f"block {readings[0].close_block}, {readings[0].finality.value}")
    return BackfillSummary(
        written=written,
        already_stored=plan.boundaries - len(plan.missing),
        unanswered=tuple(unanswered),
        not_reached=tuple(not_reached),
        confirmed=confirmed,
        reorged=reorged,
    )
