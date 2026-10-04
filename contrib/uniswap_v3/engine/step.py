"""The engine's step on one bar, and what starts and reopens a run.

One step, for the bar a :class:`~..domain.types.MarketView` ends at:

1. The run has already decided this bar: its decision is returned and
   nothing is written. This is what makes a rerun harmless.
2. The bar is suspect: the step is recorded as skipped, and the strategy is
   not asked.
3. The strategy answers ``Hold``: only the valuation changes.
4. The strategy gives target weights: the swaps toward them are planned and
   each is handed to the executor. Only when every one fills, and the
   ledger covers them and their gas, are they applied, together. Otherwise
   none is, the reason is recorded, and the next bar's decision starts from
   the unchanged balances.

Every decided bar gets one decision and one valuation, written together.

"Together or not at all" holds for an executor whose fills are virtual, as a
modelled or a quoted one is: a fill that is not applied never happened. It
does not hold for an executor that signs. A leg that is already mined when a
later one is refused has changed the wallet, and this step would still
record the bar as rejected with the balances unchanged. A signing executor
is not to be wired to this step until the step records such a partial
rebalance as what it is.

What is not recorded stops the run instead: a strategy that raises, or
answers with something other than ``Hold`` or weights over exactly the
configured tokens; a bar that does not price those tokens; an executor that
answers for another swap than the one it was handed. The bar is left
undecided, so it can be decided once the cause is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError, UniswapConfig, config_snapshot
from ..domain.ledger import Ledger, LedgerError
from ..domain.records import BarSeen, Decision, Outcome, RunRecord, StepRecord, Valuation
from ..domain.routing import plan_swaps
from ..domain.types import Bar, Fill, Hold, MarketView, Rejection, RunMode, TargetWeights
from ..ports import Executor, Journal, Strategy
from ..strategies.registry import build_strategy

__all__ = ["Engine", "EngineError", "StepResult", "open_engine", "start_run"]


class EngineError(Exception):
    """The step cannot go on, and the bar is left undecided."""


@dataclass(frozen=True)
class StepResult:
    """A bar's decision, and whether this step made it or found it already made."""

    decision: Decision
    already_run: bool


