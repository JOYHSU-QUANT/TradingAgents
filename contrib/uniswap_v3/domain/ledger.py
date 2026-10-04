"""A run's virtual balances: the tokens it trades, and the ETH it pays gas from.

The gas balance is its own pot. It is not one of the traded tokens, takes no
part in the target weights, and is not WETH: a swap's gas is taken from it
and from nowhere else, as a wallet's is taken from its ETH. A ledger is a
value. Applying fills gives a new one, or raises and changes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from .decimal_context import EXACT_CONTEXT, MAX_MAGNITUDE, plain
from .types import Fill, Portfolio

__all__ = ["InsufficientGas", "Ledger", "LedgerError"]


class LedgerError(ValueError):
    """Fills the ledger cannot take.

    They name a token it does not hold, a balance or the gas balance does
    not cover them, or they would leave a balance no chain could carry.
    """


class InsufficientGas(LedgerError):
    """The gas balance does not cover a fill's gas.

    The one shortfall a well-planned rebalance can meet: a swap never sells
    more than its balance, but nothing plans the gas.
    """


def _require_balance(value: object, what: str) -> None:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value.is_signed()
        or abs(value.adjusted()) > MAX_MAGNITUDE
    ):
        raise ValueError(f"{what} must be a finite, non-negative Decimal, got {value!r}")


@dataclass(frozen=True)
class Ledger:
    """Whole-token balances by symbol, and the ETH set aside for gas.

    ``balances`` names every token the run may hold, at zero when it holds
    none.
    """

    balances: Mapping[str, Decimal]
    gas_eth: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.balances, Mapping) or not self.balances:
            raise ValueError(f"balances must map token symbol to amount, got {self.balances!r}")
        for symbol, amount in self.balances.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(f"a balances key must be a token symbol, got {symbol!r}")
            _require_balance(amount, f"balances[{symbol!r}]")
        _require_balance(self.gas_eth, "gas_eth")
        object.__setattr__(self, "balances", MappingProxyType(dict(self.balances)))

    def portfolio(self, quote: str, prices: Mapping[str, Decimal]) -> Portfolio:
        """The balances valued at ``prices``; the gas balance is not part of it."""
        return Portfolio(quote=quote, balances=self.balances, prices=prices)

    def apply(self, fills: Sequence[Fill]) -> Ledger:
        """The ledger after every one of ``fills``, or :class:`LedgerError` and no change.

        Each fill takes its swap's ``amount_in`` from one balance, adds its
        ``amount_out`` to another and takes its gas from the gas balance. A
        fill that a balance does not cover at its turn raises, and one whose
        gas the gas balance does not cover raises :class:`InsufficientGas`.
        """
        balances = dict(self.balances)
        gas_eth = self.gas_eth
        for fill in fills:
            sold, bought = fill.swap.token_in.symbol, fill.swap.token_out.symbol
            for symbol in (sold, bought):
                if symbol not in balances:
                    raise LedgerError(f"the ledger does not hold {symbol}: {sorted(balances)}")
            if balances[sold] < fill.swap.amount_in:
                raise LedgerError(
                    f"the ledger holds {plain(balances[sold])} {sold}, less than the "
                    f"{plain(fill.swap.amount_in)} the swap sells"
                )
            if gas_eth < fill.gas_cost_eth:
                raise InsufficientGas(
                    f"the gas balance of {plain(gas_eth)} ETH does not cover the "
                    f"{plain(fill.gas_cost_eth)} ETH the swap of {sold} for {bought} costs"
                )
            balances[sold] = EXACT_CONTEXT.subtract(balances[sold], fill.swap.amount_in)
            balances[bought] = EXACT_CONTEXT.add(balances[bought], fill.amount_out)
            gas_eth = EXACT_CONTEXT.subtract(gas_eth, fill.gas_cost_eth)
        try:
            return Ledger(balances=balances, gas_eth=gas_eth)
        except ValueError as exc:
            raise LedgerError(f"the fills would leave a ledger that cannot be ({exc})") from exc
