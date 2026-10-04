"""A backtest: the engine's step replayed over the store's bars, oldest first.

The bars come from the store and the fills from the model, so a backtest
reads no chain. Each bar is decided by the same :meth:`~.step.Engine.step`
every other run mode uses.

The view a bar is decided on holds every bar the store has up to that bar,
those from before the range included, and none after it. What a strategy
sees therefore does not depend on where a run was started or carried on
from, and a bar stored later than the one being decided cannot reach its
decision.

A boundary of the range that lacks the reading of a configured pool has no
bar: it is not decided, the view simply does not hold it, and the summary
counts it. A range that holds no bar at all is refused.

A run can be carried on: a bar it has already decided is counted as such
and nothing is written for it, so the same command run twice leaves the
store as the first run left it. A run only goes forward, though, and leaves
no stored bar behind undecided:

- A range that starts after the run's latest decided bar, with stored bars
  in between, is refused. Those bars could never be decided afterwards.
- When a boundary the run passed over without a bar is given one later,
  the engine refuses that bar: the bars the run decided after the gap were
  decided without it in view. The range is replayed as a new run.

A bar is decided on the reading the store holds at that moment, final or
not. The summary counts the bars this call decided on a reading that was not
final yet, and the bars decided earlier whose reading has changed since (another
close block, or suspect now and not then, or the reverse). Their decisions
stand: a new run decides them on what the store holds now.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..config import UniswapConfig
from ..domain.bars import Finality
from ..domain.ledger import Ledger
from ..domain.records import Decision, Outcome, RejectionCode
from ..domain.types import Bar, MarketView, RunMode
from ..store.bar_source import StoredBar, load_bar
from ..store.repository import Store
from .executors import ModelExecutor
from .step import EngineError, open_engine, start_or_continue_run

__all__ = ["BacktestRangeError", "BacktestSummary", "run_backtest"]


class BacktestRangeError(ValueError):
    """The range asked for is not one the store's bars can be replayed over."""


@dataclass(frozen=True)
class BacktestSummary:
    """What a backtest did over its range, ``start`` to ``end``, both boundaries.

    ``decided`` bars were decided by this call and ``already_decided`` ones
    by an earlier one; ``outcomes`` counts both. ``on_pending`` of the bars
    decided by this call were decided on a reading that was not final; a
    suspect bar is skipped, whatever its finality, and is not one of them. The
    rest are boundaries, oldest first: ``missing`` had no bar, ``changed``
    were decided earlier on a reading the store no longer holds as it was,
    and ``gas_rejected`` had their rebalance refused for want of gas.
    """

    start: int
    end: int
    decided: int
    already_decided: int
    on_pending: int
    missing: tuple[int, ...]
    changed: tuple[int, ...]
    gas_rejected: tuple[int, ...]
    outcomes: Mapping[Outcome, int]

    @property
    def boundaries(self) -> int:
        """How many boundaries the range holds."""
        return self.decided + self.already_decided + len(self.missing)


def _stored_boundaries(store: Store, config: UniswapConfig, *, end: int | None) -> list[int]:
    """The boundaries up to ``end`` that every configured pool has a reading at, oldest first."""
    return sorted(
        set.intersection(
            *(
                store.bar_times(
                    config.chain_id, pool.address, config.bars.interval_seconds, start=0, end=end
                )
                for pool in config.pools
            )
        )
    )


def _reads_differently(decision: Decision, loaded: StoredBar) -> bool:
    """Whether the stored bar is no longer the one ``decision`` was made on."""
    seen = decision.seen
    return decision.suspect != loaded.bar.suspect or (
        seen is not None and seen.close_block_hash != loaded.seen.close_block_hash
    )


def run_backtest(
    store: Store,
    config: UniswapConfig,
    *,
    run_id: str,
    start: int,
    end: int | None = None,
    opening: Ledger | None = None,
    created_at: int,
) -> BacktestSummary:
    """Decide every stored bar from ``start`` to ``end`` for the run ``run_id``.

    ``start`` is a boundary. The range ends at the last boundary at or
    before ``end``, or, with no ``end``, at the store's latest bar. A run
    that is not stored is started with ``opening`` as its balances; one that
    is stored is carried on, under the config it was started with, and an
    ``opening`` handed with it must be the one it was started with.

    Fills come from :class:`~.executors.ModelExecutor`. Whatever stops the
    engine's step (:class:`~.step.EngineError`, or what a strategy raised)
    stops the backtest at that bar; the bars decided before it stay, and so
    does a run that was started and stopped at its first bar: its id is
    taken, with the opening balances it was given.
    """
    interval = config.bars.interval_seconds
    if start % interval:
        raise BacktestRangeError(
            f"the range must start on a bar boundary: {start} is not a multiple of {interval}"
        )
    if end is not None and end < start:
        raise BacktestRangeError(f"the range ends at {end}, before it starts at {start}")
    stored = _stored_boundaries(store, config, end=end)
    in_range = {time for time in stored if time >= start}
    if not in_range:
        until = "the store's latest bar" if end is None else str(end)
        raise BacktestRangeError(f"the store holds no bar from {start} to {until}")
    last = max(in_range) if end is None else end - end % interval

    start_or_continue_run(
        store,
        config,
        run_id=run_id,
        mode=RunMode.BACKTEST,
        opening=opening,
        created_at=created_at,
    )
    latest = store.last_decided(run_id)
    if latest is not None:
        passed_over = [time for time in stored if latest < time < start]
        if passed_over:
            raise EngineError(
                f"the run {run_id!r} has decided up to the bar at {latest} and the range "
                f"starts at {start}: the {len(passed_over)} stored bar(s) between them, the "
                f"first at {passed_over[0]}, would be left undecided for good; start the "
                f"range no later than {passed_over[0]}, or use a new run"
            )
    executor = ModelExecutor(config.quote.symbol, config.execution)
    engine = open_engine(store, config, executor, run_id=run_id)

    bars: list[Bar] = []
    decided = already_decided = on_pending = 0
    changed: list[int] = []
    gas_rejected: list[int] = []
    outcomes: Counter[Outcome] = Counter()
    for time in stored:
        loaded = load_bar(store, config, time)
        if loaded is None:
            # Only a writer taking a reading away mid-run could bring this about.
            raise EngineError(f"the bar at {time} is no longer in the store")
        bars.append(loaded.bar)
        if time < start:
            continue
        # Asked here as well as in the step, so that a bar already decided costs no view.
        decision = store.decision(run_id, time)
        if decision is not None:
            already_decided += 1
            if _reads_differently(decision, loaded):
                changed.append(time)
        else:
            try:
                view = MarketView(tuple(bars))
            except ValueError as exc:
                raise EngineError(
                    f"the stored bars up to {time} do not make a view ({exc})"
                ) from exc
            decision = engine.step(view, seen=loaded.seen, suspicion=loaded.suspicion).decision
            decided += 1
            # A suspect bar is skipped whatever its finality: nothing was decided on it.
            on_pending += loaded.finality is Finality.PENDING and not loaded.bar.suspect
        outcomes[decision.outcome] += 1
        if decision.reason_code is RejectionCode.GAS:
            gas_rejected.append(time)
    return BacktestSummary(
        start=start,
        end=last,
        decided=decided,
        already_decided=already_decided,
        on_pending=on_pending,
        missing=tuple(time for time in range(start, last + 1, interval) if time not in in_range),
        changed=tuple(changed),
        gas_rejected=tuple(gas_rejected),
        outcomes=MappingProxyType(dict(outcomes)),
    )
