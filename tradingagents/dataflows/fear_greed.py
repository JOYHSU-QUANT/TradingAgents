"""Crypto Fear & Greed Index vendor (alternative.me).

A 0-100 market-sentiment gauge (0 = Extreme Fear, 100 = Extreme Greed) derived
from volatility, momentum, volume, social media, and BTC dominance. Surfaced to
the news analyst as a crypto-only sentiment signal alongside ETF flows and news.

Keyless: GET https://api.alternative.me/fng/?limit=N&format=json. Each ``data``
row carries ``value`` (0-100 string), ``value_classification``, and
``timestamp`` (unix seconds string). A network error or malformed payload raises
so the routing layer degrades the optional crypto_sentiment category to a
sentinel instead of aborting the run.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from .errors import VendorError

logger = logging.getLogger(__name__)

FNG_URL = "https://api.alternative.me/fng/"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default trailing window when the caller does not specify one.
DEFAULT_LOOKBACK_DAYS = 30

# Row cap for the rendered table, mirroring fred.MAX_ROWS.
MAX_ROWS = 40

# Extra days fetched beyond the window so a ~30-day-ago comparison point still
# exists after the lookahead filter trims any future-dated readings.
_FETCH_BUFFER_DAYS = 45

# This vendor is deliberately uncached (freshness), which also means it has no
# buffer against a single dropped connection — unlike Farside, whose stale cache
# absorbs a blip. One retry after a short pause restores that tolerance without
# weakening the freshness rationale.
#
# The cost that matters is latency, not bandwidth: the worst case is
# REQUEST_TIMEOUT + _RETRY_DELAY_SECONDS + REQUEST_TIMEOUT ~= 62s for one tool
# call. That is fine here (the analyst graph runs off the trading hot path, on a
# 4-hour cycle), but anyone raising _RETRY_ATTEMPTS should multiply that envelope
# rather than the request count.
_RETRY_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 2

# The index is a daily series, so the newest reading is normally same- or
# previous-day. Beyond this lag the *data* is stale even though the *fetch*
# succeeded — a separate failure mode from an unreachable vendor, and one the
# report must disclose rather than presenting an old reading as current.
MAX_DATA_LAG_DAYS = 2


class FearGreedError(VendorError):
    """alternative.me was unreachable or returned an unusable payload.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing layer
    reacts by behaviour rather than by vendor, and the optional crypto_sentiment
    category degrades to a sentinel instead of aborting the run. Every failure
    mode — network error, non-2xx, undecodable body, wrong payload shape, or a
    malformed row — is funnelled through this one type.
    """


def _request(limit: int) -> dict:
    """GET the Fear & Greed history, retrying once on a transient failure.

    Wraps every failure in FearGreedError so a caller written against this
    module's documented exception type sees network errors too.
    """
    last_error: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            response = requests.get(
                FNG_URL, params={"limit": limit, "format": "json"}, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
            break
        except (requests.RequestException, ValueError) as e:
            # ValueError also covers response.json()'s JSONDecodeError subclass.
            last_error = e
            if attempt + 1 < _RETRY_ATTEMPTS:
                logger.warning(
                    "Fear & Greed request failed (%s); retrying in %ss", e, _RETRY_DELAY_SECONDS
                )
                time.sleep(_RETRY_DELAY_SECONDS)
    else:
        raise FearGreedError(
            f"alternative.me unreachable after {_RETRY_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    # A CDN/WAF error page can decode as valid JSON that is not an object; guard
    # the shape here so it surfaces as this module's typed error rather than a
    # bare AttributeError from the .get() below.
    if not isinstance(payload, dict):
        raise FearGreedError(
            f"alternative.me returned a JSON {type(payload).__name__}, expected an object"
        )
    return payload


def get_fear_greed_data(
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch the Crypto Fear & Greed Index history as a markdown report.

    Args:
        curr_date: End of the window (yyyy-mm-dd); readings dated after it are
            dropped so a past date never leaks a future sentiment reading.
        look_back_days: Trailing window length; ``None`` uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report: the latest value and classification, the change vs 7
        and 30 days ago, and a recent daily-reading table.

    Note:
        alternative.me's ``limit`` returns the N most-recent readings counting
        back from the present, with no date-range parameter, so the fetch's reach
        is measured from the real "today", not from ``curr_date``. The fetch is
        sized ``max(look_back_days, 30) + _FETCH_BUFFER_DAYS``, so a ``curr_date``
        in the past degrades in two stages rather than one:

        * more than ~``max(look_back_days, 30) + 15`` days back, the fetch no
          longer reaches a 30-days-earlier reference point, so **vs 30d** alone
          reports "insufficient history" while Latest and vs 7d stay valid;
        * more than ~``max(look_back_days, 30) + 38`` days back, the 7-days-earlier
          reference point drops out as well, so **both** deltas read "insufficient
          history" while Latest is still populated;
        * more than ~``max(look_back_days, 30) + _FETCH_BUFFER_DAYS`` days back,
          every reading is filtered out and the report says "no readings".

        At the default 30-day window those thresholds are ~45, ~68 and ~75 days. The
        fetch is deliberately not widened to cover historical ``curr_date``s: this
        is a recent-window signal and live use has ``curr_date`` ≈ today.
    """
    # A None or negative window falls back to the default rather than sizing a
    # nonsensical fetch limit.
    if look_back_days is None or look_back_days < 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Fetch enough to cover the window plus a 30-day-ago comparison point.
    limit = max(look_back_days, 30) + _FETCH_BUFFER_DAYS
    payload = _request(limit)
    data = payload.get("data")
    if not data:
        raise FearGreedError("alternative.me returned no Fear & Greed data")

    points = []
    for row in data:
        try:
            # int(float(...)) tolerates a decimal-formatted timestamp string.
            ts = int(float(row["timestamp"]))
            value = int(row["value"])
            # Inside the try so an out-of-range timestamp raises the uniform typed
            # error, not a raw exception (on Windows fromtimestamp raises OSError
            # for a bad/negative ts, elsewhere OverflowError/ValueError).
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (KeyError, ValueError, TypeError, OSError, OverflowError) as e:
            raise FearGreedError(f"Malformed Fear & Greed row {row!r}") from e
        points.append({"date": day, "value": value, "label": row.get("value_classification", "")})

    # Lookahead-safe: keep only readings on or before curr_date, ascending.
    points = [p for p in points if p["date"] <= curr_date]
    points.sort(key=lambda p: p["date"])

    header = (
        "## Crypto Fear & Greed Index (alternative.me)\n"
        "- Scale: 0 = Extreme Fear ... 50 = Neutral ... 100 = Extreme Greed | "
        f"Window ending {curr_date}\n"
    )

    if not points:
        return (
            header + f"\nNo index readings on or before {curr_date}. "
            "Report this as no sentiment reading for the date; do not fabricate values."
        )

    latest = points[-1]
    latest_dt = datetime.strptime(latest["date"], "%Y-%m-%d")
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    # Data-recency caveat. A successful fetch says nothing about whether
    # alternative.me actually published a new reading: the API can be up and
    # serving a series that stopped updating days ago. That is a different
    # failure from "vendor unreachable" and has no other guardrail here (this
    # vendor is uncached, so there is no fetched_at staleness check either), so
    # disclose it rather than letting an old value read as current.
    lag_days = (curr_dt - latest_dt).days
    lag_note = (
        f"_Data lag: the newest reading is {lag_days} days before {curr_date}; "
        f"alternative.me has published nothing since. Treat as stale._\n\n"
        if lag_days > MAX_DATA_LAG_DAYS
        else ""
    )

    # look_back_days bounds the displayed table; the 7d/30d deltas keep their fixed
    # horizons and read from the full `points` above (which reaches past the window).
    window_start = (curr_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")
    window_points = [p for p in points if p["date"] >= window_start]

    def _value_at_or_before(target: str):
        prior = [p for p in points if p["date"] <= target]
        return prior[-1] if prior else None

    def _delta_line(days: int) -> str:
        # Anchored on curr_date, not on the latest reading, so "7d" always means
        # "7 days before the date being analysed" rather than a window that
        # floats backwards whenever the series lags. The reference date is
        # printed, and a lagging series is called out by lag_note above, so the
        # span the number actually covers stays visible.
        target = (curr_dt - timedelta(days=days)).strftime("%Y-%m-%d")
        ref = _value_at_or_before(target)
        if not ref or ref["date"] == latest["date"]:
            return "n/a (insufficient history)"
        return f"{latest['value'] - ref['value']:+d} (from {ref['value']} on {ref['date']})"

    summary = (
        "\n"
        + lag_note
        + f"**Latest ({latest['date']}):** {latest['value']} — {latest['label']}\n"
        + f"**vs 7d:** {_delta_line(7)}\n"
        + f"**vs 30d:** {_delta_line(30)}\n"
    )

    # The window bounds the table; if the latest reading itself predates the window
    # (unusually stale data), still show it so the table matches the summary above.
    shown = window_points or points[-1:]
    note = ""
    if len(shown) > MAX_ROWS:
        shown = shown[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(window_points)} readings in the window)_\n"
    elif not window_points:
        note = f"\n_(no readings within the {look_back_days}-day window; showing the latest available)_\n"
    table = (
        "\n| Date | Value | Classification |\n| --- | --- | --- |\n"
        + "\n".join(f"| {p['date']} | {p['value']} | {p['label']} |" for p in shown)
        + "\n"
    )

    return header + summary + note + table
