"""Stand-ins for the engine tests: a config, bars, and a scripted strategy and executor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from contrib.uniswap_v3.config import StrategySpec, UniswapConfig
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.execution import ExecutionSettings
from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import FillSource
from contrib.uniswap_v3.domain.types import (
    Bar,
    Fill,
    Hold,
    MarketView,
    Pool,
    Portfolio,
    Rejection,
    SwapIntent,
    TargetWeights,
    Token,
)

__all__ = [
    "DAY",
    "FIRST_DAY",
    "GWEI",
    "PRICES",
    "USDC",
    "USDC_WETH",
    "WBTC",
    "WBTC_WETH",
    "WETH",
    "FixedGas",
    "ScriptedExecutor",
    "ScriptedQuoter",
    "ScriptedStrategy",
    "bar",
    "config",
    "ledger",
    "weights",
]

D = Decimal
DAY = 86_400
# 2024-01-01 00:00:00 UTC.
FIRST_DAY = 1_704_067_200
GWEI = 10**9
# What the tests price the two tokens at unless they say otherwise.
PRICES: Mapping[str, Decimal] = MappingProxyType({"WETH": D("2000"), "WBTC": D("40000")})

USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]


def config(**execution: Any) -> UniswapConfig:
    """The three tokens and two pools, quoted in USDC, with ``execution`` over the defaults."""
    return UniswapConfig(
        chain_id=ETHEREUM_MAINNET,
        quote=USDC,
        tokens=(USDC, WETH, WBTC),
        pools=(USDC_WETH, WBTC_WETH),
        strategy=StrategySpec(
            name="fixed_weights",
            params={"weights": {"USDC": "0.5", "WETH": "0.3", "WBTC": "0.2"}, "band": "0.05"},
        ),
        execution=ExecutionSettings(**execution),
    )


def bar(
    day: int = 0,
    *,
    weth: str = "2000",
    wbtc: str = "40000",
    base_fee_gwei: int = 10,
    suspect: bool = False,
) -> Bar:
    """The bar ``day`` days after :data:`FIRST_DAY`, closing on block ``1000 * (day + 1)``."""
    return Bar(
        time=FIRST_DAY + day * DAY,
        close_block=1_000 * (day + 1),
        prices={"WETH": D(weth), "WBTC": D(wbtc)},
        base_fee_wei=base_fee_gwei * GWEI,
        suspect=suspect,
    )


def ledger(usdc: str = "10000", weth: str = "0", wbtc: str = "0", gas: str = "1") -> Ledger:
    """Balances of the three tokens, and the gas balance."""
    return Ledger(balances={"USDC": D(usdc), "WETH": D(weth), "WBTC": D(wbtc)}, gas_eth=D(gas))


def weights(usdc: str, weth: str, wbtc: str) -> TargetWeights:
    return TargetWeights({"USDC": D(usdc), "WETH": D(weth), "WBTC": D(wbtc)})


class ScriptedStrategy:
    """Answers each bar with what the script holds for its time, and ``Hold`` for any other.

    An answer that is an exception is raised. ``calls`` keeps the view and
    the portfolio of every call.
    """

    def __init__(self, script: Mapping[int, object]) -> None:
        self.script = dict(script)
        self.calls: list[tuple[MarketView, Portfolio]] = []

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        self.calls.append((view, portfolio))
        answer = self.script.get(view.latest.time, Hold())
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]


class ScriptedExecutor:
    """Answers each swap with what ``answer`` returns for it; ``swaps`` keeps every one asked."""

    source = FillSource.MODEL

    def __init__(self, answer: Callable[[SwapIntent, Bar], Fill | Rejection]) -> None:
        self._answer = answer
        self.swaps: list[SwapIntent] = []

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        self.swaps.append(swap)
        return self._answer(swap, bar)


class ScriptedQuoter:
    """Answers each quote with what ``answer`` returns, or raises it; ``asked`` keeps every one."""

    def __init__(self, answer: Callable[[Token, tuple[Pool, ...], Decimal, int], object]) -> None:
        self._answer = answer
        self.asked: list[tuple[Token, tuple[Pool, ...], Decimal, int]] = []

    def quote(
        self, token_in: Token, route: Any, amount_in: Decimal, *, block: int
    ) -> tuple[Decimal, int]:
        self.asked.append((token_in, tuple(route), amount_in, block))
        answer = self._answer(token_in, tuple(route), amount_in, block)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]


class FixedGas:
    """A gas oracle with one base fee for every block; ``blocks`` keeps each one asked about."""

    def __init__(self, base_fee_wei: int) -> None:
        self._base_fee_wei = base_fee_wei
        self.blocks: list[int] = []

    def base_fee_wei(self, block: int) -> int:
        self.blocks.append(block)
        return self._base_fee_wei
