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
does not hold for an executor that signs (one whose fills come from the
chain), and the step trades from that executor's :class:`~..ports.Wallet`
another way:

1. The wallet is prepared for the bar and must hold what the ledger says,
   or the step stops with nothing sent.
2. The bar's send is written to the journal, and then each swap in turn is
   sent. Each one that fills is written as a leg of the send as it fills.
3. A swap refused (nothing of it was sent) ends the rebalance there: with
   no leg filled it is rejected, and otherwise it is partial, its filled
   legs applied as they stand. The strategy decides the next bar from
   there.
4. The wallet must then hold the ledger with the filled legs applied. The
   decision settles the send.

Anything else that stops such a step after the send was written (a failed
send, a wallet that does not hold what it should, a decision that cannot
be written) leaves the send open and says what stopped it, and
:class:`UnsettledSend` is raised. The one exception is the first swap's
executor raising with no transaction named (``tx_hashes``), which by the
:class:`~..ports.Executor` contract means nothing was sent: the send is
taken back, the error is raised as it is, and the bar is left undecided to
be tried again, as a failed read leaves a bar in every other mode. A run with an
open send goes no further, and its bar is not decided again: what reached
the chain is known only from the legs written and from the wallet.

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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ..config import ConfigError, UniswapConfig, config_snapshot
from ..domain.decimal_context import plain
from ..domain.ledger import InsufficientGas, Ledger, LedgerError
from ..domain.records import (
    BarSeen,
    Decision,
    FillSource,
    OpenSend,
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
    SwapIntent,
    TargetWeights,
)
from ..ports import Executor, Journal, Strategy, Wallet
from ..strategies.registry import build_strategy

__all__ = [
    "Engine",
    "EngineError",
    "StepResult",
    "UnsettledSend",
    "holdings_text",
    "open_engine",
    "start_or_continue_run",
    "start_run",
]


class EngineError(Exception):
    """The step cannot go on, and the bar is left undecided."""


class UnsettledSend(EngineError):
    """A bar's swaps were being sent and its step did not end: the run goes no further.

    What was written of the send is the journal's :meth:`~..ports.Journal.open_send`.
    """


def holdings_text(ledger: Ledger) -> str:
    """``ledger`` as one line: each token's balance, then the gas balance."""
    amounts = ", ".join(
        f"{symbol} {plain(amount)}" for symbol, amount in sorted(ledger.balances.items())
    )
    return f"{amounts}; gas ETH {plain(ledger.gas_eth)}"


def _checked(leg: int, swap: SwapIntent, answered: object) -> Fill | Rejection:
    """``answered``, an executor's answer to ``swap``, when it is a fill or a refusal of it."""
    if not isinstance(answered, Fill | Rejection) or answered.swap != swap:
        raise EngineError(f"the executor answered leg {leg} with {answered!r}")
    return answered


def _code(rejection: Rejection) -> RejectionCode:
    """What a refusal is recorded as: a want of gas, or the executor's answer."""
    return RejectionCode.GAS if rejection.short_of_gas else RejectionCode.EXECUTOR


