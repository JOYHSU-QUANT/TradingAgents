"""Vendor data-error taxonomy.

A single hierarchy so the routing layer reacts by *behavior*, not by vendor:
every condition where a vendor cannot return usable data derives from
``VendorError``, and the router catches the base types. A new vendor raises
these (or a thin vendor-named subclass) and needs no new ``except`` clause.

    VendorError
    ├── NoMarketDataError          no usable rows (empty result OR stale data)
    ├── VendorRateLimitError       transient throttle -> skip to next vendor
    ├── VendorUnavailableError     answered, but not with data -> next vendor, no traceback
    └── VendorNotConfiguredError   missing API key/config -> vendor unavailable

The number of types is the number of distinct router reactions, not the number
of human-describable causes: empty and stale data get identical handling, so
they share ``NoMarketDataError`` and differ only in the free-text ``detail``.
A reaction includes how the failure is logged — ``VendorUnavailableError``
continues the chain like a transport failure but without the traceback that
lane reserves for a bug, which is what earns it a type of its own.

``UnsupportedIndicatorError`` sits outside that tree on purpose: naming an
indicator no vendor computes is a caller mistake, not a vendor condition. The
router moves on to the next vendor without logging a traceback, and the
indicator tool wrapper tells it apart from every other ``ValueError``.
"""

from __future__ import annotations


class VendorError(Exception):
    """Base for any condition where a vendor could not return usable data."""


class NoMarketDataError(VendorError):
    """A vendor returned no usable rows for a symbol (empty result or stale data).

    Carries both the symbol the user requested and the canonical symbol the
    vendor was actually queried with, plus a free-text ``detail``, so callers
    can build a clear message instead of emitting a vendor-specific empty
    string into the data channel.
    """

    def __init__(self, symbol: str, canonical: str | None = None, detail: str = ""):
        self.symbol = symbol
        self.canonical = canonical or symbol
        self.detail = detail
        msg = f"No market data for {symbol!r}"
        if canonical and canonical != symbol:
            msg += f" (queried as {canonical!r})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class VendorRateLimitError(VendorError):
    """A vendor throttled the request; the router skips to the next vendor.

    ``latches_vendor`` says whether the router should stand the vendor off for
    a while on this raise instead of re-discovering the throttle per tool call
    (#114) — the default. A subclass sets it False when a router-level skip
    would refuse an answer the vendor still had: because the raise carries a
    narrower fact than the client's standing (``SoSoValueRateLimitError``:
    throttled AND no usable cache for this call, while sibling tools serve
    stale cache), or because the vendor already stands itself off at its own
    network boundary (``YFinanceRateLimitError``: the latch lives in
    ``yf_retry``, which the indicator path reaches only after its OHLCV cache
    read).
    """

    latches_vendor = True


class VendorUnavailableError(VendorError):
    """A vendor answered, but with something that is not data.

    The outage page or the unparsable body a scraper meets when the vendor is
    down or refusing this client. yfinance parses the body before it looks at
    the status, so a 5xx HTML page reaches it as a ``JSONDecodeError`` and
    Yahoo's own "Will be right back" page as its ``YFDataException``; under
    the library's swallow both became the empty answer ("No news found", the
    no-data sentinel), and let out raw the getters' broad handler rendered
    them as prose — either way the fallback vendor was never tried (#136).
    Neither a throttle nor "no data": the router reacts as it does to a
    transport failure — the chain goes on and this surfaces when nothing else
    serves — but logs it without the traceback that lane reserves for a bug,
    and the getters' ``except VendorError: raise`` lets it out without a
    clause of their own.
    """


class VendorNotConfiguredError(VendorError, ValueError):
    """A vendor was selected but its API key/configuration is missing.

    Also a ``ValueError`` so existing callers that catch ``ValueError`` keep
    working while the routing layer can treat it as "vendor unavailable".
    """


class UnsupportedIndicatorError(ValueError):
    """The caller asked for an indicator no vendor computes.

    A caller mistake, not a vendor condition, so not a ``VendorError``. The
    ``get_indicators`` tool wrapper renders this type as report text — a bad
    LLM-supplied name should cost one indicator, not the whole call — and
    lets other ``ValueError``s, ``VendorNotConfiguredError`` above included,
    reach the ToolNode as the failures they are; it used to catch the whole
    family, so a missing API key was pasted into the market report as prose
    (#117). ``route_to_vendor`` logs it without a traceback and keeps the
    chain going, since another vendor may compute the name. Still a
    ``ValueError`` so callers that catch that keep working.
    """
