"""The engine's step with an executor that signs: the wallet checked, each leg kept, a partial rebalance recorded."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import FillSource, Outcome, RejectionCode
from contrib.uniswap_v3.domain.types import Bar, Fill, MarketView, Rejection, RunMode, SwapIntent
from contrib.uniswap_v3.engine.step import (
    Engine,
    EngineError,
    UnsettledSend,
    open_engine,
    start_run,
)
from contrib.uniswap_v3.ports import Wallet
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    ScriptedExecutor,
    ScriptedStrategy,
    bar,
    config as _config,
    ledger as _ledger,
    weights,
)

D = Decimal
_RUN = "fork-1"
_CONFIG = _config()
_TARGET = weights("0.5", "0.3", "0.2")
_GAS = D("0.001")


class _Wallet:
    """A wallet that holds what it was last prepared with, moved by every fill the executor makes."""

    def __init__(self) -> None:
        self.held: Ledger | None = None
        self.prepared: list[tuple[int, Ledger]] = []

    def prepare(self, bar: Bar, ledger: Ledger) -> None:
        self.prepared.append((bar.time, ledger))
        self.held = ledger

    def holdings(self) -> Ledger:
        assert self.held is not None, "read before it was prepared"
        return self.held


class _SendFailed(Exception):
    """Shaped as the chain's send errors are: the transactions, and what their gas cost."""

    def __init__(self) -> None:
        super().__init__("the swap was mined and reverted")
        self.tx_hashes = ("0x" + "aa" * 32, "0x" + "bb" * 32)
        self.gas_cost_eth = D("0.0004")


class _Signer:
    """Signs every swap from ``wallet``; ``answer`` may refuse leg ``n``, or raise on it."""

    source = FillSource.CHAIN

    def __init__(
        self, wallet: _Wallet, answer: Callable[[int, SwapIntent], object] | None = None
    ) -> None:
        self._wallet = wallet
        self._answer = answer or (lambda leg, swap: None)
        self.swaps: list[SwapIntent] = []

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        leg = len(self.swaps)
        self.swaps.append(swap)
        answer = self._answer(leg, swap)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, Rejection | Fill):
            return answer
        fill = Fill(swap, swap.min_amount_out, _GAS, bar.close_block + 2 + leg)
        self._wallet.held = self._wallet.holdings().apply([fill])
        return fill


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        start_run(
            opened,
            _CONFIG,
            run_id=_RUN,
            mode=RunMode.FORK,
            ledger=_ledger(),
            created_at=FIRST_DAY,
            fills=FillSource.CHAIN,
            fork_block=900,
        )
        yield opened


def _engine(store, executor, wallet, script=None) -> Engine:
    return Engine(
        run_id=_RUN,
        config=_CONFIG,
        strategy=ScriptedStrategy({FIRST_DAY: _TARGET} if script is None else script),
        executor=executor,
        journal=store,
        decided_at=FIRST_DAY + 600,
        wallet=wallet,
    )


def _view(*days: int) -> MarketView:
    return MarketView(tuple(bar(day) for day in days))


def test_a_virtual_executors_refusal_for_want_of_gas_is_recorded_as_gas(tmp_path):
    with open_store(tmp_path / "virtual.db") as store:
        start_run(store, _CONFIG, run_id="bt", mode=RunMode.BACKTEST, ledger=_ledger(), created_at=0)
        executor = ScriptedExecutor(lambda swap, bar: Rejection(swap, "no ETH", short_of_gas=True))
        engine = Engine(
            run_id="bt",
            config=_CONFIG,
            strategy=ScriptedStrategy({FIRST_DAY: _TARGET}),
            executor=executor,
            journal=store,
            decided_at=0,
        )
        assert engine.step(_view(0)).decision.reason_code is RejectionCode.GAS


def test_a_fake_wallet_is_a_wallet():
    assert isinstance(_Wallet(), Wallet)


def test_every_leg_filled_is_kept_as_it_fills_and_the_decision_settles_the_send(store):
    wallet = _Wallet()
    signer = _Signer(wallet)
    result = _engine(store, signer, wallet).step(_view(0))
    assert result.decision.outcome is Outcome.FILLED
    assert [time for time, _ in wallet.prepared] == [FIRST_DAY]
    assert wallet.prepared[0][1] == _ledger()
    fills = store.fills(_RUN)
    assert len(fills) == 2 == len(signer.swaps)
    assert store.open_send(_RUN) is None
    assert store.ledger(_RUN) == wallet.held