def _refused(leg: int, swap: SwapIntent, rejection: Rejection) -> str:
    return (
        f"leg {leg} ({swap.token_in.symbol} to {swap.token_out.symbol}) was refused: "
        f"{rejection.reason}"
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and the errors it was raised from, each once, oldest last."""
    chain: list[BaseException] = []
    error: BaseException | None = exc
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__cause__
    return chain


def _hashes(exc: BaseException) -> tuple[str, ...]:
    """The transactions ``exc``, or an error it was raised from, says may have been sent.

    A send error names them in ``tx_hashes``; the engine does not know the
    chain's errors, and reads them by that name.
    """
    for error in _chain(exc):
        hashes = getattr(error, "tx_hashes", None)
        if isinstance(hashes, tuple) and hashes and all(isinstance(h, str) for h in hashes):
            return hashes
    return ()


def _unsettled(run_id: str, send: OpenSend) -> UnsettledSend:
    stopped = f" ({send.failure})" if send.failure else ""
    return UnsettledSend(
        f"the run {run_id!r} has an open send at the bar {send.time}: its swaps were being "
        f"sent and its step did not end{stopped}, after {len(send.legs)} leg(s) filled; "
        f"what the wallet holds is to be reconciled with what was written, and the run "
        f"goes no further"
    )


@dataclass(frozen=True)
class StepResult:
    """A bar's decision, and whether this step made it or found it already made."""

    decision: Decision
    already_run: bool


@dataclass(frozen=True)
class Engine:
    """One run's step. Built by :func:`open_engine`, which checks what it is handed.

    ``decided_at`` is the time of the call the engine was opened for; every
    bar it decides is recorded as decided then. ``wallet`` is what an
    executor that signs trades from, and an engine has one exactly when its
    executor signs.
    """

    run_id: str
    config: UniswapConfig
    strategy: Strategy
    executor: Executor
    journal: Journal
    decided_at: int
    wallet: Wallet | None = None

    def __post_init__(self) -> None:
        signs = self.executor.source.signs
        if signs != (self.wallet is not None):
            raise EngineError(
                "an executor whose fills come from the chain signs from a wallet, which is "
                "handed with it"
                if signs
                else f"an executor whose fills come from the {self.executor.source.value} "
                f"signs nothing, and is handed no wallet"
            )

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
        if self.wallet is not None:
            opened = self.journal.open_send(self.run_id)
            if opened is not None:
                raise _unsettled(self.run_id, opened)
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
        if self.wallet is not None:
            return self._sign(bar, seen, answer, ledger, swaps, self.wallet)
        fills: list[Fill] = []
        for leg, swap in enumerate(swaps):
            answered = _checked(leg, swap, self._execute(leg, swap, bar))
            if isinstance(answered, Rejection):
                # The legs after a refused one are not asked for.
                return self._record(
                    bar,
                    seen,
                    Outcome.REJECTED,
                    ledger,
                    target=answer,
                    reason=_refused(leg, swap, answered),
                    reason_code=_code(answered),
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

    def _execute(self, leg: int, swap: SwapIntent, bar: Bar) -> object:
        """What the executor answers to ``swap``, leg ``leg`` of the bar's rebalance, unchecked."""
        try:
            return self.executor.execute(swap, bar)
        except (ValueError, ArithmeticError) as exc:
            raise EngineError(f"the executor failed on leg {leg} ({exc!r})") from exc

    def _sign(
        self,
        bar: Bar,
        seen: BarSeen | None,
        target: TargetWeights,
        ledger: Ledger,
        swaps: Sequence[SwapIntent],
        wallet: Wallet,
    ) -> StepResult:
        """Send ``swaps`` from ``wallet``, which holds ``ledger``, keeping each leg as it fills."""
        time = bar.time
        wallet.prepare(bar, ledger)
        self._require_holds(wallet, ledger, f"before the swaps of the bar at {time}, nothing sent")
        self.journal.begin_send(self.run_id, time, started_at=self.decided_at)
        fills: list[Fill] = []
        refused: Rejection | None = None
        reason: str | None = None
        unsent: Exception | None = None
        # An answer the executor gave and the journal has not taken: what it cost is not known.
        unwritten: object | None = None
        try:
            for leg, swap in enumerate(swaps):
                try:
                    raw = self._execute(leg, swap, bar)
                except Exception as exc:
                    if fills or _hashes(exc):
                        raise
                    # Nothing of the rebalance was sent (the port's contract).
                    unsent = exc
                    break
                unwritten = raw
                # An answer that is not one may follow a send: it stops the run.
                answered = _checked(leg, swap, raw)
                if isinstance(answered, Rejection):
                    # Nothing of it was sent; the legs after it are not asked for.
                    refused, reason = answered, _refused(leg, swap, answered)
                    unwritten = None
                    break
                self.journal.record_leg(self.run_id, time, leg, answered)
                fills.append(answered)
                unwritten = None
            if unsent is None:
                try:
                    after = ledger.apply(fills)
                except LedgerError as exc:
                    # The wallet held the ledger, and paid no more than it held.
                    raise EngineError(
                        f"the fills at {time} do not apply to the ledger ({exc})"
                    ) from exc
                self._require_holds(wallet, after, f"after the swaps of the bar at {time}")
        except Exception as exc:
            raise self._stopped(time, exc, unwritten=unwritten) from exc
        if unsent is not None:
            # The bar is left undecided, as a read that failed leaves one, and can be tried again.
            try:
                self.journal.abandon_send(self.run_id, time)
            except Exception as exc:
                # The send stays: it says what stopped it, which sent nothing.
                raise self._stopped(time, unsent) from exc
            raise unsent
        if refused is None:
            outcome = Outcome.FILLED
        elif fills:
            outcome = Outcome.PARTIAL
            reason = f"{reason}; the {len(fills)} leg(s) before it filled, and stand"
        else:
            outcome = Outcome.REJECTED
        try:
            return self._record(
                bar,
                seen,
                outcome,
                after,
                target=target,
                reason=reason,
                reason_code=None if refused is None else _code(refused),
                fills=tuple(fills),
            )
        except Exception as exc:
            # The swaps were sent; the decision that settles them was not written.
            raise self._stopped(time, exc) from exc

    @staticmethod
    def _require_holds(wallet: Wallet, expected: Ledger, when: str) -> None:
        held = wallet.holdings()
        if held != expected:
            raise EngineError(
                f"{when}: the wallet holds {holdings_text(held)}, and the run's ledger says "
                f"{holdings_text(expected)}"
            )

    def _stopped(
        self, time: int, exc: Exception, *, unwritten: object | None = None
    ) -> UnsettledSend:
        """Write what stopped the send at ``time``, and the error that stops the run.

        A send error says which transactions it concerns and what gas they
        cost; it is read here by those names, from it or the errors it was
        raised from, since the engine does not know the chain's errors. An
        error that names no transaction cost none: nothing of its swap was
        sent, and the legs that filled carry their own gas. One that names
        some and no usable cost leaves the gas unknown, and so does an answer
        of the executor's, ``unwritten``, that was given and not written as
        a leg: a swap of it may have been mined.
        """
        failure = f"{type(exc).__name__}: {exc}"
        hashes = _hashes(exc)
        if hashes:
            failure += f" (transactions {', '.join(hashes)})"
        if unwritten is not None:
            failure += f" (the executor's answer was not written as a leg: {unwritten!r})"
        gas: Decimal | None = Decimal(0) if not hashes and unwritten is None else None
        for error in _chain(exc):
            cost = getattr(error, "gas_cost_eth", None)
            if isinstance(cost, Decimal) and cost.is_finite() and not cost.is_signed():
                gas = cost
                break
        try:
            self.journal.fail_send(self.run_id, time, failure=failure, gas_eth=gas)
        except Exception as failed:
            # A store that failed the step may fail this too; the send stays open either way.
            unwritten = f"; what stopped it could not be written ({failed})"
        else:
            unwritten = ""
        return UnsettledSend(
            f"the swaps of the bar at {time} were being sent when the step stopped "
            f"({failure}){unwritten}; the send is left open, and the run goes no further until "
            f"what the wallet holds is reconciled with what was written"
        )

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
            decided_at=self.decided_at,
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
    fork_block: int | None = None,
) -> RunRecord:
    """Start a run under ``config`` with ``ledger`` as its opening balances, filled from ``fills``.

    The balances must name exactly the configured tokens, at zero for one
    the run starts without, though not all at zero: a run that holds nothing
    can never trade. No balance has more decimal places than its token, nor
    the gas balance more than ETH. The config's snapshot is kept with the run,
    and a fork run keeps ``fork_block``, the block its fork was at, which no
    other run has. A config whose strategy cannot be built starts no run: the
    run could never be opened, and its id would be taken. Nor does a live run:
    trading with real funds is not built.
    """
    if mode is RunMode.LIVE:
        raise EngineError("a live run is not started: trading with real funds is not built yet")
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
            fork_block=fork_block,
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
    fork_block: int | None = None,
) -> None:
    """Start the run ``run_id`` in ``mode`` with ``opening``, or check that it can be carried on.

    A run that is not there needs ``opening``, and is started with ``fills``
    as where its fills come from (and, a fork run, with ``fork_block`` as
    the block its fork was at). One that is there must be of ``mode``,
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
            fork_block=fork_block,
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
    journal: Journal,
    config: UniswapConfig,
    executor: Executor,
    *,
    run_id: str,
    now: int,
    wallet: Wallet | None = None,
) -> Engine:
    """The engine of the run ``run_id``, with the config's strategy built, for a call at ``now``.

    A run is continued only under the config it was started with, and by an
    executor whose fills come from where the run's do: a config whose
    snapshot differs is refused, an executor of another source is, and so
    is a run that is not there. An executor whose fills come from the chain
    comes with the ``wallet`` it signs from, and no other executor does. A
    run with an open send is refused with :class:`UnsettledSend`.
    A strategy the registry does not know, or whose params it refuses, is a
    :class:`~..config.ConfigError`.
    """
    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise EngineError(f"now must be a non-negative integer of seconds, got {now!r}")
    run = journal.run(run_id)
    if run is None:
        raise EngineError(f"there is no run {run_id!r}")
    opened = journal.open_send(run_id)
    if opened is not None:
        raise _unsettled(run_id, opened)
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
        decided_at=now,
        wallet=wallet,
    )