@dataclass(frozen=True)
class Engine:
    """One run's step. Built by :func:`open_engine`, which checks what it is handed."""

    run_id: str
    config: UniswapConfig
    strategy: Strategy
    executor: Executor
    journal: Journal

    def step(self, view: MarketView, *, seen: BarSeen | None = None) -> StepResult:
        """Decide the bar ``view`` ends at, unless the run already has.

        ``seen`` is what the store said of that bar's close block, kept with
        the decision; a bar that came from no store has none.
        """
        bar = view.latest
        decided = self.journal.decision(self.run_id, bar.time)
        if decided is not None:
            return StepResult(decided, already_run=True)
        latest = self.journal.last_decided(self.run_id)
        if latest is not None and latest > bar.time:
            raise EngineError(
                f"the run {self.run_id!r} has decided the bar at {latest}, and the bar at "
                f"{bar.time} is before it"
            )
        ledger = self.journal.ledger(self.run_id)
        quote = self.config.quote.symbol
        symbols = {token.symbol for token in self.config.tokens}
        if set(ledger.balances) != symbols:
            raise EngineError(
                f"the run {self.run_id!r} holds {sorted(ledger.balances)}, and the config's "
                f"tokens are {sorted(symbols)}"
            )
        try:
            portfolio = ledger.portfolio(quote, bar.prices)
        except ValueError as exc:
            raise EngineError(f"the bar at {bar.time} does not price the run's tokens ({exc})") from exc

        if bar.suspect:
            return self._record(bar, seen, Outcome.SKIPPED_SUSPECT, ledger)
        answer = self.strategy.decide(view, portfolio)
        if isinstance(answer, Hold):
            return self._record(bar, seen, Outcome.HOLD, ledger)
        if not isinstance(answer, TargetWeights):
            raise EngineError(f"the strategy answered {answer!r}, neither Hold nor TargetWeights")
        if set(answer.weights) != symbols:
            raise EngineError(
                f"the strategy targets {sorted(answer.weights)}, and the config's tokens are "
                f"{sorted(symbols)}"
            )

        swaps = plan_swaps(
            portfolio,
            answer,
            tokens=self.config.tokens,
            pools=self.config.pools,
            settings=self.config.execution,
        )
        if not swaps:
            return self._record(bar, seen, Outcome.NO_TRADE, ledger, target=answer)
        fills: list[Fill] = []
        for leg, swap in enumerate(swaps):
            answered = self.executor.execute(swap, bar)
            if not isinstance(answered, Fill | Rejection) or answered.swap != swap:
                raise EngineError(f"the executor answered leg {leg} with {answered!r}")
            if isinstance(answered, Rejection):
                # The legs after a refused one are not asked for.
                reason = (
                    f"leg {leg} ({swap.token_in.symbol} to {swap.token_out.symbol}) was "
                    f"refused: {answered.reason}"
                )
                return self._record(
                    bar, seen, Outcome.REJECTED, ledger, target=answer, reason=reason
                )
            fills.append(answered)
        try:
            after = ledger.apply(fills)
        except LedgerError as exc:
            return self._record(
                bar, seen, Outcome.REJECTED, ledger, target=answer, reason=str(exc)
            )
        return self._record(bar, seen, Outcome.FILLED, after, target=answer, fills=tuple(fills))

    def _record(
        self,
        bar: Bar,
        seen: BarSeen | None,
        outcome: Outcome,
        ledger: Ledger,
        *,
        target: TargetWeights | None = None,
        reason: str | None = None,
        fills: tuple[Fill, ...] = (),
    ) -> StepResult:
        """Write the bar's decision with ``ledger``, the balances the step leaves, valued at the bar."""
        decision = Decision(
            time=bar.time,
            outcome=outcome,
            close_block=bar.close_block,
            target=target,
            reason=reason,
            seen=seen,
        )
        valuation = Valuation(
            time=bar.time,
            ledger=ledger,
            prices=bar.prices,
            total_value=ledger.portfolio(self.config.quote.symbol, bar.prices).total_value,
        )
        self.journal.record(
            self.run_id, StepRecord(decision=decision, valuation=valuation, fills=fills)
        )
        return StepResult(decision, already_run=False)


def start_run(
    journal: Journal,
    config: UniswapConfig,
    *,
    run_id: str,
    mode: RunMode,
    ledger: Ledger,
    created_at: int,
) -> RunRecord:
    """Start a run under ``config`` with ``ledger`` as its opening balances.

    The balances must name exactly the configured tokens, at zero for one
    the run starts without. The config's snapshot is kept with the run.
    """
    symbols = {token.symbol for token in config.tokens}
    if set(ledger.balances) != symbols:
        raise EngineError(
            f"the opening balances name {sorted(ledger.balances)}, and the config's tokens "
            f"are {sorted(symbols)}"
        )
    try:
        run = RunRecord(
            run_id=run_id,
            mode=mode,
            chain_id=config.chain_id,
            quote=config.quote.symbol,
            strategy=config.strategy.name,
            config=config_snapshot(config),
            ledger=ledger,
            created_at=created_at,
        )
    except ValueError as exc:
        raise EngineError(f"the run cannot be started ({exc})") from exc
    journal.insert_run(run)
    return run


def open_engine(
    journal: Journal, config: UniswapConfig, executor: Executor, *, run_id: str
) -> Engine:
    """The engine of the run ``run_id``, with the config's strategy built.

    A run is continued only under the config it was started with: a config
    whose snapshot differs is refused, and so is a run that is not there.
    A strategy the registry does not know, or whose params it refuses, is a
    :class:`~..config.ConfigError`.
    """
    run = journal.run(run_id)
    if run is None:
        raise EngineError(f"there is no run {run_id!r}")
    if run.config != config_snapshot(config):
        raise EngineError(
            f"the run {run_id!r} was started under another config; a changed config "
            f"needs a new run"
        )
    try:
        strategy = build_strategy(config.strategy.name, config.strategy.params)
    except ValueError as exc:
        raise ConfigError(f"strategy: {exc}") from exc
    return Engine(
        run_id=run_id, config=config, strategy=strategy, executor=executor, journal=journal
    )
