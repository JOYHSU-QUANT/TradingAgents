"""CME Bitcoin futures basis, read through the existing yfinance layer.

The basis is the gap between the front-month CME Bitcoin future (Yahoo's
continuous ``BTC=F``) and spot (``BTC-USD``), the one USD-denominated,
institution-facing price this project's other sources do not carry: Deribit
gives options-implied vol, Hyperliquid gives perp funding, and neither says
what regulated futures pay over spot.

Why HOURLY bars matched on their timestamp, and not the two daily closes the
OHLCV cache already holds. Measured 2026-09-21, each over the window named:

* Daily closes are not synchronous. ``BTC-USD`` closes its day at 00:00 UTC,
  ``BTC=F`` at the end of the CME session, and BTC moves more in the hours
  between than the basis is wide. Over a year the same-date daily basis
  averaged +0.22% with a standard deviation of 0.78%, was negative on 37.5% of
  days and ranged from -3.9% to +3.3% — clock mismatch, not carry. Annualized,
  that noise alone is several times the signal.
* Hourly closes matched on the same stamp are synchronous. Over 730 days their
  basis was negative on 2.4% of days, moved 0.07% within a typical day, and
  showed the sawtooth a front-month basis must: decaying toward zero into
  expiry and stepping back up when the continuous symbol rolls.

Two defects of Yahoo's hourly ``BTC=F`` feed decide the rest of the method,
both measured the same day:

* While CME is closed Yahoo still emits bars, with ``Close`` pinned to the
  previous settlement. Matched against a spot price that kept moving they read
  as a basis of -1% to -2%. So only hours inside the CME session are used
  (``_in_session``), and only bars that traded (``Volume > 0``) — which also
  drops the daily maintenance hour.
* A few pinned closes survive into the first hours after a reopen (0.4% of
  the hours those two filters keep). So a reading is the MEDIAN of its hours,
  never the mean and never a single print.

The sawtooth is why the report annualizes with the contract's real days to
expiry rather than a fixed multiple. A fixed multiple only rescales the
nominal figure, whose level is set mostly by how far away expiry is, so a
reader would take every month-end convergence for weakening demand. The
expiry rule is a fact about the contract that can change; it lives in
``front_expiry`` with the date it was last checked.

The cost of the hourly method is that it cannot use ``load_ohlcv``'s disk
cache, so each call makes two Yahoo requests. Both go through
``yf_fetch_unhidden`` like every other yfinance leaf, so a throttle keeps its
type and the client-wide latch in ``yfinance_common`` spares the second
request once the first has been refused.
"""

from __future__ import annotations

import calendar
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

import pandas as pd
import yfinance as yf

from .errors import VendorError
from .symbol_utils import classify_crypto_asset
from .utils import data_lag_note, date_refusal, echo_argument, quote_argument
from .yfinance_common import yf_fetch_unhidden

logger = logging.getLogger(__name__)

FUTURES_SYMBOL = "BTC=F"
SPOT_SYMBOL = "BTC-USD"

# CME lists Bitcoin and Ether futures; only Bitcoin is wired. Another asset is
# answered with a no-signal sentence rather than BTC's basis as a proxy: the
# basis is what one contract pays over its own underlying, which says nothing
# about a different underlying's carry.
SUPPORTED_ASSETS = frozenset({"BTC"})

BAR_INTERVAL = "1h"

# Yahoo serves hourly bars for the trailing 730 days only and rejects a request
# whose start is older. One day inside that, so a request built late in a UTC
# day is not refused for a boundary Yahoo measures on its own clock.
HOURLY_HISTORY_DAYS = 729

# A reading is the median over this many of the most recent synchronous hours.
# A CME day holds 23 trading hours, so this is about one session.
WINDOW_HOURS = 24

# Fewer synchronous hours than this is not a reading. Half a window: at the
# measured rate of pinned closes, and with them clustered in the first hours
# after a reopen, very unlikely to be outvoted at the median — not impossible.
MIN_MATCHED_HOURS = 12

# The hours of one reading may not reach further back than this from its
# newest hour. An ordinary weekend puts about 50 closed hours inside a window
# and a holiday weekend about 75; a span past this means hours are missing
# from the feed, and their older neighbours are not "the latest session".
MAX_WINDOW_SPAN_DAYS = 5

