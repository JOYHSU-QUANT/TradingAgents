"""A backtest: the engine's step replayed over the store's bars, oldest first.

The bars come from the store. With fills from the model, a backtest reads
no chain; with fills from quotes, the chain is asked what each swap would
have returned at its fill block and for that block's base fee, and nothing
else. Each bar is decided by
the same :meth:`~.step.Engine.step` every other run mode uses.

:func:`replay` is the loop itself, whatever the mode: a paper run is the
same replay over the bars it has just read (:mod:`..paper`).

The view a bar is decided on holds every bar the store has up to that bar,
those from before the range included, and none after it. What a strategy
sees therefore does not depend on where a run was started or carried on
from, and a bar stored later than the one being decided cannot reach its
decision. A config that reads verdicts (a ``verdicts`` section) has the
view carry, at each of those bars, what its source said of the traded
tokens there (:func:`~..store.verdict_source.load_verdicts`); a verdict
stored at a later bar cannot reach the decision either.

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
stand: a new run decides them on what the store holds now. The same goes for
the verdicts: a bar decided earlier whose verdicts in the store are no longer
the ones its decision saw (:attr:`~..domain.records.Decision.verdicts`) is
counted, and its decision stands.
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
from ..domain.verdicts import Verdict, usable_rating
from ..ports import Executor, Wallet
from ..store.bar_source import StoredBar, load_bar
from ..store.repository import Store
from ..store.verdict_source import load_verdicts
from .executors import ModelExecutor
from .step import EngineError, open_engine, start_or_continue_run

__all__ = ["BacktestRangeError", "BacktestSummary", "NoBarInRange", "replay", "run_backtest"]


class BacktestRangeError(ValueError):
    """The range asked for is not one the store's bars can be replayed over."""


class NoBarInRange(BacktestRangeError):
    """The store holds no bar in the range asked for."""


@dataclass(frozen=True)
class BacktestSummary:
    """What a backtest did over its range, ``start`` to ``end``, both boundaries.

    ``decided`` bars were decided by this call and ``already_decided`` ones
    by an earlier one; ``outcomes`` counts both. ``on_pending`` of the bars
    decided by this call were decided on a reading that was not final; a
    suspect bar is skipped, whatever its finality, and is not one of them. The
    rest are boundaries, oldest first: ``missing`` had no bar, ``changed``
    were decided earlier on a reading the store no longer holds as it was,
    ``gas_rejected`` had their rebalance refused for want of gas,
    ``executor_rejected`` had it refused by the executor (a rebalance a
    signed swap left partial is in neither, and is counted in ``outcomes``),
    ``skipped`` were suspect and not traded on, ``verdicts_changed`` were
    decided earlier on other verdicts than the store holds for them now (a
    verdict recorded after the bar was decided, most often), and ``unrated``
    were decided by this call, by a run that reads verdicts, with no rating
    on some traded token: no verdict at the bar, or a ``REVIEW``. A bar
    skipped as suspect is not among them: nothing was decided on it.
    """

    start: int
    end: int
    decided: int
    already_decided: int
    on_pending: int
    missing: tuple[int, ...]
    changed: tuple[int, ...]
    gas_rejected: tuple[int, ...]
    executor_rejected: tuple[int, ...]
    skipped: tuple[int, ...]
    outcomes: Mapping[Outcome, int]
    # Boundaries decided earlier, by a run that reads verdicts, whose verdicts in the
    # store are no longer the ones the decision saw. Like ``changed``, the decisions stand.
    verdicts_changed: tuple[int, ...] = ()
    # Boundaries this call decided on which some traded token had no rating. A
    # strategy that reads verdicts takes no new risk on such a token there.
    unrated: tuple[int, ...] = ()

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


def _judged_differently(decision: Decision, said: Mapping[str, Verdict]) -> bool:
    """Whether the stored verdicts at the bar are no longer the ones ``decision`` saw.

    A decision of a run that reads no verdicts saw none, and is compared
    with nothing.
    """
    saw = decision.verdicts
    return saw is not None and dict(saw) != {
        symbol: verdict.digest for symbol, verdict in said.items()
    }


def _unrated(config: UniswapConfig, said: Mapping[str, Verdict]) -> bool:
    """Whether some traded token has no rating in ``said``, by the strategies' own rule."""
    return any(usable_rating(said.get(symbol)) is None for symbol in config.traded_symbols)


def run_backtest(
    store: Store,
    config: UniswapConfig,
    *,
    run_id: str,
    start: int,
    end: int | None = None,
    opening: Ledger | None = None,
    now: int,
    executor: Executor | None = None,
) -> BacktestSummary:
    """Decide every stored bar from ``start`` to ``end`` for the backtest run ``run_id``.

    Fills come from :class:`~.executors.ModelExecutor`, or from ``executor``
    when one is handed. The rest is :func:`replay`'s.
    """
    return replay(
        store,
        config,
        ModelExecutor(config.quote.symbol, config.execution) if executor is None else executor,
        run_id=run_id,
        mode=RunMode.BACKTEST,
        start=start,
        end=end,
        opening=opening,
        now=now,
    )