def test_a_leg_refused_after_one_filled_leaves_the_rebalance_partial_and_the_next_bar_starts_there(
    store,
):
    wallet = _Wallet()

    def refuse_the_second(leg, swap):
        return Rejection(swap, "the quote is below the minimum; nothing was sent") if leg else None

    signer = _Signer(wallet, refuse_the_second)
    decision = _engine(store, signer, wallet).step(_view(0)).decision
    assert decision.outcome is Outcome.PARTIAL
    assert decision.reason_code is RejectionCode.EXECUTOR
    assert decision.reason == (
        "leg 1 (USDC to WBTC) was refused: the quote is below the minimum; nothing was sent; "
        "the 1 leg(s) before it filled, and stand"
    )
    (leg,) = store.fills(_RUN)
    assert (leg.token_in, leg.token_out) == ("USDC", "WETH")
    partial = store.ledger(_RUN)
    assert partial == wallet.held and partial.balances["WETH"] > 0
    assert partial.balances["WBTC"] == 0

    # The strategy decides the next bar from where the partial rebalance left the run.
    strategy = ScriptedStrategy({FIRST_DAY + DAY: _TARGET})
    later = Engine(
        run_id=_RUN,
        config=_CONFIG,
        strategy=strategy,
        executor=_Signer(wallet),
        journal=store,
        decided_at=FIRST_DAY + DAY,
        wallet=wallet,
    )
    assert later.step(_view(0, 1)).decision.outcome is Outcome.FILLED
    ((_, portfolio),) = strategy.calls
    assert dict(portfolio.balances) == dict(partial.balances)
    assert wallet.prepared[-1] == (FIRST_DAY + DAY, partial)


def test_the_first_leg_refused_rejects_the_rebalance_and_settles_the_send(store):
    wallet = _Wallet()
    signer = _Signer(wallet, lambda leg, swap: Rejection(swap, "no answer"))
    decision = _engine(store, signer, wallet).step(_view(0)).decision
    assert decision.outcome is Outcome.REJECTED
    assert decision.reason == "leg 0 (USDC to WETH) was refused: no answer"
    assert len(signer.swaps) == 1
    assert store.ledger(_RUN) == _ledger() == wallet.held
    assert store.open_send(_RUN) is None


def test_a_wallet_that_does_not_hold_the_ledger_stops_the_step_with_nothing_sent(store):
    class Short(_Wallet):
        def prepare(self, bar, ledger):
            super().prepare(bar, ledger)
            self.held = _ledger(usdc="9999")

    wallet = Short()
    signer = _Signer(wallet)
    with pytest.raises(EngineError, match="before the swaps of the bar at .*, nothing sent: the "
                       "wallet holds USDC 9999, WBTC 0, WETH 0; gas ETH 1, and the run's ledger "
                       "says USDC 10000"):
        _engine(store, signer, wallet).step(_view(0))
    assert signer.swaps == []
    assert store.open_send(_RUN) is None
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_bar_that_trades_nothing_leaves_the_wallet_alone(store):
    wallet = _Wallet()
    decision = _engine(store, _Signer(wallet), wallet, script={}).step(_view(0)).decision
    assert decision.outcome is Outcome.HOLD
    assert wallet.prepared == []


