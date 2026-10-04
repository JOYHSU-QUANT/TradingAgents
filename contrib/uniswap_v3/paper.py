"""A paper run: the bars the chain has closed since the run's last one, read and decided.

One call is one visit, and whoever schedules it decides how often. A visit

1. reads into the store every boundary from the one after the run's latest
   decided bar to the latest that has passed, as
   :func:`~.backfill.backfill` does, checking the store's pending readings
   on the way. A run that has decided nothing starts at the latest.
2. decides those bars, oldest first, through
   :func:`~.engine.backtest.replay`, with fills quoted at each bar's fill
   block.

The fill block is fixed by the bar. A visit that comes late, or that catches
up on several bars, therefore decides and fills each as an earlier visit
would have, and a backtest with quoted fills over the same bars repeats the
run.

A visit that comes before the latest boundary, or the fill block of its
bar, is on the node's chain decides nothing and raises
:class:`~.chain.errors.BlockNotFound`: a later visit finds it. A visit that
finds the latest bar already decided checks the pending readings and writes
no decision.

A bar a visit catches up on is read and quoted at blocks as old as the
visits that were missed, which takes a node that still has their state: an
archive node, once a visit has been missed by more than the node keeps.

The latest bar is decided on a reading that is not final yet: its close
block is minutes old. A reading the chain later drops is marked by a later
visit, and the decision made on it stands.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backfill import BackfillSummary, backfill
from .chain.errors import BlockNotFound
from .chain.rpc import Rpc
from .config import UniswapConfig
from .domain.ledger import Ledger
from .domain.records import Decision
from .domain.types import RunMode
from .engine.backtest import BacktestSummary, NoBarInRange, replay
from .engine.executors import fill_block
from .engine.step import EngineError, open_engine, start_or_continue_run
from .ports import Executor
from .store.bar_source import load_bar
from .store.repository import Store

__all__ = ["PaperSummary", "run_paper"]


@dataclass(frozen=True)
class PaperSummary:
    """What a visit did.

    ``latest`` is the latest boundary that has passed, and ``decision`` the
    run's decision on its bar: ``None`` when the chain had no answer at that
    boundary, so that it has no bar. ``read`` is what was read into the
    store. ``replayed`` is what was decided, ``None`` when no boundary of
    the visit has a bar.
    """

    latest: int
    decision: Decision | None
    read: BackfillSummary
    replayed: BacktestSummary | None


def run_paper(
    rpc: Rpc,
    store: Store,
    config: UniswapConfig,
    executor: Executor,
    *,
    run_id: str,
    opening: Ledger | None = None,
    now: int,
) -> PaperSummary:
    """One visit of the paper run ``run_id`` at the time ``now``.

    ``executor`` fills from quotes on ``rpc``'s chain. A run that is not
    stored is started with ``opening`` as its balances; one that is stored
    is carried on, as :func:`~.engine.backtest.replay` carries a run on.
    A run that cannot be carried on is refused before the chain is read.
    A failed chain read is raised as it is; the bars read and the bars
    decided before it stay.
    """
    interval = config.bars.interval_seconds
    latest = now - now % interval
    # Before anything is read: nothing the node says can make up for these.
    if store.run(run_id) is None:
        if opening is None:
            raise EngineError(
                f"there is no run {run_id!r}, and a new run needs opening balances"
            )
    else:
        start_or_continue_run(
            store,
            config,
            run_id=run_id,
            mode=RunMode.PAPER,
            opening=opening,
            created_at=now,
        )
        open_engine(store, config, executor, run_id=run_id)
    decided = store.last_decided(run_id)
    start = latest if decided is None else min(decided + interval, latest)
    read = backfill(rpc, store, config, start=start, end=latest)
    if latest in read.not_reached:
        raise BlockNotFound(f"the node's chain has not reached the bar boundary at {latest} yet")

    loaded = load_bar(store, config, latest)
    if loaded is not None and store.decision(run_id, latest) is None:
        needed = fill_block(loaded.bar, config.execution)
        head = rpc.latest_header().number
        if head < needed:
            raise BlockNotFound(
                f"the bar at {latest} fills at block {needed}, and the node's chain is at "
                f"block {head}"
            )
    try:
        replayed: BacktestSummary | None = replay(
            store,
            config,
            executor,
            run_id=run_id,
            mode=RunMode.PAPER,
            start=start,
            end=latest,
            opening=opening,
            created_at=now,
        )
    except NoBarInRange:
        # The chain had no answer at any boundary of the visit, so none has a bar.
        replayed = None
    return PaperSummary(
        latest=latest,
        decision=store.decision(run_id, latest),
        read=read,
        replayed=replayed,
    )
