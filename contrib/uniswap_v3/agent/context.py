"""The spot context the graph is handed beside the ticker: the recent closes, and the rating's role.

The graph's analysts fetch their own data for the ticker: price history,
indicators, news, flows. What they cannot see is the market this package
trades in, the Uniswap v3 pools, whose closes are what the strategy acts
on; nor what a rating does once given. Both go into the instrument context
every agent reads. Nothing else does: no holding, which belongs to a run
while a verdict belongs to none, and no verdict of the rule strategy, which
the rating is not to second-guess; nor the multipliers, which are each
run's own while a verdict is shared by every run that reads the source.
The text gives context and asks for no answer format: the rating is read
out of the graph's own decision.

The closes come one per boundary, with ``None`` where a bar is missing or
suspect, and a gap is told as a gap: a change is given only when the bar
exactly that many boundaries back is there, and the volatility is measured
over the returns between bars at consecutive boundaries alone, saying how
many of them there were. The estimator is the rule strategy's
(:func:`~..domain.decimal_context.sample_volatility`); the bars in a year
are derived from the bar length, where the strategy takes them as a param.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import (
    DECIMAL_CONTEXT,
    fixed_text,
    price_text,
    sample_volatility,
)
from ..domain.times import utc_text

__all__ = ["BARS_NEEDED", "CHANGE_SPANS", "VOL_WINDOW", "spot_context"]

#: The spans, in bars, the close's change is given over.
CHANGE_SPANS: Final = (1, 7, 30)
#: The log returns the realised volatility is measured over.
VOL_WINDOW: Final = 20
#: The closes the full context takes: the latest and the furthest span back.
BARS_NEEDED: Final = max(*CHANGE_SPANS, VOL_WINDOW) + 1
_SECONDS_PER_YEAR: Final = Decimal(365 * 86_400)
_HUNDRED: Final = Decimal(100)


def _percent(fraction: Decimal) -> str:
    return fixed_text(DECIMAL_CONTEXT.multiply(fraction, _HUNDRED), signed=True) + "%"


def _change(closes: Sequence[Decimal | None], span: int) -> str:
    earlier = closes[-1 - span] if len(closes) > span else None
    latest = closes[-1]
    if earlier is None or latest is None:
        return f"over {span} bar(s): not measured (no bar {span} bar(s) back)"
    fraction = DECIMAL_CONTEXT.subtract(DECIMAL_CONTEXT.divide(latest, earlier), Decimal(1))
    return f"over {span} bar(s): {_percent(fraction)}"


def _volatility(closes: Sequence[Decimal | None], interval_seconds: int) -> str:
    """The sample deviation of the log returns between consecutive bars in the window, annualised.

    The window is the last :data:`VOL_WINDOW` returns' worth of boundaries;
    a return across a missing bar is left out, and the count says so.
    """
    window = closes[-(VOL_WINDOW + 1) :]
    returns = [
        DECIMAL_CONTEXT.ln(DECIMAL_CONTEXT.divide(later, earlier))
        for earlier, later in zip(window, window[1:], strict=False)
        if earlier is not None and later is not None
    ]
    measured = f"{len(returns)} of {VOL_WINDOW} returns measured"
    if len(returns) < 2:
        return f"not measured ({measured})"
    bars_per_year = DECIMAL_CONTEXT.divide(_SECONDS_PER_YEAR, Decimal(interval_seconds))
    annualised = sample_volatility(returns, DECIMAL_CONTEXT.sqrt(bars_per_year))
    note = "" if len(returns) == VOL_WINDOW else f" ({measured})"
    return f"{DECIMAL_CONTEXT.multiply(annualised, _HUNDRED):.1f}% annualised{note}"


def _listed(quote: str, traded: Sequence[str]) -> str:
    """``USDC and WETH``, or ``USDC, WBTC and WETH``: the quote first, the rest sorted."""
    *rest, last = sorted(traded)
    return f"{', '.join([quote, *rest])} and {last}"


def spot_context(
    closes: Sequence[Decimal | None],
    *,
    symbol: str,
    ticker: str,
    quote: str,
    traded: Sequence[str],
    time: int,
    interval_seconds: int,
) -> str:
    """The context for ``symbol``, whose closes in ``quote`` are ``closes``, the latest last.

    ``closes`` are the closes at consecutive boundaries up to and including
    the bar at ``time``, ``None`` at a boundary without a bar or with a
    suspect one; the latest is the bar being judged, and is there.
    ``traded`` are every token the rebalance holds beside the quote.
    """
    if not closes or closes[-1] is None:
        raise ValueError("the spot context takes at least the close of the bar being judged")
    if any(close is not None and close <= 0 for close in closes):
        raise ValueError("a close is a positive price")
    if not traded:
        raise ValueError("the spot context names at least one traded token")
    changes = "; ".join(_change(closes, span) for span in CHANGE_SPANS)
    return (
        f"Spot context from the Uniswap v3 pools on Ethereum mainnet, where {ticker} is traded "
        f"as {symbol} against {quote}. A bar closes every {interval_seconds} seconds (at a "
        f"multiple of that since the epoch); the bar being judged closed at {utc_text(time)}.\n"
        f"- Close: {price_text(closes[-1])} {quote}.\n"
        f"- Change of the close {changes}.\n"
        f"- Realised volatility over the last {VOL_WINDOW} bar(s): "
        f"{_volatility(closes, interval_seconds)} (sample standard deviation of the log "
        f"returns, annualised).\n"
        f"The verdict feeds a spot rebalance between {_listed(quote, traded)} that trades once "
        f"per bar, at these closes. The rating sets how much of a rule-capped position in "
        f"{symbol} is held, the most on Buy and the least on Sell. Rate {ticker} on its own; "
        f"the other tokens are rated separately."
    )
