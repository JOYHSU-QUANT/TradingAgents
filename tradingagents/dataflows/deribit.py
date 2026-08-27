"""Deribit options vendor: the DVOL implied-volatility index plus 25-delta skew.

Two independent signals from Deribit's keyless public API, surfaced to the market
analyst as a crypto-only volatility-regime read:

* **DVOL** — Deribit's 30-day forward implied-volatility index, a daily history.
  Lookahead-safe to the day: the fetch window ends at ``curr_date`` and rows
  dated after it are dropped, so a past ``curr_date`` never sees a later reading.
  Day granularity is the limit of the guarantee — the candle dated ``curr_date``
  covers all of that UTC day — and the candle dated *today or later* is still
  open, which the report labels and which is why every line calls the series
  "readings" rather than "closes". ("Or later" because the DVOL fetch can itself
  span UTC midnight, returning a candle dated the new day.)
* **25-delta skew** — computed here from the live options chain: ATM (50-delta)
  implied vol, the 25-delta call and put vols, and the risk reversal between
  them. Read for one expiry inside a bounded band around 30 days, because a risk
  reversal is not comparable across tenors and every downstream agent reads this
  as a ~30-day figure. The chain endpoint takes no date, so it can only ever
  describe the present. It is **withheld for a ``curr_date`` earlier than
  today**, and the report says so, because quoting the current chain on a past
  date is future information and a prose warning is not an auditable guard. A
  ``curr_date`` up to ``MAX_FUTURE_DAYS`` *later* than the UTC clock is served
  with a note: callers derive it from a local clock, so east of UTC it routinely
  runs a few hours ahead, and the live chain is then never LATER than the analysis
  date. The note says which of the two cases holds, because they are not the same
  claim: ordinarily the book predates that date outright, but the clock can reach
  it while the report is being built, and the chain then falls inside it.
  Further ahead than that is a bad argument rather than a
  timezone, and the chain is withheld again.

The two halves fail independently. Losing the chain should not also cost the
DVOL history (and vice versa), so each is fetched inside its own try and the
report renders whichever survived, naming what is missing. EITHER half's absence
is named in all three places a reader may stop at — the italic header note, the
section body, and the closing ``_Reading:_`` sentence — the last because it is
the one line a downstream summary keeps, so an absence stated only in the body
does not survive the hop. The DVOL half was for a time named in the body alone;
its header-level trace was subtractive (a dropped clause on the source bullet),
which is invisible, and its ``_Reading:_`` clause was gated on the PERCENTILE
rather than on the half, so a served-but-unrankable feed fell silent in the same
breath as a failed one. The level is now stated there unconditionally and the
rank separately. This module raises when nothing
at all can be served — both halves failed, or DVOL failed on a date, or for a
proxied asset, where the chain is withheld by design — and when either
caller-supplied argument is malformed, except that a FALSY asset is a no-signal
sentence rather than a raise; the routing layer then degrades the optional
``options_data`` category to a sentinel. A throttle
raises ``VendorRateLimitError`` rather than ``DeribitError`` so the router keeps
its rate-limit lane.

This vendor reads a chain and a DVOL index for BTC and ETH. Another recognized
crypto risk asset (SOL, XRP, ...) is not read directly, so BTC's DVOL **level**
is served as a market-wide crypto-vol proxy, labelled as such — but its skew is
not: crypto volatility moves together, which is what makes BTC's index the
benchmark, whereas a 25-delta risk reversal measures how much more traders will
pay for downside in one specific underlying and does not carry across. A
stablecoin or an unrecognized symbol gets a no-signal note. No rendered line
claims Deribit itself lists nothing for those symbols — see SUPPORTED_CURRENCIES.

Uncached by design: a tool call costs two GETs (one where the chain is withheld)
against endpoints with no rate-limit concern at this volume, and both halves are
freshness-sensitive — a cached vol snapshot is worth little.
"""

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple, TypeVar

import requests

from .errors import VendorError, VendorRateLimitError
from .symbol_utils import classify_crypto_asset
from .utils import MAX_UNTRUSTED_CHARS, date_refusal, sanitize_untrusted

logger = logging.getLogger(__name__)

# The result type of one half's fetch, so _try_fetch does not erase it.
_FetchedT = TypeVar("_FetchedT")

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"

# The currencies this vendor reads a chain and a DVOL index for. Everything else
# recognized as a crypto risk asset is proxied to BTC's DVOL level rather than
# queried.
#
# Nothing at runtime checks this against Deribit's actual listings, which is why
# no rendered line states it as a fact ABOUT DERIBIT. It used to: the reports
# said "Deribit lists no options for 'SOL'", an exchange claim derived from a
# two-element tuple — and one this module elsewhere assumes is false, since
# parse_instrument_name documents skipping the decimal-strike form "Deribit uses
# on currencies this module does not serve". The wording now claims only what the
# tuple can support: what this vendor reads.
SUPPORTED_CURRENCIES = ("BTC", "ETH")

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# This vendor is deliberately uncached (freshness), so like fear_greed it has no
# stale-cache buffer against a single dropped connection; one retry after a short
# pause restores that tolerance. The cost that matters is latency, not bandwidth:
# a tool call makes TWO sequential requests, so the worst case is
# 2 * (REQUEST_TIMEOUT + _RETRY_DELAY_SECONDS + REQUEST_TIMEOUT) ~= 124s — double
# fear_greed's envelope. That is acceptable here (the analyst graph runs off the
# trading hot path, on a multi-hour cycle), but anyone raising _RETRY_ATTEMPTS
# should multiply that envelope rather than the request count.
#
# A FLOOR, not a guarantee: requests' timeout bounds each socket read, not the
# call, so a server dribbling a byte every 29s holds the connection open
# indefinitely and no wall-clock budget stops it. Bounding that properly needs a
# deadline around the whole call, which this vendor does not have and its
# siblings do not either. Stated so the number is not mistaken for an SLA.
_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 2

# DVOL candle resolution. "1D" gives one candle per UTC day, which is what the
# min/max/percentile statistics below want: a sub-daily resolution would weight
# recent days more heavily within the same calendar window purely by contributing
# more observations to it.
DVOL_RESOLUTION = "1D"

# The tenor of the DVOL index itself — a fixed property of Deribit's product, not
# a knob of this module. Kept separate from DVOL_WINDOW_DAYS below so that
# changing this module's statistics window cannot make the report misdescribe what
# Deribit actually publishes.
DVOL_INDEX_TENOR_DAYS = 30

# Trailing window for the DVOL min / max range: the DVOL_WINDOW_DAYS calendar days
# ending at curr_date, curr_date included.
DVOL_WINDOW_DAYS = 30

# Trailing window for the DVOL percentile — deliberately far longer than the
# min/max window above, and reported on its own line so the two are never read as
# one. A percentile is a claim about the volatility *regime*, and a month cannot
# support one: through a sustained high-vol stretch every reading in the window is
# high, so a 30-day percentile sits mid-range even at a multi-year extreme, muting
# the signal exactly when volatility is the thing that matters. A year is the
# horizon the options market means by "IV rank". The range stays at 30 days because
# a recent high/low is a different and still useful fact.
DVOL_PERCENTILE_WINDOW_DAYS = 365

# Extra days fetched beyond the longest statistics window. They are outside both
# windows and never enter the statistics.
#
# What they buy is a better DIAGNOSTIC, not a servable reading. They used to buy the
# latter — a *latest reading*, and therefore a report at all, when the feed had
# published nothing for the whole window — but MAX_DVOL_STALENESS_DAYS now caps a
# servable reading at 14 days from min(curr_date, today), which is deep inside the
# percentile window, so on any ordinary date no reading these days carry can be
# served. (Not "ever": the windows are measured from curr_date while the staleness
# bound is measured from the clock when curr_date runs ahead of it, so an analysis
# date roughly a year in the future separates the two. Nothing bounds curr_date's
# futureness — MAX_FUTURE_DAYS only withholds the CHAIN.) What survives is that a feed
# stalled for just over a year raises "the newest usable reading is dated X, N days
# before Y" instead of the far less useful "No DVOL readings on or before Y": the
# reading is fetched, found, and named in the refusal rather than never seen.
_DVOL_FETCH_BUFFER_DAYS = 10

# Deribit caps get_volatility_index_data at this many candles per response,
# returning the NEWEST page plus a ``continuation`` cursor pointing at older data.
# The fetch below spans DVOL_PERCENTILE_WINDOW_DAYS + _DVOL_FETCH_BUFFER_DAYS
# candles at the fixed 1D resolution, well under the cap, so no paging loop is
# needed. Verified live: a 400-day 1D request returns 401 candles with
# ``continuation: null``; a 2000-day one returns exactly 1000 with a cursor. Were
# the cap ever hit it drops the OLDEST readings and always keeps the newest, so it
# could shorten the percentile sample — which the report states by count — but
# could never fabricate a data lag. A test pins the span against this bound.
_DVOL_MAX_CANDLES_PER_RESPONSE = 1000

# Maximum lag (days between the newest DVOL candle and curr_date) before the
# report flags the *data* as behind. A successful fetch says nothing about
# whether the series is still being published, and DVOL is a daily series on a
# 24/7 market with no weekends and no holidays, so there is no benign reason for
# a whole calendar day to carry no candle.
#
# The threshold is the largest lag that is still ORDINARY, and the flag fires
# above it. Lag 0 is the normal case (the candle dated today, still open). Lag 1
# is the one benign gap: a run in the first minutes of a UTC day, before that
# day's candle exists. Lag 2 already means a full intervening day published
# nothing, which on this series is a stall — so 1, not 2. At 2 that state
# printed no italic lag line at all and the level travelled with a bare as-of
# date, leaving the one sentence a downstream summary keeps with no age on it.
MAX_DATA_LAG_DAYS = 1

# The age past which a DVOL reading is WITHHELD rather than served with a caveat.
#
# MAX_DATA_LAG_DAYS above only decides when to print an italic note; nothing used to
# decide when to stop serving at all, so the ceiling was whatever the fetch happened
# to span. That span is max(DVOL_WINDOW_DAYS, DVOL_PERCENTILE_WINDOW_DAYS) plus the
# buffer, and it moved 40 -> 375 days as a side effect of widening the percentile
# window — a number nobody chose for this purpose. A feed stalled 45 days therefore
# headlined a 45-day-old print as this cycle's volatility AND ranked it, since ~320
# readings still clear _MIN_PERCENTILE_SAMPLE: the percentile is the strongest claim
# in the report and it was strongest exactly where the data was weakest.
#
# Withheld rather than caveated, on the ground the historical chain and the proxied
# skew are: the caveat has to survive every downstream summarisation hop and the
# number does not. 14 days matches the sibling farside vendor's already-tuned bound.
# Measured from min(curr_date, today) — the same reference the rendered lag line
# uses — because two comparisons describing the same quantity must agree at the
# boundary or the report contradicts its own refusal.
MAX_DVOL_STALENESS_DAYS = 14

# How far ahead of the UTC clock ``curr_date`` may run and still be served the
# live chain. The tolerance exists for exactly one reason — callers derive
# curr_date from a LOCAL clock (cli/main.py does), so east of UTC it runs a few
# hours ahead for the first hours of every local day — and one day covers every
# timezone on earth. Past that, the argument is not a timezone offset but a
# mistake: curr_date arrives from an LLM tool call, and the sibling fear_greed
# vendor clamps its own LLM-supplied window (MAX_LOOKBACK_DAYS) for the same
# reason. A far-future date has the chain withheld rather than served, because
# "the live book, months before the analysis date" is not a reading of that date
# under any interpretation. The DVOL half is genuinely date-filtered and is still
# served, exactly as on a historical date.
MAX_FUTURE_DAYS = 1

# Expiry selection for the skew calculation: the eligible expiry closest to 30
# days out, ignoring anything inside 7 days. Near-expiry contracts carry
# pin/gamma noise that distorts the delta-to-strike mapping the interpolation
# relies on. "Eligible" is bounded on both sides — see MAX_TENOR_DISTANCE_DAYS.
TARGET_DTE_DAYS = 30
MIN_DTE_DAYS = 7

# How far from TARGET_DTE_DAYS an expiry may sit and still be read as this
# report's ~30-day figure. Without a ceiling the only tenor rule was the 7-day
# floor, so a thinned book whose nearest qualifying expiry was 96 days out
# rendered a 96-day risk reversal under the section's 30-day framing, with
# is_fallback False because it genuinely was the nearest — correct by the ranking
# and wrong to every downstream agent that reads RR25 as a ~30-day number.
# Bounded rather than labelled more loudly, because that is how this module
# treats every other non-transferable figure: the proxied skew and the historical
# chain are WITHHELD, on the stated grounds that a caveat has to survive each
# summarisation hop and the number does not. A tenor from an unrelated part of
# the surface is the same kind of claim.
MAX_TENOR_DISTANCE_DAYS = 15

# The eligible tenor band, derived so that neither bound can drift out of
# agreement with the constants above. At the current values the distance bound
# (15 days) is the binding floor and MIN_DTE_DAYS never fires; the max() keeps
# the pin-noise floor honest anyway, so raising MAX_TENOR_DISTANCE_DAYS past
# TARGET_DTE_DAYS - MIN_DTE_DAYS (23) re-arms it instead of silently admitting
# 2-day expiries.
MIN_ELIGIBLE_DTE_DAYS = max(MIN_DTE_DAYS, TARGET_DTE_DAYS - MAX_TENOR_DISTANCE_DAYS)
MAX_ELIGIBLE_DTE_DAYS = TARGET_DTE_DAYS + MAX_TENOR_DISTANCE_DAYS

# How many ranked expiries compute_skew may try before giving up on a complete
# surface: the nearest, and — if it cannot be used, meaning it either fails to
# bracket both 25-delta wings or carries no usable forward — the next-nearest.
# (Both conditions, matching SkewSnapshot.is_fallback and compute_skew; this
# comment named only the first while its siblings named both. Uncounted: the
# figure here was four and the sibling sites that spell out both conditions are
# SkewSnapshot.is_fallback and compute_skew — compute_skew's own Raises paragraph
# names the forward alone, and neither rendered fallback wording names either.)
# Deliberately not the whole ranking, even now that the tenor band
# bounds how far the ranking can reach. The band decides which expiries may
# answer the ~30-day question at all; this bounds how hard the module hunts for a
# complete pair of wings within it, so a chain whose whole band is sparse reports
# no skew instead of quietly settling for the last entry that happened to answer.
_MAX_EXPIRY_CANDIDATES = 2

# The delta the risk reversal is quoted at (the market-standard 25-delta wing).
WING_DELTA = 0.25

# The delta defining the at-the-money point. As much a convention as WING_DELTA,
# and named for the same reason: both delta conventions should be greppable in one
# place rather than one named and the other a bare literal in the maths.
ATM_DELTA = 0.5

# Deribit options expire at 08:00 UTC on their expiry date.
_EXPIRY_HOUR_UTC = 8

# ACT/365 year fraction, the convention Deribit's own quoted greeks use.
_SECONDS_PER_YEAR = 365 * 24 * 3600

# Locale-independent English month lookup for the expiry token in an instrument
# name (e.g. the "28AUG26" of "BTC-28AUG26-100000-C"). Parsing by hand avoids
# strptime's %b, which resolves against the process-wide LC_TIME locale and would
# break every contract if another component changed the locale.
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}

# Minimum number of daily readings before a percentile is worth stating. The
# latest reading is itself in the sample, so a percentile over n of them can never
# read below 100/n: a 1-reading sample is always the "100th percentile" and a
# 4-reading sample can never land in the bottom fifth. Below this size the report
# gives the range and the sample count and computes no percentile, rather than
# publishing a number that is an artefact of how much data arrived.
_MIN_PERCENTILE_SAMPLE = 10

# Minimum number of readings before a min/max range is worth stating. At one
# reading the min and the max are both the latest value, so the range renders as
# "min 62.30 / max 62.30" — the strongest possible claim about the regime (a
# month of pinned volatility) manufactured from a single observation, and with no
# other warning beside it: a feed that stopped for four weeks and resumed today
# has a lag of zero, so the data-lag note does not fire either. The empty-window
# branch already refuses to compute a range for exactly this reason ("whose min
# and max would both be the latest value itself"); this extends that refusal to
# the one-reading case it did not cover. Lower than the percentile's threshold
# because a range needs only two points to say something true.
_MIN_RANGE_SAMPLE = 2

# Below this magnitude an RR25 rounds away at the report's two decimals, so the
# two wings are level as far as anything printed can tell and the report says so
# rather than emitting a signed zero.
_RR_ZERO_EPSILON = 0.005

# A 25-delta wing interpolated between two strikes further apart than this
# fraction of the forward is called out in the reading line as a coarse read.
#
# The strikes themselves are always printed (see WingQuote), which is the primary
# disclosure and is unchanged; this threshold exists only to decide when the fact
# also has to survive into the one sentence a downstream summary keeps, since a
# bracket's width is exactly the sort of qualifier a summariser drops.
#
# 10%: a 30-day 25-delta wing sits roughly one standard deviation out, which at
# the 40-60% implied vols this index normally prints is 11-17% of the forward. A
# bracket wider than a tenth of the forward therefore spans a large part of the
# distance from the money to the wing, so the linear interpolation is reading
# across the smile's curvature rather than along a local segment of it. Deribit's
# BTC strikes are spaced far tighter than this on a liquid expiry — the gap opens
# up only once the open-interest filter has thinned the chain, which is precisely
# the case worth naming.
_WIDE_BRACKET_FRACTION = 0.10

# Why a 25Δ point could not be interpolated, as reported by
# _interpolate_with_reason. Two distinct facts about the chain — a thin book
# versus a quote the monotonicity guard judged suspect — kept apart because they
# lead a reader to different conclusions about the same expiry.
# A closed set rather than a bare str, because ``_wing_line`` tests only for
# _MISS_NON_MONOTONE and falls through to the thin-book wording for anything else:
# under a plain ``str`` a mistyped constant degrades SILENTLY into the wrong
# disclosure, which is the class of failure this split exists to remove. As a
# Literal it is a type error instead, at no runtime cost.
_MissReason = Literal["no_bracket", "non_monotone"]

_MISS_NO_BRACKET: _MissReason = "no_bracket"
_MISS_NON_MONOTONE: _MissReason = "non_monotone"

# The closing sentence both rejection notes carry. Each note can appear without
# the other, so neither may lean on the other being present — but sharing it by
# COPYING is how the two would drift, which is the defect those notes exist to
# report. Only the noun differs (a "reading" was a usable value, a "candle" was a
# whole malformed row), so the noun is the parameter and the caveat is one text.
_WINDOW_COUNT_CAVEAT = (
    "does not contribute to a window's count above, though a day that also carried a "
    "surviving reading still counts once."
)

