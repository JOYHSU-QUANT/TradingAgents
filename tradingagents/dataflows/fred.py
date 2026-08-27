"""FRED (Federal Reserve Economic Data) macro vendor.

Fetches macroeconomic time series — policy rates, Treasury yields, inflation,
labor, growth — from the St. Louis Fed's free API. Used by the news analyst to
ground macro commentary in actual numbers rather than headlines alone.

A free API key (https://fred.stlouisfed.org/docs/api/api_key.html) is read from
``FRED_API_KEY``; if it is unset the vendor raises ``FredNotConfiguredError`` so
the routing layer treats it as "unavailable" rather than a hard crash.
"""

import logging
import os
from datetime import datetime, timedelta

import requests

from .errors import VendorError, VendorNotConfiguredError
from .utils import data_lag_note, date_refusal

logger = logging.getLogger(__name__)

FRED_API_BASE = "https://api.stlouisfed.org/fred"

# Network timeout (seconds) so a stalled request can't hang the agents,
# mirroring the Alpha Vantage client.
REQUEST_TIMEOUT = 30

# Default trailing window when the caller does not specify one. A year captures
# the trend and the year-over-year base for most monthly/quarterly series.
DEFAULT_LOOKBACK_DAYS = 365

# Rows cap for the rendered table: recent values matter most for a decision, and
# daily series (yields, VIX) over a long window would otherwise flood context.
MAX_ROWS = 40

# Freshness thresholds (calendar days) for the data-lag note, keyed by FRED's
# frequency_short code: how far the newest observation may trail curr_date
# before the report flags it. FRED dates observations at the PERIOD START
# (July CPI is dated 07-01 but releases ~Aug 12), and the lag keeps growing
# until the *next* release — so each bound must cover roughly two periods plus
# the publication delay, the worst on-schedule lag. Tighter bounds (e.g. M:45)
# would flag an on-schedule CPI as stale for about half of every month. A note
# therefore means the series is genuinely behind, at the cost of detecting a
# stall about one period later. Codes not listed get no note — an annotation
# must not false-alarm on a cadence it does not understand (#30).
_MAX_LAG_DAYS_BY_FREQUENCY = {
    "D": 7,
    "W": 14,
    "BW": 21,
    "M": 80,
    "Q": 220,
    "SA": 420,
    "A": 750,
}

# Curated human-friendly aliases -> FRED series IDs. Anything not listed is used
# verbatim as a raw FRED series ID, so power users are never limited to this set.
MACRO_SERIES = {
    # Policy rate & Treasury yields
    "fed_funds_rate": "FEDFUNDS",
    "federal_funds_rate": "FEDFUNDS",
    "fed_funds": "FEDFUNDS",
    "2y_treasury": "DGS2",
    "10y_treasury": "DGS10",
    "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y",
    "yield_curve": "T10Y2Y",
    # Inflation
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "pce": "PCEPI",
    "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    # Growth & output
    "real_gdp": "GDPC1",
    "gdp": "GDP",
    "industrial_production": "INDPRO",
    # Labor
    "unemployment_rate": "UNRATE",
    "unemployment": "UNRATE",
    "nonfarm_payrolls": "PAYEMS",
    "payrolls": "PAYEMS",
    "initial_claims": "ICSA",
    # Money & markets
    "m2": "M2SL",
    "money_supply": "M2SL",
    "vix": "VIXCLS",
    "dollar_index": "DTWEXBGS",
    # Sentiment & housing
    "consumer_sentiment": "UMCSENT",
    "housing_starts": "HOUST",
    "retail_sales": "RSAFS",
}


class FredNotConfiguredError(VendorNotConfiguredError):
    """Raised when FRED is selected but no API key is configured.

    A VendorNotConfiguredError (and thus still a ValueError), so the routing
    layer's "vendor unavailable" handling and existing ValueError callers both
    keep working.
    """


def get_api_key() -> str:
    """Retrieve the FRED API key from the environment."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        raise FredNotConfiguredError(
            "FRED_API_KEY environment variable is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html."
        )
    return api_key


def _resolve_series_id(indicator: str) -> str:
    """Map a friendly alias to a FRED series ID, or pass a raw ID through.

    Raises ``ValueError`` when the input is neither a known alias nor a plausible
    series ID — typically a descriptive phrase the LLM passed instead (e.g.
    "bank of japan rate"). FRED IDs are short and alphanumeric, so this rejects
    it up front with guidance rather than letting it 400 the API.
    """
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MACRO_SERIES:
        return MACRO_SERIES[key]
    candidate = indicator.strip().upper()
    # FRED series IDs never contain whitespace and are short; reject anything
    # else (a descriptive phrase the LLM passed) rather than 400ing the API.
    if not candidate or len(candidate) > 30 or any(c.isspace() for c in candidate):
        raise ValueError(
            f"'{indicator}' is not a known macro alias or a valid FRED series ID. "
            f"Use an alias (e.g. 'cpi', 'unemployment', '10y_treasury') or a raw "
            f"FRED series ID (e.g. 'CPIAUCSL')."
        )
    return candidate


def _request(path: str, params: dict) -> dict:
    """GET a FRED endpoint, surfacing FRED's JSON error body on a bad request."""
    api_params = {**params, "api_key": get_api_key(), "file_type": "json"}
    response = requests.get(f"{FRED_API_BASE}/{path}", params=api_params, timeout=REQUEST_TIMEOUT)
    # FRED returns 400 with a JSON {"error_message": ...} for unknown series IDs
    # or malformed params; turn that into a clear, actionable error.
    if response.status_code == 400:
        try:
            message = response.json().get("error_message", response.text)
        except ValueError:
            message = response.text
        raise ValueError(f"FRED request failed: {message}")
    response.raise_for_status()
    return response.json()


