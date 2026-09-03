from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Literal

from .errors import VendorUnavailableError

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(f"ticker contains characters not allowed in a filesystem path: {value!r}")
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


# How far the analysis date may sit behind the wall clock before a live-only
# vendor's report discloses that its values are today's, not that date's. One
# shared bound (the default of live_snapshot_note) so every live-only vendor
# agrees on what counts as "a backtest" within the same run (#30).
MAX_LIVE_SNAPSHOT_BEHIND_DAYS = 2

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
# One definition for both market-data paths — the stockstats/yfinance loader and
# the Alpha Vantage daily getter reject the same gap, so a stalled feed cannot
# short-circuit the vendor chain that could have served fresh bars (#30, #70).
MAX_OHLCV_STALE_DAYS = 10

# Maximum age (calendar days) of the newest insider filing before the report
# carries a lag note. Insider activity is legitimately sparse, so the bound is
# generous — the note flags a long-dead filing stream, not a quiet quarter.
# Relative to the wall clock: no curr_date reaches the insider call path, and
# the filings are fetched live either way (#30). Shared by both vendors serving
# the insider tool so its honesty does not depend on which one ``data_vendors``
# selected (#69).
MAX_INSIDER_LAG_DAYS = 90

# Maximum age (calendar days) of the newest financial-statement period relative
# to curr_date before the report carries a data-lag note, keyed by the requested
# freq. Statements file with a delay, so a period plus a filing window is normal
# cadence — a quarter+~90d for quarterlies, a year+filing window for annuals;
# beyond that the newest statement is genuinely old (#30). Shared rather than
# per-vendor so a statement tool's honesty does not depend on which vendor
# ``data_vendors`` happened to select (#58).
MAX_STATEMENT_LAG_DAYS = {"quarterly": 180, "annual": 550}

# The description each indicator report ends with — agent-facing prompt text,
# so the sentence an analyst reads must not depend on which vendor
# ``data_vendors`` selected. One table for every vendor serving the indicator
# tools (#137): the yfinance getter serves it whole, and the Alpha Vantage
# vendor derives its registry from the slice it supports, so editing a
# description edits BOTH REPORT LANES — but not (yet) the market analyst's
# prompt menu, which still hard-codes its own verbatim copy (#187); edit
# that too until it is derived from here. Lives here (stdlib-only) rather
# than beside either vendor so the pure-requests Alpha Vantage module does
# not import the yfinance stack for a dict of strings (#70).
INDICATOR_DESCRIPTIONS = {
    # Moving Averages
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
    ),
    # MACD Related
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes. "
        "Tips: Confirm with other indicators in low-volatility or sideways markets."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades. "
        "Tips: Should be part of a broader strategy to avoid false positives."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early. "
        "Tips: Can be volatile; complement with additional filters in fast-moving markets."
    ),
    # Momentum Indicators
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
        "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
    ),
    # Volatility Indicators
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement. "
        "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones. "
        "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions. "
        "Tips: Use additional analysis to avoid false reversal signals."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
        "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
    ),
    # Volume-Based Indicators
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data. "
        "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    ),
    "mfi": (
        "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
        "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
    ),
}