# The both-classes descriptor, shared for the _WINDOW_COUNT_CAVEAT reason: the
# empty-history raise and the staleness refusal's rejection note are the only two
# sentences that name both rejection classes in one clause, and copying the
# clause is precisely how the two would drift apart. Each site keeps its own
# head (the raise counts bare, the refusal counts "candle{s}"), so the shared
# text starts after the non-positive count.
_BOTH_REJECTION_CLASSES = (
    "with a non-positive reading and {inconsistent} with a non-positive low or an "
    "open or close outside the candle's own high/low range, the newest of them "
    "dated {newest}"
)


class DeribitError(VendorError):
    """Deribit was unreachable, or returned a payload this module cannot use.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing layer
    reacts by behaviour rather than by vendor, and the optional options_data
    category degrades to a sentinel instead of aborting the run.
    """


class _TransientDeribitFault(Exception):
    """Internal marker for a fault worth one retry; never escapes this module."""


class Contract(NamedTuple):
    """One parsed option from the chain snapshot."""

    expiry: str  # the raw Deribit expiry token, e.g. "28AUG26"
    expiry_dt: datetime
    strike: float
    is_call: bool
    mark_iv: float  # implied vol in percent, as Deribit quotes it
    underlying: float  # the expiry's forward, as Deribit quotes it per contract


class WingQuote(NamedTuple):
    """One interpolated point on the smile, with the two quotes behind it.

    ``strike_low`` / ``strike_high`` are the strike-adjacent listed contracts the
    IV was interpolated between. The report prints them so a point derived from an
    implausible pair — a sparse chain, or a stale quote sitting next to the wing —
    is visible instead of being indistinguishable from a tight bracket.
    """

    iv: float
    strike_low: float
    strike_high: float


class SkewSnapshot(NamedTuple):
    """The volatility surface read for one expiry.

    ``atm`` / ``call_25`` / ``put_25`` are None when the chain does not support
    that point — either no two strike-adjacent listed contracts *bracket* it, or a
    bracket was found and REFUSED because the quotes around it are not a monotone
    smile. Nothing is extrapolated: a wing the chain does not reach is reported
    missing rather than guessed, and one it reaches through a suspect quote is too.
    (Which of the two fired is carried by ``call_25_miss`` / ``put_25_miss`` below.)

    ``n_calls`` / ``n_puts`` count the contracts of each type on this expiry that
    yielded a usable delta — the quotes the interpolation actually had to work
    with, which need not be every contract Deribit listed for the expiry.

    ``is_fallback`` is True when this is NOT the expiry nearest the target tenor,
    i.e. the nearest one could not be used — it could not bracket both wings, or it
    carried no usable forward — and ``compute_skew`` stepped once past it. It exists
    so the report cannot keep calling the expiry "the eligible expiry closest to 30
    days" while showing a different one. Both candidates come from the same tenor
    band, so a fallback is still a ~30-day expiry.

    ``call_25_miss`` / ``put_25_miss`` carry WHICH guard refused a missing wing —
    see ``_interpolate_with_reason``. They are None when that wing is present, and
    default to None so a caller constructing a snapshot by hand still gets the
    conservative "no bracket" wording rather than an unfounded claim that a quote
    was suspect.
    """

    expiry: str
    days_to_expiry: float
    forward: float
    atm: WingQuote | None
    call_25: WingQuote | None
    put_25: WingQuote | None
    n_calls: int
    n_puts: int
    is_fallback: bool = False
    call_25_miss: _MissReason | None = None
    put_25_miss: _MissReason | None = None
    # How many contracts Deribit LISTED for the selected expiry, against which
    # n_calls/n_puts are the survivors. Without it "3 call quotes and 2 put quotes"
    # cannot distinguish a chain where Deribit listed four strikes from one where
    # it listed forty and this module's own open-interest policy removed
    # thirty-six — and that policy is what produces the thinned book
    # _WIDE_BRACKET_FRACTION then flags, so the reader was told the bracket was
    # wide but never that we were the reason. The DVOL half has a whole paragraph
    # defending exactly this disclosure for its own rejections; the chain half
    # discarded its rows in silence.
    #
    # Optional because compute_skew is public and builds snapshots from contracts
    # alone, which cannot know what was filtered out upstream. Only _fetch_chain,
    # which still holds the raw payload, can count it — so None means "not
    # measured" and the line simply omits the clause, rather than asserting a zero
    # that would read as a chain with nothing listed on it.
    listed_for_expiry: int | None = None

    @property
    def rr25(self) -> float | None:
        """25-delta risk reversal in vol points (call IV - put IV), or None.

        Negative means the put wing carries the higher implied vol.
        """
        if self.call_25 is None or self.put_25 is None:
            return None
        return self.call_25.iv - self.put_25.iv


class DvolSeries(NamedTuple):
    """The DVOL daily readings visible at curr_date, ascending by date.

    Never empty: ``_fetch_dvol`` raises rather than building one with no rows, so
    ``latest`` / ``latest_date`` always have a value to return.

    The field is named ``closes`` because that is the candle field it is read from
    (``[timestamp, open, high, low, close]``), but every line of the report calls
    these "readings": the candle dated today — or later, when the fetch itself
    crossed UTC midnight — is still open, so its value is the
    level so far rather than a settled close.

    ``dropped_non_positive`` counts the candles dated on or before ``curr_date``
    that ``_fetch_dvol`` rejected as broken — across the whole fetch span, which is
    wider than either statistics window, so the report states it as a fact about
    the fetch rather than as a shortfall in a particular window's count. It is
    carried out of the fetch rather than discarded there because
    the report's shortfall sentences name the calendar window as the cause ("the
    365 days ending X hold only N readings"), and a window this module emptied
    part of is not a sparse window. Without the count those sentences attribute a
    rejection here to the feed, and a reader deciding whether the sample is
    sufficient reads exactly that sentence.

    ``dropped_inconsistent`` counts the second rejection class on the same terms: a
    candle whose low is not positive, or whose open OR close falls outside its own
    high/low range — every field is tested, and every sentence that reports the
    rejection names each condition, so no
    description is narrower than the check. Kept as its own count
    and its own sentence rather than folded into ``dropped_non_positive``, because
    the two name different feed faults and therefore different operator actions —
    a non-positive close is a broken value, whereas a self-contradicting candle is
    most likely a CHANGED RESPONSE SHAPE (a reordered row), which is a vendor
    integration change rather than a data glitch. Merging them would also make the
    "came back zero or negative" wording false for half the candles it counted.
    """

    dates: list[str]
    closes: list[float]
    dropped_non_positive: int = 0
    newest_dropped_date: str | None = None
    dropped_inconsistent: int = 0
    newest_inconsistent_date: str | None = None
    # True when Deribit answered with a continuation cursor: the response is the
    # NEWEST page and older readings are missing. Carried out of the fetch for the
    # same reason the rejection counts are — the report's shortfall sentences name
    # the calendar window as the cause ("the 365 days ending X hold only N
    # readings"), and a window shortened by a truncated FETCH is not a sparse
    # window. Logged-only, this was the module's last degradation with no path to
    # the reader, so the one sentence they saw blamed the feed's publishing for
    # something the transport did.
    truncated: bool = False

    @property
    def latest(self) -> float:
        return self.closes[-1]

    @property
    def latest_date(self) -> str:
        return self.dates[-1]


class DvolReport(NamedTuple):
    """What ``_dvol_section`` hands back to the report builder.

    ``percentile_n`` travels with ``percentile`` so the reading line can state the
    sample the figure was computed over. Without it that sentence named only the
    window's calendar span, which reads as one observation per day — so a feed that
    stopped publishing a fortnight ago still produced "the Nth percentile of the
    365-day window", with the shortfall disclosed only in the section above and
    dropped by the first downstream summary.

    ``as_of_phrase`` is always populated, not only when the feed is late. It used
    to exist solely as a staleness warning, so a reading a day old — the ordinary
    case, at MAX_DATA_LAG_DAYS — reached the reading line with no date at all, and
    that line is the one clause downstream agents quote on its own.
    The "N days old" half is still appended only past the threshold.

    ``latest`` travels alongside for the same reason ``percentile_n`` does: the
    reading line states the DVOL LEVEL unconditionally now, so it needs the number
    rather than only the rank computed from it. Gating that clause on the
    percentile made a served-but-unrankable feed silent in the one sentence a
    downstream summary keeps — indistinguishable from a DVOL half that never
    arrived, in a report whose body carries a perfectly usable level.

    ``latest_is_open`` travels for the same reason again: once the reading line
    states the level, a still-open candle's "this is the level so far" caveat has
    to travel with it, or the survivable sentence quotes a provisional number as a
    settled one — the same hop the rest of this module is built around, one figure
    further on.
    """

    markdown: str
    percentile: float | None
    percentile_n: int
    as_of_phrase: str
    latest: float
    latest_is_open: bool


def _utc_now() -> datetime:
    """The single UTC clock source (tests patch this one function)."""
    return datetime.now(timezone.utc)


# The flattening itself (which characters, why a space and not deletion, why
# "<"/">" and intraword "_" survive) is the one shared definition in
# ``utils.sanitize_untrusted``; this module keeps the reasoning for WHICH of its
# fragments go through it. The deletion-fuses-tokens hazard was found here:
# "BTC|USD" collapsed to "BTCUSD", which normalize_symbol resolves to BTC, so a
# symbol this vendor had always refused started producing a confident BTC report.
_MAX_UNTRUSTED_CHARS = MAX_UNTRUSTED_CHARS


def _sanitize(text: object, *, limit: int | None = None) -> str:
    """Flatten a fragment this module did not author so it cannot forge structure.

    ``limit`` caps the result, and is passed ONLY where the untrusted fragment is
    ISOLATED: Deribit's ``error.message``, a raw candle row echoed into the
    malformed-shape error, and the caller-supplied ``asset``. It is not passed
    when flattening a whole exception message, because most of that string is
    this module's own carefully-worded diagnostic: capping there truncated
    "...points at a response-shape change rather than an empty market" one
    clause from the end, cutting off the very distinction the sentence exists to
    draw.

    Several inputs reach the report without this module choosing their contents:
    Deribit's JSON-RPC ``error.message``, a raw candle row, and the caller-supplied
    ``asset`` (``curr_date`` is judged and echoed by the shared date refusal
    before it gets this far). Interpolated raw, any can close the sentence it
    sits in and open new blocks — a second ``_Reading:_`` line, a fresh ``##``
    heading, a fabricated DVOL level — and the forged copy renders ABOVE the
    real one, so a downstream summariser keeping "the Reading line" quotes the
    forgery.

    Applied where the fragment enters the message, not only where the report
    renders it: an optional category's failure is handed to the model by
    ``route_to_vendor`` as ``DATA_UNAVAILABLE: ... ({error})``, so a vendor string
    embedded in a RAISED error reaches the prompt just as surely as a rendered
    one, and sanitising only the renderers would leave that path open.
    """
    return sanitize_untrusted(text, limit=limit)


