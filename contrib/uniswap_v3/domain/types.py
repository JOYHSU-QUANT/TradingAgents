"""The value types the engine, the strategies and the adapters exchange.

All of them are frozen and check themselves on construction, so an invalid
value is refused where it is built rather than where it is used. Token
amounts are whole-token ``Decimal`` values (``1.5`` WETH, not wei); an
adapter that speaks to a chain converts at its own edge with
:attr:`Token.decimals`. Prices and values are in the portfolio's quote token.
A mapping handed to a constructor is copied and exposed read-only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from .decimal_context import DECIMAL_CONTEXT

__all__ = [
    "Bar",
    "Fill",
    "Hold",
    "MarketView",
    "Pool",
    "Portfolio",
    "Rejection",
    "RunMode",
    "SwapIntent",
    "TargetWeights",
    "Token",
]

_ADDRESS: Final = re.compile(r"0x[0-9a-fA-F]{40}")
# ERC-20 ``decimals()`` is a uint8.
_MAX_DECIMALS: Final = 255
# A v3 fee is in hundredths of a basis point; 1_000_000 would be 100%.
_FEE_DENOMINATOR: Final = 1_000_000
# How many digits from the decimal point an amount's leading digit may sit. A
# uint256 has 78 digits, so no token amount, price or weight is outside it,
# and arithmetic on two checked values stays inside the context's exponents.
_MAX_MAGNITUDE: Final = 77


class RunMode(str, Enum):
    """Where bars and fills come from; the engine step is the same in all four."""

    BACKTEST = "backtest"
    PAPER = "paper"
    FORK = "fork"
    LIVE = "live"


def _require_symbol(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")


def _require_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{what} must be a non-negative integer, got {value!r}")


def _require_address(value: object, what: str) -> None:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ValueError(f"{what} must be 0x followed by 40 hex digits, got {value!r}")


def _require_amount(value: object, what: str, *, positive: bool = False) -> None:
    """A finite ``Decimal`` that is not negative, and not zero when ``positive``.

    A negative zero is refused with the negatives, and so is a value whose
    magnitude no amount on a chain can have.
    """
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value.is_signed()
        or (positive and value == 0)
        or abs(value.adjusted()) > _MAX_MAGNITUDE
    ):
        bound = "positive" if positive else "non-negative"
        raise ValueError(
            f"{what} must be a finite, {bound} Decimal between 1e-{_MAX_MAGNITUDE} and "
            f"1e{_MAX_MAGNITUDE}, got {value!r}"
        )


def _frozen_amounts(
    values: object, what: str, *, positive: bool = False
) -> Mapping[str, Decimal]:
    """A read-only copy of a symbol -> amount mapping, every entry checked."""
    if not isinstance(values, Mapping):
        raise ValueError(f"{what} must be a mapping of token symbol to Decimal, got {values!r}")
    for symbol, amount in values.items():
        _require_symbol(symbol, f"a {what} key")
        _require_amount(amount, f"{what}[{symbol!r}]", positive=positive)
    return MappingProxyType(dict(values))


def _sum(amounts: Mapping[str, Decimal]) -> Decimal:
    total = Decimal(0)
    for amount in amounts.values():
        total = DECIMAL_CONTEXT.add(total, amount)
    return total


@dataclass(frozen=True)
class Token:
    """An ERC-20 token on one chain."""

    symbol: str
    address: str
    decimals: int

    def __post_init__(self) -> None:
        _require_symbol(self.symbol, "symbol")
        _require_address(self.address, f"{self.symbol} address")
        _require_int(self.decimals, f"{self.symbol} decimals")
        if self.decimals > _MAX_DECIMALS:
            raise ValueError(f"{self.symbol} decimals must fit a uint8, got {self.decimals}")


@dataclass(frozen=True)
class Pool:
    """A Uniswap v3 pool: two tokens in the pool's own order, and a fee tier.

    ``token0`` is the token with the numerically smaller address, the order
    the factory creates every pool in and the one ``sqrtPriceX96`` and ticks
    are quoted in. A pool declared the other way round is refused: it would
    read every price inverted.
    """

    address: str
    token0: Token
    token1: Token
    fee: int

    def __post_init__(self) -> None:
        _require_address(self.address, "pool address")
        for token in (self.token0, self.token1):
            if not isinstance(token, Token):
                raise ValueError(f"a pool's tokens must be Token values, got {token!r}")
        if int(self.token0.address, 16) >= int(self.token1.address, 16):
            raise ValueError(
                f"token0 must have the smaller address: {self.token0.symbol} "
                f"({self.token0.address}) is not below {self.token1.symbol} ({self.token1.address})"
            )
        _require_int(self.fee, "pool fee")
        if not 0 < self.fee < _FEE_DENOMINATOR:
            raise ValueError(
                f"pool fee is in hundredths of a basis point, between 1 and "
                f"{_FEE_DENOMINATOR - 1}, got {self.fee}"
            )

    @property
    def fee_rate(self) -> Decimal:
        """The fee as a fraction of the amount in: ``Decimal("0.0005")`` for the 500 tier."""
        return DECIMAL_CONTEXT.divide(Decimal(self.fee), Decimal(_FEE_DENOMINATOR))


@dataclass(frozen=True)
class TargetWeights:
    """The share of portfolio value each token should hold.

    No weight is negative and they sum to exactly 1. Whether the symbols are
    the configured ones is the engine's check, since it is the engine that
    knows the configuration.
    """

    weights: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        weights = _frozen_amounts(self.weights, "weights")
        if not weights:
            raise ValueError("weights must name at least one token")
        # Summed as fractions: the decimal context would round a sum of more
        # than 28 digits before it was compared.
        if sum(Fraction(weight) for weight in weights.values()) != 1:
            raise ValueError(f"weights must sum to exactly 1, got about {_sum(weights)}")
        object.__setattr__(self, "weights", weights)


@dataclass(frozen=True)
class Hold:
    """A strategy's answer when the portfolio should be left as it is."""