# The comparison reading sits this many days before the current one, and the
# trailing median covers the same span. One week: the cadence of the CFTC
# positioning report this tool is read beside.
LOOKBACK_DAYS = 7

# Past this the feed has stalled and the reading is withheld rather than
# captioned: a caption has to survive every downstream summary, and the number
# does not need to.
MAX_STALENESS_DAYS = 7

# How much history one call asks Yahoo for, counted back from the analysis
# date — NOT from the newest hour, which is only known after the fetch and may
# itself trail by ``MAX_STALENESS_DAYS``. The earlier reading then ends
# ``LOOKBACK_DAYS`` before that and reaches ``MAX_WINDOW_SPAN_DAYS`` further.
# The three add up to 19, and two more days cover the closed days a holiday
# weekend puts between the earlier anchor and its newest traded hour (a second
# closure inside that window would need more; short, the report says "no
# earlier reading", never a wrong figure). At 14, a feed a week behind reported
# "no earlier reading" for a reading that existed and had not been asked for.
FETCH_WINDOW_DAYS = 21

# The oldest analysis date served. A past date's fetch starts
# ``FETCH_WINDOW_DAYS - 1`` days before it (the window is counted back from the
# midnight that ENDS the date), and that start has to be inside Yahoo's reach.
# Named because it is the number a reader needs — "how old a date can I ask
# about" — and it is not ``HOURLY_HISTORY_DAYS``: three descriptions of this
# tool said 729 where the guard's arithmetic said 716.
MAX_DATE_AGE_DAYS = HOURLY_HISTORY_DAYS - FETCH_WINDOW_DAYS + 1

# The annualized figure divides by days to expiry, so it is withheld inside
# this many days: the quotient blows up as the contract converges, and Yahoo
# rolls the continuous symbol to the next month on a day of expiry week it
# does not announce (the Thursday in one month measured, the Friday in two).
# Five days withholds the whole of expiry week, Monday to Friday.
#
# It also bounds a second distortion. Yahoo's spot is an aggregate, not the
# reference rate the contract settles to, so the nominal basis carries a
# small level offset that annualizing magnifies as expiry nears. Measured by
# bucket over 610 days (2026-09-21), the annualized median was 6.4% to 6.6%
# everywhere from 8 to 28 days out, 7.4% at 5 to 7 days and 9.2% at 3 to 4
# — flat until the last week, then about a point high, then three. (A linear
# regression of the same series puts a fixed term of +0.05% to +0.1% under a
# carry of about 5.9% a year, but 365/days of that term would already add
# three points at 8 days out, and the buckets show no such thing: the fit is
# not the mechanism, the buckets are the measurement.) At 5 the drift is about
# a point against an interquartile range of five, so the bound stays where
# expiry week ends.
MIN_DAYS_TO_EXPIRY = 5

# Simple, not compounded: nominal basis x this / days to expiry.
ANNUALIZATION_DAYS = 365

# The newest synchronous hour may trail the analysis date by this many days
# without remark — a Sunday is two days after Friday's close, a holiday Monday
# three. Past it the report says so.
MAX_DATA_LAG_DAYS = 3

# A reading whose newest hour ended more than this many hours before the
# instant it is read at is not a live one: CME was closed, or Yahoo had no
# traded bar. The paper loop runs around the clock and CME does not, so
# between a quarter and a third of its cycles read Friday's basis. Three
# hours: more than the daily maintenance hour plus a bar Yahoo is late with.
NOT_LIVE_HOURS = 3

# What size of difference is ordinary, in annualized points. Measured
# 2026-09-21 over 492 days of daily readings: on three days in four the 7-day
# change was under 2.8 points and the reading sat within 1.4 points of its own
# 7-day median. Rounded, and said in the report, because adjacent 4-hour
# cycles share 20 of a window's 24 hours — without a scale a model narrates
# the same point of noise six times a day.
ORDINARY_CHANGE_POINTS = 3
ORDINARY_GAP_POINTS = 1.5

# How far ahead of the UTC clock ``curr_date`` may run and still be served.
# Callers derive it from a local clock, which east of UTC runs a few hours
# ahead; one day covers every timezone. Deribit's bound, for Deribit's reason.
MAX_FUTURE_DAYS = 1

