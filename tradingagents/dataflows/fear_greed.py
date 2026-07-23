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
from datetime import datetime, timedelta, timezone

import requests

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


class FearGreedError(RuntimeError):
    """alternative.me was unreachable or returned an unusable payload.

    A plain exception so the optional crypto_sentiment category degrades to a
    sentinel instead of aborting the run.
    """


def _request(limit: int) -> dict:
    response = requests.get(
        FNG_URL, params={"limit": limit, "format": "json"}, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


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
        alternative.me's ``limit`` returns the N most-recent readings from the
        present, with no date-range parameter. This is a recent-window signal:
        for a ``curr_date`` more than ~75 days in the past (e.g. an old backtest)
        every fetched reading is filtered out and the report says "no readings".
        For live/near-real-time use (curr_date ≈ today) the full window is served.
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
        except (KeyError, ValueError, TypeError) as e:
            raise FearGreedError(f"Malformed Fear & Greed row {row!r}") from e
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
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
        return header + f"\nNo index readings on or before {curr_date}."

    latest = points[-1]
    latest_dt = datetime.strptime(latest["date"], "%Y-%m-%d")

    def _value_at_or_before(target: str):
        prior = [p for p in points if p["date"] <= target]
        return prior[-1] if prior else None

    def _delta_line(days: int) -> str:
        target = (latest_dt - timedelta(days=days)).strftime("%Y-%m-%d")
        ref = _value_at_or_before(target)
        if not ref or ref["date"] == latest["date"]:
            return "n/a (insufficient history)"
        return f"{latest['value'] - ref['value']:+d} (from {ref['value']} on {ref['date']})"

    summary = (
        f"\n**Latest ({latest['date']}):** {latest['value']} — {latest['label']}\n"
        f"**vs 7d:** {_delta_line(7)}\n"
        f"**vs 30d:** {_delta_line(30)}\n"
    )

    shown = points
    note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} readings)_\n"
    table = (
        "\n| Date | Value | Classification |\n| --- | --- | --- |\n"
        + "\n".join(f"| {p['date']} | {p['value']} | {p['label']} |" for p in shown)
        + "\n"
    )

    return header + summary + note + table