def normalize_iso_date(value) -> str | None:
    """Canonical ``YYYY-MM-DD`` for a date string, or None if it is not a date.

    ``strptime`` accepts non-zero-padded input ("2026-6-5"), which then compares
    WRONG lexically against zero-padded vendor date fields — ``"2024-12-31" <=
    "2026-6-5"`` is True — so the raw string must be normalised before any
    look-ahead filter rather than compared as text.

    That lexical comparison is also why this parser is strict where
    :func:`_parse_day` below is deliberately lenient: that one prefix-trims so an
    annotation can read datetimes and ISO time suffixes, and relaxing this one to
    match would drop the guarantee silently. They are not two spellings of one
    idea — do not unify them.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# The closed set of date arguments a refusal can name, and the tag each one
# carries. Closed on purpose (the same reasoning as the disposition vocabulary,
# #84): the tags are read by the model, so a new one must be a decision made
# here, not minted by whatever a new call site happens to pass — an unknown
# argument name raises at the call rather than inventing a tag.
_DATE_ARGUMENT_TAGS = {
    "curr_date": "INVALID_CURR_DATE",
    "start_date": "INVALID_START_DATE",
    "end_date": "INVALID_END_DATE",
}

# What a usable date would have done: bounded a single point in time (the
# analysis date), bounded one end of a window, or — for the live-only sources
# whose ``curr_date`` never bounds the data at all — gated the as-of
# DISCLOSURE the report carries (#89: the date decides whether the report may
# claim its live values are that date's). Stated by the caller, not inferred
# from the argument's name — a future point-in-time tool whose argument is
# called ``as_of_date`` must not silently be told it asked for a window, and a
# disclosure-only tool must not claim its data could have been bounded (#144:
# that claim invited the model to retry historical dates against a live
# snapshot).
DateKind = Literal["point", "window", "disclosure"]

# Everything the vendor modules render — and every error they raise, since
# ``route_to_vendor`` hands an optional category's failure to the model as
# ``DATA_UNAVAILABLE: ... ({error})`` — is assembled into an LLM prompt, so a
# fragment that survives verbatim from outside the module can forge report
# structure rather than merely read oddly. The one definition of that
# flattening (it used to be copied into deribit and sosovalue_common, whose
# ``_sanitize`` now delegate here); the vendor modules keep the reasoning for
# WHICH fragments they flatten.
#
# ``sanitize_untrusted`` and the three names it reads (``MARKDOWN_CONTROL``,
# ``EMPHASIS_UNDERSCORE``, ``MAX_UNTRUSTED_CHARS``) are SUPPORTED API for
# vendor modules (#140): a new vendor module calls them directly rather than
# minting its own flattening or aliasing the cap under a private name — the
# alias is one more site a cap change has to find (deribit's was the last).
#
# Collapsing whitespace is the load-bearing half: the reports are block-level
# (sections joined by blank lines, headings and labels at the start of a line),
# so a fragment with no line breaks cannot open a block however it is
# punctuated. The character strip is the second line of defence, against what
# still reads as a marker mid-line: "*" and "`" for bold and code spans, "#"
# and "|" because a heading or a table row is worth denying even inert. "<"
# and ">" are deliberately NOT stripped: both are block-level only, and both
# carry real meaning in vendor diagnostics ("start_timestamp must be <
# end_timestamp"). Translated to a SPACE rather than deleted: deletion joins
# the fragments either side and can fuse two tokens into a third that reads as
# legitimate ("BTC|USD" → "BTCUSD").
MARKDOWN_CONTROL = str.maketrans(dict.fromkeys("#*`|", " "))

# "_" is handled separately because it also occurs inside ordinary words (a
# field name in a vendor diagnostic, an event or company name). Only underscores
# in EMPHASIS position — at a word boundary, where "_caveat._" lines sit — are
# removed; one between two alphanumerics stays.
EMPHASIS_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")

# Long enough for any real vendor error message or caller argument, short
# enough that a payload cannot bury the report's own sentences under its bulk.
MAX_UNTRUSTED_CHARS = 200


_WHITESPACE_RUN = re.compile(r"\s+")

# The omission clause both the sentinel tail and the tool-description note
# append for the disclosure-only tools — one literal, so a description cannot
# promise a remedy the sentinel stopped offering, or vice versa (#140 review).
_OMIT_CLAUSE = " or omit it"


def sanitize_untrusted(text: object, *, limit: int | None = None, keep_edges: bool = False) -> str:
    """Flatten a fragment the vendor did not author so it cannot forge structure.

    The strip runs FIRST and whitespace is collapsed after it, so neither the
    spaces the translation introduces nor the ones already in the fragment can
    survive as a run or rebuild a line break inside a table cell. ``limit``
    caps the result. Pass it where the fragment is ISOLATED (an echoed raw
    row or argument in a raised message — not a vendor's whole reason, which
    the router caps at its slot), and where a whole message is about
    to enter text the MODEL reads (a sentinel slot, a leaf's degrade line):
    there the vendor's share of the prompt has to be bounded, and what the
    cap drops is the operator's to read in a log (#171, #172). Not when
    flattening a whole message for its own raise or a log line, where the
    module's own diagnostic carries the meaning.

    ``keep_edges`` is for echoing a REFUSED value back to its author: markers
    become a space rather than vanishing, and nothing is trimmed off the ends,
    so ``"_2026-08-18"`` cannot come back as ``2026-08-18`` inside a sentence
    calling it invalid. A rendered vendor fragment wants the default — there
    the marker is noise and a boundary space would rebuild a table cell.
    """
    marker = " " if keep_edges else ""
    flat = EMPHASIS_UNDERSCORE.sub(marker, str(text).translate(MARKDOWN_CONTROL))
    flat = _WHITESPACE_RUN.sub(" ", flat) if keep_edges else " ".join(flat.split())
    if limit is not None and len(flat) > limit:
        flat = (flat[:limit] if keep_edges else flat[:limit].rstrip()) + "..."
    return flat


def _echo_untrusted(value) -> str:
    """The refused date argument, quoted, flattened and capped, for the sentinel.

    The value is the model's own text echoed back into a sentence it reads, so
    it gets the vendor flattening with ``keep_edges`` (see there). The cap is
    enforced on the ESCAPED form: the character cap runs first (bounding the
    re-cap loop's work), ``repr`` then escapes control and non-printable
    astral characters — 4-10x growth that used to carry a "capped" hostile
    value far past the promise (#140) — and the re-cap below drops whole
    characters until the quoted form fits, so an escape sequence stays whole
    or vanishes and the quotes stay balanced. A clean value such as ``'abc'``
    comes through byte for byte.
    """
    if isinstance(value, str):
        flat = sanitize_untrusted(value, limit=MAX_UNTRUSTED_CHARS, keep_edges=True)
        quoted = repr(flat)
        if len(quoted) > MAX_UNTRUSTED_CHARS + 2:
            # Whole characters, not repr bytes, and repr is recomputed each
            # step because dropping a character can flip repr's quote choice.
            # O(cap^2) in the worst case, bounded by the character pre-cap
            # above — rework this loop before ever growing
            # MAX_UNTRUSTED_CHARS by orders of magnitude.
            while len(repr(flat)) > MAX_UNTRUSTED_CHARS + 2:
                flat = flat[:-1]
            requoted = repr(flat)
            quoted = requoted[:-1] + "..." + requoted[-1]
        return quoted
    try:
        raw = repr(value)
    except Exception:  # noqa: BLE001 — a refusal must not become an untyped raise
        # Only a direct caller can hand over an object whose repr raises; the
        # tool schemas send JSON values. Still, the refusal is what stands
        # between that caller and the router's "vendor down" lane.
        raw = f"<{type(value).__name__} value>"
    # A non-string can only come from a direct caller (tool schemas send JSON
    # strings), so this lane is best-effort by design: bound the work up front
    # (a buggy caller can hand over a million-element repr), keep the
    # truncation boundary in the shared helper, and re-close the OUTERMOST
    # delimiter when the echo was cut so it reads as a cut value rather than a
    # broken one (#140). No whole-escape promise here — that promise above is
    # for model input, which is always a string.
    closer = raw[-1] if raw and raw[-1] in "'\")]}" else ""
    sliced = len(raw) > MAX_UNTRUSTED_CHARS * 4
    raw = raw[: MAX_UNTRUSTED_CHARS * 4]
    flat = sanitize_untrusted(raw, keep_edges=True)
    if sliced or len(flat) > MAX_UNTRUSTED_CHARS:
        # ``sliced`` forces the cut MARKING even when whitespace collapse
        # brought the prefix under the cap — a cut without an ellipsis reads
        # as the whole value.
        capped = sanitize_untrusted(raw, limit=MAX_UNTRUSTED_CHARS, keep_edges=True)
        if not capped.endswith("..."):
            capped += "..."
        return capped + closer
    return flat


def invalid_date_sentinel(
    value,
    *,
    what: str,
    kind: DateKind,
    param: str = "curr_date",
    _echo: str | None = None,
) -> str:
    """The sentinel served when a supplied date argument is not a usable date.

    Loud to the LLM (it can retry with a valid date), leaks no data, and is
    RETURNED, never raised, by every date-taking routed tool, core or optional:
    the router serves any returned string as the tool's answer, whereas a raise
    is judged by the category — a core one crashes the ToolNode-wrapped run
    (``raise first_error``), and an optional one is rendered as "this source is
    down, proceed without it" (#119), a verdict the model cannot fix by
    retrying with a better argument.

    Shared by both vendors serving each routed tool so the agent reads the same
    sentence either way (#89) — which vendor ``data_vendors`` selected is not
    something the agent can see, so an answer that differs by vendor is one it
    has no way to interpret. ``what`` names the data the date was meant to
    bound and ``kind`` says how (see :data:`DateKind`); both are required, so a
    caller that forgot either cannot emit a confident sentence about the wrong
    thing. ``param`` names the argument being refused (#111) — the OHLCV and
    ticker-news tools take ``start_date``/``end_date`` — and must be one of
    :data:`_DATE_ARGUMENT_TAGS`.

    The retry tail offers omission exactly for ``kind="disclosure"`` (#144 —
    the model failing RIGHT NOW reads this sentence, not the tool
    description, so a legal exit stated only there kept it resubmitting
    reformatted dates). Derived from ``kind`` rather than carried as a second
    flag, so "these values are live" and "or omit it" cannot ship separately:
    a BOUNDED tool must never invite the omission that would switch its
    look-ahead filtering off, even where omission is technically legal (the
    statement getters' #73 lane — their ``date_refusal`` gate stays
    ``omitted_ok=True``, their sentence stays a bounding one). ``_echo`` lets
    :func:`date_refusal` pass the echo it already computed for its log line;
    anyone else leaves it to be computed here.

    The echoed value is flattened and capped (see :func:`_echo_untrusted`):
    the optional-category getters used to carry that guard in their own
    vendor-error messages (#119 moved them onto this sentinel), and the core
    tools never had it.
    """
    tag = _DATE_ARGUMENT_TAGS[param]
    retry = "retry with a valid yyyy-mm-dd date"
    if kind == "point":
        consequence = f"{what} cannot be bounded to a point in time"
    elif kind == "window":
        consequence = f"the {what} window cannot be resolved"
    elif kind == "disclosure":
        # Disclosure-only tools serve LIVE values whatever the date; the date
        # only gates the as-of disclosure. "Cannot be bounded" here claimed
        # the opposite and invited a historical-date retry (#144). The
        # template conjugates for a PLURAL ``what`` ("are") — both current
        # disclosure whats are plural; a singular one needs a decision here.
        consequence = f"the report cannot say whether the live {what} are as of that date"
        retry += _OMIT_CLAUSE
    else:
        # Mirror _DATE_ARGUMENT_TAGS: an unknown kind fails at the call
        # rather than falling into whichever branch is last — which would
        # hand a typo the strongest wrong claim (#140 review).
        raise ValueError(f"unknown DateKind {kind!r}")
    echo = _echo if _echo is not None else _echo_untrusted(value)
    return (
        f"{tag}: {param} {echo} is not a valid yyyy-mm-dd date, "
        f"so {consequence}. "
        f"No data returned; {retry}. Do not fabricate values."
    )


def date_refusal(
    value,
    *,
    what: str,
    kind: DateKind,
    param: str = "curr_date",
    omitted_ok: bool = False,
) -> str | None:
    """The sentinel refusing a SUPPLIED-but-unusable date, or None to proceed.

    ``None`` is refused by default: the routed tools declare their dates as
    required ``str`` arguments, so the model cannot send it (the tool schema
    rejects a null before the getter runs) and a ``None`` that arrives came
    from a direct caller — passed on, it reached a ``strptime`` as a TypeError
    outside every vendor lane. The fundamentals getters and the
    prediction-market getter are the stated exceptions: they pass
    ``omitted_ok=True`` because there ``None`` means the model omitted the
    argument, which keeps the date-less fallback lane (#73, #139).
    Any other value was supplied, so one that will not parse — the empty string
    included — is a request that cannot be answered, not a request for no bound.

    Both halves are one judgement, so it lives here rather than in either vendor:
    a later refinement (say, also refusing a future-dated curr_date) applied to a
    vendor-local copy would reach that vendor's getters and silently miss the
    other's, which is the drift this whole change exists to close. The three
    inputs that used to be answered differently per vendor are in the CHANGELOG.
    """
    if value is None and omitted_ok:
        return None
    if value is not None and normalize_iso_date(value) is not None:
        return None
    # The refusal is RETURNED, so it never reaches the router's warning lane;
    # without this line an operator's log shows nothing for a model that keeps
    # sending a date no tool can use (#119). Info, not warning: the model is
    # told to retry, and the echo is the flattened one the sentence carries —
    # computed once here and handed to the sentinel (#140).
    echo = _echo_untrusted(value)
    logger.info("Refusing unusable %s %s for %s", param, echo, what)
    return invalid_date_sentinel(value, what=what, kind=kind, param=param, _echo=echo)


def date_sentinel_note(*params: str, omitted_ok: bool = False, disclosure: bool = False) -> str:
    """The tool-description sentence for a tool that answers with the date sentinel.

    Appended to ``tool.description`` by
    ``agents.utils.tool_notes.notes_date_sentinel`` — one wording, owned here
    beside the sentinel it describes, so a wrapper cannot hand-write a variant
    or describe itself as returning only "a formatted report" while its getter
    answers a bad date with :func:`invalid_date_sentinel` (#140: eleven
    wrappers did exactly that, and the model reads the description when
    CHOOSING arguments). ``params`` are the tool's date arguments (a typo
    raises through :data:`_DATE_ARGUMENT_TAGS`, same as the sentinel).

    Two knobs because they answer different questions: ``omitted_ok`` matches
    the getter's None-gate and shapes the trigger ("is supplied but ..."),
    while ``disclosure`` matches the getter's ``kind="disclosure"`` and adds
    the omission remedy. The statement tools legally accept omission (#73)
    yet must not ADVERTISE it — their date bounds the data, and inviting
    omission invites switching look-ahead filtering off (#140 review).
    """
    if not params:
        raise ValueError("date_sentinel_note needs at least one date parameter")
    if disclosure and not omitted_ok:
        raise ValueError("a disclosure-only date is omittable by construction")
    tags = " or ".join(_DATE_ARGUMENT_TAGS[p] for p in params)
    names = " or ".join(params)
    supplied = "is supplied but " if omitted_ok else "is "
    remedy = "retry with a valid date" + (_OMIT_CLAUSE if disclosure else "")
    # Own paragraph ("\n\n"): appended to docstrings whose last line rarely
    # ends with a period, a same-line note read as a run-on sentence in 14 of
    # the 16 rendered descriptions.
    return (
        f"\n\nReturns an {tags} sentinel instead of data when {names} {supplied}"
        f"not a usable yyyy-mm-dd date ({remedy}; do not fabricate values)."
    )


def date_range_refusal(start_date, end_date, *, what: str) -> str | None:
    """:func:`date_refusal` over a required ``start_date``/``end_date`` pair.

    The OHLCV and ticker-news tools bound a window rather than a point, so they
    have two arguments to refuse and no date-less lane. Start is judged first
    and only the first unusable one is named: one sentence asks for one fix (a
    preference, not a measured claim about how the model retries).
    """
    refusal = date_refusal(start_date, what=what, kind="window", param="start_date")
    if refusal is not None:
        return refusal
    return date_refusal(end_date, what=what, kind="window", param="end_date")


def statement_lag_bound(freq) -> int:
    """Days the newest fiscal period may lag curr_date before it is flagged.

    An unknown or missing freq falls back to the annual bound — the looser one,
    because an annotation must not false-alarm. Shared by every vendor serving
    the statement tools so that fallback rule cannot drift between them (#58).
    """
    key = freq.lower() if isinstance(freq, str) else ""
    return MAX_STATEMENT_LAG_DAYS.get(key, MAX_STATEMENT_LAG_DAYS["annual"])


def _plural_days(n: int) -> str:
    return "day" if n == 1 else "days"


def _parse_day(value, label: str) -> datetime | None:
    """Parse a date-ish value's ``yyyy-mm-dd`` prefix, or ``None``.

    Accepts strings, datetimes, and pandas Timestamps (anything whose ``str``
    starts with the date). An unparseable non-empty value is logged: the
    freshness annotations degrade to silence on bad input, and without a log
    line a vendor-side date-format change would turn every future disclosure
    off invisibly.
    """
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        if value:
            # Context-neutral wording: single-value callers suppress their
            # note on this, but row-reduction callers may still note from the
            # other rows — so the log claims only what is true everywhere.
            logger.warning("freshness note: unparseable %s %r; ignored", label, value)
        return None


def data_lag_note(
    latest_date,
    curr_date,
    max_lag_days: int,
    source_phrase: str,
) -> str:
    """Return a one-line staleness disclosure when the newest data row lags curr_date.

    Opt-in freshness annotation (#30): a vendor that renders a "latest" value
    compares that row's date against the date being analysed and discloses the
    gap instead of letting an old value read as current. Annotation, not a
    raise (PR #16 route): these vendors have no fresher fallback and a lag
    usually means the upstream has not published yet, so the honest output is
    the stale value plus this disclosure.

    Both dates accept ``yyyy-mm-dd`` strings (extra ISO time suffixes are
    ignored) or date-like objects. Returns ``""`` when the data is fresh
    enough or when either date is unparseable — an annotation helper must
    degrade, never raise.
    """
    latest_dt = _parse_day(latest_date, "latest_date")
    curr_dt = _parse_day(curr_date, "curr_date")
    if latest_dt is None or curr_dt is None:
        return ""
    lag_days = (curr_dt - latest_dt).days
    if lag_days <= max_lag_days:
        return ""
    return (
        f"_Data lag: the newest {source_phrase} is {latest_dt.strftime('%Y-%m-%d')}, "
        f"{lag_days} {_plural_days(lag_days)} before {str(curr_date)[:10]}; "
        f"treat the latest value as stale._"
    )


def live_snapshot_note(
    curr_date,
    source_phrase: str,
    max_behind_days: int = MAX_LIVE_SNAPSHOT_BEHIND_DAYS,
    today: str | None = None,
) -> str:
    """Return a disclosure when live-only data is rendered for a past analysis date.

    Some vendors (prediction markets, current-state fundamentals, live message
    streams) can only serve *today's* values — there is no historical snapshot
    to fetch. When the date being analysed sits meaningfully behind the wall
    clock (a backtest), the report must say the numbers are live as of the
    fetch, not as of ``curr_date``, or the agent will read today's state as
    history (#30).

    Returns ``""`` when curr_date is close enough to today or unparseable —
    same degrade-never-raise contract as :func:`data_lag_note`. That silence
    is only honest when the date came from inside the program (the graph's
    own ``end_date``, as the StockTwits block receives it): a getter whose
    ``curr_date`` the model supplies must judge it with :func:`date_refusal`
    first, or a string no sibling tool accepts is served here as an
    undisclosed live report (#139).
    """
    today_str = today or date.today().strftime("%Y-%m-%d")
    curr_dt = _parse_day(curr_date, "curr_date")
    today_dt = _parse_day(today_str, "today")
    if curr_dt is None or today_dt is None:
        return ""
    behind_days = (today_dt - curr_dt).days
    if behind_days <= max_behind_days:
        return ""
    return (
        f"_Note: {source_phrase} live values as of the fetch ({today_str}), not a "
        f"snapshot as of {str(curr_date)[:10]} ({behind_days} {_plural_days(behind_days)} "
        f"earlier); do not read them as that date's state._"
    )


def http_status(exc: BaseException) -> int | None:
    """The HTTP status an ``HTTPError`` carries, or ``None`` for any other error.

    Both transport libraries attach the response: curl_cffi's
    ``raise_for_status`` builds ``HTTPError(msg, 0, response)`` and requests'
    sets ``.response`` the same way (measured, curl_cffi 0.15.0). The one
    library-neutral way to read a status off an exception — the two
    ``HTTPError`` classes share no base below ``OSError``. ``None`` too for
    a status of 0: curl_cffi attaches a ``Response`` to its transport
    failures as well (a ``Timeout``, a ``ConnectionError``), with
    ``status_code == 0`` because nothing answered (measured), and a caller
    asking "did the vendor answer a status?" must hear no.
    """
    return getattr(getattr(exc, "response", None), "status_code", None) or None


def raise_for_http_status(response, vendor: str) -> None:
    """Raise ``VendorUnavailableError`` for a 5xx; otherwise ``response.raise_for_status()``.

    The one status decision at a vendor's request boundary — the 5xx check
    and the library's own raise in one call, in a fixed order, so a boundary
    has nothing of its own to sequence (a bare ``raise_for_status()`` ahead
    of the 5xx check would silently restore what this exists for).
    A 5xx is the vendor being down, and ``route_to_vendor`` reacts to that
    by behaviour — the chain goes on, logged without the traceback its
    generic lane reserves for a bug, and a fallback's "no data" is then
    reported as unconfirmed rather than as a verdict on the symbol — only
    when the type says so. Left to ``raise_for_status()`` the same event is
    a ``requests.HTTPError``, which that generic lane logs as a bug and the
    no-data verdict then hides (#142). Every other status keeps the vendor's
    own handling: a 4xx is the vendor's answer about this client or this
    request (a 401/403 refusal, a 404), which is the vendor's boundary to
    judge, not this helper's, so it leaves as the ``requests.HTTPError`` it
    always did; a status the vendor classifies itself (FRED's 400 with the
    reason in its body, Alpha Vantage's 429) is checked before this is
    called. (The router reads the status off an exception that carries one
    and counts a 401/403 — the vendor refusing this client — as an outage
    for its verdict; see ``route_to_vendor``.)

    The message carries the status code only, never the body: a 5xx body is
    the vendor's error page, and this message travels into a sentinel the
    model reads.
    """
    status = response.status_code
    if status >= 500:
        raise VendorUnavailableError(f"{vendor} answered HTTP {status} without data")
    response.raise_for_status()


def json_body_or_outage(response, vendor: str):
    """Decode ``response`` as JSON, or raise ``VendorUnavailableError`` when it is not.

    For a vendor whose every data answer is JSON: a body that does not decode
    is an error page served with a 2xx (a CDN or WAF interstitial), so the
    vendor answered without data — the same reaction as a 5xx above, where a
    bare ``.json()`` raised a ``ValueError`` into the router's generic lane.
    Not for a vendor that serves data in another shape (Alpha Vantage's CSV,
    Farside's HTML), where a non-JSON body is the data. A body that fails
    below the decoder — ``requests``' ``ContentDecodingError`` on a broken
    gzip stream — is a transport failure by the library's own taxonomy and
    is left to the caller's transport handling.
    """
    try:
        return response.json()
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        raise VendorUnavailableError(
            f"{vendor} answered HTTP {response.status_code} with a body that is not JSON"
        ) from e


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date