def _request(endpoint: str, params: dict) -> object:
    """GET a Deribit public endpoint and return its ``result`` payload.

    Deribit answers a malformed request with HTTP 400 *and* a JSON-RPC ``error``
    object carrying the actual reason, so the body is decoded and inspected before
    the status is judged — otherwise every parameter mistake would surface as a
    bare "400 Client Error" with no clue which parameter was wrong. Status codes
    are handled explicitly here; ``raise_for_status`` is deliberately never called
    (see the 4xx branch for why).

    Retries once on a transient fault: a network error, an undecodable body, or a
    5xx that carries no JSON-RPC error object. Anything Deribit explains — a
    JSON-RPC error object at any status — is deterministic and raises immediately,
    as does any other 4xx; an HTTP 429 raises ``VendorRateLimitError`` so the
    router treats it as a throttle rather than as a broken vendor.
    """
    url = f"{DERIBIT_BASE}/{endpoint}"
    last_error: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                raise VendorRateLimitError(f"Deribit rate-limited the {endpoint} request")
            try:
                payload = response.json()
            except ValueError as e:
                # A CDN/WAF error page rather than the API: transient enough to be
                # worth the one retry.
                raise _TransientDeribitFault(f"undecodable response body ({e})") from e
            if isinstance(payload, dict) and payload.get("error"):
                error = payload["error"]
                detail = error.get("message", error) if isinstance(error, dict) else error
                if isinstance(error, dict) and isinstance(error.get("data"), dict):
                    detail = f"{detail} ({error['data']})"
                # Vendor-authored text. This message is rendered into the report
                # AND, when it escapes as the vendor's failure, into
                # route_to_vendor's DATA_UNAVAILABLE sentinel — both of which are
                # read by an LLM, so it is flattened here at the point it enters
                # the message rather than at each of those two sites.
                raise DeribitError(
                    f"Deribit rejected the {endpoint} request: "
                    f"{_sanitize(detail, limit=_MAX_UNTRUSTED_CHARS)}"
                )
            if response.status_code >= 500:
                raise _TransientDeribitFault(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                # Deterministic rejection with no JSON-RPC error object to explain
                # it — a WAF block, a renamed endpoint. Raised here rather than
                # left to raise_for_status(), whose requests.HTTPError IS a
                # RequestException and would therefore be caught by the retry
                # handler below: the request would be slept on and repeated to no
                # purpose, then reported as "unreachable" when the vendor answered
                # perfectly clearly the first time.
                raise DeribitError(
                    f"Deribit rejected the {endpoint} request with HTTP {response.status_code}"
                )
            if not isinstance(payload, dict) or "result" not in payload:
                raise DeribitError(
                    f"Deribit {endpoint} response has no 'result' field "
                    f"(got a JSON {type(payload).__name__})"
                )
            return payload["result"]
        except (requests.RequestException, _TransientDeribitFault) as e:
            last_error = e
            if attempt + 1 < _RETRY_ATTEMPTS:
                logger.warning(
                    "Deribit %s request failed (%s); retrying in %ss",
                    endpoint,
                    e,
                    _RETRY_DELAY_SECONDS,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
    # "did not return a usable response", not "unreachable": both retryable faults
    # can arrive on an answered request — _TransientDeribitFault covers an
    # undecodable body (only the 429 check precedes it, so an HTTP 200 serving a
    # CDN error page lands here) and a 5xx, where the server plainly responded.
    # Only requests.RequestException is genuinely a reachability failure, and
    # naming it for all three sends an operator hunting an outage that a WAF
    # interception will not explain.
    raise DeribitError(
        f"Deribit {endpoint} did not return a usable response after {_RETRY_ATTEMPTS} "
        f"attempts: {last_error}"
    ) from last_error


def _is_finite_number(x: object) -> bool:
    """True only for a real, finite number (not a bool, NaN, Infinity, or a huge int).

    bool is an int subclass, so a JSON ``true``/``false`` must not pass as a price
    or a vol. ``json`` decodes ``NaN``/``Infinity`` to non-finite floats, which
    would poison every comparison downstream (every comparison with NaN is False)
    and render as a literal "nan" figure.

    ``math.isfinite`` RAISES on an int too large to convert to a float, and a JSON
    integer literal has no bound, so ``json.loads`` can hand back an
    arbitrary-precision int that makes this predicate throw rather than answer.
    That inverts its contract at one of the input classes it exists to turn away,
    and it does so at every call site: ``parse_chain`` documents "rows that cannot
    be used are skipped, not fatal" and would instead lose all six chain figures to
    a single row, while ``_fetch_dvol``'s own malformed-candle diagnostic would be
    replaced by a bare "int too large to convert to float" — a message that reaches
    the model through route_to_vendor's sentinel and sends an operator to
    ``math.isfinite`` as though the bug were here.
    """
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


# ---------------------------------------------------------------------------
# Skew: pure functions (no I/O), so the maths is unit-testable against fixtures.
# ---------------------------------------------------------------------------


def parse_expiry(token: str) -> datetime | None:
    """Parse a Deribit expiry token ("28AUG26", "5AUG26") to its UTC expiry instant.

    Returns None for anything that is not a ``DMMMYY`` / ``DDMMMYY`` token, so a
    contract naming scheme this parser does not understand is skipped rather than
    mis-dated. Deribit settles at 08:00 UTC on the expiry date.
    """
    token = token.strip().upper()
    if len(token) not in (6, 7):
        return None
    day_part, month_part, year_part = token[:-5], token[-5:-2], token[-2:]
    month = _MONTHS.get(month_part)
    if month is None or not day_part.isdigit() or not year_part.isdigit():
        return None
    try:
        return datetime(
            2000 + int(year_part),
            month,
            int(day_part),
            _EXPIRY_HOUR_UTC,
            tzinfo=timezone.utc,
        )
    except ValueError:  # e.g. 31FEB26
        return None


def parse_instrument_name(name: object) -> tuple[str, str, datetime, float, bool] | None:
    """Parse ``BTC-28AUG26-100000-C`` into ``(base, expiry token, expiry, strike, is_call)``.

    Returns None — the caller skips the contract — for anything that does not fit
    that four-part shape: a future/perpetual name, a combo leg, the ``d``-separated
    decimal-strike form Deribit uses on currencies this module does not serve, or a
    strike that is not a positive finite number.

    The leading base segment is RETURNED rather than unpacked into ``_``. It is the
    only field in the whole ``get_book_summary_by_currency`` payload that says which
    underlying the rows describe — the response carries no currency of its own — so
    a caller that drops it has nothing left to check the book it is reading against
    the book it asked for. That is the one integration fault in this module whose
    symptom is six ordinary-looking numbers under the wrong heading rather than an
    error, and the analyst prompt disarms the only downstream cross-check by
    forbidding the model to reconcile the forward against spot.

    Normalised like ``expiry_token``, so the caller compares two canonical strings
    rather than re-deciding case and whitespace at the comparison site.
    """
    if not isinstance(name, str):
        return None
    parts = name.split("-")
    if len(parts) != 4:
        return None
    base, expiry_token, strike_text, option_type = parts
    option_type = option_type.strip().upper()
    if option_type not in ("C", "P"):
        return None
    expiry_dt = parse_expiry(expiry_token)
    if expiry_dt is None:
        return None
    try:
        strike = float(strike_text)
    except ValueError:
        return None
    if not math.isfinite(strike) or strike <= 0:
        return None
    return (
        base.strip().upper(),
        expiry_token.strip().upper(),
        expiry_dt,
        strike,
        option_type == "C",
    )


def _canonical_currency(currency: str) -> str:
    """The form a caller's currency must take to be compared against a parsed base.

    One definition, because two sites match a base against a requested currency —
    ``parse_chain``'s row filter and ``_fetch_chain``'s listed-contract census — and
    a normalisation rule that lives at both of them is one a later change updates at
    one and misses at the other. That is the precise failure the base check itself
    exists to prevent, so it must not be reintroduced by the check's own plumbing.

    Matches what ``parse_instrument_name`` returns, which is normalised the same way.
    """
    return currency.strip().upper()


def parse_chain(rows: object, now: datetime, currency: str) -> list[Contract]:
    """Turn a ``get_book_summary_by_currency`` result into usable Contracts.

    Rows that cannot be used are skipped, not fatal: the chain carries hundreds of
    contracts and a handful of unquoted or oddly-named ones is normal. A row is
    kept only with a parseable option name whose base is ``currency``, a strictly
    positive finite mark IV, underlying price and open interest, and an expiry
    still in the future at ``now``.

    ``currency`` is required rather than defaulted to "no check". A default would
    leave the one call site that matters free to drop the argument and lose the
    check silently, which is how this module has previously watched a guard be
    reverted under a green suite: a test calling this function directly cannot see
    what the caller passes.

    The base check is what stops a wrong-currency book being read as the requested
    one. The payload names its underlying nowhere else, so without it a vendor,
    CDN or routing fault that answers an ETH request with the BTC book yields a
    report headed ``## Options Volatility — ETH`` carrying BTC's forward and BTC's
    smile: six ordinary-looking numbers, no log line, no caveat, no rejection
    tally. It also turns away the linear ``BTC_USDC-...`` names, whose four
    ``-``-separated segments would otherwise parse and interleave a second,
    differently-margined order book into one smile.

    Open interest is required because the two guards in ``interpolate_iv_at_delta``
    defend the wing against a bad quote that *inverts* the smile, and nothing else
    defends it against one that merely sits there being wrong. The strikes nobody
    holds are exactly the ones carrying a stale or modelled mark: Deribit still
    publishes a confident ``mark_iv`` for a contract with no open interest, no
    volume and a bid/ask spanning two orders of magnitude, and such a quote can be
    perfectly monotone with its neighbours — so it passes both guards, gets
    interpolated, and prints the very strikes a reader expects. Requiring a
    contract someone actually holds costs nothing on a liquid chain and removes
    that whole class of quote from the smile.

    Raises DeribitError only when the payload is not a list at all — that is a
    response-shape change, not a data quirk.
    """
    if not isinstance(rows, list):
        raise DeribitError(
            f"Deribit returned a {type(rows).__name__} for the options chain, expected a list"
        )
    wanted = _canonical_currency(currency)
    contracts: list[Contract] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_instrument_name(row.get("instrument_name"))
        if parsed is None:
            continue
        base, expiry_token, expiry_dt, strike, is_call = parsed
        if base != wanted:
            continue
        if expiry_dt <= now:
            continue
        mark_iv = row.get("mark_iv")
        underlying = row.get("underlying_price")
        open_interest = row.get("open_interest")
        if not _is_finite_number(mark_iv) or mark_iv <= 0:
            continue
        if not _is_finite_number(underlying) or underlying <= 0:
            continue
        if not _is_finite_number(open_interest) or open_interest <= 0:
            continue
        contracts.append(
            Contract(expiry_token, expiry_dt, strike, is_call, float(mark_iv), float(underlying))
        )
    return contracts


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_delta(
    forward: float,
    strike: float,
    iv_pct: float,
    years_to_expiry: float,
    is_call: bool,
) -> float | None:
    """Black-76 forward delta of an option, or None if the inputs are degenerate.

    Undiscounted (rate = 0): Deribit reports ``interest_rate: 0.0`` on every
    listed option, so the discount factor is 1 and applying it would only add a
    field this module would then have to trust. Note that a non-zero rate would
    not merely rescale the answer — it shifts *which* strikes bracket the 0.25
    wing — so if Deribit ever starts quoting a real rate, this needs the factor.

    Call delta is ``N(d1)`` and put delta ``N(d1) - 1``, so a call delta lands in
    (0, 1) and a put delta in (-1, 0).
    """
    if not all(_is_finite_number(v) for v in (forward, strike, iv_pct, years_to_expiry)):
        return None
    if forward <= 0 or strike <= 0 or iv_pct <= 0 or years_to_expiry <= 0:
        return None
    sigma = iv_pct / 100.0
    denominator = sigma * math.sqrt(years_to_expiry)
    moneyness = forward / strike
    # The positivity tests above are made on the INPUTS; these two quantities are
    # derived from them and can still collapse to zero by underflow. A denormal
    # mark_iv (1e-322) divides to sigma == 0.0, and a denormal forward divides to
    # moneyness == 0.0 — the first a ZeroDivisionError, the second a math domain
    # error, and neither is a DeribitError, so neither is caught by the
    # unusable-contract handling that skips a bad row. One such quote would
    # instead escape to _try_fetch and cost all six chain figures, reported to the
    # reader as "unavailable — float division by zero". Returning None puts them
    # back under this function's own "degenerate inputs are skipped" contract.
    if denominator == 0.0 or moneyness <= 0.0:
        return None
    d1 = (math.log(moneyness) + 0.5 * sigma * sigma * years_to_expiry) / denominator
    return _normal_cdf(d1) if is_call else _normal_cdf(d1) - 1.0


def interpolate_iv_at_delta(
    quotes: list[tuple[float, float, float]],
    target_delta: float,
) -> WingQuote | None:
    """Interpolate IV at ``target_delta`` between two STRIKE-ADJACENT quotes.

    ``quotes`` are ``(strike, delta, iv)`` triples for ONE option type on ONE
    expiry. The scan walks strikes in order and only ever interpolates between
    neighbouring strikes.

    Two guards, because one collapsed or stale ``mark_iv`` corrupts both that
    contract's IV *and* its delta:

    * **Order by strike, never by delta.** A bad quote's delta lands far from
      where its strike sits on the smile, so a delta-ordered scan lets it drift
      next to the target and pair with a non-neighbour — interpolating the wing
      between two strikes nowhere near each other.
    * **Require the bracket to sit in a monotone stretch of the smile.** Strike
      adjacency alone does not save the wing from a bad quote sitting *inside*
      the bracket it legitimately borders; that case returns a number no sane
      chain supports, printed with exactly the strikes a reader would expect, so
      there is no visible symptom at all. Delta falls with strike on any
      well-formed smile, for calls and puts alike, and a corrupt quote breaks
      that at its own strike (verified: a 61000 put collapsing to 0.5% IV flipped
      RR25 from -4.74 to +2.33 — the opposite volatility regime — while still
      printing the expected 61000/62000 bracket).

    Returns None when no strike-adjacent pair brackets the target, or when the
    quotes around that pair are not a monotone smile. Both mean the chain does not
    support this point, and inventing it is the fabrication this codebase refuses
    everywhere else — but they are not the same FACT about the chain, so
    ``_interpolate_with_reason`` reports which one fired and the report says so.
    """
    return _interpolate_with_reason(quotes, target_delta)[0]


def _interpolate_with_reason(
    quotes: list[tuple[float, float, float]],
    target_delta: float,
) -> tuple[WingQuote | None, _MissReason | None]:
    """``interpolate_iv_at_delta``, plus WHICH guard refused the point.

    Split out rather than folded into the public function's return type so that
    function keeps the plain ``WingQuote | None`` signature its callers and the
    maths tests are written against.

    The distinction is worth carrying because the two misses are different facts
    about the market and lead to different reads. ``_MISS_NO_BRACKET`` is a THIN
    BOOK: no two strike-adjacent quotes straddle the target delta, which is an
    ordinary property of a sparse expiry. ``_MISS_NON_MONOTONE`` is a SUSPECT
    QUOTE: a bracket was found and then rejected because delta rises with strike
    across it, which no well-formed smile does — the guard that caught a 61000 put
    collapsing to 0.5% IV and flipping RR25 from -4.74 to +2.33. Collapsing them
    into one "or" sentence was the module's only remaining site where two causes
    with different operator actions shared one message; ``_fetch_chain`` and
    ``_wing_line`` both split theirs on exactly this ground.
    """
    ordered = sorted(quotes)
    # strict=False is the point of this zip: pairing each quote with its successor
    # deliberately walks one shorter than the list.
    for index, (low, high) in enumerate(zip(ordered, ordered[1:], strict=False)):
        strike_low, delta_a, iv_a = low
        strike_high, delta_b, iv_b = high
        # Compare against the pair's own span rather than assuming a direction, so
        # an inverted step cannot skip a genuine bracket before the monotonicity
        # guard below gets to judge it.
        if not min(delta_a, delta_b) <= target_delta <= max(delta_a, delta_b):
            continue
        if not _is_locally_monotone(ordered, index):
            return None, _MISS_NON_MONOTONE
        span = delta_b - delta_a
        if span == 0:
            # Both neighbours sit exactly on the target; neither is more correct
            # than the other, so take their midpoint.
            iv = (iv_a + iv_b) / 2.0
        else:
            iv = iv_a + (target_delta - delta_a) / span * (iv_b - iv_a)
        return WingQuote(iv, strike_low, strike_high), None
    return None, _MISS_NO_BRACKET


def _is_locally_monotone(ordered: list[tuple[float, float, float]], index: int) -> bool:
    """True when delta never rises with strike across a bracket and its neighbours.

    Scoped to the bracket plus one quote on each side rather than the whole
    expiry: a rounding inversion between two deep-wing strikes says nothing about
    a bracket far away, and letting it veto the entire surface would trade a
    silent wrong answer for a needless missing one. Equal deltas pass — two
    adjacent strikes can round to the same delta far out of the money — so only a
    genuine rise fails the check.
    """
    window = ordered[max(0, index - 1) : index + 3]
    return all(a[1] >= b[1] for a, b in zip(window, window[1:], strict=False))


def rank_expiries(contracts: list[Contract], now: datetime) -> list[str]:
    """Expiry tokens inside the eligible tenor band, best-first by closeness to 30 days.

    Eligible means ``MIN_ELIGIBLE_DTE_DAYS <= DTE <= MAX_ELIGIBLE_DTE_DAYS``, i.e.
    within ``MAX_TENOR_DISTANCE_DAYS`` of the 30-day target and never inside the
    ``MIN_DTE_DAYS`` pin-noise floor. The ceiling is what stops a thinned book from
    answering the ~30-day question with a 96-day expiry; the floor is why a 3-day
    expiry is not the answer either. Returns an empty list when nothing qualifies,
    and ``compute_skew`` then reports no skew rather than a figure from an
    unrelated tenor.

    Ties (two expiries equidistant from the 30-day target) go to the longer-dated
    one, consistent with the reason the floor exists at all: closer to expiry is
    the noisier side.

    A ranking rather than a single winner because a sparse or non-monotone stretch
    around the wing is a property of one expiry, not of the surface: the neighbour
    is usually clean, and ``compute_skew`` falls through to it rather than
    reporting no skew at all.
    """
    tenors: dict[str, float] = {}
    for contract in contracts:
        days = (contract.expiry_dt - now).total_seconds() / 86400.0
        if MIN_ELIGIBLE_DTE_DAYS <= days <= MAX_ELIGIBLE_DTE_DAYS:
            tenors[contract.expiry] = days
    return sorted(tenors, key=lambda token: (abs(tenors[token] - TARGET_DTE_DAYS), -tenors[token]))


def _points_read(snapshot: SkewSnapshot) -> int:
    """How many of the three smile points (ATM, 25Δ call, 25Δ put) this one carries.

    Used only to choose between two INCOMPLETE candidate expiries — a complete one
    returns immediately — so it never competes with tenor closeness for a surface
    the report could have used in full.
    """
    return sum(p is not None for p in (snapshot.atm, snapshot.call_25, snapshot.put_25))


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_skew(contracts: list[Contract], now: datetime) -> SkewSnapshot:
    """Compute the ATM and 25-delta wing vols for the ~30-day expiry.

    Tries the nearest expiry and, if that one cannot be used — it fails to bracket
    BOTH 25-delta wings, or it carries no usable forward — the next-nearest, and
    stops there. A sparse or non-monotone stretch around the
    wing is a property of one expiry rather than of the surface, and its immediate
    neighbour is usually clean, so one labelled step is a far better input to a risk
    debate than the silence of an all-``n/a`` skew section. When neither candidate
    brackets both wings, the one carrying MORE of the three smile points is
    returned — ties going to the nearer expiry — so the forward, ATM and whichever
    wings exist are still reported, and the richer of two incomplete surfaces is
    the one the reader gets.

    Both candidates come from ``rank_expiries``, so both are already inside the
    eligible tenor band; the step can only ever move to another ~30-day expiry.
    Deliberately NOT a walk down the whole ranking either, for the same reason the
    band exists: a risk reversal is not tenor-invariant, and every downstream agent
    reads this number as a ~30-day figure.

    Raises DeribitError when no listed expiry falls inside the eligible tenor band,
    or when no candidate has a usable forward — the cases where there is no surface
    to read at all. A merely *incomplete* surface is not an error: a wing the chain
    cannot bracket comes back as None on the snapshot for the report to disclose.
    """
    ranked = rank_expiries(contracts, now)
    if not ranked:
        raise DeribitError(
            f"No listed expiry falls between {MIN_ELIGIBLE_DTE_DAYS:.0f} and "
            f"{MAX_ELIGIBLE_DTE_DAYS:.0f} days out in the options chain, so no expiry can be "
            f"read as the {TARGET_DTE_DAYS}-day skew this report states"
        )
    first_error: DeribitError | None = None
    best: SkewSnapshot | None = None
    for expiry in ranked[:_MAX_EXPIRY_CANDIDATES]:
        try:
            snapshot = _snapshot_for_expiry(contracts, expiry, now, is_fallback=expiry != ranked[0])
        except DeribitError as e:
            # No usable forward on this expiry; try the next rather than losing the
            # whole chain half to one bad expiry. The first failure is kept so the
            # error the caller sees is about the expiry it would have used.
            if first_error is None:
                first_error = e
            continue
        if snapshot.call_25 is not None and snapshot.put_25 is not None:
            return snapshot
        # Neither candidate is complete so far, so keep whichever surface carries
        # more of the three points. Latching the FIRST incomplete snapshot handed
        # back an all-n/a section while the neighbouring expiry sat there with an
        # ATM point and a wing — precisely "the silence of an all-n/a skew
        # section" this second candidate exists to avoid. RR25 is None either way,
        # so what moves is the body: the forward, the ATM line, one wing and the
        # expiry the reading line names.
        #
        # STRICTLY greater, so a tie leaves the nearer expiry in place: closeness
        # to the target tenor remains the tiebreak and only a genuinely richer
        # surface displaces it.
        if best is None or _points_read(snapshot) > _points_read(best):
            best = snapshot
    if best is not None:
        return best
    if first_error is not None:
        raise first_error
    # Unreachable: ranked is non-empty, so the loop ran at least once, and every
    # iteration either returns, records a candidate in `best`, or records a
    # first_error.
    # Spelled out rather than silenced with a type: ignore, so a future edit that
    # breaks that reasoning fails with a message instead of a None-raise.
    tried = len(ranked[:_MAX_EXPIRY_CANDIDATES])
    raise DeribitError(
        f"No usable expiry among the {tried} candidate{'' if tried == 1 else 's'} tried"
    )


def _snapshot_for_expiry(
    contracts: list[Contract], expiry: str, now: datetime, is_fallback: bool = False
) -> SkewSnapshot:
    """Read the surface for ONE expiry token.

    The forward is the median ``underlying_price`` across the expiry's contracts.
    Deribit stamps each row with the index at its own quote instant, so the values
    differ in the last decimals across one snapshot; the median ignores that
    jitter and any single outlying row.
    """
    selected = [c for c in contracts if c.expiry == expiry]
    expiry_dt = selected[0].expiry_dt
    days_to_expiry = (expiry_dt - now).total_seconds() / 86400.0
    years = (expiry_dt - now).total_seconds() / _SECONDS_PER_YEAR

    forward = _median([c.underlying for c in selected])
    if not math.isfinite(forward) or forward <= 0:
        raise DeribitError(f"Deribit {expiry} contracts carry no usable forward price")

    call_quotes: list[tuple[float, float, float]] = []
    put_quotes: list[tuple[float, float, float]] = []
    for contract in selected:
        delta = black_scholes_delta(
            forward, contract.strike, contract.mark_iv, years, contract.is_call
        )
        if delta is None:
            # Unreachable via _fetch_chain — parse_chain already requires a
            # positive finite IV and underlying, and rank_expiries a positive
            # tenor — but compute_skew is a public pure function and a caller can
            # hand it contracts this module did not build.
            continue
        (call_quotes if contract.is_call else put_quotes).append(
            (contract.strike, delta, contract.mark_iv)
        )

    # ATM is the 50-delta point. The call and put curves cross it at the same
    # strike (call delta 0.5 and put delta -0.5 both mean d1 = 0), so the put
    # curve is an exact substitute when the call side cannot bracket it.
    atm = interpolate_iv_at_delta(call_quotes, ATM_DELTA)
    if atm is None:
        atm = interpolate_iv_at_delta(put_quotes, -ATM_DELTA)

    # The 25Δ wings keep the reason their point is missing; ATM deliberately does
    # not. ATM is attempted on TWO curves (calls at +0.5, then puts at -0.5), so
    # there is no single guard to name — which is exactly why _atm_line keeps the
    # combined "or" wording that the wing lines no longer use.
    call_25, call_25_miss = _interpolate_with_reason(call_quotes, WING_DELTA)
    put_25, put_25_miss = _interpolate_with_reason(put_quotes, -WING_DELTA)

    return SkewSnapshot(
        expiry=expiry,
        days_to_expiry=days_to_expiry,
        forward=forward,
        atm=atm,
        call_25=call_25,
        put_25=put_25,
        n_calls=len(call_quotes),
        n_puts=len(put_quotes),
        is_fallback=is_fallback,
        call_25_miss=call_25_miss,
        put_25_miss=put_25_miss,
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _fetch_chain(currency: str, now: datetime) -> SkewSnapshot:
    result = _request("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    contracts = parse_chain(result, now, currency)
    if not contracts:
        # Two different states, told apart because the operator action differs and
        # because "every row failed the checks" is vacuous when there were no rows
        # to fail them. Both are abnormal on a live chain: Deribit lists options
        # for BTC and ETH continuously, so an EMPTY list is a vendor-side or
        # routing fault, whereas a fully-rejected list points at a response-shape
        # change (a renamed field drops every row at once). Neither is a quiet
        # market, which is what an unqualified "no contracts" would be read as.
        if isinstance(result, list) and not result:
            raise DeribitError(
                f"Deribit returned an empty {currency} option chain — the endpoint answered "
                f"with no contracts at all, which on a continuously-listed chain points at a "
                f"vendor-side fault rather than an empty market"
            )
        raise DeribitError(
            # The base-currency check belongs in this enumeration for the reason
            # every other member does: this sentence is the operator's only account
            # of why a non-empty response yielded nothing, and a check missing from
            # it sends them looking at the wrong field. Its conclusion is widened to
            # match — a book that is entirely the wrong currency is not a changed
            # response shape, and naming only that cause would point at a renamed
            # field when the fault is a misrouted request.
            f"Deribit returned no usable {currency} option contracts — every row failed the "
            f"instrument-name, base-currency, mark-IV, underlying or open-interest checks, "
            f"which on a live chain points at a response-shape change or a book for another "
            f"currency rather than an empty market"
        )
    snapshot = compute_skew(contracts, now)
    # Counted from the RAW payload, which is the only place the filtered-out rows
    # still exist: parse_chain returns survivors, so by the time compute_skew picks
    # an expiry the denominator is gone. Counted here rather than threaded through
    # parse_chain so that function keeps its single job and its signature.
    wanted = _canonical_currency(currency)
    listed = 0
    for row in result:
        if not isinstance(row, dict):
            continue
        parsed = parse_instrument_name(row.get("instrument_name"))
        if parsed is not None and parsed[0] == wanted and parsed[1] == snapshot.expiry:
            listed += 1
    return snapshot._replace(listed_for_expiry=listed)


def _fetch_dvol(currency: str, curr_dt: datetime, today: str) -> DvolSeries:
    """Fetch the DVOL daily readings for the window ending at ``curr_date``.

    The request window ends at the last instant of ``curr_date`` (UTC) and the
    parsed rows are filtered again on the date string, so a past ``curr_date``
    cannot pick up a later reading even if Deribit widened the range it honours.

    The span is driven by the longer of the two statistics windows (the
    percentile's), since the shorter range window is a subset of it.

    Raises when the newest usable reading is older than MAX_DVOL_STALENESS_DAYS, so
    a stalled feed reports its half absent rather than headlining a months-old print.

    ``today`` — the UTC date of the clock — is passed in rather than read here. The
    staleness bound is measured from ``min(curr_date, today)`` because a feed cannot
    be behind a date that has not arrived, and that is the identical rule
    ``_dvol_section`` applies to the lag note it renders. Taking a fourth clock
    reading instead would let the two drift apart by a fetch envelope, which is how
    a report comes to refuse a reading on one arithmetic and date it on another.
    """
    # max() rather than naming the percentile window directly: the span only has
    # to cover the LONGER of the two statistics windows, and hardcoding which one
    # that is turns a future change to DVOL_WINDOW_DAYS into a range computed over
    # a silently truncated sample and still labelled with its full span.
    window_start = curr_dt - timedelta(
        days=max(DVOL_WINDOW_DAYS, DVOL_PERCENTILE_WINDOW_DAYS) + _DVOL_FETCH_BUFFER_DAYS
    )
    end = curr_dt.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
    result = _request(
        "get_volatility_index_data",
        {
            "currency": currency,
            "resolution": DVOL_RESOLUTION,
            "start_timestamp": int(window_start.replace(tzinfo=timezone.utc).timestamp() * 1000),
            "end_timestamp": int(end.timestamp() * 1000),
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise DeribitError(
            "Deribit volatility-index response has no 'data' list "
            "(the endpoint's response shape may have changed)"
        )
    truncated = result.get("continuation") is not None
    if truncated:
        # Unreachable at this module's span (see _DVOL_MAX_CANDLES_PER_RESPONSE).
        # If Deribit ever lowers the cap this fires: the response is the newest
        # page and older readings are missing, so the percentile sample is shorter
        # than its window claims. Logged rather than fatal — the newest readings
        # always survive, so no figure is wrong, only under-sampled — but it is
        # also carried into the report now. "The report already states that sample
        # by count" was the same defence this module rejected for the rejection
        # tallies: a count without a cause is read as a fact about the feed by the
        # sentence around it, which names the calendar window as the reason.
        logger.warning(
            "Deribit truncated the %s DVOL history (continuation cursor set); the "
            "percentile sample is shorter than the %d-day window",
            currency,
            DVOL_PERCENTILE_WINDOW_DAYS,
        )
    curr_date = curr_dt.strftime("%Y-%m-%d")
    by_date: dict[str, float] = {}
    # The candle timestamp behind each kept day, so a repeated day resolves by
    # which candle is actually newer rather than by which arrived last. Deribit
    # returns candles ascending today and the comment below used to rest on that,
    # but nothing verifies it: were the order ever to reverse, a day carrying both
    # a partial and a settled candle would keep the PARTIAL one, and neither the
    # date nor the count in the report would change.
    ts_by_date: dict[str, float] = {}
    skipped_non_positive = 0
    # The newest rejected day, so the report can say whether the rejections are
    # what the data-lag gap is made of. Without it a genuine stall plus one
    # unrelated rejection a year ago renders identically to a feed whose every
    # recent print was rejected — two different operator actions.
    newest_skipped: str | None = None
    # The same pair for the second rejection class (see the consistency check
    # below), counted separately because it names a different fault.
    skipped_inconsistent = 0
    newest_inconsistent: str | None = None
    for row in result["data"]:
        # [timestamp_ms, open, high, low, close]; a shape change here would
        # silently reinterpret which number is the close, so it is fatal. Note
        # what this test can and cannot see: it catches a row of the wrong LENGTH
        # or carrying a non-number, but a permutation of the five values passes it
        # untouched. The consistency check below is what covers that case.
        if not isinstance(row, list) or len(row) != 5 or not all(_is_finite_number(v) for v in row):
            raise DeribitError(
                # Capped here rather than where this is rendered: the render sites
                # flatten a whole exception message without a length limit (so this
                # module's own long diagnostics survive intact), and the raw row is
                # unbounded vendor bytes. A response carrying a megabyte-long cell
                # would otherwise reach the report twice — once in the body line and
                # once in the Reading clause — burying the report's own sentences,
                # which is exactly what _MAX_UNTRUSTED_CHARS exists to prevent.
                f"Malformed DVOL candle {_sanitize(repr(row), limit=_MAX_UNTRUSTED_CHARS)} "
                f"(the endpoint's candle shape may have changed)"
            )
        try:
            day = datetime.fromtimestamp(row[0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError) as e:
            raise DeribitError(f"Out-of-range DVOL candle timestamp {row[0]!r}") from e
        if row[4] <= 0:
            # A zero or negative implied-vol index is not a low reading, it is a
            # broken one, and parse_chain rejects a non-positive mark_iv or
            # underlying for exactly this glitch class. Left in, a single 0.0
            # candle renders as "0.00 ... 3rd percentile" — a maximally
            # vol-is-cheap read — with no caveat at all. Skipped rather than
            # fatal, like an unusable chain row: one bad candle should not cost a
            # year of history, and dropping the newest one simply leaves an older
            # reading as the latest, which the report dates and ages honestly.
            if day <= curr_date:
                # Only an IN-WINDOW candle counts toward the corrupt-feed verdict.
                # The date filter below is there because Deribit may honour a wider
                # range than asked, so a non-positive candle dated after curr_date
                # was never a candidate for by_date — counting it let "every
                # reading was non-positive" fire on a window that simply held no
                # readings, naming the wrong cause exactly as the messages this
                # round rewrote elsewhere did.
                skipped_non_positive += 1
                if newest_skipped is None or day > newest_skipped:
                    newest_skipped = day
            logger.warning(
                "Skipping non-positive Deribit DVOL close %r dated %s for %s",
                row[4],
                day,
                currency,
            )
            continue
        # The candle must satisfy its own arithmetic: a positive low, with the open
        # and the close both inside its high/low range.
        # Until this check the row was guarded on ONE side only — a close of 0.0
        # was refused as broken while a close of 3000.0 sitting in a 39.0/41.0
        # candle was published as fact, and 3000% reads to a model as a real
        # extreme-vol regime rather than as garbage. It set the headline level, the
        # 30-day max and the percentile basis, in all three of the places an
        # absence is otherwise disclosed, with no caveat anywhere. The row carries
        # the evidence that refutes it.
        #
        # BOTH price fields are tested, not only the close this module reads: an
        # open outside the range is the same evidence of the same fault. Every
        # sentence describing the rejection therefore names each condition this
        # tests, so the wording cannot be narrower than the test — the defect that
        # first produced this guard, and which the guard's own first draft repeated.
        #
        # The positivity term is keyed off the LOW alone, and covers all four prices
        # because of the ordering terms beside it: high >= open >= low and
        # high >= close >= low, so low > 0 makes every price positive. Without it
        # the row was still guarded on one side only, one level down — the close
        # was checked for sign and the other three were not, so a candle carrying
        # open -5 and low -10 beside a close of 40.5 passed both guards and
        # published, though a negative low on an implied-vol index is exactly the
        # sign or field-mapping fault this check exists to catch, and the row again
        # carries the evidence that refutes it. contrib's Candle.__post_init__ is
        # the same shape: one positivity comparison on the minimum, plus ordering.
        #
        # This is also the only check that can see a REORDERED row, which the shape
        # guard above cannot: a permuted [ts, close, open, high, low] has length 5
        # and five finite numbers, so the module would read the day's LOW as the
        # close, silently, on every row, for a year.
        #
        # Skipped rather than fatal, on the grounds the non-positive branch already
        # states: one bad candle must not cost a year of history, and dropping it
        # leaves an older reading as the latest, which the report dates and ages
        # honestly. No plausibility bound is implied — a uniformly rescaled candle
        # is self-consistent and passes, and inventing a ceiling for DVOL is a
        # different decision from enforcing the row's own arithmetic.
        _, candle_open, candle_high, candle_low, candle_close = row
        if not (
            candle_low > 0
            and candle_low <= candle_open <= candle_high
            and candle_low <= candle_close <= candle_high
        ):
            if day <= curr_date:
                # Counted on the same terms as the non-positive class above, and
                # for the same reason: only an IN-WINDOW candle was ever a
                # candidate for by_date.
                skipped_inconsistent += 1
                if newest_inconsistent is None or day > newest_inconsistent:
                    newest_inconsistent = day
            logger.warning(
                "Skipping self-contradicting Deribit DVOL candle %r dated %s for %s "
                "(a non-positive low, or an open or close outside its own high/low "
                "range; a reordered row looks like this)",
                row,
                day,
                currency,
            )
            continue
        # A repeated day (a resolution change, or a partial candle alongside a
        # settled one) keeps whichever candle carries the later timestamp, rather
        # than whichever the response happened to list last.
        if day <= curr_date and (day not in ts_by_date or row[0] >= ts_by_date[day]):
            ts_by_date[day] = row[0]
            by_date[day] = float(row[4])
    # The newest date across BOTH rejection classes, derived once here rather than
    # at each of the two places that interpolate it — the empty-series raises below
    # and the staleness refusal further down. Both run after this loop, so the
    # inputs are final at this point, and computing it twice gave a third rejection
    # class two sites to update in lockstep.
    #
    # ``default=None`` rather than a guard around the assignment: with no rejections
    # at all both dates are None, and a bare max() over the empty generator would
    # raise ValueError on the plainest of the paths below. Bound unconditionally so
    # the name exists on every path — a guard whose condition has to stay exactly
    # the union of the branches that read it is the kind of invariant that survives
    # only in a comment, and a later branch added outside it would read a
    # possibly-unbound local. Every branch that interpolates this requires a
    # non-zero count, and a count is only ever incremented in the same branch that
    # records its date.
    newest_rejected = max(
        (d for d in (newest_skipped, newest_inconsistent) if d is not None), default=None
    )
    if not by_date:
        # Distinct states, and the message is rendered verbatim into
        # "**DVOL:** unavailable — ...". "No readings published" sends the reader
        # to Deribit's status page; a fully-rejected history is a corrupt feed that
        # answered in full, and WHICH rejection emptied it decides what the
        # operator looks at. Also names the currency, which is the non-obvious part
        # on a proxied asset.
        #
        # Both rejection classes are named, and named for what they are. Reporting
        # only the non-positive count would have said "every reading was
        # non-positive (0 skipped)" on a feed whose every candle was instead
        # self-contradicting: a false cause with a self-refuting number beside it,
        # sending the operator after a broken value when the likely fault is a
        # changed response shape.
        #
        # The newest rejected date travels with every count, exactly as on the
        # rendered path — and it matters MORE here, not less, because this is the
        # branch where the reader gets no series at all, so it is the only thing
        # saying whether the feed broke recently or has been broken throughout.
        # The render note documents that reasoning at length; these raises had the
        # value in scope and dropped it, the render-vs-raise drift this module has
        # now been bitten by twice. (``newest_rejected`` is derived once above, so
        # this branch and the staleness refusal below cannot drift apart either.)
        if skipped_non_positive and skipped_inconsistent:
            raise DeribitError(
                # "candle", not "reading", and possessives that do not assume a
                # count. Half of what this sentence tallies is malformed ROWS, and
                # the module's own noun rule (see _WINDOW_COUNT_CAVEAT) reserves
                # "reading" for a usable value; the count is also plural here
                # whenever either class exceeds one, so "its own" was ungrammatical
                # on the very branch that requires both classes to have fired.
                f"Every {currency} DVOL candle on or before {curr_date} was rejected as broken "
                f"({skipped_non_positive} "
                + _BOTH_REJECTION_CLASSES.format(
                    inconsistent=skipped_inconsistent, newest=newest_rejected
                )
                + "), so the feed "
                "returned history but no usable level — a self-contradicting candle points at "
                "a reordered candle shape rather than at bad values"
            )
        if skipped_non_positive:
            raise DeribitError(
                f"Every {currency} DVOL reading on or before {curr_date} was non-positive "
                f"({skipped_non_positive} skipped, the newest dated {newest_rejected}), so the "
                f"feed returned history but no usable level"
            )
        if skipped_inconsistent:
            raise DeribitError(
                f"Every {currency} DVOL candle on or before {curr_date} carried a non-positive "
                f"low or an open or close "
                f"outside its own high/low range ({skipped_inconsistent} skipped, the newest "
                f"dated {newest_rejected}), so the feed returned history but no usable level — "
                f"across "
                f"a whole history this points at a reordered candle shape rather than at bad "
                f"values"
            )
        raise DeribitError(f"No {currency} DVOL readings on or before {curr_date}")
    dates = sorted(by_date)
    # The staleness ceiling, applied here rather than at render time so the half is
    # ABSENT rather than present-with-a-caveat: every other withholding decision in
    # this module turns on the same judgement, that a caveat does not survive a
    # downstream summary while the number it qualifies does.
    #
    # min(curr_date, today), matching _dvol_section's lag_from exactly. On a
    # historical date the series is filtered to curr_date, so measuring from the
    # clock would call every backtest stale; on an ahead-of-clock date the feed has
    # simply not reached curr_date yet, which is not the feed being behind.
    latest_date = dates[-1]
    lag_from = min(curr_date, today)
    stale_days = (
        datetime.strptime(lag_from, "%Y-%m-%d") - datetime.strptime(latest_date, "%Y-%m-%d")
    ).days
    if stale_days > MAX_DVOL_STALENESS_DAYS:
        # The rejection counts travel with the refusal for the reason they travel
        # with the empty-by_date raises above: a reader cannot otherwise tell a feed
        # that stopped publishing from one whose recent prints this module dropped,
        # and the two are different operator actions. Named only when non-zero, so
        # the ordinary stall says nothing about rejections that did not happen.
        #
        # Split by class, like those raises and the rendered _Rejected:/
        # _Inconsistent: notes — a summed "as broken" count dropped WHICH class
        # fired, on a path where this sentence is all the reader gets: bad values
        # and a reordered candle shape are different faults, and the class is what
        # says which one to chase. A flat ladder like those raises, every clause
        # guarded by its own count, so a rejection class this block does not know
        # about degrades to the documented named-only-when-non-zero silence
        # rather than to a zero-count cause; the both-class descriptor is shared
        # with the both-class raise (_BOTH_REJECTION_CLASSES) rather than copied,
        # which is what keeps those two from drifting apart.
        if skipped_non_positive and skipped_inconsistent:
            dropped_clause = (
                f"{skipped_non_positive} candle"
                f"{'' if skipped_non_positive == 1 else 's'} "
                + _BOTH_REJECTION_CLASSES.format(
                    inconsistent=skipped_inconsistent, newest=newest_rejected
                )
            )
        elif skipped_non_positive:
            dropped_clause = (
                f"{skipped_non_positive} candle"
                f"{'' if skipped_non_positive == 1 else 's'} with a non-positive "
                f"reading, the newest dated {newest_rejected}"
            )
        elif skipped_inconsistent:
            dropped_clause = (
                f"{skipped_inconsistent} candle"
                f"{'' if skipped_inconsistent == 1 else 's'} with a non-positive "
                f"low or an open or close outside "
                f"{'its' if skipped_inconsistent == 1 else 'their'} own high/low "
                f"range, the newest dated {newest_rejected}"
            )
        else:
            dropped_clause = None
        dropped_note = (
            ""
            if dropped_clause is None
            else (
                f" (this module also rejected {dropped_clause}, so some of this gap "
                f"may be prints that arrived and were dropped)"
            )
        )
        raise DeribitError(
            # "on or before {curr_date}", like every sibling message here and the
            # rendered lag line. The series is filtered to curr_date, so an
            # unqualified "the newest usable reading is dated X" is a claim about
            # the LIVE feed made from a view this module deliberately truncated —
            # false on any backtest where the index kept publishing, which is the
            # ordinary case. This refusal was the one message that opted out of the
            # rule the others follow.
            f"The newest usable {currency} DVOL reading on or before {curr_date} is dated "
            f"{latest_date}, {stale_days} days before {lag_from} — further back than this "
            f"vendor serves ({MAX_DVOL_STALENESS_DAYS} days), so the level is withheld rather "
            f"than presented as the current volatility regime{dropped_note}"
        )
    return DvolSeries(
        dates=dates,
        closes=[by_date[d] for d in dates],
        dropped_non_positive=skipped_non_positive,
        newest_dropped_date=newest_skipped,
        dropped_inconsistent=skipped_inconsistent,
        newest_inconsistent_date=newest_inconsistent,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _classify_asset(asset: str) -> tuple[str | None, bool]:
    """Classify a caller symbol for options reporting: ``(currency, is_proxy)``.

    * ``(BTC|ETH, False)`` — this vendor reads a chain and a DVOL index for it.
    * ``("BTC", True)`` — a recognized crypto *risk* asset with no Deribit chain of
      its own (SOL, XRP, ...): BTC's DVOL *level* is the market's crypto-vol
      benchmark, so it is served as a market-wide proxy. Its skew is not — see the
      withholding rule in ``get_options_market_data``.
    * ``(None, False)`` — not a recognized crypto risk asset (a stablecoin, a
      quote-only, or an unrecognized symbol). A stablecoin has no crypto-vol
      character to proxy, so there is no signal to serve.

    Delegates to the shared ``normalize_symbol`` so pair forms and crypto-base
    recognition match the rest of the system — the same classification farside
    applies to ETF flows. ``BTC``, ``ETH-USD``, ``ETHUSD``, ``ETHUSDT`` and
    ``BTC/USD`` resolve to their base; look-alikes (``ETHW``, ``WETH``, ``BTCB``)
    and stablecoins fall through to no-signal. Slash pair forms are converted to
    the dash form first, because ``normalize_symbol`` only strips dashes.
    """
    return classify_crypto_asset(asset, SUPPORTED_CURRENCIES)


def _readings(count: int) -> str:
    """ "N usable daily readings", singular at one — the count reaches a sparse feed's text.

    "usable", because this count is what the shortfall sentences blame the
    calendar window for ("the 365 days ending X hold only N"). Candles this module
    rejected as broken are absent from it, so an unqualified count attributes a
    rejection made here to the feed — and this is the sentence a reader consults
    to decide whether the sample supports the figure computed from it. Worded on
    the shared helper rather than at the shortfall sites, so no site keeps the old
    claim (the count also reaches the reading line, which is quoted on its own).
    """
    return f"{count} usable daily reading{'' if count == 1 else 's'}"


def _quotes(count: int, side: str) -> str:
    """ "N call quotes", singular at one.

    A one-quote side is exactly the sparse-chain case this count exists to expose,
    so it is the number least worth garbling.
    """
    return f"{count} {side} quote{'' if count == 1 else 's'}"


def _percentile_of(value: float, sample: list[float]) -> float:
    """Percent of the sample at or below ``value`` (``value`` is in the sample)."""
    return 100.0 * sum(1 for x in sample if x <= value) / len(sample)


def _ordinal(value: float) -> str:
    """Render a percentile as an English ordinal ("1st", "3rd", "21st", "7th").

    A hardcoded "th" produced "the 1th percentile" for every value ending in 1,
    2 or 3 — cosmetic, but this is the report's most-quoted figure.

    Rounds half UP rather than via ``round()``, whose banker's rounding sends an
    exact .5 to the nearest EVEN integer: 62.5 rendered "62nd" while 37.5 rendered
    "38th", so the same half-percent landed a different way depending on the
    neighbouring digit. Percentiles here are always positive, so ``floor(x + 0.5)``
    is exactly half-up.

    Floored at 1st. A percentile here counts the readings at or below the latest
    one, and the latest reading is itself in the sample, so the true value can
    never be 0 — its minimum is 100/n. Widening the window to 365 days made that
    minimum 0.27, which half-up rounding renders "0th": a figure contradicting the
    definition printed on the same line, reachable any time BTC's DVOL prints a
    one-year low. (At 30 days the floor was 3.33, so nothing below "3rd" could
    appear and this could not arise.)
    """
    number = max(1, math.floor(value + 0.5))
    if 11 <= number % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _dvol_section(series: DvolSeries, curr_dt: datetime, today: str) -> DvolReport:
    """Render the DVOL lines.

    ``curr_date`` is derived here rather than passed alongside ``curr_dt``. As two
    parameters nothing made them agree, and a mismatched pair computes both
    windows off one while every rendered line names the other — a silently wrong
    window of exactly the kind the ``_window`` bound comment exists to prevent.

    The range and the percentile are computed over DIFFERENT windows and printed on
    separate lines, each naming its own span and its own sample count. Folding them
    into one line was how "the latest reading sits at the Nth percentile" came to
    sit beside a span it was not computed over.

    The percentile is None — and no percentile is rendered — whenever its window
    holds fewer than ``_MIN_PERCENTILE_SAMPLE`` readings, so a sparse or stalled
    feed cannot manufacture a position-within-range that only reflects how few
    observations arrived.

    ``today`` is the UTC date of the clock, which is not always ``curr_date``: a
    caller deriving its analysis date from a local clock east of UTC hands us a
    ``curr_date`` a few hours into the future. The newest candle is judged open
    against ``today`` (a candle is open because the day is not over, which has
    nothing to do with the date being analysed), and the feed is judged late
    against whichever of the two is earlier (no feed can be behind a date that
    has not happened yet).
    """
    curr_date = curr_dt.strftime("%Y-%m-%d")

    # Exclusive lower bound: N calendar days ending at curr_date is
    # curr_date-(N-1) ... curr_date. An inclusive bound would put N+1 readings in a
    # window the report calls "Nd".
    def _window(days: int) -> list[float]:
        start = (curr_dt - timedelta(days=days)).strftime("%Y-%m-%d")
        # strict=True: the two lists are built together in _fetch_dvol, so a length
        # mismatch would mean dates and readings had drifted out of correspondence.
        return [c for d, c in zip(series.dates, series.closes, strict=True) if d > start]

    window = _window(DVOL_WINDOW_DAYS)
    pct_window = _window(DVOL_PERCENTILE_WINDOW_DAYS)
    latest = series.latest
    percentile = (
        _percentile_of(latest, pct_window) if len(pct_window) >= _MIN_PERCENTILE_SAMPLE else None
    )

    # The unit is stated because the chain half labels its own figures ("39.59%",
    # "vol points") and this one did not, leaving a downstream agent comparing
    # "DVOL 40.45" against "ATM IV 39.59%" with nothing in the text saying the two
    # are the same quantity. Both are annualized vol points.
    latest_line = (
        # "latest usable", not "latest". A candle this module rejected can be dated
        # LATER than this one, and the rejection notes below print that date — so
        # the unqualified label is contradicted a few italic lines down on exactly
        # the path (a feed whose recent prints are all broken) those notes exist to
        # expose. Same word, same reason, as every other sentence here that names
        # a reading or a count of them. Deliberately not enumerated: an earlier
        # version of this comment counted the swept sites and was wrong in BOTH
        # directions the moment the sweep widened — the same staleness the
        # cause-count comments further down were de-counted to avoid. Grep
        # "usable" for the live list.
        f"**DVOL ({DVOL_INDEX_TENOR_DAYS}-day implied vol index), latest usable reading:** "
        f"{latest:.2f}% annualized on {series.latest_date}"
    )
    latest_is_open = series.latest_date >= today
    if latest_is_open:
        # Deribit stamps a 1D candle at the START of its day, so the candle dated
        # today is still open and its value is only the level so far. Every line
        # below therefore says "reading", never "close": labelling it here and
        # then calling the same number "the latest close" two lines down would
        # take the caveat back in the same breath.
        #
        # ">=", not "==". ``today`` is the clock taken BEFORE the DVOL fetch, and
        # that fetch can span UTC midnight (two timeouts plus the retry sleep,
        # ~62s), in which case it returns a candle dated the NEW day — strictly
        # greater than ``today``, and by definition seconds old. Under "==" the
        # label was dropped from exactly that candle: the one case where it is most
        # certainly open. The partial level then printed as the settled latest, set
        # the 30-day max, and became what the percentile was measured against.
        # "That day", not "today". This line is dated by the clock the DVOL half
        # was read on, which an ahead-of-clock run can print three lines below a
        # caveat saying the clock has since reached the NEXT day — so the report
        # called one date "today" while stating the clock was on another. The claim
        # itself is right either way: the candle was open when it was read.
        latest_line += (
            " (that day's candle was still open when it was read, so this is the level so far)"
        )
    lines = [latest_line]

    if not window:
        # Every reading predates the window. Say so rather than computing a range
        # over a one-element fallback sample, whose min and max would both be the
        # latest value itself.
        #
        # No longer reachable through _fetch_dvol on an ordinary date: an empty
        # 30-day window needs a reading over 30 days old, and MAX_DVOL_STALENESS_DAYS
        # refuses one past 14. The stalled feed this used to describe is now a
        # refusal, not an empty render. Two triggers survive — an analysis date
        # ahead of the clock, where this window, measured from curr_date, runs
        # past the newest served reading while the staleness bound, measured
        # from the clock, still passes it (30 days of lead for a current feed,
        # and as little as DVOL_WINDOW_DAYS - MAX_DVOL_STALENESS_DAYS = 16 for
        # a feed at the staleness allowance — weeks of lead either way, not the
        # year that emptying the percentile window too would take) — and a
        # caller invoking this renderer directly. The branch stays and both
        # triggers are tested.
        #
        # "on or before {curr_date}", not "at all". The series is filtered to
        # curr_date, so on a historical date "the newest reading Deribit has
        # published at all" is a claim about the LIVE feed made from a deliberately
        # truncated view of it — false whenever the index kept publishing after the
        # analysis date, which is the ordinary case for any backtest, and stated
        # three lines under the header note saying the history was filtered.
        lines.append(
            # "no usable reading", for the reason _readings() carries the same
            # word: candles rejected here are absent from this window too, so an
            # unqualified "no DVOL reading falls inside" is false whenever the
            # rejection note below is reporting prints that did fall inside it.
            f"**{DVOL_WINDOW_DAYS}d range:** not computed — no usable DVOL reading falls "
            f"inside the {DVOL_WINDOW_DAYS} days ending {curr_date}; the level above is the "
            f"newest usable reading on or before {curr_date}"
        )
    elif len(window) < _MIN_RANGE_SAMPLE:
        # The same objection as the empty case, one reading later: min and max
        # would both be the latest value, printing a collapsed range that reads as
        # a month of pinned volatility.
        lines.append(
            f"**{DVOL_WINDOW_DAYS}d range:** not computed — the {DVOL_WINDOW_DAYS} days ending "
            f"{curr_date} hold only {_readings(len(window))}, so a min and a max would "
            f"both be the level above"
        )
    else:
        # Dated, like both refusal branches of this line. The success branch was
        # the one DVOL line carrying no window end, so quoted on its own during a
        # backtest it read as "the last 30 days" from the reader's present.
        lines.append(
            f"**{DVOL_WINDOW_DAYS}d range:** min {min(window):.2f}% / max {max(window):.2f}% "
            f"over the {_readings(len(window))} in the {DVOL_WINDOW_DAYS} days ending "
            f"{curr_date}"
        )

    if percentile is None:
        # Two genuinely different states, and the "latest reading is itself in the
        # sample" rationale only applies to one of them: appended to both, it told
        # a reader that a window holding "no daily readings at all" nonetheless
        # contained the latest reading. Name the shortfall in readings rather than
        # calendar days, since the sample size is what makes the percentile
        # meaningless, and 100/n says how coarse it would have been.
        if pct_window:
            # Floored, not rounded. At ":.0f" a six-reading window printed "could
            # not read below 17%" when the true floor is 16.67 — a bound the
            # figure it describes can actually breach, which is worse than no
            # bound. floor() at one decimal states a value that is always true.
            floor_pct = math.floor(1000 / len(pct_window)) / 10
            lines.append(
                f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** not computed — the "
                f"{DVOL_PERCENTILE_WINDOW_DAYS} days ending {curr_date} hold only "
                f"{_readings(len(pct_window))}, and the latest usable reading is itself in that "
                f"sample, so a percentile over it could not read below "
                f"{floor_pct:.1f}%"
            )
        else:
            lines.append(
                # "usable", for the reason the range branch above carries it.
                f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** not computed — no usable "
                f"DVOL reading falls inside the {DVOL_PERCENTILE_WINDOW_DAYS} days ending "
                f"{curr_date}"
            )
    else:
        lines.append(
            f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** the latest usable reading sits at "
            f"the "
            f"{_ordinal(percentile)} percentile of the {_readings(len(pct_window))} in "
            f"the {DVOL_PERCENTILE_WINDOW_DAYS} days ending {curr_date} (percentile = share "
            f"of those readings at or below it)"
        )

    # A feed cannot be behind a date that has not arrived: measure lateness from
    # whichever of the analysis date and the clock is earlier, or a caller whose
    # curr_date runs ahead of UTC would be told a perfectly current index had
    # "not printed" for days.
    lag_from = min(curr_date, today)
    lag_days = (
        datetime.strptime(lag_from, "%Y-%m-%d") - datetime.strptime(series.latest_date, "%Y-%m-%d")
    ).days
    # Always dated, not only when late: this phrase is the only date the reading
    # line carries, and that line is what a downstream summary quotes on its own.
    # Gated on the lag threshold, a reading a day old — the ordinary
    # case — was quoted bare and read as current.
    as_of_phrase = f"as of {series.latest_date}"
    if lag_days > MAX_DATA_LAG_DAYS:
        # (The threshold makes lag_days at least 2 here, so "days" is always the
        # right plural. It stays right for any MAX_DATA_LAG_DAYS >= 1; drop the
        # threshold to 0 and the singular case becomes reachable.)
        if curr_date < today:
            # A historical analysis date: readings AFTER curr_date were filtered
            # out by this module, so nothing here licenses a claim about what the
            # live index has done since. "The index itself has not printed since"
            # is then false in the ordinary backtest case — the feed kept
            # publishing, we simply declined to look — and it is asserted in the
            # same section whose header note says the history was filtered.
            lines.append(
                # "no usable reading", not "nothing": the rejection note below may
                # be reporting that the index DID publish across this span and
                # that this module dropped those prints as broken. Two adjacent
                # italic lines asserting opposite facts about the same span is the
                # contradiction this round exists to remove, not to add.
                # "newest USABLE reading" in the opening clause too. The rejection
                # note below names the newest REJECTED date, which can be later
                # than this one — so an unqualified "the newest DVOL reading is X"
                # is contradicted one italic line down, on precisely the path (a
                # feed whose recent prints are all corrupt) that note exists to
                # expose.
                f"_Data lag: the newest usable DVOL reading on or before {curr_date} is "
                f"{series.latest_date}, {lag_days} days earlier — the index published no "
                f"usable reading between the two. Treat the level as {lag_days} days old as "
                f"at the analysis date._"
            )
        else:
            # Here curr_date has been reached, so the gap runs to the present and a
            # successful fetch really does mean a live series that stopped
            # updating: Deribit answered normally with a history that ends short.
            lines.append(
                # "published no usable reading", not "has not printed since". The
                # rejection note below may be reporting that the index DID print
                # across this very span and that this module dropped those prints
                # — and the two lines are adjacent italics, so both survive a
                # summary that keeps only those. The historical branch above was
                # corrected for exactly this and this branch was left behind; the
                # two operator actions differ (vendor status page vs corrupt feed).
                f"_Data lag: the newest usable DVOL reading is {series.latest_date}, "
                f"{lag_days} days before {lag_from} — the fetch succeeded, so the index "
                f"published no usable reading between the two. Treat the level as "
                f"{lag_days} days old._"
            )
        # Names what the age is measured FROM. The reading line quotes this phrase
        # on its own beside "the window ending on the analysis date", so a bare "N
        # days old" was the one figure in that sentence with an unstated reference
        # — and the two differ whenever curr_date runs ahead of the clock.
        as_of_phrase = f"as of {series.latest_date}, {lag_days} days before {lag_from}"
    if series.dropped_non_positive:
        # The counts above are of readings that SURVIVED; this is what did not.
        # Without it the shortfall sentences describe a window this module
        # partially emptied as though the feed had never filled it, and a reader
        # judging sample sufficiency cannot tell a sparse feed from a corrupt one
        # — which are different operator actions.
        dropped = series.dropped_non_positive
        lines.append(
            # Scoped to what the count actually measures. `dropped_non_positive`
            # counts rejections across the whole FETCH span (the longer window
            # plus its buffer), not within the two statistics windows named above,
            # so "the counts above are shorter" was false whenever the rejected
            # candle fell outside them — or shared a day with a surviving one,
            # where the day still counts once and no count moves at all.
            #
            # The newest rejected date is named so a reader can tell whether these
            # rejections ARE the data-lag gap above or are unrelated history: a
            # genuine stall plus one rejection a year ago otherwise rendered
            # identically to a feed whose every recent print was rejected.
            # "the newest dated", with no plural pronoun, like the staleness
            # ladder's clauses: this count can be 1, and "the newest of them"
            # reads a set into the very branch that can hold a single reading.
            # The both-class descriptor keeps "of them" — its two classes
            # guarantee a plural.
            f"_Rejected: {dropped} DVOL reading{'' if dropped == 1 else 's'} dated on or "
            f"before {curr_date} came back zero or negative and "
            f"{'was' if dropped == 1 else 'were'} dropped as broken (a non-positive vol index "
            f"is not a low reading), the newest dated {series.newest_dropped_date}. "
            f"A rejected reading {_WINDOW_COUNT_CAVEAT}_"
        )
    if series.dropped_inconsistent:
        # Its own sentence rather than a second clause on the line above, because
        # the two rejection classes point at different faults: a non-positive close
        # is a broken VALUE, while a candle failing its own arithmetic — a
        # non-positive low, or an open or close outside its high/low range — points
        # at a reordered response SHAPE. Folding them into one count would have made
        # that line's "came back zero or negative" false for half of what it
        # counted — the partial-update failure this module keeps re-learning.
        #
        # The two lines carry the same closing sentence about window counts because
        # either can appear without the other, so neither may lean on the other
        # being present — shared as _WINDOW_COUNT_CAVEAT rather than copied, since
        # copying it is precisely how the two would drift apart.
        bad = series.dropped_inconsistent
        lines.append(
            f"_Inconsistent: {bad} DVOL candle{'' if bad == 1 else 's'} dated on or before "
            f"{curr_date} carried a non-positive low, or an open or close outside "
            f"{'its' if bad == 1 else 'their'} own "
            f"high/low range, and {'was' if bad == 1 else 'were'} dropped as broken (a candle "
            f"that contradicts itself cannot be read as a level, and a reordered candle shape "
            f"would look exactly like this), the newest dated "
            f"{series.newest_inconsistent_date}. A rejected candle {_WINDOW_COUNT_CAVEAT}_"
        )
    if series.truncated:
        # The third way a count above can be short, and the only one that was not
        # the feed's doing at all. Stated separately from the two rejection notes
        # because it names a different fault again — those say this module dropped
        # prints, this says the response never carried them — and because the
        # operator action differs: nothing here is broken, the history simply has
        # to be paged for. Says which direction was lost, since that is what makes
        # the level itself still trustworthy: the cap always keeps the NEWEST page,
        # so the headline reading and its date are unaffected and only the
        # statistics are under-sampled.
        lines.append(
            # No date on the boundary. ``series.dates`` holds only the readings that
            # were KEPT, so its earliest entry is not where delivery stopped
            # whenever a rejected candle was older — and the two rejection notes
            # beside this one are exactly the states in which that happens. Naming
            # a date here would put a false boundary next to the notes that refute
            # it, so the mechanism is stated and the boundary is not.
            # "a count above may be shorter than its window", not "the counts are".
            # A cursor only shortens a window whose candles exceed the page cap, and
            # the fetch spans ~376 candles — so a cap anywhere in 366..375 sets the
            # cursor while leaving BOTH rendered counts complete (only buffer days,
            # which enter no statistic, are lost), and a cap of 200 shortens the
            # 365-day count while the 30-day one stays whole. The two rejection
            # notes beside this one hedge for the same reason, in _WINDOW_COUNT_CAVEAT.
            "_Truncated: Deribit returned only the newest page of DVOL history and set a "
            "continuation cursor, so the older part of the fetch was never delivered. A count "
            "above may therefore be shorter than its window for a reason that is not the "
            "index's publishing — the level and its date are unaffected, since the newest "
            "readings are the ones a truncated page keeps._"
        )
    return DvolReport(
        "\n".join(lines), percentile, len(pct_window), as_of_phrase, latest, latest_is_open
    )


def _format_wing(quote: WingQuote | None) -> str:
    """Render an interpolated smile point with the strikes it came from.

    The None wording keeps both causes in one "or" because the only caller that
    can still reach it is ``_atm_line``, and ATM genuinely has no single guard to
    name: it is attempted on the call curve and then on the put curve, so either
    cause may have fired on either attempt. The 25Δ wings are attempted once each
    and name the guard that actually fired — see ``_wing_line``.
    """
    if quote is None:
        return (
            "n/a (no two strike-adjacent quotes bracket this point, or the quotes around it "
            "are not a monotone smile)"
        )
    return f"{quote.iv:.2f}% (between the {quote.strike_low:,.0f} and {quote.strike_high:,.0f} strikes)"


def _wing_line(quote: WingQuote | None, n_quotes: int, miss: _MissReason | None = None) -> str:
    """Render a smile point, stating the TRUE reason when it is missing.

    ``_format_wing``'s wording presumes there were quotes to bracket with. When a
    whole side of the expiry produced no usable delta — every put unquoted, say —
    that wording names causes that did not happen, and the honest signal is buried
    in a quote count several lines up.

    With two or more quotes the reason comes from the guard that actually fired
    (``miss``), because "the book is thin here" and "one of these quotes looks
    corrupt" are different reads of the same expiry and the second is the stronger
    signal: the monotonicity veto is the guard that catches a collapsed mark_iv
    capable of inverting the sign of RR25. ``miss`` defaults to None so a caller
    that has no reason to offer gets the conservative thin-book wording rather
    than an unfounded suspicion about a quote.
    """
    if quote is not None:
        return _format_wing(quote)
    if n_quotes == 0:
        return "n/a (no quote on this side of the expiry yielded a usable delta)"
    if n_quotes == 1:
        return "n/a (only one usable quote on this side, so no pair can bracket this point)"
    if miss == _MISS_NON_MONOTONE:
        return (
            "n/a (a strike-adjacent pair does bracket this point, but delta rises with strike "
            "across it, which no well-formed smile does — so one of those quotes is suspect "
            "and the point was refused rather than interpolated from it)"
        )
    return "n/a (no two strike-adjacent quotes bracket this point)"


def _rr_points(rr: float) -> str:
    """Signed RR25 at the report's two decimals, with no negative zero.

    An RR25 in (-0.005, 0) formats as "-0.00": a minus sign asserting the put wing
    is the richer one, bolted to a magnitude saying the wings are level. That
    reads as a data error rather than as the flat smile it is. Anything that
    rounds away at two decimals is printed as +0.00, which is what two decimals
    actually resolve.
    """
    return f"{0.0 if abs(rr) < _RR_ZERO_EPSILON else rr:+.2f}"


def _atm_line(skew: SkewSnapshot) -> str:
    """Render the ATM point, which is interpolated per curve rather than per side.

    ATM is tried on the call curve at +0.5 and then on the put curve at -0.5, so
    neither side's count alone explains a miss and their SUM does not either: one
    call and one put total two quotes while neither curve has the pair an
    interpolation needs. The deciding number is the better-populated curve.
    """
    if skew.atm is not None:
        return _format_wing(skew.atm)
    if max(skew.n_calls, skew.n_puts) >= 2:
        return _format_wing(None)
    return (
        "n/a (neither the call nor the put curve has two usable quotes, so there is no pair "
        "to bracket the 50Δ point with)"
    )


def _listed_clause(skew: SkewSnapshot) -> str:
    """How many contracts the survivors above were drawn from, when it is known.

    Body only, deliberately. ``_reading_line``'s scope paragraph excludes the quote
    counts and the open-interest policy because both qualify figures that sentence
    already names, and this is the denominator of one of them — a reader who needs
    it has the body. What it adds here is the one thing the numerator cannot say
    alone: whether a thin smile is a thin CHAIN or a chain this module thinned.

    Omitted rather than zero when unmeasured (``compute_skew`` called directly),
    since "0 contracts listed" beside two usable quotes is a self-refuting claim.

    Two forms, because the healthy case has no remainder to explain: on a fully
    quoted expiry every listed contract survives, and a trailing "the rest carried
    no open interest" then asserts a rest that does not exist — the first draft of
    this clause did exactly that on the fixture chain, 16 of 16. The shortfall form
    states the remainder as a NUMBER rather than implying one, and names every
    reason a contract can be dropped between the two counts, including a delta that
    could not be solved — which is not a field the payload carries at all, and which
    an enumeration of payload fields alone would silently exclude.
    """
    listed = skew.listed_for_expiry
    if listed is None:
        return ""
    used = skew.n_calls + skew.n_puts
    if used >= listed:
        return f", which is every contract Deribit lists for it ({listed})"
    dropped = listed - used
    return (
        f", out of the {listed} Deribit lists for it — the other "
        f"{dropped} carried no open interest, or no usable mark IV, underlying or delta"
    )


def _skew_section(skew: SkewSnapshot, snapshot_time: str) -> str:
    """Render the live-chain lines for the selected expiry."""
    # Both branches name the same exclusions. The fallback branch used to name
    # none of them, so a report that stepped to the second candidate dropped the
    # only line carrying them — the same partial-update defect the non-fallback
    # wording was widened to fix.
    eligibility = (
        f"only expiries {MIN_ELIGIBLE_DTE_DAYS:.0f}-{MAX_ELIGIBLE_DTE_DAYS:.0f} days out are "
        f"eligible, since a risk reversal is not comparable across tenors, and contracts with "
        f"no open interest never enter the smile"
    )
    if skew.is_fallback:
        expiry_basis = (
            f"the eligible expiry nearest {TARGET_DTE_DAYS} days could not be used, so this is "
            f"the next eligible one; {eligibility}"
        )
    else:
        expiry_basis = (
            f"the eligible expiry closest to {TARGET_DTE_DAYS} days among those this report "
            f"can read; {eligibility}"
        )
    lines = [
        f"**Chain snapshot:** taken {snapshot_time} (Deribit publishes no historical chain, "
        f"so these figures are the live book at that instant)",
        f"**Expiry used:** {skew.expiry} — {skew.days_to_expiry:.1f} days out, {expiry_basis}; "
        f"{_quotes(skew.n_calls, 'call')} and {_quotes(skew.n_puts, 'put')} on this expiry "
        f"yielded a usable delta{_listed_clause(skew)}",
        # Labelled, like every other figure in this section. Alone among them it
        # carried no unit and no basis, so a reader meeting "62,000.00" between
        # two percentages had only the tool docstring to tell them it is not spot.
        f"**Forward:** {skew.forward:,.2f} (Deribit's forward for this expiry, not spot)",
        f"**ATM IV ({ATM_DELTA * 100:.0f}Δ):** {_atm_line(skew)}",
        f"**25Δ call IV:** {_wing_line(skew.call_25, skew.n_calls, skew.call_25_miss)}",
        f"**25Δ put IV:** {_wing_line(skew.put_25, skew.n_puts, skew.put_25_miss)}",
    ]
    if skew.rr25 is None:
        lines.append(
            # "does not supply", not "does not bracket": _wing_line exists because
            # a side with no usable quote — or one — was never a bracketing
            # failure, and the monotonicity veto is a bracket that WAS found and
            # rejected. Naming bracketing here contradicts the wing line printed
            # directly above it in exactly those cases. The true reason is on
            # that line; this one only has to be true in all of them.
            # "the wing lines above say which and why", not "each wing line": rr25
            # is None when AT LEAST ONE wing is missing, so with the other wing
            # present that line carries a number and no explanation — "each" points
            # at it and quietly re-reads "does not supply both wings" as "supplies
            # neither", the opposite of what is rendered.
            "**RR25 (25Δ call IV − 25Δ put IV):** n/a — the chain does not supply both "
            "wings (the wing lines above say which and why), so the risk reversal is not "
            "computed (no wing vol is extrapolated)"
        )
    elif abs(skew.rr25) < _RR_ZERO_EPSILON:
        # The sign is deliberately suppressed at this magnitude (see _rr_points),
        # and the analyst prompt tells the model that RR25's sign is meaningful and
        # to state it — so a bare "+0.00" standing for a marginally NEGATIVE value
        # is a manufactured sign the model is instructed to read. The Reading line
        # already says this in words ("both wings carry the same implied vol"); the
        # body line is where a reader meets the figure first, and it was the one
        # surface stating the rule nowhere.
        lines.append(
            f"**RR25 (25Δ call IV − 25Δ put IV):** {_rr_points(skew.rr25)} vol points — the two "
            f"wings are level at the two decimals this report prints, so the sign carries no "
            f"information at this magnitude"
        )
    else:
        lines.append(f"**RR25 (25Δ call IV − 25Δ put IV):** {_rr_points(skew.rr25)} vol points")
    return "\n".join(lines)


def _wide_wing_brackets(skew: SkewSnapshot) -> dict[str, float]:
    """EVERY 25Δ bracket wider than the threshold, as ``{side: fraction_of_forward}``.

    Only the two RR25 inputs are measured. ATM's bracket is not: it qualifies a
    figure the reading line does not restate, and its absence is reported in its
    own right by the caller.

    The SIDE travels with each number because the reading line is what survives a
    downstream summary and the strikes that would identify the wing are body-only
    — "that wing reads across the smile" named neither of the two.

    All of them rather than the widest one. Reporting only ``max()`` meant that
    when BOTH wings were coarse the sentence named one and said nothing about the
    other, so a summary keeping that line concluded the unnamed wing was sound —
    and RR25 is the DIFFERENCE of the two, which makes both being coarse
    materially worse than one, not the same thing said once. The old tie-break was
    an artefact besides: ``max()`` on ``(fraction, side)`` pairs fell through to
    comparing the side STRINGS at equal widths, so "put" beat "call" alphabetically
    and two identically-wide brackets always reported the put.

    Keyed by side rather than a list of pairs. The ``(fraction, side)`` element
    order existed ONLY so ``max()`` would sort by width, and ``max()`` is gone —
    leaving a shape the caller immediately rebuilt into this dict to look up
    "call" and "put" by name. Insertion order is still call-first, so the report
    reads in its own order rather than the data's.
    """
    if skew.forward <= 0:
        return {}
    wide: dict[str, float] = {}
    for quote, side in ((skew.call_25, "call"), (skew.put_25, "put")):
        if quote is None:
            continue
        fraction = (quote.strike_high - quote.strike_low) / skew.forward
        if fraction > _WIDE_BRACKET_FRACTION:
            wide[side] = fraction
    return wide


def _reading_line(
    currency: str,
    skew: SkewSnapshot | None,
    dvol_report: DvolReport | None,
    dvol_absence: str | None = None,
    chain_absence: str | None = None,
) -> str:
    """Restate the two headline numbers in words and define them — nothing further.

    Deliberately mechanical. This is the most quotable sentence in the report and
    the research and risk agents re-read it downstream, so it says what RR25 and
    the percentile *are* and leaves the judgement to them. Characterising the
    print instead ("strongly tilted", "the market is paying up for downside
    protection") would inject a directional prior into every one of those hops,
    and a negative RR25 is the ordinary resting state of crypto options rather
    than news. No sibling vendor in this package interprets its own figures.

    It also names the currency and the tenor: for a proxied asset the lines that
    otherwise say whose surface this is are ones a downstream summary may drop,
    and a bare "the 28AUG26 skew ..." is precisely what survives with the proxy
    framing gone. The tenor is named for the same reason and became load
    bearing once the expiry stopped being fixed — a risk reversal is not
    tenor-invariant, so a reader who assumes ~30 days when the fallback supplied a
    different expiry is reading a different quantity than the one printed.

    An RR25 that is NOT in the report is stated here rather than left out, which is
    why ``chain_absence`` exists. Every other disclosure in this module is built on
    "the caveat has to survive every summarisation hop" — it is the reason the
    proxied and historical skews are withheld outright rather than annotated — yet
    the one sentence written to survive that hop used to fall silent when the chain
    half was absent. A backtest, a proxied asset and a chain outage then produced a
    Reading line differing from a healthy report's only by a clause that is not
    there, and an absent clause is precisely what a summary cannot preserve: the
    next agent reads "options positioning: DVOL at the 41st percentile" and has no
    way to know skew was never assessed. ``chain_absence`` carries WHY, since
    "withheld by policy" and "the fetch failed" are different facts to a reader
    deciding whether to wait for the next cycle.

    ``dvol_absence`` is the same disclosure for the other half, and the DVOL level
    is now stated unconditionally rather than only as the base of a percentile.
    Gating the whole DVOL clause on the percentile conflated three different
    states into one silence: the half failed, the half arrived but its window held
    too few readings to rank, and — in the worst case as it then stood, a feed
    stalled for over a year — a level so stale that both windows came back empty.
    The more broken the feed, the less that sentence said, while the body above it
    carried a usable number the whole time. (That third state is no longer reached
    by a stall: MAX_DVOL_STALENESS_DAYS refuses a level past 14 days old, so
    emptying both windows now needs an analysis date about a year ahead of the
    clock — the 30-day range window alone empties from a month of lead for a
    current feed, as little as 16 days (30 minus the staleness allowance) for
    one at the staleness bound. The clause is unconditional either way, which
    is what stops the next cause added from silently re-creating the silence.)

    **Scope of what reaches this line.** Everything that changes WHICH quantity
    the figures describe, or whether they exist at all, belongs here; nothing that
    merely adds precision does. So: both halves' absence and why; the RR25 and the
    DVOL level themselves; the expiry and its tenor (a risk reversal is not
    tenor-invariant); a fallback expiry (the nearest one failed, which is itself
    the surface-sparseness signal); a missing ATM point (the prompt's longest
    paragraph describes a figure that is then not there); and — ONLY where an RR25
    is printed — each 25Δ wing interpolated across an implausibly wide bracket
    (the strikes are printed in the body, but a summary keeps this line and drops
    them), both of them when both are that wide. That last one is gated where its
    two siblings are not, because with no RR25 this line states no wing vol at
    all, so there is nothing here for the qualifier to attach to; the wing lines in
    the body still carry it. Not here: the exact strikes, the quote counts and the
    open-interest policy — all of these qualify figures this line already names,
    and a reader who needs them has the body.

    The window SPANS are not on that exclusion list, though they read like they
    belong on it: the percentile clause names its span and its sample count in
    both branches, deliberately, because "the 365-day window" alone reads as one
    observation per day and a stalled feed then sounds like a full year of
    evidence once the clause is quoted on its own.
    """
    parts = []
    if skew is not None and skew.rr25 is not None:
        rr = skew.rr25
        # One decimal, matching _skew_section's "N.N days out". At ":.0f" this used
        # format's banker's rounding — the very thing _ordinal goes out of its way
        # to avoid — so one report could say "22.5 days out" and "(22-day)".
        tenor = f"{skew.expiry} ({skew.days_to_expiry:.1f}-day)"
        if abs(rr) < _RR_ZERO_EPSILON:
            parts.append(
                f"in the live {currency} {tenor} chain both 25Δ wings carry the same "
                f"implied vol (RR25 {_rr_points(rr)})"
            )
        else:
            richer, cheaper = ("puts", "calls") if rr < 0 else ("calls", "puts")
            parts.append(
                f"in the live {currency} {tenor} chain, 25Δ {richer} are priced "
                f"{abs(rr):.2f} vol points above 25Δ {cheaper} (RR25 {_rr_points(rr)}, defined "
                f"as 25Δ call IV minus 25Δ put IV)"
            )
    elif skew is not None:
        # The chain WAS read and simply did not yield both wings — a different
        # fact from an absent chain half, and the distinction is the reader's:
        # here the surface exists and is incomplete, there it was never seen at
        # all. Stated with the expiry so it cannot be read as a claim about the
        # whole surface, and worded "does not supply" rather than "does not
        # bracket" for the reason _skew_section's RR25 line gives: two of the
        # three reachable causes are not bracketing failures at all.
        parts.append(
            f"no 25Δ risk reversal is in this report (the live {currency} {skew.expiry} chain "
            f"does not supply both 25Δ wings)"
        )
    elif chain_absence:
        parts.append(f"no 25Δ skew is in this report ({chain_absence})")
    if skew is not None:
        # Degradations of a chain that WAS read. Each changes what the printed
        # figures mean rather than merely how precise they are, and each is
        # otherwise disclosed only in the body — which is the half a summary
        # drops. Collected into one trailing clause so the sentence gains one
        # segment rather than three.
        # Parenthesised, and no qualifier contains a comma. The enclosing sentence
        # joins its clauses with "; and " — the serial-semicolon CLOSING form — so
        # a bare "; "-joined list left the sentence's own final clause reading as
        # one more qualifier: "...; and BTC's latest usable DVOL reading is ..." parsed as
        # a fourth qualification OF THE CHAIN, which is at its worst when that
        # clause is the other half's absence. The brackets close the list so it
        # cannot swallow what follows it.
        qualifiers = []
        if skew.is_fallback:
            qualifiers.append(
                f"the eligible expiry nearest {TARGET_DTE_DAYS} days could not be used so "
                f"this is the next one"
            )
        if skew.atm is None:
            qualifiers.append(f"no ATM ({ATM_DELTA * 100:.0f}Δ) IV could be read from it")
        if skew.rr25 is not None:
            wide = _wide_wing_brackets(skew)
            # The measured width, not "up to N%": at ":.0%" a bracket spanning
            # 10.49% rendered "up to 10%", an upper bound the figure it
            # describes breaches — the same defect this round floored the
            # percentile bound to remove. Two decimals so a bracket just past
            # the threshold cannot print AS the threshold.
            #
            # Em-dashes and no commas in the two-wing form, for the reason the
            # comment above this block gives: a comma inside a qualifier would let
            # the "; "-joined list bleed into the enclosing sentence's own clauses.
            if len(wide) == 1:
                side, fraction = next(iter(wide.items()))
                qualifiers.append(
                    f"its 25Δ {side} wing was interpolated across a bracket spanning "
                    f"{fraction:.2%} of the forward so it reads across the smile rather "
                    f"than along it"
                )
            elif wide:
                qualifiers.append(
                    f"BOTH 25Δ wings were interpolated across wide brackets — the call "
                    f"spanning {wide['call']:.2%} of the forward and the put "
                    f"{wide['put']:.2%} — so both read across the smile rather than along "
                    f"it and RR25 is the difference between two such reads"
                )
        if qualifiers:
            parts.append("this chain read is qualified (" + "; ".join(qualifiers) + ")")
    if dvol_report is not None:
        # The as-of date travels with the claim for the same reason the currency
        # does: this sentence is what survives a downstream summary, and a level
        # presented bare reads as current however old the reading is. Present on
        # every report, not only late ones — see DvolReport.
        # The open-candle caveat follows the number it qualifies — stating the
        # level without it quotes a provisional reading as a settled one in the
        # sentence built to survive a summary. Folded INSIDE the as-of parenthesis
        # rather than trailing it, so the percentile clause's "it" still refers to
        # the reading: appended outside, the nearest antecedent became "a
        # still-open candle", and a candle does not sit at a percentile.
        # Past tense, and attributed to the read — mirroring the body line this
        # clause exists to carry into the summarisable sentence. `latest_is_open` is
        # computed from the clock taken BEFORE the DVOL fetch, so it is by
        # construction a statement about read time, not render time: across a
        # mid-fetch UTC midnight the present tense claimed a candle was *still* open
        # three lines under a header note saying the clock had passed that very
        # date. The body carrier was reworded for exactly this and its Reading-line
        # twin was left behind — the partial sweep this module keeps re-learning.
        so_far = "; that day's candle was still open when it was read, so this is the level so far"
        # Unconditional parentheses: as_of_phrase is populated on every report (see
        # DvolReport), so `detail` is never empty and the old `if detail else ""`
        # guarded a state that cannot occur — it read as though a bare level were
        # still reachable here, which is the opposite of the invariant.
        detail = dvol_report.as_of_phrase + (so_far if dvol_report.latest_is_open else "")
        as_of = f" ({detail})"
        # "latest usable", for the reason the body headline carries it: a rejected
        # candle can be newer, and this is the one sentence a downstream summary
        # keeps — so an unqualified claim here outlives the note that corrects it.
        level = (
            f"{currency}'s latest usable DVOL reading is "
            f"{dvol_report.latest:.2f}% annualized{as_of}"
        )
        if dvol_report.percentile is not None:
            # State the sample, not just the span. "the 365-day window" reads as
            # one observation per day, so a feed publishing only intermittently
            # across it still sounded like a full year of evidence once this clause was
            # quoted on its own — and this clause is exactly what gets quoted on
            # its own. The definition travels with it for the same reason: it
            # lived only on the body line, and "at or below" is what makes a
            # repeating feed print the 100th percentile.
            parts.append(
                f"{level}, and it sits at the {_ordinal(dvol_report.percentile)} percentile of "
                f"the {_readings(dvol_report.percentile_n)} in the "
                f"{DVOL_PERCENTILE_WINDOW_DAYS}-day window ending on the analysis date "
                f"(percentile = share of those readings at or below it)"
            )
        else:
            # Says the rank is absent AND why, rather than dropping the half. The
            # level is still the report's headline number and is stated either way.
            #
            # An EMPTY window is not a thin one: "0 usable daily readings, too few
            # to rank against" frames a window holding nothing as a shortfall, the
            # same distinction the body's two branches already draw.
            if dvol_report.percentile_n == 0:
                shortfall = "no usable daily reading falls inside that window at all"
            else:
                shortfall = (
                    f"{_readings(dvol_report.percentile_n)} in that window, too few to rank "
                    f"the level against"
                )
            parts.append(
                f"{level}, and no {DVOL_PERCENTILE_WINDOW_DAYS}-day percentile is in this "
                f"report ({shortfall})"
            )
    elif dvol_absence:
        parts.append(f"no {currency} DVOL level is in this report ({dvol_absence})")
    if not parts:
        return ""
    # Capitalise the joined sentence rather than each clause: "DVOL" is an
    # acronym, so per-clause case fixing would render it "dVOL".
    sentence = "; and ".join(parts)
    return "_Reading:_ " + sentence[0].upper() + sentence[1:] + "."


def _try_fetch(
    fetch: Callable[[], _FetchedT], label: str, currency: str
) -> tuple[_FetchedT | None, Exception | None]:
    """Run one half's fetch, turning any failure into ``(None, error)``.

    Catches ``Exception``, not only this module's own types. The whole point of
    the two halves is that one failing cannot cost the other, and an unforeseen
    exception escaping here would discard an already-successful half just as
    surely as a DeribitError would. ``exc_info`` so a genuine bug still leaves a
    traceback rather than looking like an ordinary outage.

    Generic in the fetch's return type so the successful branch keeps it: an
    ``object`` here erased ``DvolSeries`` and ``SkewSnapshot`` at the one point
    where both flow through the same helper, leaving the renderers' argument types
    unchecked.
    """
    try:
        return fetch(), None
    except Exception as e:
        logger.warning("Deribit %s unavailable for %s: %s", label, currency, e, exc_info=True)
        return None, e


def get_options_market_data(asset: str, curr_date: str) -> str:
    """Fetch Deribit DVOL history and 25-delta skew for a crypto asset as markdown.

    Args:
        asset: "BTC" or "ETH" (pair forms like "BTC-USD" are accepted). A
            recognized crypto risk asset this vendor does not read a chain for
            (SOL, XRP, ...) is served BTC's DVOL alone as a market-wide crypto-vol
            proxy — the skew is withheld, being a claim about one underlying's own
            demand for downside; a stablecoin or unrecognized symbol gets a
            no-signal message rather than a markdown report.
        curr_date: End of the DVOL window (yyyy-mm-dd). The chain half takes no
            date, so it is withheld when this is EARLIER than today's UTC date
            rather than backfilled from the present. A curr_date up to
            MAX_FUTURE_DAYS later than the UTC clock is served, with a note: the
            live chain is then never later than the analysis date, which is not
            lookahead — it predates that date outright, or falls inside it when
            the clock reaches the date mid-run, and the note says which.
            Further ahead than that is treated like a historical date and the
            chain is withheld, since curr_date is an LLM-supplied argument and
            past one day the gap is a mistake rather than a timezone.

    Returns:
        A markdown report: the latest DVOL with its 30-day range (when that window
        holds at least two readings) and its 365-day percentile (when that one
        holds enough for a percentile to mean anything), and — unless the chain is
        withheld — the selected expiry, ATM (50Δ) IV, the 25-delta wings with the
        strikes they were interpolated between, and the risk reversal, closing
        with one fixed-format sentence restating those figures. When no risk
        reversal is in the report that closing sentence says so and why (withheld
        by policy, a chain that yielded no usable surface, or wings the chain does
        not supply) rather than falling silent, since it is the line a downstream
        summary keeps; the same sentence states the DVOL level, or names that
        half's absence, on the same ground. An
        unrecognized symbol returns a bare no-signal sentence instead.

    Raises:
        VendorRateLimitError: when Deribit throttled every request actually made
            (one, where the chain is withheld), so the router can treat it as a
            throttle rather than a broken vendor.
        DeribitError: when nothing could be served — both halves failed, or DVOL
            failed on a date where the chain is withheld by design. The routing
            layer then degrades the optional options_data category to a
            sentinel. Whenever either half survives, the report renders it and
            names what is missing instead of raising. Also raised for a truthy
            non-string ``asset``: validated into this type rather than being
            left to escape as a built-in, since the router would report it as a
            vendor outage.

    An unusable ``curr_date`` is not raised at all: it answers the shared
    ``INVALID_CURR_DATE`` sentinel before any request, as the core-category
    tools do (#119). It used to be a DeribitError, which the router's optional
    lane rendered as DATA_UNAVAILABLE — "this source is down" — for what was
    the caller's own argument, while the OHLCV tool in the same turn told the
    model to fix the date and retry.
    """
    refusal = date_refusal(curr_date, what="options market data", kind="point")
    if refusal is not None:
        return refusal
    # Normalise curr_date BEFORE any date comparison. strptime accepts
    # non-zero-padded input ("2026-6-5"), which then compares wrong against the
    # canonical ISO candle dates — '2026-12-31' <= '2026-6-5' is True — silently
    # admitting future readings and defeating the lookahead guard. (farside and
    # fear_greed carry the same guard.) The refusal above already proved this
    # parses.
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    # The caller-supplied ``asset`` is validated into the vendor's error type
    # rather than left to escape as a built-in. A truthy non-string asset (a
    # resolved-ticker object, a list) reaches
    # ``normalize_symbol((asset or "").replace(...))`` and escapes as
    # AttributeError into route_to_vendor's generic `except Exception` lane,
    # which logs it with exc_info and reports "Vendor 'deribit' failed" — a
    # caller's bug dressed up as a Deribit outage, complete with a traceback
    # naming Deribit. Falsy values are already safe: they render the no-signal
    # sentence.
    # ``bytes`` has to be caught by THIS guard rather than left to a duck-typed
    # one: ``bytes.replace`` exists, so a hasattr check passes it straight through
    # to ``normalize_symbol``, where ``.replace("/", "-")`` then raises TypeError
    # on the str arguments. An isinstance test against ``str`` rather than a
    # duck-typed one, so it also turns away string-likes such as
    # collections.UserString — narrower than before, and matching what the tool's
    # own ``Annotated[str, ...]`` already declares. isinstance and deliberately
    # NOT ``type(asset) is str``: a str subclass is a genuine symbol string and
    # every str operation below works on it, so it is accepted.
    if asset and not isinstance(asset, str):
        raise DeribitError(f"asset must be a symbol string, got {type(asset).__name__}")
    curr_date = curr_dt.strftime("%Y-%m-%d")

    # Flattened BEFORE classification, not after, so exactly one string is both
    # decided on and rendered. ``asset`` is a tool argument the analyst LLM writes
    # and is rendered at many sites — the no-signal sentence, the proxy heading
    # and its caveat, the body line, the Reading clause and its proxy suffix, and
    # more than one raise — so every
    # copy is a chance to forge a heading or a second Reading line. Uncounted on
    # purpose: this comment carried the number eight beside a list naming six, and
    # said "one raise" where there are two. Grep `{asset}` for the live set.
    # Sanitising
    # after ``_classify_asset`` made the two disagree: "`BTC`" classified on the
    # raw string (unrecognized) but rendered from the flattened one, producing
    # "'BTC' is not a recognized crypto risk asset" as the report's ONLY content,
    # with nothing else in it to correct the claim.
    #
    # After the isinstance guard, so a non-string is still rejected as a caller's
    # bug rather than silently stringified into a symbol.
    asset = _sanitize(asset, limit=_MAX_UNTRUSTED_CHARS)
    currency, market_proxy = _classify_asset(asset)
    if currency is None:
        # Not a recognized crypto risk asset. Like farside's equivalent path this
        # is a "no signal" statement rather than an error, so it is returned (more
        # useful to the analyst than a generic degraded-category sentinel).
        return (
            f"This vendor reads Deribit options for {' and '.join(SUPPORTED_CURRENCIES)} only, "
            f"and '{asset}' is not a recognized crypto risk asset for which BTC's DVOL level "
            f"serves as a market-wide proxy (e.g. a stablecoin or an unrecognized symbol). Do "
            f"not substitute BTC or ETH implied volatility for it."
        )

    now = _utc_now()
    today = now.strftime("%Y-%m-%d")
    # The chain endpoint takes no date, so its figures are always the present.
    # Serving them for a PAST curr_date would be future information, and a prose
    # warning is not a guard — whether it holds depends on the model choosing to
    # obey it, and nothing in the run records whether it did — so that half is
    # simply withheld. Note the test is "earlier than the clock", not "not equal
    # to it": callers derive curr_date from a local clock (cli/main.py does), so
    # east of UTC it routinely runs a few hours AHEAD of today. The live chain is
    # then never later than the analysis date, which is not lookahead at all, and
    # treating it as such would withhold this vendor's main signal for the first
    # hours of every local day. The DVOL half is genuinely date-filtered and is
    # served either way.
    is_historical = curr_date < today
    # Naive on both sides (``today`` is re-parsed rather than compared as text) so
    # this is a day count, not a string ordering.
    days_ahead = (curr_dt - datetime.strptime(today, "%Y-%m-%d")).days

    # Why the chain half is not in the report, classified ONCE. Several later sites
    # have to explain it — the both-halves-failed raise, the header note, the
    # section body and the Reading line's chain_absence — and re-testing
    # is_historical/market_proxy at each of them means the precedence between the
    # reasons is re-decided by hand every time, so a new reason can update some of
    # them and silently miss the rest. (This is now a four-valued classification for
    # exactly that reason.) The consumers are named but not COUNTED: this comment
    # said "three" while there were four, having been written when there were three
    # — grep `chain_withheld` for the live set rather than trusting a number here.
    #
    # A proxied asset gets the DVOL level but never the skew. A market-wide
    # volatility LEVEL is a defensible stand-in — crypto vol moves together, which
    # is what makes BTC's index the benchmark — but a 25-delta risk reversal is a
    # statement about how much more traders will pay for downside in one specific
    # underlying, and that does not transfer: alt skew responds to different flow
    # and sits at a different level. Withheld rather than caveated, for the same
    # reason the historical chain is: the caveat has to survive every downstream
    # summarisation hop, and the number does not.
    #
    # Historical outranks proxy when both hold (a past date requested for SOL):
    # the lookahead guard is the stronger claim and names a date the reader asked
    # for, so it is the more useful of the two explanations.
    chain_withheld: str | None = None
    if is_historical:
        chain_withheld = "historical"
    elif days_ahead > MAX_FUTURE_DAYS:
        chain_withheld = "far_future"
    elif market_proxy:
        chain_withheld = "proxy"

    # The two halves are fetched independently so one outage cannot cost both.
    dvol, dvol_error = _try_fetch(lambda: _fetch_dvol(currency, curr_dt, today), "DVOL", currency)
    skew: SkewSnapshot | None = None
    skew_error: Exception | None = None
    # Both set whenever the chain is actually fetched. ``skew`` stays None
    # otherwise, so the empty default never reaches _skew_section, and
    # ``chain_date`` is only read on branches guarded by ``skew is not None``.
    snapshot_time = ""
    chain_date = today
    if chain_withheld is None:
        # Re-read the clock instead of reusing the one taken above. DVOL is
        # fetched first and its worst case is two timeouts plus the retry sleep
        # (~62s), so the earlier instant is not when the book was read — and
        # across a UTC midnight it is not even the same day, which would serve a
        # D+1 chain for curr_date = D. That is precisely the lookahead the
        # withholding rule exists to prevent, so the rule is re-tested against the
        # clock the fetch will actually use rather than against a stale one.
        chain_now = _utc_now()
        chain_date = chain_now.strftime("%Y-%m-%d")
        if curr_date < chain_date:
            chain_withheld = "historical"
        else:
            snapshot_time = chain_now.strftime("%Y-%m-%dT%H:%M:%SZ")
            skew, skew_error = _try_fetch(
                lambda: _fetch_chain(currency, chain_now), "options chain", currency
            )

    # A curr_date that was NOT historical at classification time but is by the time
    # the chain is fetched: the clock crossed UTC midnight during the DVOL half.
    # Derivable rather than tracked as a flag: only the mid-run re-take can withhold
    # as historical while curr_date is still >= the clock the run started on.
    #
    # Derived HERE, above the both-halves-failed raise, rather than below it. It
    # used to sit under that block, so the raise was the one consumer of
    # "historical" that could not read it and re-decided the wording by hand — and
    # got it wrong in exactly the state the variable exists to isolate, calling
    # today "the historical date" while its three rendered siblings all refuse to.
    # That is the partial-update failure chain_withheld was introduced to prevent,
    # reappearing one scope up.
    withheld_mid_run = chain_withheld == "historical" and curr_date >= today

    if dvol is None and skew is None:
        # Keep a throttle in the router's rate-limit lane whichever path got here.
        # Collapsing it into a DeribitError would make a temporary throttle look
        # like a broken vendor to any future multi-vendor chain, which is the very
        # distinction _request raises VendorRateLimitError to preserve. Judged over
        # the requests actually made, so it still holds on a historical date where
        # the chain was never attempted and DVOL's 429 is the only verdict there
        # is — and checked before the historical branch below, which would
        # otherwise re-classify that same throttle.
        # (_try_fetch never reports None without an error, so this list is never
        # empty on the path that reaches here.)
        attempted = [e for e in (dvol_error, skew_error) if e is not None]
        if all(isinstance(e, VendorRateLimitError) for e in attempted):
            raise VendorRateLimitError(f"Deribit rate-limited every request made for {currency}")
        # Every message below is handed to the model by route_to_vendor as
        # "DATA_UNAVAILABLE: optional options_data could not be retrieved
        # ({error})", so the cause text is flattened here for the same reason the
        # rendered branches flatten it. Not every exception reaching this point
        # was authored by this module: a requests failure carries the URL and
        # query string, and an unforeseen bug carries whatever repr it has.
        dvol_reason = _sanitize(dvol_error)
        # A proxied asset's chain is never served on ANY date, and the two
        # withholding reasons that OUTRANK "proxy" say nothing about that. On the
        # rendered path the proxy fact still reaches the reader twice — the heading
        # and its italic caveat — and _reading_line re-adds it for precisely this
        # reason; here there is no report at all, so this string is everything the
        # operator and the model get. Without it a SOL backtest reads as "not
        # served for the historical date", implying a live date would serve it. It
        # never will. The caller's own symbol appears for the same reason: naming
        # only the proxy currency shows an operator a BTC failure for a SOL request.
        proxy_note = (
            f", and this vendor reads no options chain for '{asset}' on any date"
            if market_proxy and chain_withheld != "proxy"
            else ""
        )
        if chain_withheld == "historical":
            # Read off withheld_mid_run rather than re-deciding it: the post-DVOL
            # midnight re-test also classifies as "historical", with curr_date
            # still equal to today, and calling that "the historical date"
            # contradicts the three rendered sites corrected for exactly this.
            basis = (
                f"the analysis date {curr_date} — which the UTC clock passed while this "
                f"report was being built"
                if withheld_mid_run
                else f"the historical date {curr_date}"
            )
            raise DeribitError(
                f"Deribit DVOL is unavailable for {currency} ({dvol_reason}), and the options "
                f"chain is not served for {basis}{proxy_note}"
            )
        if chain_withheld == "far_future":
            # Past tense and naming the clock — the eighth site of a sweep that
            # covered seven. `today` and `days_ahead` are both taken BEFORE the
            # DVOL fetch, and this is the ONE branch that requires that fetch to
            # have burned its full retry envelope (~62s) to be reached at all, so
            # it carries the highest prior of a midnight crossing of any site the
            # sweep touched, and it was the site the sweep missed.
            raise DeribitError(
                # Parenthesised for the reason chain_absence's far-future branch is:
                # proxy_note appends ", and this vendor reads no options chain for
                # '{asset}' on any date", and against a trailing "which ..." clause
                # that reads as one more remark about the DATE — the exact
                # misreading proxy_note exists to prevent. Both far-future sites
                # carry the suffix and both needed the brackets; only one of them
                # was found first.
                f"Deribit DVOL is unavailable for {currency} ({dvol_reason}), and the options "
                f"chain is not served for {curr_date} (which was {days_ahead} days ahead of "
                f"the UTC clock ({today}) when this report was built){proxy_note}"
            )
        if chain_withheld == "proxy":
            raise DeribitError(
                f"Deribit DVOL is unavailable for {currency} ({dvol_reason}), and the options "
                f"chain is not served for '{asset}', which has no Deribit chain of its own"
            )
        raise DeribitError(
            f"Deribit returned neither DVOL nor an options chain for {currency} "
            f"(DVOL: {dvol_reason}; chain: {_sanitize(skew_error)})"
        )

    if market_proxy:
        # The requested asset belongs in the heading, not only in a caveat below:
        # this report is re-summarised by downstream agents, and a hop that keeps
        # the heading would otherwise carry one byte-identical to a real BTC
        # report, with the proxy framing stripped off.
        header_lines = [
            f"## Options Volatility — {currency} (market-wide proxy for '{asset}', Deribit)",
            f"_This vendor reads no options chain for '{asset}'; showing the {currency} DVOL "
            f"level as a market-wide crypto-vol proxy, not a signal specific to '{asset}'. "
            f"The 25Δ skew is "
            f"NOT shown: a risk reversal measures demand for downside in {currency} itself and "
            f"does not carry across to '{asset}'._",
        ]
    else:
        header_lines = [f"## Options Volatility — {currency} (Deribit)"]
    # Without saying so the report calls today's date "a historical date" and gives
    # the reader nothing to reconstruct why — the historical branch, unlike the
    # far-future one, never prints the clock. ``withheld_mid_run`` is derived above
    # the both-halves-failed raise and reused by all FOUR consumers (that raise,
    # this note, the body section and the Reading line), which otherwise re-decide
    # the distinction by hand — how the sibling notes drifted apart five times.
    crossed_midnight = (
        " (the UTC clock passed the analysis date while this report was being built)"
        if withheld_mid_run
        else ""
    )
    if chain_withheld == "historical":
        header_lines.append(
            f"_Historical date{crossed_midnight}: Deribit's options chain is a live endpoint "
            f"with no history, so the ATM IV, 25Δ wings, RR25 and forward are NOT served for "
            f"{curr_date} — quoting the current chain would be future information. The DVOL "
            f"history below IS filtered to {curr_date} and is safe to use._"
        )
    elif chain_withheld == "far_future":
        header_lines.append(
            # Past tense and attributed, for the same reason the sibling note at
            # the branch below is: `today` and `days_ahead` are both derived from
            # the clock taken BEFORE the DVOL fetch, and that fetch can span UTC
            # midnight — after which a present-tense "is N days ahead of the UTC
            # clock (X)" names a clock that has moved and a count one too large,
            # three lines above a DVOL reading dated later than X. This is the
            # fifth site of that defect; the previous four were all in the sibling
            # note, which was rewritten three times without this one being read.
            f"_Analysis date {curr_date} was {days_ahead} days ahead of the UTC clock ({today}) "
            f"when this report was built, further than any timezone offset explains. Deribit's "
            f"options chain is a live "
            f"endpoint, so the ATM IV, 25Δ wings, RR25 and forward would be the CURRENT book, "
            f"not "
            f"{curr_date}'s, and they are NOT served. The DVOL history below IS filtered to "
            # The same fact as the ahead-of-clock note's DVOL sentence, stated
            # differently on purpose. That sibling now reads "the feed's newest
            # usable reading is X", because a candle this module rejected leaves
            # the feed's own reach further forward. Here the distinction cannot
            # arise: days_ahead >= 2, so the feed has genuinely not reached
            # curr_date whatever it published, and "a date the feed has not
            # reached" is the more direct claim.
            f"{curr_date}; its windows end at a date the feed has not reached, so they hold fewer "
            f"readings than their full spans._"
        )
    elif curr_date > today:
        # A curr_date past the UTC clock (a caller using a local date east of UTC).
        # The chain is still served — it is never LATER than the analysis date, so
        # it is not lookahead — but it is not "as of" that date either, and the
        # DVOL windows run past the data rather than the data stopping short.
        # "Never later", not "predates": the two sentences below render the two
        # cases separately, since the clock can reach the analysis date while the
        # report is being built and the book then falls inside it rather than
        # before it. Six sibling sites were corrected for this; the comment sitting
        # directly above the code was the seventh.
        #
        # Each sentence is gated on the half it describes ACTUALLY being present,
        # not on whether it was withheld by policy: a half that was attempted and
        # failed is equally absent, and this note is an italic caveat — exactly the
        # line a downstream summary keeps when it drops the body. Claiming "the
        # chain figures below are the live book as of ..." three lines above "the
        # chain request failed" hands the next agent a live-book reading that was
        # never fetched. At least one half is non-None here; both failing raises
        # above.
        sentences = [
            f"_Analysis date {curr_date} was ahead of the UTC clock ({today}) when this "
            f"report was built."
        ]
        if skew is not None:
            # Dated by the clock taken immediately BEFORE the chain fetch, not by
            # the one taken before the DVOL half. (Not the read instant itself: the
            # chain GET has its own envelope, so a retry across midnight can still
            # leave this a day behind. That residue is ~62s wide and unfixable
            # without timing the response, whereas the DVOL-half gap it replaces
            # was the common case.) The two clocks differ by up to DVOL's whole
            # envelope
            # (~62s), and across UTC midnight they are different days — so on an
            # east-of-UTC run started just before midnight this sentence claimed a
            # book "as of {today}, BEFORE the analysis date" three lines above a
            # **Chain snapshot** stamped the next day, inside the analysis date.
            # Re-clocking the fetch without re-clocking the sentence that describes
            # it left the caveat asserting the very thing it exists to deny.
            #
            # chain_date can only be <= curr_date here: a later one is exactly what
            # the midnight re-test above withholds on. In the first branch it is
            # further provably EQUAL to today — reaching it with a crossed midnight
            # would need curr_date >= today + 2, which is withheld as far_future —
            # so substituting `today` there is an equivalent mutation and no test
            # can distinguish it. It stays `chain_date` because the second branch
            # is the one that was wrong, and because widening MAX_FUTURE_DAYS would
            # make the two diverge here as well.
            if chain_date < curr_date:
                sentences.append(
                    f"The chain figures below are the live book as of {chain_date}, which is "
                    f"BEFORE the analysis date rather than after it."
                )
            else:
                sentences.append(
                    f"The chain figures below are the live book as of {chain_date}: the UTC "
                    f"clock reached the analysis date while this report was being built, so "
                    f"they fall within it rather than before it."
                )
        if dvol is not None and dvol.latest_date < curr_date:
            # Gated on the DATA, not on a clock. Two previous wordings here were
            # clock-derived and both went stale: "a date that has not arrived"
            # contradicted the chain sentence once the clock crossed into
            # curr_date mid-run, and "had not arrived when they were read" was
            # asserted from `today`, an instant captured BEFORE the DVOL fetch that
            # can itself span midnight. Whether the windows actually fall short is
            # decidable from what came back — the feed either reached the analysis
            # date or it did not — so it is read off the series instead of inferred
            # from a clock that may have moved. When the feed did reach curr_date
            # the windows are full and there is no shortfall to declare, which is
            # why this is now gated rather than reworded again.
            sentences.append(
                # "newest usable reading", not "the feed has only reached": a
                # candle this module rejected leaves the feed's own reach further
                # forward than this date, so the unqualified phrasing is false
                # exactly when EITHER rejection note is printing (there are two
                # classes now, non-positive and self-contradicting). One of many
                # sites carrying that word — grep "usable" rather than trusting a
                # count here, which is how this comment went stale before.
                f"The DVOL windows end at the analysis date but the feed's newest usable "
                f"reading is {dvol.latest_date}, so they hold fewer readings than their "
                f"full spans."
            )
        # Both consequence sentences are separately gated, so both can be absent at
        # once — a proxied asset (or a failed chain) whose DVOL feed did reach
        # curr_date. The opening sentence alone states a fact with no consequence
        # attached, in the italic line a downstream summary keeps, so the note is
        # dropped entirely rather than rendered content-free.
        if len(sentences) > 1:
            # Every sentence already ends in a full stop; only the closing italic
            # marker is appended.
            header_lines.append(" ".join(sentences) + "_")
    # A chain that was ATTEMPTED and FAILED. Each of the three withheld-by-policy
    # states above prints an italic header note; an outage printed none, so the
    # absence lived only in a bold body line — and the italic line is exactly what
    # a downstream summary keeps when it drops the body. That asymmetry made a
    # cycle whose chain request died read as a complete report one hop later.
    #
    # Appended rather than folded into the elif chain above, because these are
    # independent facts: an ahead-of-clock run can also fail its chain, and both
    # notes are then true and both belong in the report.
    if chain_withheld is None and skew is None:
        header_lines.append(
            # "No 25Δ skew could be derived", not "the chain could not be read".
            # Nearly every reachable cause is a SUCCESSFUL 200 — an empty chain, a
            # payload that is not a list, no row surviving the checks, no expiry in
            # the tenor band, no usable forward, or a 200 carrying a JSON-RPC error
            # object or no "result" field — and only a genuine network fault is
            # not. (Enumerated rather than counted: the count said "four of five"
            # and was short by the empty-chain state and both _request cases, so it
            # went stale the moment a cause was added.) That is exactly why the
            # body branch below is worded as it is. This note and the Reading clause were
            # left on the network wording, so the italic line and the summarisable
            # line both told a reader to retry in the one state where retrying
            # cannot help: a book with no ~30-day expiry listed.
            "_No 25Δ skew could be derived from the options chain for this report, so the ATM "
            "IV, 25Δ wings, RR25 and forward are absent — absent, not flat and not zero. The "
            "section below says why. The DVOL history is unaffected._"
        )
    # The mirror of the chain note above, and the reason it exists: the three
    # withheld-by-policy chain states and the chain outage all print an italic
    # header line, while a failed DVOL half printed none. Its only header-level
    # trace was SUBTRACTIVE — the "| DVOL window ending ..." clause silently
    # dropped from the source bullet — so a summariser that keeps the italic lines
    # saw a report with no caveats at all, in a cycle whose implied-vol level, its
    # range and its percentile were never read. Reachable only alongside a live
    # chain: both halves absent raises above.
    if dvol is None:
        header_lines.append(
            # Spans interpolated from the constants, not spelled out: every other
            # prose site describing these two windows had to be swept by hand when
            # the percentile window moved 30 -> 365, and a fixed "365-day" here
            # would be the next site to miss.
            # "No DVOL level is served", not "no history could be derived": the
            # staleness ceiling refuses a fetch that returned history and found a
            # usable reading in it, and the body line three lines down names that
            # reading's date. This is the other italic a summariser keeps, so it
            # must hold for every cause the section below can give.
            f"_No DVOL level is served in this report, so the implied-vol level, "
            f"its {DVOL_WINDOW_DAYS}-day range and its {DVOL_PERCENTILE_WINDOW_DAYS}-day "
            f"percentile are absent — absent, not flat and not zero. The section below says "
            f"why. The chain figures are unaffected._"
        )
    # The DVOL clause is gated on the half existing, for the same reason every
    # sentence of the ahead-of-clock note is: unconditional, it advertised a
    # window immediately above "**DVOL:** unavailable".
    source_line = "- Source: deribit.com public API"
    if dvol is not None:
        source_line += f" | DVOL window ending {curr_date}"
    header_lines.append(source_line)

    sections = []
    dvol_report: DvolReport | None = None
    if dvol is not None:
        dvol_report = _dvol_section(dvol, curr_dt, today)
        sections.append(dvol_report.markdown)
    else:
        # Not "the request failed": several of _fetch_dvol's raises follow a request
        # that SUCCEEDED — a response holding no reading on or before curr_date, one
        # whose every candle was rejected as broken, and one whose newest usable
        # reading was simply too old to serve. Naming the wrong cause sends an
        # operator to the network first, and the staleness case is the one they
        # would misdiagnose longest, since the feed is answering perfectly well.
        # Uncounted deliberately: grep `raise DeribitError` in _fetch_dvol for the
        # live set rather than trusting a number here.
        sections.append(
            f"**DVOL:** unavailable — {_sanitize(dvol_error)}. Do not fabricate a DVOL level."
        )
    if skew is not None:
        sections.append(_skew_section(skew, snapshot_time))
    elif chain_withheld == "historical":
        # The third site of the mid-run distinction, and the one the "derive it
        # once" refactor did not reach: calling curr_date "a historical analysis
        # date" is false when the clock crossed into it mid-run, which is the
        # same mis-framing the Reading line was corrected for.
        basis = (
            "an analysis date the UTC clock passed while this report was being built"
            if withheld_mid_run
            else "a historical analysis date"
        )
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** not served for {basis} — see the note "
            f"above. Do not substitute the current chain's skew for {curr_date}."
        )
    elif chain_withheld == "far_future":
        sections.append(
            # Past tense and naming the clock, like the header note and the Reading
            # clause: `days_ahead` and `today` are both taken BEFORE the DVOL
            # fetch, which can span UTC midnight, after which a present-tense "is
            # N days ahead of the UTC clock" states a count one too large against
            # a clock it does not name. This was the seventh site.
            f"**Options chain (ATM IV / 25Δ skew):** not served for an analysis date that "
            f"was {days_ahead} days ahead of the UTC clock ({today}) when this report was "
            f"built — see the note above. Do not substitute the current chain's skew for "
            f"{curr_date}."
        )
    elif chain_withheld == "proxy":
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** not served for '{asset}' — see the note "
            f"above. {currency}'s skew describes demand for downside in {currency}, not in "
            f"'{asset}'. Do not substitute it."
        )
    else:
        # Not "the chain request failed": nearly every reachable cause comes from
        # a SUCCESSFUL 200 — an empty chain, a payload that is not a list, no row
        # surviving the name/IV/underlying/open-interest checks, no expiry inside
        # the tenor band, no usable forward, or a 200 carrying a JSON-RPC error
        # object or no "result" field. Naming the network sends an operator to look
        # for an outage that will never pass, and does it in the same sentence
        # that quotes the module saying otherwise. Same fix already applied to the
        # DVOL branch above.
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** unavailable — {_sanitize(skew_error)}. The "
            f"DVOL figures above are unaffected; do not fabricate skew."
        )

    # Why no 25Δ skew is in this report, worded for the Reading line. Read off the
    # same four-valued classification its sibling consumers use, so a future
    # withholding reason cannot update them and silently miss this one — the
    # partial-update failure chain_withheld exists to prevent. Deliberately not
    # counted or listed: an earlier version named two of them and there are more
    # than two, and every counted enumeration in this module has gone stale. Grep
    # `chain_withheld ==` for the live set.
    chain_absence: str | None = None
    if skew is None:
        if withheld_mid_run:
            # "the past analysis date" is false here and self-contradicting in
            # this particular line: the DVOL clause beside it quotes that same
            # date as the latest reading. Only the mid-run re-clock withholds as
            # historical while curr_date is still >= the clock the run started on,
            # which is what withheld_mid_run isolates.
            chain_absence = (
                f"the UTC clock passed the analysis date {curr_date} while this report was "
                f"being built, and Deribit's options chain is a live endpoint with no history"
            )
        elif chain_withheld == "historical":
            chain_absence = (
                f"the options chain is not served for the past analysis date {curr_date}"
            )
        elif chain_withheld == "far_future":
            # Past tense and naming the clock, for the reason the sibling header
            # note documents at length: `today` and `days_ahead` are both taken
            # BEFORE the DVOL fetch, and that fetch can span UTC midnight, after
            # which a present-tense "is N days ahead of the UTC clock" states a
            # count one too large against a clock that has moved. Naming `today`
            # lets the reader reconstruct it, and this line has to stand alone.
            # The clock clause is parenthesised rather than left as a trailing
            # "which ..." relative clause. The proxy suffix below appends ", and
            # this vendor reads no options chain for '{asset}' on any date", which
            # against a trailing relative clause reads as one more thing said about
            # the DATE — re-creating, in grammar, exactly the misreading the suffix
            # was added to prevent. The historical sibling has no trailing clause
            # and so never had the problem; this branch was the only one that did.
            chain_absence = (
                f"the options chain is not served for {curr_date} (which was {days_ahead} days "
                f"ahead of the UTC clock ({today}) when this report was built)"
            )
        elif chain_withheld == "proxy":
            chain_absence = f"this vendor reads no options chain for '{asset}'"
        else:
            # A cause, not a restatement: the template around it already says no
            # skew is in the report, so "no skew could be derived" read as a
            # tautology there. The lead-in is worded to hold for EVERY reachable
            # cause — the request genuinely failing, and the several successful-200
            # payloads this module cannot build a surface from — because "could not
            # be read" named only the first and pointed the reader at the network.
            # The sanitized cause is inlined, as the DVOL clause below inlines its
            # own: this line exists for the summary that DROPS the body, so the
            # "the section above says why" it used to end with handed the one
            # surviving sentence a pointer to text that did not survive — while
            # its siblings, the policy branches above and the DVOL clause below,
            # all carry their cause inside the clause itself.
            chain_absence = (
                f"the chain request did not yield a usable surface — {_sanitize(skew_error)}"
            )
        if market_proxy and chain_withheld != "proxy":
            # historical/far_future outrank proxy in chain_withheld, which is a
            # settled precedence — but at the header and body the proxy fact is
            # still carried, by the heading and its caveat. This line has no such
            # second carrier, so without this the summarisable sentence says SOL's
            # skew was withheld because of the DATE, implying a live date would
            # serve it. It never will.
            chain_absence += f", and this vendor reads no options chain for '{asset}' on any date"

    # Why no DVOL level is in this report, worded for the Reading line. The cause
    # text is the same one the body line carries; a reader deciding whether to wait
    # for the next cycle needs it as much here as there.
    #
    # The wrapper states only that no LEVEL could be served, and leaves the cause
    # entirely to the interpolated reason. It used to assert "no usable DVOL history
    # came back", which was true of every reason then reachable — and false of the
    # first one added since: MAX_DVOL_STALENESS_DAYS refuses a fetch that succeeded,
    # returned history, and yielded a usable reading, rejecting it only for age. The
    # sentence then contradicted its own next clause, which names that reading and
    # its date, in the one line a downstream summary keeps. Worded to hold for EVERY
    # reachable cause, exactly as chain_absence's fallback branch already is, so the
    # next cause added cannot falsify it either.
    # No currency here: _reading_line already opens the clause with "no {currency}
    # DVOL level is in this report", so naming it again rendered the phrase twice
    # inside one sentence.
    dvol_absence = (
        f"the DVOL half could not be served — {_sanitize(dvol_error)}" if dvol is None else None
    )
    reading = _reading_line(currency, skew, dvol_report, dvol_absence, chain_absence)
    if reading:
        sections.append(reading)

    # Blank line between entries so consecutive italic caveats are not rendered as
    # one run-on paragraph.
    return "\n\n".join(header_lines) + "\n\n" + "\n\n".join(sections) + "\n"