def replay(
    store: Store,
    config: UniswapConfig,
    executor: Executor,
    *,
    run_id: str,
    mode: RunMode,
    start: int,
    end: int | None = None,
    opening: Ledger | None = None,
    now: int,
    wallet: Wallet | None = None,
    fork_block: int | None = None,
) -> BacktestSummary:
    """Decide every stored bar from ``start`` to ``end`` for the run ``run_id``, filled by ``executor``.

    ``start`` is a boundary. The range ends at the last boundary at or
    before ``end``, or, with no ``end``, at the store's latest bar. A run
    that is not stored is started in ``mode`` with ``opening`` as its
    balances, and keeps where ``executor``'s fills come from; one that is
    stored is carried on, in the mode, from the source and under the
    config it was started with, and an ``opening`` handed with it must be
    the one it was started with. A range that holds no bar is
    :class:`NoBarInRange`, raised before a run is started. ``now`` is the
    time of the call: a run started here is created then, and every bar
    decided here is decided then. An executor that signs comes with the
    ``wallet`` it signs from, and a fork run is started with ``fork_block``
    (:func:`~.step.open_engine`, :func:`~.step.start_run`).

    Whatever stops the engine's step (:class:`~.step.EngineError`, what a
    strategy raised, or a chain read of the executor's that failed) stops
    the replay at that bar; the bars decided before it stay, and so does a
    run that was started and stopped at its first bar: its id is taken,
    with the opening balances it was given.
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
        raise NoBarInRange(f"the store holds no bar from {start} to {until}")
    last = max(in_range) if end is None else end - end % interval

    start_or_continue_run(
        store,
        config,
        run_id=run_id,
        mode=mode,
        opening=opening,
        created_at=now,
        fills=executor.source,
        fork_block=fork_block,
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
    engine = open_engine(store, config, executor, run_id=run_id, now=now, wallet=wallet)

    bars: list[Bar] = []
    # What the config's source said at each bar so far, for bars it said anything at.
    verdicts: dict[int, Mapping[str, Verdict]] = {}
    source = None if config.verdicts is None else config.verdicts.source
    decided = already_decided = on_pending = 0
    changed: list[int] = []
    verdicts_changed: list[int] = []
    unrated: list[int] = []
    gas_rejected: list[int] = []
    executor_rejected: list[int] = []
    skipped: list[int] = []
    outcomes: Counter[Outcome] = Counter()
    for time in stored:
        loaded = load_bar(store, config, time)
        if loaded is None:
            # Only a writer taking a reading away mid-run could bring this about.
            raise EngineError(f"the bar at {time} is no longer in the store")
        bars.append(loaded.bar)
        said = load_verdicts(store, config, time)
        if said:
            verdicts[time] = said
        if time < start:
            continue
        # Asked here as well as in the step, so that a bar already decided costs no view.
        decision = store.decision(run_id, time)
        if decision is not None:
            already_decided += 1
            if _reads_differently(decision, loaded):
                changed.append(time)
            if _judged_differently(decision, said):
                verdicts_changed.append(time)
        else:
            try:
                view = MarketView(tuple(bars), verdicts, source)
            except ValueError as exc:
                raise EngineError(
                    f"the stored bars up to {time} do not make a view ({exc})"
                ) from exc
            decision = engine.step(view, seen=loaded.seen, suspicion=loaded.suspicion).decision
            decided += 1
            if source is not None and not loaded.bar.suspect and _unrated(config, said):
                unrated.append(time)
            # A suspect bar is skipped whatever its finality: nothing was decided on it.
            on_pending += loaded.finality is Finality.PENDING and not loaded.bar.suspect
        outcomes[decision.outcome] += 1
        # A partial rebalance is counted by its outcome alone.
        rejected = decision.outcome is Outcome.REJECTED
        if rejected and decision.reason_code is RejectionCode.GAS:
            gas_rejected.append(time)
        elif rejected and decision.reason_code is RejectionCode.EXECUTOR:
            executor_rejected.append(time)
        elif decision.outcome is Outcome.SKIPPED_SUSPECT:
            skipped.append(time)
    return BacktestSummary(
        start=start,
        end=last,
        decided=decided,
        already_decided=already_decided,
        on_pending=on_pending,
        missing=tuple(time for time in range(start, last + 1, interval) if time not in in_range),
        changed=tuple(changed),
        gas_rejected=tuple(gas_rejected),
        executor_rejected=tuple(executor_rejected),
        skipped=tuple(skipped),
        outcomes=MappingProxyType(dict(outcomes)),
        verdicts_changed=tuple(verdicts_changed),
        unrated=tuple(unrated),
    )