def test_a_leg_that_fails_after_it_may_have_been_sent_leaves_the_send_open_and_says_why(store):
    wallet = _Wallet()
    signer = _Signer(wallet, lambda leg, swap: _SendFailed() if leg else None)
    with pytest.raises(UnsettledSend, match="the send is left open, and the run goes no further"):
        _engine(store, signer, wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert [leg.leg for leg in opened.legs] == [0]
    assert opened.failure == (
        f"_SendFailed: the swap was mined and reverted (transactions 0x{'aa' * 32}, "
        f"0x{'bb' * 32})"
    )
    assert opened.failed_gas_eth == D("0.0004")
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_wallet_that_does_not_hold_the_fills_afterwards_leaves_the_send_open(store):
    wallet = _Wallet()

    def skim(leg, swap):
        if leg == 1:
            # Something took a little of the wallet's USDC beside the swaps.
            held = wallet.holdings()
            wallet.held = Ledger(
                balances={**held.balances, "USDC": held.balances["USDC"] - 1},
                gas_eth=held.gas_eth,
            )
        return None

    with pytest.raises(UnsettledSend):
        _engine(store, _Signer(wallet, skim), wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert len(opened.legs) == 2
    assert opened.failure.startswith("EngineError: after the swaps of the bar at")
    # The error names no transaction: nothing it stopped cost gas, and ETH is compared.
    assert opened.failed_gas_eth == D(0)


def test_an_answer_for_another_swap_leaves_the_send_open(store):
    wallet = _Wallet()
    other = SwapIntent(
        token_in=_CONFIG.tokens[1], route=(_CONFIG.pools[0],), amount_in=D(1), min_amount_out=D(1)
    )
    signer = _Signer(wallet, lambda leg, swap: Rejection(other, "not this one"))
    with pytest.raises(UnsettledSend):
        _engine(store, signer, wallet).step(_view(0))
    assert "the executor answered leg 0" in store.open_send(_RUN).failure


class _ReadFailed(Exception):
    """A failure of the executor's that names no transaction: by the port's contract, nothing was sent."""


def test_a_first_swap_that_fails_before_anything_is_sent_leaves_the_bar_to_be_tried_again(store):
    wallet = _Wallet()
    failing = _Signer(wallet, lambda leg, swap: _ReadFailed("the quote timed out"))
    with pytest.raises(_ReadFailed, match="the quote timed out"):
        _engine(store, failing, wallet).step(_view(0))
    assert store.open_send(_RUN) is None
    assert store.decision(_RUN, FIRST_DAY) is None
    # Tried again, the bar is decided.
    assert _engine(store, _Signer(wallet), wallet).step(_view(0)).decision.outcome is Outcome.FILLED


def test_a_first_swap_the_executor_cannot_make_is_an_engine_error_and_leaves_no_send(store):
    wallet = _Wallet()
    signer = _Signer(wallet, lambda leg, swap: ValueError("the wallet holds less"))
    with pytest.raises(EngineError, match="the executor failed on leg 0"):
        _engine(store, signer, wallet).step(_view(0))
    assert store.open_send(_RUN) is None


def test_a_first_swap_that_may_have_been_sent_leaves_the_send_open(store):
    wallet = _Wallet()
    with pytest.raises(UnsettledSend):
        _engine(store, _Signer(wallet, lambda leg, swap: _SendFailed()), wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert opened.legs == () and opened.failed_gas_eth == D("0.0004")


def test_a_later_swap_that_fails_with_no_transaction_named_still_leaves_the_send_open(store):
    wallet = _Wallet()
    signer = _Signer(wallet, lambda leg, swap: _ReadFailed("timed out") if leg else None)
    with pytest.raises(UnsettledSend, match="_ReadFailed: timed out"):
        _engine(store, signer, wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert len(opened.legs) == 1
    # It names no transaction: the leg it stopped sent nothing, and cost no gas.
    assert opened.failed_gas_eth == D(0)


def test_the_gas_a_wrapped_send_error_names_is_kept(store):
    wallet = _Wallet()

    def wrapped(leg, swap):
        if not leg:
            return None
        try:
            raise _SendFailed()
        except _SendFailed as exc:
            raise RuntimeError("the swap could not be made") from exc

    with pytest.raises(UnsettledSend):
        _engine(store, _Signer(wallet, wrapped), wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert "transactions 0x" in opened.failure
    assert opened.failed_gas_eth == D("0.0004")


def test_a_send_error_wrapped_twice_still_names_its_transactions(store):
    wallet = _Wallet()

    def twice(leg, swap):
        try:
            try:
                raise _SendFailed()
            except _SendFailed as exc:
                raise RuntimeError("inner") from exc
        except RuntimeError as exc:
            raise LookupError("outer") from exc

    with pytest.raises(UnsettledSend):
        _engine(store, _Signer(wallet, twice), wallet).step(_view(0))
    # Not taken for "nothing was sent": the send stays open, with its transactions.
    opened = store.open_send(_RUN)
    assert "transactions 0x" in opened.failure and opened.failed_gas_eth == D("0.0004")


def test_a_fill_the_journal_did_not_take_leaves_its_gas_unknown(store):
    wallet = _Wallet()

    class Unwriting(_Unrecording):
        def record_leg(self, run_id, time, leg, fill):
            if leg == 1:
                raise RuntimeError("database is locked")
            self._store.record_leg(run_id, time, leg, fill)

    engine = _engine(store, _Signer(wallet), wallet)
    engine = Engine(**{**engine.__dict__, "journal": Unwriting(store)})
    with pytest.raises(UnsettledSend):
        engine.step(_view(0))
    opened = store.open_send(_RUN)
    assert len(opened.legs) == 1
    assert "the executor's answer was not written as a leg: Fill(" in opened.failure
    assert opened.failed_gas_eth is None


def test_an_answer_that_is_not_one_leaves_its_gas_unknown_even_when_it_is_none(store):
    wallet = _Wallet()

    class Mute(_Signer):
        def execute(self, swap, bar):
            # It may have sent the swap: it does not say.
            return None

    with pytest.raises(UnsettledSend, match="the executor answered leg 0 with None"):
        _engine(store, Mute(wallet), wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert "the executor's answer was not written as a leg: None" in opened.failure
    assert opened.failed_gas_eth is None


def test_a_swap_the_wallets_eth_cannot_pay_for_is_a_want_of_gas(store):
    wallet = _Wallet()

    def short(leg, swap):
        return Rejection(swap, "cannot be paid for", short_of_gas=True) if leg else None

    decision = _engine(store, _Signer(wallet, short), wallet).step(_view(0)).decision
    assert (decision.outcome, decision.reason_code) == (Outcome.PARTIAL, RejectionCode.GAS)


def test_a_send_that_cannot_be_taken_back_stays_open_saying_nothing_was_sent(store):
    wallet = _Wallet()

    class Unabandoning(_Unrecording):
        def record(self, run_id, step):
            return self._store.record(run_id, step)

        def abandon_send(self, run_id, time):
            raise RuntimeError("database is locked")

    engine = _engine(store, _Signer(wallet, lambda leg, swap: _ReadFailed("timed out")), wallet)
    engine = Engine(**{**engine.__dict__, "journal": Unabandoning(store)})
    with pytest.raises(UnsettledSend, match="_ReadFailed: timed out"):
        engine.step(_view(0))
    opened = store.open_send(_RUN)
    assert opened.legs == () and opened.failed_gas_eth == D(0)


class _Unrecording:
    """The store, except that a decision cannot be written, nor, with ``failing``, a failure."""

    def __init__(self, store, *, failing: bool = False) -> None:
        self._store = store
        self._failing = failing

    def __getattr__(self, name):
        return getattr(self._store, name)

    def record(self, run_id, step):
        raise RuntimeError("database is locked")

    def fail_send(self, run_id, time, *, failure, gas_eth):
        if self._failing:
            raise RuntimeError("database is locked")
        self._store.fail_send(run_id, time, failure=failure, gas_eth=gas_eth)


def _unrecorded_engine(store, wallet, **kwargs) -> Engine:
    engine = _engine(store, _Signer(wallet), wallet)
    return Engine(**{**engine.__dict__, "journal": _Unrecording(store, **kwargs)})


def test_swaps_whose_decision_cannot_be_written_leave_the_send_open_and_say_why(store):
    wallet = _Wallet()
    with pytest.raises(UnsettledSend, match="RuntimeError: database is locked"):
        _unrecorded_engine(store, wallet).step(_view(0))
    opened = store.open_send(_RUN)
    assert len(opened.legs) == 2
    assert opened.failure == "RuntimeError: database is locked"


def test_a_failure_that_cannot_be_written_either_is_said_in_the_error(store):
    wallet = _Wallet()
    with pytest.raises(UnsettledSend, match="what stopped it could not be written"):
        _unrecorded_engine(store, wallet, failing=True).step(_view(0))
    assert store.open_send(_RUN).failure is None


def test_a_run_with_an_open_send_is_not_opened_nor_stepped_and_nothing_is_sent_again(store):
    wallet = _Wallet()
    signer = _Signer(wallet, lambda leg, swap: _SendFailed() if leg else None)
    engine = _engine(store, signer, wallet)
    with pytest.raises(UnsettledSend):
        engine.step(_view(0))
    sent = len(signer.swaps)

    with pytest.raises(UnsettledSend, match="has an open send at the bar"):
        open_engine(store, _CONFIG, signer, run_id=_RUN, now=FIRST_DAY + DAY, wallet=wallet)
    # An engine already open asks before anything else, the bar's strategy included.
    with pytest.raises(UnsettledSend, match="after 1 leg"):
        engine.step(_view(0))
    with pytest.raises(UnsettledSend):
        engine.step(_view(0, 1))
    assert len(signer.swaps) == sent
    assert len(engine.strategy.calls) == 1