def get_macro_data(
    indicator: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch a FRED macroeconomic series as a formatted markdown report.

    Args:
        indicator: A friendly alias (e.g. "cpi", "unemployment", "10y_treasury")
            or a raw FRED series ID (e.g. "CPIAUCSL", "DGS10").
        curr_date: End of the window (yyyy-mm-dd); no later observations are
            returned, so a past date never leaks future data.
        look_back_days: Trailing window length; ``None`` uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report with the series title, units, frequency, the latest
        value, the change over the window, and a recent observation table.

    Raises:
        VendorError: When the series endpoint's metadata names a different
            series than requested (identity echo, #36 family) — routed
            callers turn this into try-next-vendor / DATA_UNAVAILABLE.

    An unusable ``curr_date`` answers the shared ``INVALID_CURR_DATE`` sentinel
    before any request, as the core-category tools do (#119): a bare
    ``strptime`` ValueError used to reach the router's optional lane and come
    back as DATA_UNAVAILABLE for what was the caller's own argument.
    """
    refusal = date_refusal(curr_date, what="macro data", kind="point")
    if refusal is not None:
        return refusal

    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (end_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    # Invalid LLM-supplied indicator: return guidance rather than raising, so a
    # bad argument doesn't abort the run (the routing layer also degrades macro
    # data, but a specific message is more useful to the analyst).
    try:
        series_id = _resolve_series_id(indicator)
    except ValueError as e:
        return f"FRED: {e}"

    meta = _request("series", {"series_id": series_id}).get("seriess") or []
    if not meta:
        return (
            f"FRED series '{series_id}' not found. Pass a known alias "
            f"(e.g. 'cpi', 'unemployment') or a valid FRED series ID."
        )
    info = meta[0]
    # Identity echo: the series endpoint names the series it is answering for;
    # a mismatch means FRED responded for a different instrument — refuse to
    # render another series' numbers (#36 family). Raised as a typed
    # VendorError (not returned as prose) because this vendor is routed:
    # route_to_vendor turns the raise into a vendor failure — try the next
    # vendor, else the DATA_UNAVAILABLE sentinel — whereas a returned string
    # would count as a *successful* fetch and short-circuit the chain. A
    # missing/malformed id just skips the check: this is a guard, not a new
    # failure mode.
    echoed_id = info.get("id")
    if isinstance(echoed_id, str) and echoed_id.strip().upper() != series_id.upper():
        raise VendorError(
            f"FRED series identity mismatch: requested '{series_id}', response "
            f"is for '{echoed_id.strip()}'; refusing to render another series' data"
        )
    title = info.get("title", series_id)
    units = info.get("units_short") or info.get("units", "")
    frequency = info.get("frequency", "")
    seasonal = info.get("seasonal_adjustment_short", "")

    observations = _request(
        "series/observations",
        {
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": curr_date,
            "sort_order": "asc",
        },
    ).get("observations", [])

    # FRED encodes a missing observation as ".".
    points = [
        (o["date"], o["value"]) for o in observations if o.get("value") not in (".", None, "")
    ]

    header = (
        f"## FRED: {title} ({series_id})\n"
        f"- Units: {units}\n"
        f"- Frequency: {frequency}"
        f"{f' ({seasonal})' if seasonal else ''}\n"
        f"- Window: {start_date} to {curr_date}\n"
    )

    if not points:
        return header + (
            f"\nNo observations for {series_id} in this window. The series may "
            f"report less frequently than the window length; widen look_back_days."
        )

    first_date, first_val = points[0]
    last_date, last_val = points[-1]
    try:
        delta = float(last_val) - float(first_val)
        base = float(first_val)
        pct = f" ({delta / base * 100:+.2f}%)" if base != 0 else ""
        summary = (
            f"\n**Latest:** {last_val} ({last_date}) | "
            f"**Change over window:** {delta:+.2f}{pct} "
            f"from {first_val} ({first_date})\n"
        )
    except ValueError:
        summary = f"\n**Latest:** {last_val} ({last_date})\n"

    # Freshness: a successful fetch says nothing about whether FRED has a
    # recent observation — the window header above even advertises coverage
    # "to {curr_date}". Compare the newest observation against curr_date with
    # a cadence-aware bound so a genuinely behind series is disclosed (#30).
    max_lag = _MAX_LAG_DAYS_BY_FREQUENCY.get(str(info.get("frequency_short") or "").strip().upper())
    lag_note = ""
    if max_lag is not None:
        lag_note = data_lag_note(last_date, curr_date, max_lag, f"{series_id} observation")
        if lag_note:
            # Leading blank line so the note renders as its own markdown
            # paragraph instead of a soft continuation of the Latest line.
            lag_note = "\n" + lag_note + "\n"

    shown = points
    truncation_note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        truncation_note = (
            f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"
        )

    table = (
        "\n| Date | Value |\n| --- | --- |\n" + "\n".join(f"| {d} | {v} |" for d, v in shown) + "\n"
    )

    return header + summary + lag_note + truncation_note + table