# The two sentences a reader needs in order not to misread the figures. Each
# is said twice — in the report's closing line, which is what a downstream
# summary keeps, and in the market analyst's instructions — and both places
# read them from here, so the two cannot drift into saying different things.
SAWTOOTH_NOTE = (
    "The nominal basis decays toward zero as the contract nears expiry and steps back up at "
    "the monthly roll, so compare annualized figures, never nominal ones across dates"
)
CARRY_NOTE = (
    "A positive basis is the usual state and mostly reflects the cost of carry; this is a "
    "positioning-and-carry input, not a standalone directional signal"
)
# The scale, and the one artifact in it (the MIN_DAYS_TO_EXPIRY comment has
# the measurement). In the report's Method line, where a summary can keep it.
SCALE_NOTE = (
    f"On three days in four the {LOOKBACK_DAYS}-day change is under about "
    f"{ORDINARY_CHANGE_POINTS} annualized points and a reading sits within about "
    f"{ORDINARY_GAP_POINTS} points of its own {LOOKBACK_DAYS}-day median, so treat differences "
    f"of that size as ordinary variation. The annualized figure also runs about a point high "
    f"in the last days before it is withheld ({MIN_DAYS_TO_EXPIRY} to {MIN_DAYS_TO_EXPIRY + 2} "
    f"days from expiry) and steps back down after the roll, because Yahoo's spot is an "
    f"aggregate rather than the rate the contract settles to"
)


class CmeBasisError(VendorError):
    """The futures-basis tool could not build a reading from what Yahoo served."""


class Reading(NamedTuple):
    """One basis reading: the median over a window of synchronous hours."""

    basis_pct: float
    hours: int
    first: pd.Timestamp
    last: pd.Timestamp
    expiry: date
    days_to_expiry: int
    # How many of the window's hours sit far enough from their own expiry to
    # be annualized. Short of ``hours`` when the window reaches back across a
    # roll, into the previous contract's expiry week.
    annualized_hours: int
    # None when ``days_to_expiry`` is inside the bound, or when too few of the
    # window's hours are annualizable for a median.
    annualized_pct: float | None


def _utc_now() -> datetime:
    """The one clock this module reads. Tests patch it."""
    return datetime.now(timezone.utc)


def _last_friday(year: int, month: int) -> date:
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return last_day - timedelta(days=(last_day.weekday() - calendar.FRIDAY) % 7)


def front_expiry(day: date) -> date:
    """The expiry of the CME Bitcoin contract that is front-month on ``day``.

    CME Bitcoin futures stop trading on the last Friday of the contract month
    (CME Group contract specifications, checked 2026-09-21), and a contract is
    listed for every month, so the front month on a given day is that month's
    contract until its last Friday and the next month's from the day after.

    Approximate in one respect: when that Friday is not a business day in both
    London and the US, the exchange moves the last trading day to the business
    day before, and this function does not know the holiday calendars. The
    date can therefore be one day late. ``MIN_DAYS_TO_EXPIRY`` keeps the
    annualized figure out of the stretch where a day matters most; at its edge
    a one-day error still moves the annualized figure by a fifth, and the
    report calls the expiry date approximate for that reason.
    """
    expiry = _last_friday(day.year, day.month)
    if day > expiry:
        year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
        expiry = _last_friday(year, month)
    return expiry


def _in_session(index: pd.DatetimeIndex) -> pd.Series:
    """Which UTC hour stamps fall inside the CME Globex week, conservatively.

    Globex trades Bitcoin futures from Sunday 17:00 to Friday 16:00 Central
    Time. In UTC that is Sunday 22:00 to Friday 21:00 under daylight time and
    an hour later on both ends under standard time. The rule below is the
    intersection of the two — closed from Friday 21:00 to Sunday 23:00 UTC —
    so it needs no timezone database and cannot be wrong across a DST change;
    the price is one genuine trading hour a week, given up in each season.
    The daily maintenance hour is not handled here: no contract trades in it,
    so the ``Volume > 0`` filter drops it.
    """
    weekday, hour = index.dayofweek, index.hour
    closed = ((weekday == 4) & (hour >= 21)) | (weekday == 5) | ((weekday == 6) & (hour < 23))
    return pd.Series(~closed, index=index)


