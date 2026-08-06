"""Deribit options vendor: the DVOL implied-volatility index plus 25-delta skew.

Two independent signals from Deribit's keyless public API, surfaced to the market
analyst as a crypto-only volatility-regime read:

* **DVOL** — Deribit's 30-day forward implied-volatility index, a daily history.
  Lookahead-safe to the day: the fetch window ends at ``curr_date`` and rows
  dated after it are dropped, so a past ``curr_date`` never sees a later reading.
  Day granularity is the limit of the guarantee — the candle dated ``curr_date``
  covers all of that UTC day — and the candle dated *today* is still open, which
  the report labels and which is why every line calls the series "readings"
  rather than "closes".
* **25-delta skew** — computed here from the live options chain: ATM (50-delta)
  implied vol, the 25-delta call and put vols, and the risk reversal between
  them. Read for one expiry inside a bounded band around 30 days, because a risk
  reversal is not comparable across tenors and every downstream agent reads this
  as a ~30-day figure. The chain endpoint takes no date, so it can only ever
  describe the present. It is **withheld for a ``curr_date`` earlier than
  today**, and the report says so, because quoting today's chain on a past date
  is future information and a prose warning is not an auditable guard. A
  ``curr_date`` up to ``MAX_FUTURE_DAYS`` *later* than the UTC clock is served
  with a note: callers derive it from a local clock, so east of UTC it routinely
  runs a few hours ahead, and the live chain is then older than the analysis date
  rather than newer. Further ahead than that is a bad argument rather than a
  timezone, and the chain is withheld again.

The two halves fail independently. Losing the chain should not also cost the
DVOL history (and vice versa), so each is fetched inside its own try and the
report renders whichever survived, naming what is missing. This module raises
only when nothing at all can be served — both halves failed, or DVOL failed on a
date, or for a proxied asset, where the chain is withheld by design — and the routing layer
then degrades the optional ``options_data`` category to a sentinel. A throttle
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
from typing import NamedTuple, TypeVar

import requests

from .errors import VendorError, VendorRateLimitError
from .symbol_utils import CRYPTO_BASES, normalize_symbol

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
# windows and never enter the statistics — what they buy is a *latest reading* (and
# therefore a report at all, rather than "No DVOL readings on or before ...") when
# the feed has published nothing for the whole window.
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
# 24/7 market, so anything past a couple of days means the feed itself stalled.
MAX_DATA_LAG_DAYS = 2

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
# surface: the nearest, and — if it cannot bracket both 25-delta wings — the
# next-nearest. Deliberately not the whole ranking, even now that the tenor band
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

    ``atm`` / ``call_25`` / ``put_25`` are None when that point could not be
    *bracketed* by two strike-adjacent listed contracts. Nothing is extrapolated:
    a wing the chain does not reach is reported missing rather than guessed.

    ``n_calls`` / ``n_puts`` count the contracts of each type on this expiry that
    yielded a usable delta — the quotes the interpolation actually had to work
    with, which need not be every contract Deribit listed for the expiry.

    ``is_fallback`` is True when this is NOT the expiry nearest the target tenor,
    i.e. the nearest one could not be used — it could not bracket both wings, or it
    carried no usable forward — and ``compute_skew`` stepped once past it. It exists
    so the report cannot keep calling the expiry "the eligible expiry closest to 30
    days" while showing a different one. Both candidates come from the same tenor
    band, so a fallback is still a ~30-day expiry.
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
    these "readings": the candle dated today is still open, so its value is the
    level so far rather than a settled close.
    """

    dates: list[str]
    closes: list[float]

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
    to exist solely as a staleness warning, so a reading one or two days old — the
    ordinary case, below MAX_DATA_LAG_DAYS — reached the reading line with no date
    at all, and that line is the one clause downstream agents quote on its own.
    The "N days old" half is still appended only past the threshold.
    """

    markdown: str
    percentile: float | None
    percentile_n: int
    as_of_phrase: str


def _utc_now() -> datetime:
    """The single UTC clock source (tests patch this one function)."""
    return datetime.now(timezone.utc)


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
                raise DeribitError(f"Deribit rejected the {endpoint} request: {detail}")
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
    """True only for a real, finite number (not a bool, NaN, or Infinity).

    bool is an int subclass, so a JSON ``true``/``false`` must not pass as a price
    or a vol. ``json`` decodes ``NaN``/``Infinity`` to non-finite floats, which
    would poison every comparison downstream (every comparison with NaN is False)
    and render as a literal "nan" figure.
    """
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


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


def parse_instrument_name(name: object) -> tuple[str, datetime, float, bool] | None:
    """Parse ``BTC-28AUG26-100000-C`` into ``(expiry token, expiry, strike, is_call)``.

    Returns None — the caller skips the contract — for anything that does not fit
    that four-part shape: a future/perpetual name, a combo leg, the ``d``-separated
    decimal-strike form Deribit uses on currencies this module does not serve, or a
    strike that is not a positive finite number.
    """
    if not isinstance(name, str):
        return None
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _, expiry_token, strike_text, option_type = parts
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
    return expiry_token.strip().upper(), expiry_dt, strike, option_type == "C"


def parse_chain(rows: object, now: datetime) -> list[Contract]:
    """Turn a ``get_book_summary_by_currency`` result into usable Contracts.

    Rows that cannot be used are skipped, not fatal: the chain carries hundreds of
    contracts and a handful of unquoted or oddly-named ones is normal. A row is
    kept only with a parseable option name, a strictly positive finite mark IV,
    underlying price and open interest, and an expiry still in the future at
    ``now``.

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
    contracts: list[Contract] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_instrument_name(row.get("instrument_name"))
        if parsed is None:
            continue
        expiry_token, expiry_dt, strike, is_call = parsed
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
    d1 = (math.log(forward / strike) + 0.5 * sigma * sigma * years_to_expiry) / denominator
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
    quotes around that pair are not a monotone smile. Both mean the same thing to
    the caller: the chain does not support this point, and inventing it is the
    fabrication this codebase refuses everywhere else.
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
            return None
        span = delta_b - delta_a
        if span == 0:
            # Both neighbours sit exactly on the target; neither is more correct
            # than the other, so take their midpoint.
            iv = (iv_a + iv_b) / 2.0
        else:
            iv = iv_a + (target_delta - delta_a) / span * (iv_b - iv_a)
        return WingQuote(iv, strike_low, strike_high)
    return None


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
    brackets both wings the first one's snapshot is returned, so the forward, ATM
    and whichever wing exists are still reported.

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
    fallback: SkewSnapshot | None = None
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
        if fallback is None:
            fallback = snapshot
    if fallback is not None:
        return fallback
    if first_error is not None:
        raise first_error
    # Unreachable: ranked is non-empty, so the loop ran at least once, and every
    # iteration either returns, records a fallback, or records a first_error.
    # Spelled out rather than silenced with a type: ignore, so a future edit that
    # breaks that reasoning fails with a message instead of a None-raise.
    raise DeribitError(
        f"No usable expiry among the {len(ranked[:_MAX_EXPIRY_CANDIDATES])} candidates tried"
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

    return SkewSnapshot(
        expiry=expiry,
        days_to_expiry=days_to_expiry,
        forward=forward,
        atm=atm,
        call_25=interpolate_iv_at_delta(call_quotes, WING_DELTA),
        put_25=interpolate_iv_at_delta(put_quotes, -WING_DELTA),
        n_calls=len(call_quotes),
        n_puts=len(put_quotes),
        is_fallback=is_fallback,
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _fetch_chain(currency: str, now: datetime) -> SkewSnapshot:
    result = _request("get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    contracts = parse_chain(result, now)
    if not contracts:
        # Every row failed the name / IV / underlying / open-interest checks. On a
        # live chain that means a response-shape change (a renamed field drops all
        # rows at once), not a quiet market — say so, or the operator reads this as
        # an ordinary outage and waits for it to pass.
        raise DeribitError(
            f"Deribit returned no usable {currency} option contracts — every row failed the "
            f"instrument-name, mark-IV, underlying or open-interest checks, which on a live "
            f"chain points at a response-shape change rather than an empty market"
        )
    return compute_skew(contracts, now)


def _fetch_dvol(currency: str, curr_dt: datetime) -> DvolSeries:
    """Fetch the DVOL daily readings for the window ending at ``curr_date``.

    The request window ends at the last instant of ``curr_date`` (UTC) and the
    parsed rows are filtered again on the date string, so a past ``curr_date``
    cannot pick up a later reading even if Deribit widened the range it honours.

    The span is driven by the longer of the two statistics windows (the
    percentile's), since the shorter range window is a subset of it.
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
    if result.get("continuation") is not None:
        # Unreachable at this module's span (see _DVOL_MAX_CANDLES_PER_RESPONSE).
        # If Deribit ever lowers the cap this fires: the response is the newest
        # page and older readings are missing, so the percentile sample is shorter
        # than its window claims. The report already states that sample by count,
        # so this is logged rather than fatal — but it must not pass unremarked.
        logger.warning(
            "Deribit truncated the %s DVOL history (continuation cursor set); the "
            "percentile sample is shorter than the %d-day window",
            currency,
            DVOL_PERCENTILE_WINDOW_DAYS,
        )
    curr_date = curr_dt.strftime("%Y-%m-%d")
    by_date: dict[str, float] = {}
    skipped_non_positive = 0
    for row in result["data"]:
        # [timestamp_ms, open, high, low, close]; a shape change here would
        # silently reinterpret which number is the close, so it is fatal.
        if not isinstance(row, list) or len(row) != 5 or not all(_is_finite_number(v) for v in row):
            raise DeribitError(
                f"Malformed DVOL candle {row!r} (the endpoint's candle shape may have changed)"
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
            logger.warning(
                "Skipping non-positive Deribit DVOL close %r dated %s for %s",
                row[4],
                day,
                currency,
            )
            continue
        if day <= curr_date:
            # Candles arrive ascending, so a repeated day (a resolution change, or
            # a partial candle alongside a settled one) keeps the later value.
            by_date[day] = float(row[4])
    if not by_date:
        # Two different states, and the message is rendered verbatim into
        # "**DVOL:** unavailable — ...". "No readings published" sends the reader
        # to Deribit's status page; "every reading was non-positive" is a corrupt
        # feed that answered with a full history. Also names the currency, which
        # is the non-obvious part on a proxied asset — every other fetch-side
        # error in this module states it.
        if skipped_non_positive:
            raise DeribitError(
                f"Every {currency} DVOL reading on or before {curr_date} was non-positive "
                f"({skipped_non_positive} skipped), so the feed returned history but no usable "
                f"level"
            )
        raise DeribitError(f"No {currency} DVOL readings on or before {curr_date}")
    dates = sorted(by_date)
    return DvolSeries(dates=dates, closes=[by_date[d] for d in dates])


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
    base = normalize_symbol((asset or "").replace("/", "-")).split("-")[0]
    if base in SUPPORTED_CURRENCIES:
        return base, False
    if base in CRYPTO_BASES:
        return "BTC", True
    return None, False


def _readings(count: int) -> str:
    """ "N daily readings", singular at one — the count reaches a sparse feed's text."""
    return f"{count} daily reading{'' if count == 1 else 's'}"


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
        f"**DVOL ({DVOL_INDEX_TENOR_DAYS}-day implied vol index), latest:** "
        f"{latest:.2f}% annualized on {series.latest_date}"
    )
    if series.latest_date >= today:
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
        # Every reading predates the window: a stalled or backfilled feed. Say so
        # rather than computing a range over a one-element fallback sample, whose
        # min and max would both be the latest value itself.
        lines.append(
            f"**{DVOL_WINDOW_DAYS}d range:** not computed — no DVOL reading falls inside the "
            f"{DVOL_WINDOW_DAYS} days ending {curr_date}; the level above is the newest "
            f"reading Deribit has published at all"
        )
    elif len(window) < _MIN_RANGE_SAMPLE:
        # The same objection as the empty case, one reading later: min and max
        # would both be the latest value, printing a collapsed range that reads as
        # a month of pinned volatility.
        lines.append(
            f"**{DVOL_WINDOW_DAYS}d range:** not computed — the {DVOL_WINDOW_DAYS} days ending "
            f"{curr_date} hold only {_readings(len(window))}, so a min and a max would both be "
            f"the level above"
        )
    else:
        lines.append(
            f"**{DVOL_WINDOW_DAYS}d range:** min {min(window):.2f}% / max {max(window):.2f}% "
            f"over {_readings(len(window))}"
        )

    if percentile is None:
        # Two genuinely different states, and the "latest reading is itself in the
        # sample" rationale only applies to one of them: appended to both, it told
        # a reader that a window holding "no daily readings at all" nonetheless
        # contained the latest reading. Name the shortfall in readings rather than
        # calendar days, since the sample size is what makes the percentile
        # meaningless, and 100/n says how coarse it would have been.
        if pct_window:
            lines.append(
                f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** not computed — the "
                f"{DVOL_PERCENTILE_WINDOW_DAYS} days ending {curr_date} hold only "
                f"{_readings(len(pct_window))}, and the latest reading is itself in that "
                f"sample, so a percentile over it could not read below "
                f"{100 / len(pct_window):.0f}%"
            )
        else:
            lines.append(
                f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** not computed — no DVOL "
                f"reading falls inside the {DVOL_PERCENTILE_WINDOW_DAYS} days ending "
                f"{curr_date}"
            )
    else:
        lines.append(
            f"**{DVOL_PERCENTILE_WINDOW_DAYS}d percentile:** the latest reading sits at the "
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
    # Gated on the lag threshold, a reading one or two days old — the ordinary
    # case — was quoted bare and read as current.
    as_of_phrase = f"as of {series.latest_date}"
    if lag_days > MAX_DATA_LAG_DAYS:
        # A successful fetch is not the same as a live series: Deribit can answer
        # normally with a history that stopped updating. (The threshold makes
        # lag_days at least 3 here, so "days" is always the right plural.)
        lines.append(
            f"_Data lag: the newest DVOL reading is {series.latest_date}, {lag_days} days "
            f"before {lag_from} — the fetch succeeded, so the index itself has not printed "
            f"since. Treat the level as {lag_days} days old._"
        )
        as_of_phrase = f"as of {series.latest_date}, {lag_days} days old"
    return DvolReport("\n".join(lines), percentile, len(pct_window), as_of_phrase)


def _format_wing(quote: WingQuote | None) -> str:
    """Render an interpolated smile point with the strikes it came from."""
    if quote is None:
        return (
            "n/a (no two strike-adjacent quotes bracket this point, or the quotes around it "
            "are not a monotone smile)"
        )
    return f"{quote.iv:.2f}% (between the {quote.strike_low:,.0f} and {quote.strike_high:,.0f} strikes)"


def _wing_line(quote: WingQuote | None, n_quotes: int) -> str:
    """Render a smile point, stating the TRUE reason when it is missing.

    ``_format_wing``'s bracket/monotonicity wording presumes there were quotes to
    bracket with. When a whole side of the expiry produced no usable delta — every
    put unquoted, say — that wording names two causes that did not happen, and the
    honest signal is buried in a quote count several lines up.
    """
    if quote is not None or n_quotes >= 2:
        return _format_wing(quote)
    if n_quotes == 0:
        return "n/a (no quote on this side of the expiry yielded a usable delta)"
    return "n/a (only one usable quote on this side, so no pair can bracket this point)"


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
        f"yielded a usable delta",
        f"**Forward:** {skew.forward:,.2f}",
        f"**ATM IV ({ATM_DELTA * 100:.0f}Δ):** {_atm_line(skew)}",
        f"**25Δ call IV:** {_wing_line(skew.call_25, skew.n_calls)}",
        f"**25Δ put IV:** {_wing_line(skew.put_25, skew.n_puts)}",
    ]
    if skew.rr25 is None:
        lines.append(
            "**RR25 (25Δ call IV − 25Δ put IV):** n/a — the chain does not bracket both "
            "wings, so the risk reversal is not computed (no wing vol is extrapolated)"
        )
    else:
        lines.append(f"**RR25 (25Δ call IV − 25Δ put IV):** {_rr_points(skew.rr25)} vol points")
    return "\n".join(lines)


def _reading_line(
    currency: str,
    skew: SkewSnapshot | None,
    percentile: float | None,
    percentile_n: int,
    dvol_as_of: str | None = None,
) -> str:
    """Restate the two headline numbers in words and define them — nothing further.

    Deliberately mechanical. This is the most quotable sentence in the report and
    the research and risk agents re-read it downstream, so it says what RR25 and
    the percentile *are* and leaves the judgement to them. Characterising the
    print instead ("strongly tilted", "the market is paying up for downside
    protection") would inject a directional prior into every one of those hops,
    and a negative RR25 is the ordinary resting state of crypto options rather
    than news. No sibling vendor in this package interprets its own figures.

    It also names the currency and the tenor: for a proxied asset the heading is
    otherwise the only place that says whose surface this is, and a bare "the
    28AUG26 skew ..." is precisely what survives a downstream summary with the
    proxy framing gone. The tenor is named for the same reason and became load
    bearing once the expiry stopped being fixed — a risk reversal is not
    tenor-invariant, so a reader who assumes ~30 days when the fallback supplied a
    different expiry is reading a different quantity than the one printed.
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
    if percentile is not None:
        # The as-of date travels with the claim for the same reason the currency
        # does: this sentence is what survives a downstream summary, and a
        # percentile presented bare reads as current however old the reading is.
        # Present on every report, not only late ones — see DvolReport.
        as_of = f" ({dvol_as_of})" if dvol_as_of else ""
        # State the sample, not just the span. "the 365-day window" reads as one
        # observation per day, so a feed that stopped publishing weeks ago still
        # sounded like a full year of evidence once this clause was quoted on its
        # own — and this clause is exactly what gets quoted on its own.
        parts.append(
            f"{currency}'s latest DVOL reading{as_of} sits at the {_ordinal(percentile)} "
            f"percentile of the {_readings(percentile_n)} in the "
            f"{DVOL_PERCENTILE_WINDOW_DAYS}-day window ending on the analysis date"
        )
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
            live chain then predates the analysis date, which is not lookahead.
            Further ahead than that is treated like a historical date and the
            chain is withheld, since curr_date is an LLM-supplied argument and
            past one day the gap is a mistake rather than a timezone.

    Returns:
        A markdown report: the latest DVOL with its 30-day range (when that window
        holds at least two readings) and its 365-day percentile (when that one
        holds enough for a percentile to mean anything), and — unless the chain is
        withheld — the selected expiry, ATM (50Δ) IV, the 25-delta wings with the
        strikes they were interpolated between, and the risk reversal, closing
        with one fixed-format sentence restating those figures. An unrecognized
        symbol returns a bare no-signal sentence instead.

    Raises:
        VendorRateLimitError: when Deribit throttled every request actually made
            (one, where the chain is withheld), so the router can treat it as a
            throttle rather than a broken vendor.
        DeribitError: when nothing could be served — both halves failed, or DVOL
            failed on a date where the chain is withheld by design — and when
            curr_date is not a yyyy-mm-dd date. The routing layer then degrades
            the optional options_data category to a sentinel. Whenever either half
            survives, the report renders it and names what is missing instead of
            raising.
    """
    # Normalise curr_date BEFORE any date comparison. strptime accepts
    # non-zero-padded input ("2026-6-5"), which then compares wrong against the
    # canonical ISO candle dates — '2026-12-31' <= '2026-6-5' is True — silently
    # admitting future readings and defeating the lookahead guard. (farside and
    # fear_greed carry the same guard.)
    #
    # Wrapped because curr_date is an LLM-supplied tool argument, and this
    # function's Raises: contract says DeribitError / VendorRateLimitError. A bare
    # ValueError (or TypeError for None) escapes into route_to_vendor's generic
    # `except Exception` lane, which logs it with exc_info and reports "Vendor
    # 'deribit' failed" — a caller's malformed argument dressed up as a vendor
    # outage, complete with a traceback pointing at Deribit.
    try:
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    except (ValueError, TypeError) as e:
        raise DeribitError(f"curr_date {curr_date!r} is not a yyyy-mm-dd date ({e})") from e
    curr_date = curr_dt.strftime("%Y-%m-%d")

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
    # then older than the analysis date, which is not lookahead at all, and
    # treating it as such would withhold this vendor's main signal for the first
    # hours of every local day. The DVOL half is genuinely date-filtered and is
    # served either way.
    is_historical = curr_date < today
    # Naive on both sides (``today`` is re-parsed rather than compared as text) so
    # this is a day count, not a string ordering.
    days_ahead = (curr_dt - datetime.strptime(today, "%Y-%m-%d")).days

    # Why the chain half is not in the report, classified ONCE. Three later sites
    # have to explain it — the both-halves-failed raise, the header note and the
    # section body — and re-testing is_historical/market_proxy at each of them
    # means the precedence between the reasons is re-decided by hand every time,
    # so a new reason can update two sites and silently miss the third. (This is
    # now a four-valued classification for exactly that reason.)
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
    dvol, dvol_error = _try_fetch(lambda: _fetch_dvol(currency, curr_dt), "DVOL", currency)
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
        if chain_withheld == "historical":
            raise DeribitError(
                f"Deribit DVOL is unavailable for {currency} ({dvol_error}), and the options "
                f"chain is not served for the historical date {curr_date}"
            )
        if chain_withheld == "far_future":
            raise DeribitError(
                f"Deribit DVOL is unavailable for {currency} ({dvol_error}), and the options "
                f"chain is not served for {curr_date}, which is {days_ahead} days ahead of the "
                f"UTC clock ({today})"
            )
        if chain_withheld == "proxy":
            raise DeribitError(
                f"Deribit DVOL is unavailable for {currency} ({dvol_error}), and the options "
                f"chain is not served for '{asset}', which has no Deribit chain of its own"
            )
        raise DeribitError(
            f"Deribit returned neither DVOL nor an options chain for {currency} "
            f"(DVOL: {dvol_error}; chain: {skew_error})"
        )

    if market_proxy:
        # The requested asset belongs in the heading, not only in a caveat below:
        # this report is re-summarised by downstream agents, and a heading
        # byte-identical to a real BTC report is what survives that hop with the
        # proxy framing stripped off.
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
    if chain_withheld == "historical":
        header_lines.append(
            f"_Historical date: Deribit's options chain is a live endpoint with no history, so "
            f"the ATM IV, 25Δ wings, RR25 and forward are NOT served for {curr_date} — quoting "
            f"today's chain would be future information. The DVOL history below IS filtered to "
            f"{curr_date} and is safe to use._"
        )
    elif chain_withheld == "far_future":
        header_lines.append(
            f"_Analysis date {curr_date} is {days_ahead} days ahead of the UTC clock ({today}), "
            f"further than any timezone offset explains. Deribit's options chain is a live "
            f"endpoint, so the ATM IV, 25Δ wings, RR25 and forward would be today's book, not "
            f"{curr_date}'s, and they are NOT served. The DVOL history below IS filtered to "
            # Same fact as the ahead-of-clock note's DVOL sentence, so it is worded
            # the same way. It was left on the pre-rewrite phrasing, which is still
            # true here (days_ahead >= 2, so no mid-run crossing can reach
            # curr_date) but stated the identical fact two ways three lines apart.
            f"{curr_date}; its windows end at a date the feed has not reached, so they hold fewer "
            f"readings than their full spans._"
        )
    elif curr_date > today:
        # A curr_date past the UTC clock (a caller using a local date east of UTC).
        # The chain is still served — it predates the analysis date, so it is not
        # lookahead — but it is not "as of" that date either, and the DVOL windows
        # run past the data rather than the data stopping short.
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
            # Dated by the clock the book was ACTUALLY read on, not by the one taken
            # before the DVOL half. Those differ by up to DVOL's whole envelope
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
                f"The DVOL windows end at the analysis date but the feed has only reached "
                f"{dvol.latest_date}, so they hold fewer readings than their full spans."
            )
        # Every sentence already ends in a full stop; only the closing italic marker
        # is appended.
        header_lines.append(" ".join(sentences) + "_")
    # The DVOL clause is gated on the half existing, for the same reason every
    # sentence of the ahead-of-clock note is: unconditional, it advertised a
    # window immediately above "**DVOL:** unavailable".
    source_line = "- Source: deribit.com public API"
    if dvol is not None:
        source_line += f" | DVOL window ending {curr_date}"
    header_lines.append(source_line)

    sections = []
    percentile = None
    percentile_n = 0
    dvol_as_of = None
    if dvol is not None:
        report = _dvol_section(dvol, curr_dt, today)
        percentile, percentile_n, dvol_as_of = (
            report.percentile,
            report.percentile_n,
            report.as_of_phrase,
        )
        sections.append(report.markdown)
    else:
        # Not "the request failed": _fetch_dvol also raises when the request
        # SUCCEEDED and the response held no reading on or before curr_date, and
        # naming the wrong cause sends an operator to the network first.
        sections.append(f"**DVOL:** unavailable — {dvol_error}. Do not fabricate a DVOL level.")
    if skew is not None:
        sections.append(_skew_section(skew, snapshot_time))
    elif chain_withheld == "historical":
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** not served for a historical analysis "
            f"date — see the note above. Do not substitute today's skew for {curr_date}."
        )
    elif chain_withheld == "far_future":
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** not served for an analysis date "
            f"{days_ahead} days ahead of the UTC clock — see the note above. Do not substitute "
            f"today's skew for {curr_date}."
        )
    elif chain_withheld == "proxy":
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** not served for '{asset}' — see the note "
            f"above. {currency}'s skew describes demand for downside in {currency}, not in "
            f"'{asset}'. Do not substitute it."
        )
    else:
        # Not "the chain request failed": four of the five reachable causes come
        # from a SUCCESSFUL 200 — a payload that is not a list, no row surviving
        # the name/IV/underlying/open-interest checks, no expiry inside the tenor
        # band, no usable forward. Naming the network sends an operator to look
        # for an outage that will never pass, and does it in the same sentence
        # that quotes the module saying otherwise. Same fix already applied to the
        # DVOL branch above.
        sections.append(
            f"**Options chain (ATM IV / 25Δ skew):** unavailable — {skew_error}. The DVOL "
            f"figures above are unaffected; do not fabricate skew."
        )

    reading = _reading_line(currency, skew, percentile, percentile_n, dvol_as_of)
    if reading:
        sections.append(reading)

    # Blank line between entries so consecutive italic caveats are not rendered as
    # one run-on paragraph.
    return "\n\n".join(header_lines) + "\n\n" + "\n\n".join(sections) + "\n"
