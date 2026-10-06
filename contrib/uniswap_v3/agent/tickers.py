"""The ticker the graph analyses for each token the package trades.

The graph's crypto pipeline reads its data for ``<base>-USD`` tickers; a
wrapped token on Ethereum is the asset its ticker names. A token not in the
table has no judge: asking the graph about ``WETH`` would have it look the
symbol up and find nothing, or something else.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Final

from ..config import ConfigError

__all__ = ["TICKERS", "tickers_for"]

#: Token symbol, as :mod:`..constants` spells it, to the ticker the graph analyses.
TICKERS: Final[Mapping[str, str]] = MappingProxyType({"WETH": "ETH-USD", "WBTC": "BTC-USD"})


def tickers_for(symbols: Iterable[str]) -> dict[str, str]:
    """Every symbol's ticker, sorted by symbol; one without a ticker refuses them all, before any is asked."""
    wanted = sorted(symbols)
    unknown = [symbol for symbol in wanted if symbol not in TICKERS]
    if unknown:
        raise ConfigError(
            f"no ticker is known for {unknown}; the judge can be asked about {sorted(TICKERS)}"
        )
    return {symbol: TICKERS[symbol] for symbol in wanted}
