"""The engine's step on one bar, and what starts and reopens a run.

One step, for the bar a :class:`~..domain.types.MarketView` ends at:

1. The run has already decided this bar: its decision is returned and
   nothing is written. This is what makes a rerun harmless.
2. The bar is suspect: the step is recorded as skipped, with why, and the
   strategy is not asked.
3. The strategy answers ``Hold``: only the valuation changes.
4. The strategy gives target weights: the swaps toward them are planned and
   each is handed to the executor. Only when every one fills, and the gas
   balance covers their gas, are they applied, together. Otherwise none
   is, the reason is recorded, and the next bar's decision starts from the
   unchanged balances.

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
fails, or answers for another swap than the one it was handed; a ledger
that does not hold exactly the configured tokens; swaps that cannot be
planned, or that sell more than the ledger holds; a ``seen`` that describes
another block than the bar's; a ``suspicion`` of a bar that is not suspect,
or a suspect bar with a ``seen`` and no ``suspicion``;
a bar before the latest one the run has decided. The bar is left undecided,
so it can be decided once the cause is fixed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ConfigError, UniswapConfig, config_snapshot
from ..domain.decimal_context import plain
from ..domain.ledger import InsufficientGas, Ledger, LedgerError
from ..domain.records import (
    BarSeen,
    Decision,
    FillSource,
    Outcome,
    RejectionCode,
    RunRecord,
    SkipCode,
    StepRecord,
    Suspicion,
    Valuation,
)
from ..domain.routing import plan_swaps
from ..domain.types import (
    ETH_DECIMALS,
    Bar,
    Fill,
    Hold,
    MarketView,
    Rejection,
    RunMode,
    TargetWeights,
)
from ..ports import Executor, Journal, Strategy
from ..strategies.registry import build_strategy

__all__ = [
    "Engine",
    "EngineError",
    "StepResult",
    "open_engine",
    "start_or_continue_run",
    "start_run",
]


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

    def step(
        self,
        view: MarketView,
        *,
        seen: BarSeen | None = None,
        suspicion: Suspicion | None = None,
    ) -> StepResult:
        """Decide the bar ``view`` ends at, unless the run already has.

        ``seen`` is what the store said of that bar's close block, and
        ``suspicion`` why it holds the bar suspect; both are kept with the
        decision. A bar that came from no store has neither, and when such
        a bar is suspect the decision says that nothing said why.
        """
        bar = view.latest
        decided = self.journal.decision(self.run_id, bar.time)
        if decided is not None:
            return StepResult(decided, already_run=True)
        if seen is not None and seen.close_block != bar.close_block:
            raise EngineError(
                f"seen describes block {seen.close_block}, and the bar at {bar.time} closed "
                f"on {bar.close_block}"
            )
        if bar.suspect and seen is not None and suspicion is None:
            # A bar that came with what its store said of it comes with why it is suspect.
            raise EngineError(
                f"the bar at {bar.time} is suspect and came from a store, and nothing says "
                f"why it is suspect"
            )
        if suspicion is not None and not bar.suspect:
            raise EngineError(
                f"the bar at {bar.time} is not suspect, and it came with a reason to "
                f"skip it ({suspicion.reason})"
            )
        latest = self.journal.last_decided(self.run_id)
        if latest is not None and latest > bar.time:
            # A run only goes forward: the bars after this one were decided without it in view.
            raise EngineError(
                f"the run {self.run_id!r} has decided the bar at {latest}, and the bar at "
                f"{bar.time} is before it; a run only goes forward, so an earlier bar "
                f"needs a new run"
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
            raise EngineError(
                f"the bar at {bar.time} does not price the run's tokens ({exc})"
            ) from exc

        if bar.suspect:
            why = suspicion or Suspicion(
                SkipCode.UNSPECIFIED, "the bar was marked suspect, and nothing said why"
            )
            return self._record(
                bar, seen, Outcome.SKIPPED_SUSPECT, ledger, reason=why.reason, reason_code=why.code
            )
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

        try:
            swaps = plan_swaps(
                portfolio,
                answer,
                tokens=self.config.tokens,
                pools=self.config.pools,
                settings=self.config.execution,
            )
        except (ValueError, ArithmeticError) as exc:
            raise EngineError(f"the swaps at {bar.time} cannot be planned ({exc!r})") from exc
        if not swaps:
            return self._record(bar, seen, Outcome.NO_TRADE, ledger, target=answer)
        fills: list[Fill] = []
        for leg, swap in enumerate(swaps):
            try:
                answered = self.executor.execute(swap, bar)
            except (ValueError, ArithmeticError) as exc:
                raise EngineError(f"the executor failed on leg {leg} ({exc!r})") from exc
            if not isinstance(answered, Fill | Rejection) or answered.swap != swap:
                raise EngineError(f"the executor answered leg {leg} with {answered!r}")
            if isinstance(answered, Rejection):
                # The legs after a refused one are not asked for.
                reason = (
                    f"leg {leg} ({swap.token_in.symbol} to {swap.token_out.symbol}) was "
                    f"refused: {answered.reason}"
                )
                return self._record(
                    bar,
                    seen,
                    Outcome.REJECTED,
                    ledger,
                    target=answer,
                    reason=reason,
                    reason_code=RejectionCode.EXECUTOR,
                )
            fills.append(answered)
        try:
            after = ledger.apply(fills)
        except InsufficientGas as exc:
            return self._record(
                bar,
                seen,
                Outcome.REJECTED,
                ledger,
                target=answer,
                reason=str(exc),
                reason_code=RejectionCode.GAS,
            )
        except LedgerError as exc:
            # The plan sells no more than the ledger holds; this is a planning fault.
            raise EngineError(
                f"the fills at {bar.time} do not apply to the ledger ({exc})"
            ) from exc
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
        reason_code: RejectionCode | SkipCode | None = None,
        fills: tuple[Fill, ...] = (),
    ) -> StepResult:
        """Write the bar's decision, with ``ledger`` (what the step leaves) valued at the bar."""
        decision = Decision(
            time=bar.time,
            outcome=outcome,
            close_block=bar.close_block,
            target=target,
            reason=reason,
            reason_code=reason_code,
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


def _strategy(config: UniswapConfig) -> Strategy:
    """The config's strategy, built; one the registry refuses is a :class:`~..config.ConfigError`."""
    try:
        return build_strategy(config.strategy.name, config.strategy.params)
    except ValueError as exc:
        raise ConfigError(f"strategy: {exc}") from exc


def start_run(
    journal: Journal,
    config: UniswapConfig,
    *,
    run_id: str,
    mode: RunMode,
    ledger: Ledger,
    created_at: int,
    fills: FillSource = FillSource.MODEL,
) -> RunRecord:
    """Start a run under ``config`` with ``ledger`` as its opening balances, filled from ``fills``.

    The balances must name exactly the configured tokens, at zero for one
    the run starts without, though not all at zero: a run that holds nothing
    can never trade. No balance has more decimal places than its token, nor
    the gas balance more than ETH. The config's snapshot is kept with the run.
    A config whose strategy cannot be built starts no run: the run could
    never be opened, and its id would be taken.
    """
    _strategy(config)
    symbols = {token.symbol for token in config.tokens}
    if set(ledger.balances) != symbols:
        raise EngineError(
            f"the opening balances name {sorted(ledger.balances)}, and the config's tokens "
            f"are {sorted(symbols)}"
        )
    if not any(ledger.balances.values()):
        raise EngineError("the opening balances are all zero: the run would have nothing to trade")
    places = {token.symbol: token.decimals for token in config.tokens}
    for what, amount, limit in (
        *((symbol, ledger.balances[symbol], places[symbol]) for symbol in sorted(symbols)),
        ("gas ETH", ledger.gas_eth, ETH_DECIMALS),
    ):
        numerator, denominator = amount.as_integer_ratio()
        if (numerator * 10**limit) % denominator:
            raise EngineError(
                f"the opening {what} balance {plain(amount)} has more than {limit} decimal places"
            )
    # Outside the try: a config that cannot be written down is a ConfigError, and stays one.
    snapshot = config_snapshot(config)
    try:
        run = RunRecord(
            run_id=run_id,
            mode=mode,
            chain_id=config.chain_id,
            quote=config.quote.symbol,
            strategy=config.strategy.name,
            config=snapshot,
            ledger=ledger,
            created_at=created_at,
            fills=fills,
        )
    except ValueError as exc:
        raise EngineError(f"the run cannot be started ({exc})") from exc
    journal.insert_run(run)
    return run


def start_or_continue_run(
    journal: Journal,
    config: UniswapConfig,
    *,
    run_id: str,
    mode: RunMode,
    opening: Ledger | None,
    created_at: int,
    fills: FillSource = FillSource.MODEL,
) -> None:
    """Start the run ``run_id`` in ``mode`` with ``opening``, or check that it can be carried on.

    A run that is not there needs ``opening``, and is started with ``fills``
    as where its fills come from. One that is there must be of ``mode``,
    and an ``opening`` handed with it must be the balances it was started
    with. Whether it was started under ``config`` is
    :func:`open_engine`'s check.
    """
    run = journal.run(run_id)
    if run is None:
        if opening is None:
            raise EngineError(f"there is no run {run_id!r}, and a new run needs opening balances")
        start_run(
            journal,
            config,
            run_id=run_id,
            mode=mode,
            ledger=opening,
            created_at=created_at,
            fills=fills,
        )
        return
    if run.mode is not mode:
        raise EngineError(
            f"the run {run_id!r} is a {run.mode.value} run, and is not carried on as a "
            f"{mode.value} run"
        )
    if opening is not None and opening != run.ledger:
        raise EngineError(
            f"the run {run_id!r} was started with other opening balances; other balances "
            f"need a new run"
        )


def open_engine(
    journal: Journal, config: UniswapConfig, executor: Executor, *, run_id: str
) -> Engine:
    """The engine of the run ``run_id``, with the config's strategy built.

    A run is continued only under the config it was started with, and by an
    executor whose fills come from where the run's do: a config whose
    snapshot differs is refused, an executor of another source is, and so
    is a run that is not there.
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
    if run.fills is not executor.source:
        raise EngineError(
            f"the run {run_id!r} takes its fills from the {run.fills.value}, and is not "
            f"carried on with fills from the {executor.source.value}; another source of "
            f"fills needs a new run"
        )
    return Engine(
        run_id=run_id,
        config=config,
        strategy=_strategy(config),
        executor=executor,
        journal=journal,
    )
