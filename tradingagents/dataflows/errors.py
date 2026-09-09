"""Vendor data-error taxonomy.

A single hierarchy so the routing layer reacts by *behavior*, not by vendor:
every condition where a vendor cannot return usable data derives from
``VendorError``, and the router catches the base types. A new vendor raises
these (or a thin vendor-named subclass) and needs no new ``except`` clause.

    VendorError
    ├── NoMarketDataError          no usable rows (empty result OR stale data)
    ├── VendorRateLimitError       transient throttle -> skip to next vendor
    ├── VendorUnavailableError     down: an outage page, or unreachable -> next vendor, no traceback
    ├── VendorLibraryError         the vendor's own library failed -> next vendor; when none serves: prose, a raise in a loud category, the sentinel in an optional one
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

``WiringGapError`` sits outside it for the opposite reason: it is this
project's own breakage, not the vendor's, and the router's rule for an
untyped failure — read it as the vendor's library and report it as text
(#219) — must not cover it.
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

    ``latch_ttl_s`` is how long that stand-off lasts, in seconds; ``None``
    is the shared window (``throttle.THROTTLE_LATCH_TTL_S``, sized for a
    burst throttle). A subclass sets it when the refusal itself names a
    longer period — Alpha Vantage's daily quota
    (``AlphaVantageDailyQuotaError``) is not over in five minutes, and
    re-probing it on the shared window only adds refused requests (#153).
    Read by the router's arm only, so it has no effect on a type whose
    ``latches_vendor`` is False — those stand off elsewhere, on their own
    terms.
    """

    latches_vendor = True
    latch_ttl_s: float | None = None


class VendorUnavailableError(VendorError):
    """A vendor was down: it answered with something that is not data, or not at all.

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
    and a getter needs no clause of its own to send it there: its lane at the
    router precedes the untyped one. Not yfinance's alone: the vendors whose boundary
    is a bare ``requests.get`` — FRED, Polymarket, Farside, Alpha Vantage —
    map a 5xx (and, where every data answer is JSON, a non-JSON body) to
    this type through the shared ``utils.raise_for_http_status`` /
    ``utils.json_body_or_outage``, so one real event — a vendor down — is
    one router reaction across those (#142). The boundaries that own their
    transport handling — Deribit's retry loop, Fear & Greed's, Farside's
    cache lane, SoSoValue's request itself (#217) — raise it themselves
    for the same events, a request that could not be reached included:
    the router's generic lane already reads an unreached vendor
    as down (``utils.is_vendor_outage``), and a boundary that retries or
    serves stale first must not downgrade that verdict to a bug on the way
    out (#172). Deribit's, Fear & Greed's and Farside's are subclasses of
    their module errors too, so every ``except`` and caller written against
    those keeps working; SoSoValue's is not a ``SoSoValueError``, because
    that family reads its module error as structural breakage. The router remembers
    this type past the chain: a fallback's "no data" after it is reported
    as unconfirmed by the vendor that was down, not as the symbol being
    invalid.
    """


class VendorLibraryError(VendorError):
    """A vendor's own library failed while computing the answer.

    Everything a getter meets that is neither in this taxonomy nor a
    transport failure: a stockstats or pandas bug on a frame the vendor did
    serve, a parser tripping over a shape yfinance's own scraper let
    through. The router raises it, in its untyped lane, for any getter whose
    category is not declared loud (#219) — nine getters used to wear a
    ``with`` block that did the same, and their siblings at the same routed
    tool, having never worn one, aborted the run over the identical bug. A
    boundary may still raise it itself where it knows the failure is its
    library's; the router's lane for this type then logs it without a
    traceback, the raiser having logged one.

    Its router reaction is what earns it a type: the chain goes on, since a
    sibling vendor computes the same routed tool its own way (Alpha Vantage
    has an RSI endpoint; a local stockstats bug is no reason not to ask it),
    and when no vendor serves, the router renders ONE line of report text
    instead of raising — the policy the getters' broad handlers used to
    apply at the leaf, where returning prose read as a successful answer and
    ended the chain at the vendor that had just failed (#187). The text is
    the router's to write, in one place for every vendor: ``what`` is the
    subject the getter named (``rsi values for AAPL``), ``detail`` the
    library's message, which the router flattens and caps on its way into
    the report; the ERROR the untyped lane logs keeps the whole of it. The
    library's exception itself travels as ``__cause__``, not as a field.
    """

    def __init__(self, what: str, detail: str):
        self.what = what
        self.detail = detail
        super().__init__(f"{what}: {detail}")


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


class WiringGapError(RuntimeError):
    """A guard a getter runs found this project's own wiring broken.

    The config key a deployment did not set, the library attribute a version
    bump moved, the indicator a table registers as supported and another
    table has no row for: not the vendor's library failing on data it
    served, so the router's untyped lane — which reads an untyped failure as
    exactly that and reports it as one line of text — must not cover it. It
    never did: those guards were placed ahead of the nine getters' ``with``
    blocks on purpose, so a yfinance that drops ``cache_get.cache_clear``
    fails the call rather than freezing global news behind a report again
    (#111, #200), and Alpha Vantage's registry checks ran before the request
    for the same reason (#106). With the conversion moved to the router
    (#219) the guards are no longer told apart by where they sit, so they
    say so by type, and the router's untyped lane lets this one through to
    the ending it has always had. A core category's chain raises it, ahead
    of the report line a vendor's library failure would otherwise end on —
    ours is the one someone can fix — though a sibling that answered with a
    clean no-data verdict still ends the chain in that sentinel, as it did
    before this type existed. An optional category's degrades to its own
    sentinel with this message inside it. The one ending it never takes is
    the library failure's report line, which would hand the analyst our bug
    as the answer it asked for.

    Not a ``VendorError``: every type in that tree names something the
    vendor did, and the router would route past this one to a sibling that
    is wired the same way. ``RuntimeError`` rather than a bare ``Exception``
    so a caller that means to catch a bug can.
    """
