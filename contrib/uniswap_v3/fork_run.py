"""A fork run: the store's bars replayed through the engine, each bar's swaps signed on a local fork.

The bars come from the store, as a backtest's do, and the range is replayed
by the same :func:`~.engine.backtest.replay`. What differs is the executor
and its wallet: for each bar that trades, the fork is reset to the bar's
fill block and the wallet is given the run's balances
(:class:`~.chain.wallet.ForkWallet`), and the swaps are signed and sent
there (:class:`~.chain.swaps.ChainExecutor`). The engine checks the wallet
against the run's ledger before and after them, and keeps each swap that
fills as it fills (:mod:`.engine.step`). Fills are therefore what the
chain paid, at the block a paper run and a quoted backtest price the same
bar at.

A run whose step stopped while it was sending is left with an open send,
and goes no further: :func:`reconcile_open_send` sets what was written of
it beside what the wallet holds, for whoever has to settle it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import UniswapConfig
from .domain.decimal_context import EXACT_CONTEXT
from .domain.ledger import Ledger, LedgerError
from .domain.records import FillRecord, OpenSend
from .domain.types import Fill, RunMode, SwapIntent
from .engine.backtest import BacktestSummary, replay
from .engine.step import EngineError
from .ports import Executor, Wallet
from .store.repository import Store

__all__ = ["Reconciliation", "reconcile_open_send", "run_fork"]


def run_fork(
    store: Store,
    config: UniswapConfig,
    executor: Executor,
    wallet: Wallet,
    *,
    run_id: str,
    start: int,
    end: int | None = None,
    opening: Ledger | None = None,
    now: int,
    fork_block: int | None,
) -> BacktestSummary:
    """Decide every stored bar from ``start`` to ``end`` for the fork run ``run_id``.

    ``executor`` signs from ``wallet``; ``fork_block`` is the block the fork
    is at, kept by a run started here, which needs it (a run carried on
    does not). The rest is
    :func:`~.engine.backtest.replay`'s: an executor that does not sign
    fills no fork run. A run with an open send raises
    :class:`~.engine.step.UnsettledSend` before anything is sent.
    """
    return replay(
        store,
        config,
        executor,
        run_id=run_id,
        mode=RunMode.FORK,
        start=start,
        end=end,
        opening=opening,
        now=now,
        wallet=wallet,
        fork_block=fork_block,
    )


@dataclass(frozen=True)
class Reconciliation:
    """An open send beside what the wallet holds.

    ``before`` is the run's ledger before the send's bar, and ``expected``
    that ledger with the legs written applied and the gas of the failed
    swap, when it is known (``gas_known``), taken off. ``held`` is what the
    wallet holds now.
    """

    send: OpenSend
    before: Ledger
    expected: Ledger
    held: Ledger

    @property
    def gas_known(self) -> bool:
        """Whether what the failed swap's transactions cost is known."""
        return self.send.failed_gas_eth is not None

    @property
    def agrees(self) -> bool:
        """Whether the wallet holds what was written: each token, and ETH when its gas is known."""
        if self.held.balances != self.expected.balances:
            return False
        return not self.gas_known or self.held.gas_eth == self.expected.gas_eth


def _fill(config: UniswapConfig, record: FillRecord) -> Fill:
    """``record`` as the fill it was written from, its symbols and pools resolved by ``config``."""
    tokens = {token.symbol: token for token in config.tokens}
    pools = {pool.address: pool for pool in config.pools}
    try:
        swap = SwapIntent(
            token_in=tokens[record.token_in],
            route=tuple(pools[address] for address in record.route),
            amount_in=record.amount_in,
            min_amount_out=record.min_amount_out,
        )
        return Fill(
            swap=swap,
            amount_out=record.amount_out,
            gas_cost_eth=record.gas_cost_eth,
            block=record.block,
        )
    except (KeyError, ValueError) as exc:
        raise EngineError(
            f"leg {record.leg} of the send at {record.time} is not a swap of the run's tokens "
            f"and pools ({exc!r})"
        ) from exc


def reconcile_open_send(
    store: Store, config: UniswapConfig, wallet: Wallet, run_id: str
) -> Reconciliation | None:
    """The run's open send beside what ``wallet`` holds now, or ``None`` when no send is open.

    The wallet is read as it stands: on a fork reset or restarted since the
    send, it no longer shows what the send did.
    """
    send = store.open_send(run_id)
    if send is None:
        return None
    before = store.ledger(run_id)
    try:
        applied = before.apply([_fill(config, leg) for leg in send.legs])
        failed_gas = send.failed_gas_eth
        expected = (
            applied
            if failed_gas is None
            else Ledger(
                balances=applied.balances,
                gas_eth=EXACT_CONTEXT.subtract(applied.gas_eth, failed_gas),
            )
        )
    except (LedgerError, ValueError) as exc:
        raise EngineError(
            f"the legs written for the send of run {run_id!r} at {send.time} do not apply "
            f"to its ledger ({exc})"
        ) from exc
    return Reconciliation(send=send, before=before, expected=expected, held=wallet.holdings())
