"""The engine's step: one decision per bar, and fills applied together or not at all."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.config import ConfigError, StrategySpec
from contrib.uniswap_v3.domain.bars import Finality
from contrib.uniswap_v3.domain.execution import ExecutionSettings
from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import BarSeen, Outcome, RejectionCode
from contrib.uniswap_v3.domain.types import (
    Bar,
    Fill,
    Hold,
    MarketView,
    Rejection,
    RunMode,
    SwapIntent,
    TargetWeights,
)
from contrib.uniswap_v3.engine import step as step_module
from contrib.uniswap_v3.engine.executors import ModelExecutor
from contrib.uniswap_v3.engine.step import Engine, EngineError, open_engine, start_run
from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.strategies.fixed_weights import FixedWeights
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    USDC,
    USDC_WETH,
    WBTC,
    WETH,
    ScriptedExecutor,
    ScriptedStrategy,
    bar,
    config as _config,
    ledger as _ledger,
    weights,
)

D = Decimal
_RUN = "run-1"
# A model that is easy to work by hand: 0.1% off the close, 100,000 gas a hop.
_CONFIG = _config(model_slippage=D("0.001"), model_gas_units_per_hop=100_000)
_TARGET = weights("0.5", "0.3", "0.2")


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _engine(store, strategy, executor=None, *, config=_CONFIG, ledger=None) -> Engine:
    start_run(
        store,
        config,
        run_id=_RUN,
        mode=RunMode.BACKTEST,
        ledger=ledger or _ledger(),
        created_at=FIRST_DAY,
    )
    return Engine(
        run_id=_RUN,
        config=config,
        strategy=strategy,
        executor=executor or ModelExecutor("USDC", config.execution),
        journal=store,
    )


def _view(*bars: Bar) -> MarketView:
    return MarketView(tuple(bars))


def _at_minimum(swap, bar) -> Fill:
    return Fill(swap, swap.min_amount_out, D("0.001"), bar.close_block + 1)


def _balances(store) -> dict[str, Decimal]:
    return dict(store.ledger(_RUN).balances)


# --- the acceptance replay -------------------------------------------------


def test_a_scripted_replay_matches_the_balances_and_values_worked_by_hand(store):
    bars = [
        bar(0, weth="2000", wbtc="40000", base_fee_gwei=10),
        bar(1, weth="2200", wbtc="40000"),
        bar(2, weth="3000", wbtc="40000", suspect=True),
        bar(3, weth="2500", wbtc="50000", base_fee_gwei=20),
    ]
    strategy = ScriptedStrategy(
        {
            bars[0].time: _TARGET,
            # Asked for on the suspect bar, and never seen by the engine.
            bars[2].time: weights("0", "1", "0"),
            bars[3].time: weights("1", "0", "0"),
        }
    )
    engine = _engine(store, strategy)
    outcomes = [
        engine.step(_view(*bars[: index + 1])).decision.outcome for index in range(len(bars))
    ]
    assert outcomes == [Outcome.FILLED, Outcome.HOLD, Outcome.SKIPPED_SUSPECT, Outcome.FILLED]

    # Bar 0. 10,000 USDC to 50/30/20 at WETH 2,000 and WBTC 40,000:
    #   3,000 USDC -> WETH: 1.5 * 0.9995 * 0.999                  = 1.49775075
    #   2,000 USDC -> WBTC: 0.05 * 0.9995**2 * 0.999, eight places = 0.04990006
    #   gas: 100,000 * 10 gwei for one hop, twice that for two     = 0.003 ETH
    first = store.valuation(_RUN, bars[0].time)
    assert first.ledger.balances == {
        "USDC": D("5000"),
        "WETH": D("1.49775075"),
        "WBTC": D("0.04990006"),
    }
    assert first.ledger.gas_eth == D("0.997")
    # 5,000 + 1.49775075 * 2,000 + 0.04990006 * 40,000.
    assert first.total_value == D("9991.5039")

    # Bar 1. Held; only WETH's price moved: 5,000 + 1.49775075 * 2,200 + 1,996.0024.
    second = store.valuation(_RUN, bars[1].time)
    assert second.ledger == first.ledger
    assert second.total_value == D("10291.05405")

    # Bar 2. Suspect: nothing changes hands, and the valuation is at the bar's own prices.
    third = store.valuation(_RUN, bars[2].time)
    assert third.ledger == first.ledger
    assert third.total_value == D("11489.25465")
    assert [view.latest.time for view, _ in strategy.calls] == [
        bars[0].time,
        bars[1].time,
        bars[3].time,
    ]

    # Bar 3. Everything to USDC at WETH 2,500 and WBTC 50,000:
    #   1.49775075 WETH -> USDC: 3,744.376875 * 0.9995 * 0.999, six places   = 3,738.762181
    #   0.04990006 WBTC -> USDC: 2,495.003 * 0.9995**2 * 0.999, six places   = 2,490.016112
    #   gas: 100,000 * 20 gwei for one hop, twice that for two               = 0.006 ETH
    last = store.valuation(_RUN, bars[3].time)
    assert last.ledger.balances == {"USDC": D("11228.778293"), "WETH": D("0"), "WBTC": D("0")}
    assert last.ledger.gas_eth == D("0.991")
    assert last.total_value == D("11228.778293")

    fills = store.fills(_RUN)
    assert [(fill.time, fill.leg, fill.token_in, fill.token_out) for fill in fills] == [
        (bars[0].time, 0, "USDC", "WETH"),
        (bars[0].time, 1, "USDC", "WBTC"),
        (bars[3].time, 0, "WETH", "USDC"),
        (bars[3].time, 1, "WBTC", "USDC"),
    ]
    assert [fill.amount_out for fill in fills] == [
        D("1.49775075"),
        D("0.04990006"),
        D("3738.762181"),
        D("2490.016112"),
    ]
    # Each fill is dated 25 blocks after the first block of its bar's boundary.
    assert [fill.block for fill in fills] == [1_026, 1_026, 4_026, 4_026]


# --- one step --------------------------------------------------------------


def test_a_bar_already_decided_is_reported_and_nothing_is_done_again(store):
    strategy = ScriptedStrategy({FIRST_DAY: _TARGET})
    executor = ScriptedExecutor(_at_minimum)
    engine = _engine(store, strategy, executor)
    first = engine.step(_view(bar(0)))
    again = engine.step(_view(bar(0)))
    assert (first.already_run, again.already_run) == (False, True)
    assert again.decision == first.decision
    assert len(strategy.calls) == 1
    assert len(executor.swaps) == 2
    assert len(store.fills(_RUN)) == 2


def test_a_suspect_bar_is_recorded_as_skipped_without_asking_the_strategy(store):
    strategy = ScriptedStrategy({FIRST_DAY: _TARGET})
    executor = ScriptedExecutor(_at_minimum)
    result = _engine(store, strategy, executor).step(_view(bar(0, suspect=True)))
    assert result.decision.outcome is Outcome.SKIPPED_SUSPECT
    assert result.decision.suspect
    assert result.decision.target is None
    assert strategy.calls == [] and executor.swaps == []
    assert _balances(store) == {"USDC": D("10000"), "WETH": D("0"), "WBTC": D("0")}


def test_a_hold_records_the_valuation_and_trades_nothing(store):
    executor = ScriptedExecutor(_at_minimum)
    engine = _engine(store, ScriptedStrategy({}), executor, ledger=_ledger("1000", "2", "0.1"))
    result = engine.step(_view(bar(0)))
    assert result.decision.outcome is Outcome.HOLD
    assert executor.swaps == []
    assert store.valuation(_RUN, FIRST_DAY).total_value == D("9000")


def test_a_target_the_portfolio_already_meets_is_recorded_with_no_trade(store):
    executor = ScriptedExecutor(_at_minimum)
    engine = _engine(
        store, ScriptedStrategy({FIRST_DAY: _TARGET}), executor, ledger=_ledger("5000", "1.5", "0.05")
    )
    result = engine.step(_view(bar(0)))
    assert result.decision.outcome is Outcome.NO_TRADE
    assert result.decision.target == _TARGET
    assert executor.swaps == []


def test_the_strategy_sees_the_view_it_was_handed_and_the_portfolio_at_the_bars_prices(store):
    strategy = ScriptedStrategy({})
    engine = _engine(store, strategy, ledger=_ledger("1000", "2", "0.1"))
    view = _view(bar(0, weth="1000"), bar(1, weth="3000", wbtc="50000"))
    engine.step(view)
    ((seen_view, portfolio),) = strategy.calls
    assert seen_view is view
    assert portfolio.total_value == D("1000") + D("6000") + D("5000")


def test_one_refused_leg_leaves_every_balance_as_it_was(store):
    def answer(swap, bar):
        if swap.token_out == WBTC:
            return Rejection(swap, "no liquidity")
        return _at_minimum(swap, bar)

    executor = ScriptedExecutor(answer)
    # USDC is over; WETH is under by more than WBTC, so WETH is leg 0 and fills.
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}), executor)
    result = engine.step(_view(bar(0)))
    assert result.decision.outcome is Outcome.REJECTED
    assert result.decision.reason == "leg 1 (USDC to WBTC) was refused: no liquidity"
    assert result.decision.reason_code is RejectionCode.EXECUTOR
    assert result.decision.target == _TARGET
    assert store.fills(_RUN) == []
    assert store.ledger(_RUN) == _ledger()
    # The next bar starts from the unchanged balances and is decided afresh.
    executor_calls = len(executor.swaps)
    assert engine.step(_view(bar(0), bar(1))).decision.outcome is Outcome.HOLD
    assert len(executor.swaps) == executor_calls


def test_the_legs_after_a_refused_one_are_not_asked_for(store):
    executor = ScriptedExecutor(lambda swap, bar: Rejection(swap, "closed"))
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}), executor)
    assert engine.step(_view(bar(0))).decision.reason.startswith("leg 0 (USDC to WETH)")
    assert len(executor.swaps) == 1


def test_fills_whose_gas_the_gas_balance_does_not_cover_are_refused_together(store):
    # The two legs cost 0.001 and 0.002 ETH.
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}), ledger=_ledger(gas="0.0025"))
    result = engine.step(_view(bar(0)))
    assert result.decision.outcome is Outcome.REJECTED
    assert "the gas balance of 0.0015 ETH does not cover the 0.002 ETH" in result.decision.reason
    assert result.decision.reason_code is RejectionCode.GAS
    assert store.ledger(_RUN) == _ledger(gas="0.0025")
    assert store.fills(_RUN) == []


def test_what_the_store_said_of_the_bar_is_kept_with_the_decision(store):
    engine = _engine(store, ScriptedStrategy({}))
    seen = BarSeen(close_block=1_000, close_block_hash="0x" + "ab" * 32, finality=Finality.PENDING)
    engine.step(_view(bar(0)), seen=seen)
    engine.step(_view(bar(0), bar(1)))
    assert store.decision(_RUN, FIRST_DAY).seen == seen
    assert store.decision(_RUN, FIRST_DAY).close_block == 1_000
    assert store.decision(_RUN, FIRST_DAY + DAY).seen is None


def test_what_is_said_of_another_block_than_the_bars_stops_the_run(store):
    engine = _engine(store, ScriptedStrategy({}))
    other = BarSeen(close_block=999, close_block_hash="0x" + "ab" * 32, finality=Finality.FINAL)
    with pytest.raises(EngineError, match="seen describes block 999, and the bar at .* on 1000"):
        engine.step(_view(bar(0)), seen=other)
    assert store.decision(_RUN, FIRST_DAY) is None


# --- what stops the run ----------------------------------------------------


def test_a_strategy_that_raises_stops_the_run_and_leaves_the_bar_undecided(store):
    strategy = ScriptedStrategy({FIRST_DAY: RuntimeError("no data")})
    engine = _engine(store, strategy)
    with pytest.raises(RuntimeError, match="no data"):
        engine.step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None
    assert store.last_decided(_RUN) is None
    # Fixed, the same bar is decided.
    strategy.script[FIRST_DAY] = _TARGET
    assert engine.step(_view(bar(0))).decision.outcome is Outcome.FILLED


@pytest.mark.parametrize(
    ("answer", "match"),
    [
        (TargetWeights({"USDC": D("0.5"), "WETH": D("0.5")}), "the strategy targets"),
        (
            TargetWeights({"USDC": D("0.5"), "WETH": D("0.3"), "WBTC": D("0.1"), "DAI": D("0.1")}),
            "the strategy targets",
        ),
        (None, "neither Hold nor TargetWeights"),
        ({"USDC": D("1")}, "neither Hold nor TargetWeights"),
        (Hold, "neither Hold nor TargetWeights"),
    ],
)
def test_an_answer_that_is_not_hold_or_weights_over_the_configured_tokens_stops_the_run(
    store, answer, match
):
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: answer}))
    with pytest.raises(EngineError, match=match):
        engine.step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_bar_that_does_not_price_the_runs_tokens_stops_the_run(store):
    engine = _engine(store, ScriptedStrategy({}))
    unpriced = Bar(time=FIRST_DAY, close_block=1, prices={"WETH": D("2000")}, base_fee_wei=1)
    with pytest.raises(EngineError, match="does not price the run's tokens"):
        engine.step(_view(unpriced))
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_run_that_holds_other_tokens_than_the_configs_stops_at_its_first_step(store):
    engine = _engine(store, ScriptedStrategy({}))
    two_tokens = replace(_CONFIG, tokens=(USDC, WETH), pools=(USDC_WETH,))
    with pytest.raises(EngineError, match="and the config's tokens are \\['USDC', 'WETH'\\]"):
        replace(engine, config=two_tokens).step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None


def test_a_bar_before_the_latest_decided_one_stops_the_run(store):
    engine = _engine(store, ScriptedStrategy({}))
    engine.step(_view(bar(1)))
    with pytest.raises(EngineError, match="is before it"):
        engine.step(_view(bar(0)))


def test_a_plan_that_sells_more_than_the_ledger_holds_stops_the_run(store, monkeypatch):
    # No plan does; one that did would be a fault to stop on, not a refusal to record.
    oversold = SwapIntent(USDC, (USDC_WETH,), D("20000"), D("1"))
    monkeypatch.setattr(step_module, "plan_swaps", lambda *args, **kwargs: (oversold,))
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}), ScriptedExecutor(_at_minimum))
    with pytest.raises(EngineError, match="do not apply to the ledger .*holds 10000 USDC"):
        engine.step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None


def test_swaps_that_cannot_be_planned_stop_the_run(store, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("no pool path joins USDC to WBTC")

    monkeypatch.setattr(step_module, "plan_swaps", refuse)
    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}))
    with pytest.raises(EngineError, match="cannot be planned .*no pool path joins USDC to WBTC"):
        engine.step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None


@pytest.mark.parametrize("wrong", ["other swap", "not an answer"])
def test_an_executor_that_answers_for_another_swap_stops_the_run(store, wrong):
    def answer(swap, bar):
        if wrong == "not an answer":
            return None
        return _at_minimum(replace(swap, amount_in=swap.amount_in + 1), bar)

    engine = _engine(store, ScriptedStrategy({FIRST_DAY: _TARGET}), ScriptedExecutor(answer))
    with pytest.raises(EngineError, match="the executor answered leg 0"):
        engine.step(_view(bar(0)))
    assert store.decision(_RUN, FIRST_DAY) is None


# --- starting and reopening a run ------------------------------------------


def test_a_run_is_started_with_the_configs_snapshot_and_reopened_under_the_same_config(store):
    run = start_run(
        store, _CONFIG, run_id=_RUN, mode=RunMode.PAPER, ledger=_ledger(), created_at=FIRST_DAY
    )
    assert store.run(_RUN) == run
    assert (run.mode, run.chain_id, run.quote, run.strategy) == (
        RunMode.PAPER,
        1,
        "USDC",
        "fixed_weights",
    )
    engine = open_engine(store, _CONFIG, ModelExecutor("USDC", _CONFIG.execution), run_id=_RUN)
    assert isinstance(engine.strategy, FixedWeights)
    # The shipped placeholder drives the same step.
    assert engine.step(_view(bar(0))).decision.outcome is Outcome.FILLED
    assert engine.step(_view(bar(0), bar(1))).decision.outcome is Outcome.HOLD


def test_a_run_is_not_continued_under_a_changed_config(store):
    start_run(
        store, _CONFIG, run_id=_RUN, mode=RunMode.PAPER, ledger=_ledger(), created_at=FIRST_DAY
    )
    executor = ModelExecutor("USDC", _CONFIG.execution)
    changed = replace(_CONFIG, execution=ExecutionSettings(max_slippage=D("0.01")))
    with pytest.raises(EngineError, match="started under another config"):
        open_engine(store, changed, executor, run_id=_RUN)
    # Where the node's URL comes from changes no decision.
    open_engine(store, replace(_CONFIG, rpc_url_env="OTHER_RPC_URL"), executor, run_id=_RUN)
    with pytest.raises(EngineError, match="there is no run 'run-2'"):
        open_engine(store, _CONFIG, executor, run_id="run-2")


def test_a_strategy_the_registry_refuses_is_a_config_error(store):
    for spec, match in (
        (StrategySpec(name="momentum", params={}), "unknown strategy 'momentum'"),
        (StrategySpec(name="fixed_weights", params={"band": "0.05"}), "takes exactly the params"),
    ):
        config = replace(_CONFIG, strategy=spec)
        run_id = f"run-{spec.name}-{len(spec.params)}"
        start_run(
            store, config, run_id=run_id, mode=RunMode.BACKTEST, ledger=_ledger(), created_at=0
        )
        with pytest.raises(ConfigError, match=match):
            open_engine(store, config, ModelExecutor("USDC", config.execution), run_id=run_id)


def test_a_run_cannot_be_started_twice_or_over_other_tokens(store):
    start_run(
        store, _CONFIG, run_id=_RUN, mode=RunMode.BACKTEST, ledger=_ledger(), created_at=0
    )
    with pytest.raises(StoreError, match="the run 'run-1' is already stored"):
        start_run(
            store, _CONFIG, run_id=_RUN, mode=RunMode.BACKTEST, ledger=_ledger(), created_at=0
        )
    two = Ledger(balances={"USDC": D("1"), "WETH": D("0")}, gas_eth=D("0"))
    with pytest.raises(EngineError, match="the opening balances name"):
        start_run(store, _CONFIG, run_id="run-2", mode=RunMode.BACKTEST, ledger=two, created_at=0)
    with pytest.raises(EngineError, match="run_id must be a non-empty string"):
        start_run(store, _CONFIG, run_id=" ", mode=RunMode.BACKTEST, ledger=_ledger(), created_at=0)
    assert store.run("run-2") is None


@pytest.mark.parametrize(
    ("opening", "match"),
    [
        (_ledger("0", "0", "0"), "the opening balances are all zero"),
        (_ledger("10000.0000001"), "the opening USDC balance 10000.0000001 has more than 6"),
        (_ledger(wbtc="0.000000001"), "the opening WBTC balance 0.000000001 has more than 8"),
        (_ledger(gas="0.0000000000000000001"), "the opening gas ETH balance .* more than 18"),
    ],
)
def test_a_run_is_not_started_with_balances_it_could_not_trade_or_a_chain_could_not_hold(
    store, opening, match
):
    with pytest.raises(EngineError, match=match):
        start_run(
            store, _CONFIG, run_id=_RUN, mode=RunMode.BACKTEST, ledger=opening, created_at=0
        )
    assert store.run(_RUN) is None