def _fetch_hourly(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Hourly bars for ``symbol`` over ``[start, end)``, indexed by UTC hour.

    Through ``Ticker.history`` and ``yf_fetch_unhidden``, as ``load_ohlcv``
    fetches: ``yf.download`` swallows a throttle into an empty frame (#67).
    ``auto_adjust`` is off because there is nothing to adjust on a future or a
    coin, and the adjust step is one more place for the library to fail.
    """
    ticker = yf.Ticker(symbol)
    frame = yf_fetch_unhidden(
        lambda: ticker.history(
            start=start.isoformat(),
            end=end.isoformat(),
            interval=BAR_INTERVAL,
            auto_adjust=False,
            actions=False,
        ),
        hidden_answer=pd.DataFrame,
    )
    if frame is None or frame.empty or "Close" not in frame.columns:
        # Not raised: an empty answer is one more way of having too few hours,
        # and ``get_futures_basis`` answers that with a notice naming the
        # series that fell short. The no-data sentinel would say "BTC ... may
        # be invalid, delisted" to the analyst that also reads BTC's prices.
        logger.warning("Yahoo Finance returned no hourly rows for %s", symbol)
        return pd.DataFrame(
            {"Close": [], "Volume": []}, index=pd.DatetimeIndex([], tz="UTC"), dtype="float64"
        )
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise CmeBasisError(
            f"hourly {symbol} rows are not indexed by time ({type(frame.index).__name__})"
        )
    index = frame.index
    frame = frame.set_axis(
        index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    )
    # Yahoo stamps a bar by its start; keep one row per hour if it repeats one.
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _usable(prices: pd.Series) -> pd.Series:
    prices = pd.to_numeric(prices, errors="coerce")
    return prices[prices.map(math.isfinite) & (prices > 0)]


def usable_legs(
    futures: pd.DataFrame, spot: pd.DataFrame, bound: datetime
) -> tuple[pd.Series, pd.Series]:
    """Each series' usable hourly closes before ``bound``, before they are matched.

    Kept apart from the match so that a report with too few synchronous hours
    can say which series fell short, instead of naming one of them by habit.
    """
    if "Volume" not in futures.columns:
        raise CmeBasisError(f"hourly {FUTURES_SYMBOL} rows carry no Volume column")
    traded = pd.to_numeric(futures["Volume"], errors="coerce").fillna(0) > 0
    kept = futures[traded & _in_session(futures.index)]
    cut = pd.Timestamp(bound)
    futures_leg, spot_leg = _usable(kept["Close"]), _usable(spot["Close"])
    return futures_leg[futures_leg.index < cut], spot_leg[spot_leg.index < cut]


def matched_basis(futures: pd.DataFrame, spot: pd.DataFrame, bound: datetime) -> pd.Series:
    """Per-hour basis in percent, over the hours both series share before ``bound``.

    ``bound`` is the look-ahead guard: a bar is stamped by its START, so only
    bars stamped strictly before it are kept. For a past analysis date it is
    the midnight that ends that date; for a live one it is the clock.
    """
    futures_leg, spot_leg = usable_legs(futures, spot, bound)
    pair = pd.concat({"futures": futures_leg, "spot": spot_leg}, axis=1, join="inner")
    return (pair["futures"] / pair["spot"] - 1.0) * 100.0


def _annualized(basis: pd.Series) -> pd.Series:
    """Each hour's basis annualized by its own days to expiry, where that is allowed."""
    days = pd.Series(
        [(front_expiry(stamp.date()) - stamp.date()).days for stamp in basis.index],
        index=basis.index,
        dtype="float64",
    )
    days = days.where(days >= MIN_DAYS_TO_EXPIRY)
    return (basis * ANNUALIZATION_DAYS / days).dropna()


def reading_at(basis: pd.Series, anchor: pd.Timestamp) -> Reading | None:
    """The reading whose newest hour is the last one at or before ``anchor``."""
    window = basis[basis.index <= anchor].tail(WINDOW_HOURS)
    if window.empty:
        return None
    last = window.index[-1]
    window = window[window.index > last - pd.Timedelta(days=MAX_WINDOW_SPAN_DAYS)]
    if len(window) < MIN_MATCHED_HOURS:
        return None
    expiry = front_expiry(last.date())
    days_to_expiry = (expiry - last.date()).days
    annualized = _annualized(window)
    # Judged on the reading's OWN distance from expiry first. A window that
    # ends inside the bound still holds earlier hours outside it — Friday's,
    # seen from the Monday of expiry week — and a median over those alone
    # would print an annualized figure beside a days-to-expiry it was not
    # computed for.
    served = days_to_expiry >= MIN_DAYS_TO_EXPIRY and len(annualized) >= MIN_MATCHED_HOURS
    return Reading(
        basis_pct=float(window.median()),
        hours=len(window),
        first=window.index[0],
        last=last,
        expiry=expiry,
        days_to_expiry=days_to_expiry,
        annualized_hours=len(annualized),
        annualized_pct=float(annualized.median()) if served else None,
    )


def _stamp(value: pd.Timestamp) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def _pct(value: float) -> str:
    return f"{value:+.2f}%"


def _annualized_withheld(reading: Reading) -> str:
    """Why a reading carries no annualized figure: one of two reasons, never both."""
    if reading.days_to_expiry < MIN_DAYS_TO_EXPIRY:
        return (
            f"withheld — the front contract is within {MIN_DAYS_TO_EXPIRY} days of its expiry "
            f"(about {reading.expiry.isoformat()}), where the figure divides by almost nothing "
            f"and Yahoo's continuous symbol may already quote the next month"
        )
    return (
        f"withheld — the contract has just rolled, and only {reading.annualized_hours} of this "
        f"reading's {reading.hours} hours belong to the new front month (the rest are the "
        f"previous contract's expiry week); at least {MIN_MATCHED_HOURS} are needed"
    )


def _withheld(coin: str, curr_date: str, why: str) -> str:
    """The whole report, for a date this tool cannot serve: no figures at all."""
    return (
        f"## CME Bitcoin Futures Basis — {coin}\n"
        f"- Withheld for {curr_date}\n"
        f"\nThe futures basis is withheld for this date. {why} Treat the {coin} futures "
        f"basis as unavailable for {curr_date}; do not compute one from the futures and "
        f"spot prices in other reports, whose closes are hours apart."
    )


def get_futures_basis(asset: str, curr_date: str) -> str:
    """Fetch the CME Bitcoin front-month futures basis as a markdown report.

    Args:
        asset: "BTC", or a pair form like "BTC-USD". Any other symbol gets a
            no-signal sentence: this tool reads one contract, and its basis is
            not a proxy for another underlying's.
        curr_date: The analysis date (yyyy-mm-dd). Hourly bars stamped after
            it are never read. A date more than ``MAX_FUTURE_DAYS`` ahead of
            the UTC clock, or more than ``MAX_DATE_AGE_DAYS`` behind it, is
            answered with a withheld notice carrying no figures; an unusable
            one is refused up front with the shared ``INVALID_CURR_DATE``
            sentinel.

    Returns:
        A markdown report: the nominal basis (median of the latest synchronous
        hours), the same annualized by the front contract's days to expiry —
        or the reason that figure is withheld — the trailing annualized
        median, and the change against the reading ``LOOKBACK_DAYS`` earlier.

        When Yahoo served too few synchronous hours for a reading, or the
        newest one is more than ``MAX_STALENESS_DAYS`` old, the same withheld
        notice instead, saying what each series had. Returned rather than
        raised as no-data: the router's no-data sentence says the SYMBOL "may
        be invalid, delisted" — about BTC, to the analyst that also reads
        BTC's prices — and could only ever name one of the two series.

    Raises:
        VendorError: a throttle or an outage, typed by the yfinance boundary,
            or ``CmeBasisError`` for rows this module cannot read.
    """
    refusal = date_refusal(curr_date, what="futures basis", kind="point")
    if refusal is not None:
        return refusal
    # Canonical before it is rendered or compared: strptime accepts "2026-6-5".
    curr_day = datetime.strptime(curr_date, "%Y-%m-%d").date()
    curr_date = curr_day.isoformat()

    # Before the echo: ``echo_argument`` goes through ``str``, so a truthy
    # non-string would be turned into a symbol and answered about (#233).
    if asset and not isinstance(asset, str):
        raise CmeBasisError(f"asset must be a symbol string, got {type(asset).__name__}")
    asset = echo_argument(asset)
    coin, is_proxy = classify_crypto_asset(asset, SUPPORTED_ASSETS)
    if coin is None or is_proxy:
        return (
            f"There is no futures-basis signal for {quote_argument(asset)}: this tool reads "
            f"the CME Bitcoin front-month future only. Do not substitute BTC's basis for "
            f"another asset's."
        )

    now = _utc_now()
    today = now.date()
    if (curr_day - today).days > MAX_FUTURE_DAYS:
        return _withheld(
            coin,
            curr_date,
            f"It is more than {MAX_FUTURE_DAYS} day ahead of the UTC clock ({today.isoformat()}), "
            f"which is a mistaken date rather than a timezone offset.",
        )
    # Bars are kept strictly before this instant: the midnight ending the
    # analysis date, or the clock when that midnight has not come yet.
    bound = min(
        datetime.combine(curr_day + timedelta(days=1), datetime.min.time(), timezone.utc), now
    )
    if (today - curr_day).days > MAX_DATE_AGE_DAYS:
        return _withheld(
            coin,
            curr_date,
            f"It is more than {MAX_DATE_AGE_DAYS} days before the UTC clock "
            f"({today.isoformat()}): a reading needs {FETCH_WINDOW_DAYS} days of synchronous "
            f"hourly prices and Yahoo serves hourly bars for only the trailing "
            f"{HOURLY_HISTORY_DAYS}. Daily closes are not a substitute, because the futures and "
            f"spot days close hours apart.",
        )
    start = bound.date() - timedelta(days=FETCH_WINDOW_DAYS)

    # All or nothing, unlike the vendors that fetch two halves independently:
    # there is no figure here that needs only one series, and one price alone
    # would invite exactly the asynchronous subtraction this tool exists to
    # replace. The first failure therefore leaves with its own type.
    end = bound.date() + timedelta(days=1)
    futures = _fetch_hourly(FUTURES_SYMBOL, start, end)
    spot = _fetch_hourly(SPOT_SYMBOL, start, end)
    basis = matched_basis(futures, spot, bound)

    current = reading_at(basis, pd.Timestamp(bound)) if not basis.empty else None
    reference = min(curr_day, today)
    stale_days = None if current is None else (reference - current.last.date()).days
    if current is None or stale_days > MAX_STALENESS_DAYS:
        # Both series are described, whichever fell short: they are fetched
        # and filtered apart, and only the match between them is one thing.
        legs = "; ".join(
            f"{symbol} had {len(leg)} usable hours, the newest "
            f"{_stamp(leg.index[-1]) + ' UTC' if len(leg) else 'none'}"
            for symbol, leg in zip(
                (FUTURES_SYMBOL, SPOT_SYMBOL), usable_legs(futures, spot, bound), strict=True
            )
        )
        if current is None:
            shortfall = (
                f"Yahoo served fewer than {MIN_MATCHED_HOURS} synchronous hours of the two "
                f"series before {_stamp(pd.Timestamp(bound))} UTC"
            )
        else:
            shortfall = (
                f"The newest synchronous hour Yahoo served is {_stamp(current.last)} UTC, "
                f"{stale_days} days before {reference.isoformat()}, and a reading that old is "
                f"not served"
            )
        logger.warning("Futures basis withheld for %s: %s (%s)", curr_date, shortfall, legs)
        return _withheld(coin, curr_date, f"{shortfall} ({legs}).")

    lookback = pd.Timedelta(days=LOOKBACK_DAYS)
    prior = reading_at(basis, current.last - lookback)
    trailing = _annualized(basis[basis.index > current.last - lookback])

    sign = (
        "futures above spot (contango)"
        if current.basis_pct > 0
        else "futures below spot (backwardation)"
        if current.basis_pct < 0
        else "futures level with spot"
    )
    lines = [
        f"## CME Bitcoin Futures Basis — {coin}",
        f"- CME front-month future (Yahoo {FUTURES_SYMBOL}) against spot (Yahoo {SPOT_SYMBOL}), "
        f"hourly closes matched on the same UTC hour | newest hour {_stamp(current.last)} UTC "
        f"| analysis date {curr_date}",
    ]
    lag = data_lag_note(
        current.last.date().isoformat(),
        reference.isoformat(),
        MAX_DATA_LAG_DAYS,
        "synchronous futures/spot hour",
    )
    if lag:
        lines.append(lag)
    # The newest bar ENDS an hour after its stamp: a past date's last bar ends
    # at the bound exactly, and the hour in progress ends after it.
    idle_hours = int((pd.Timestamp(bound) - current.last) / pd.Timedelta(hours=1)) - 1
    not_live = idle_hours > NOT_LIVE_HOURS
    if not_live:
        lines.append(
            f"_Not a live reading: the newest synchronous hour ended {idle_hours} hours before "
            f"{_stamp(pd.Timestamp(bound))} UTC — CME was closed, or Yahoo has no traded bar "
            f"since. Spot has traded in that time, and this reading says nothing about that "
            f"move._"
        )
    lines.append("")
    lines.append(
        f"**Basis:** {_pct(current.basis_pct)} nominal — {sign}; the median of "
        f"{current.hours} synchronous hours from {_stamp(current.first)} to "
        f"{_stamp(current.last)} UTC"
    )
    if current.annualized_pct is None:
        lines.append(f"**Annualized:** {_annualized_withheld(current)}")
    else:
        lines.append(
            f"**Annualized:** {_pct(current.annualized_pct)} — simple, nominal x "
            f"{ANNUALIZATION_DAYS} / days to expiry; the front contract expires about "
            f"{current.expiry.isoformat()}, {current.days_to_expiry} days after this reading"
        )
    if len(trailing) >= MIN_MATCHED_HOURS:
        lines.append(
            f"**{LOOKBACK_DAYS}-day median, annualized:** {_pct(float(trailing.median()))} over "
            f"{len(trailing)} synchronous hours at least {MIN_DAYS_TO_EXPIRY} days from expiry"
        )
    else:
        lines.append(
            f"**{LOOKBACK_DAYS}-day median, annualized:** withheld — only {len(trailing)} "
            f"synchronous hours in that span sit at least {MIN_DAYS_TO_EXPIRY} days from expiry"
        )

    if prior is None:
        change = (
            f"none to report — no reading of at least {MIN_MATCHED_HOURS} synchronous hours "
            f"ends {LOOKBACK_DAYS} days earlier"
        )
    elif current.annualized_pct is None or prior.annualized_pct is None:
        change = (
            f"none to report — the annualized figure is withheld for "
            f"{'this reading' if current.annualized_pct is None else 'the earlier reading'}, "
            f"and nominal levels are not comparable across the month (the earlier reading, "
            f"ending {_stamp(prior.last)} UTC, was {_pct(prior.basis_pct)} nominal with "
            f"{prior.days_to_expiry} days to its expiry)"
        )
    else:
        change = (
            f"{current.annualized_pct - prior.annualized_pct:+.2f} points annualized, from "
            f"{_pct(prior.annualized_pct)} on the reading ending {_stamp(prior.last)} UTC"
        )
    lines.append(f"**Change over {LOOKBACK_DAYS} days:** {change}")

    lines.append("")
    lines.append(
        f"_Method: only hours inside the CME session in which the future traded are used, and "
        f"each figure is a median, because Yahoo's hourly {FUTURES_SYMBOL} closes are unreliable "
        f"around session breaks. The expiry date follows the last-Friday rule and can be a day "
        f"late around an exchange holiday, so read the annualized figure as approximate. "
        f"{SCALE_NOTE}._"
    )
    headline = (
        f"annualized {_pct(current.annualized_pct)}"
        if current.annualized_pct is not None
        else f"annualized figure {_annualized_withheld(current)}"
    )
    lines.append(
        f"_Reading: {coin} CME front-month basis {_pct(current.basis_pct)} nominal as of "
        f"{_stamp(current.last)} UTC"
        f"{f' ({idle_hours} hours old, not a live reading)' if not_live else ''}, {headline}. "
        f"{SAWTOOTH_NOTE}. {CARRY_NOTE}._"
    )
    return "\n".join(lines) + "\n"