@dataclass(frozen=True)
class Bar:
    """What the engine knows at one bar boundary.

    ``time`` is the boundary in epoch seconds (UTC). ``close_block`` is the
    last block before it, and ``prices`` are the pool prices at the end of
    that block: the quote-token price of one whole token, for every token
    but the quote itself. ``base_fee_wei`` is that block's base fee. A
    ``suspect`` bar failed a data check and is not to be traded on.
    """

    time: int
    close_block: int
    prices: Mapping[str, Decimal]
    base_fee_wei: int
    suspect: bool = False

    def __post_init__(self) -> None:
        _require_int(self.time, "time")
        _require_int(self.close_block, "close_block")
        _require_int(self.base_fee_wei, "base_fee_wei")
        prices = _frozen_amounts(self.prices, "prices", positive=True)
        if not prices:
            raise ValueError("prices must hold at least one token")
        if not isinstance(self.suspect, bool):
            raise ValueError(f"suspect must be a bool, got {self.suspect!r}")
        object.__setattr__(self, "prices", prices)


@dataclass(frozen=True)
class MarketView:
    """The bars a strategy may see: oldest first, ending at the bar being decided.

    Whoever builds the view cuts it at the bar being decided; a strategy is
    handed nothing else, which is what keeps it from looking ahead. The type
    itself holds only the order: times strictly increase, and a later bar
    does not close on an earlier block.
    """

    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.bars, tuple) or not self.bars:
            raise ValueError("a market view holds a non-empty tuple of bars")
        for bar in self.bars:
            if not isinstance(bar, Bar):
                raise ValueError(f"a market view holds Bar values, got {bar!r}")
        for earlier, later in zip(self.bars, self.bars[1:], strict=False):
            if later.time <= earlier.time:
                raise ValueError(
                    f"bars must be strictly increasing in time: {later.time} follows {earlier.time}"
                )
            if later.close_block < earlier.close_block:
                raise ValueError(
                    f"a later bar cannot close on an earlier block: {later.close_block} "
                    f"follows {earlier.close_block}"
                )

    @property
    def latest(self) -> Bar:
        """The bar being decided."""
        return self.bars[-1]


@dataclass(frozen=True)
class Portfolio:
    """Token balances and the prices they are valued at.

    ``balances`` holds every token the portfolio may hold, the quote token
    included, at zero when it holds none. ``prices`` holds the quote-token
    price of one whole token for every one of them but the quote itself.
    """

    quote: str
    balances: Mapping[str, Decimal]
    prices: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        _require_symbol(self.quote, "quote")
        balances = _frozen_amounts(self.balances, "balances")
        prices = _frozen_amounts(self.prices, "prices", positive=True)
        if self.quote not in balances:
            raise ValueError(f"balances must include the quote token {self.quote!r}")
        priced = set(balances) - {self.quote}
        if set(prices) != priced:
            raise ValueError(
                f"prices must cover exactly the non-quote tokens {sorted(priced)}, "
                f"got {sorted(prices)}"
            )
        object.__setattr__(self, "balances", balances)
        object.__setattr__(self, "prices", prices)

    def value_of(self, symbol: str) -> Decimal:
        """The quote-token value of the balance held in ``symbol``."""
        if symbol not in self.balances:
            raise ValueError(f"the portfolio does not hold {symbol!r}: {sorted(self.balances)}")
        balance = self.balances[symbol]
        if symbol == self.quote:
            return balance
        return DECIMAL_CONTEXT.multiply(balance, self.prices[symbol])

    @property
    def total_value(self) -> Decimal:
        """The quote-token value of every balance together."""
        total = Decimal(0)
        for symbol in self.balances:
            total = DECIMAL_CONTEXT.add(total, self.value_of(symbol))
        return total


@dataclass(frozen=True)
class SwapIntent:
    """One exact-input swap the engine wants: sell ``amount_in`` of one token for another."""

    token_in: str
    token_out: str
    amount_in: Decimal

    def __post_init__(self) -> None:
        _require_symbol(self.token_in, "token_in")
        _require_symbol(self.token_out, "token_out")
        if self.token_in == self.token_out:
            raise ValueError(f"a swap needs two different tokens, got {self.token_in!r} twice")
        _require_amount(self.amount_in, "amount_in", positive=True)


@dataclass(frozen=True)
class Fill:
    """A swap that went through: what came out, what the gas cost, and in which block."""

    swap: SwapIntent
    amount_out: Decimal
    gas_cost_eth: Decimal
    block: int

    def __post_init__(self) -> None:
        if not isinstance(self.swap, SwapIntent):
            raise ValueError(f"a fill names the SwapIntent it filled, got {self.swap!r}")
        _require_amount(self.amount_out, "amount_out", positive=True)
        _require_amount(self.gas_cost_eth, "gas_cost_eth")
        _require_int(self.block, "block")


@dataclass(frozen=True)
class Rejection:
    """A swap that did not go through, and why."""

    swap: SwapIntent
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.swap, SwapIntent):
            raise ValueError(f"a rejection names the SwapIntent it refused, got {self.swap!r}")
        _require_symbol(self.reason, "reason")
