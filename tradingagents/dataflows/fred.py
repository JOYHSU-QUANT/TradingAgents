"""FRED (Federal Reserve Economic Data) macro vendor.

Fetches macroeconomic time series — policy rates, Treasury yields, inflation,
labor, growth — from the St. Louis Fed's free API. Used by the news analyst to
ground macro commentary in actual numbers rather than headlines alone.

A free API key (https://fred.stlouisfed.org/docs/api/api_key.html) is read from
``FRED_API_KEY``; if it is unset the vendor raises ``FredNotConfiguredError`` so
the routing layer treats it as "unavailable" rather than a hard crash.
"""

import logging
import math
import os
from datetime import datetime, timedelta

import requests

from .errors import VendorError, VendorNotConfiguredError
from .utils import (
    MAX_UNTRUSTED_CHARS,
    data_lag_note,
    date_refusal,
    echo_argument,
    json_body_or_outage,
    normalize_iso_date,
    quote_argument,
    raise_for_http_status,
    sanitize_untrusted,
)

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

# What the heading says when FRED gave the series no renderable title. Named
# rather than defaulted to the series id, which would read as a series titled
# after itself (#233).
TITLE_UNAVAILABLE = "(title unavailable)"

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


class FredRequestError(VendorError, ValueError):
    """FRED answered 400 about this request, with its own reason in the body.

    A ``VendorError`` so the reason — "Bad value for variable series_id", the
    one thing that lets the analyst pick a better series — rides into the
    optional category's sentinel as vendor text, where an untyped exception
    contributes its class name only (#171). Also a ``ValueError``, like
    ``FredNotConfiguredError``, which is what ``_request``'s 400 raise used
    to be.
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


def _is_finite_number(value) -> bool:
    """Whether FRED's raw observation value IS a number, before any flattening.

    ``nan`` and ``inf`` are refused with the unparseable ones: both are floats
    ``float()`` accepts, and either would poison the window delta and the
    percentage change computed from it — a fabricated figure rather than a
    missing one, which is the failure the observation guard exists to prevent.
    """
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        # ``OverflowError`` is not hypothetical: ``json.loads`` keeps arbitrary
        # precision, so a 400-digit integer literal raises here rather than in
        # ``float()``'s usual lanes, and this guard exists precisely because
        # the payload is not to be trusted.
        return False


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
        # The rejected value is the model's own text echoed back into a
        # sentence it reads (``get_macro_data`` serves this as prose), so it
        # goes through the shared argument echo: flattened, capped, edges kept
        # so ``_foo`` is not quoted back as ``foo`` beside "not a valid ID"
        # (#231).
        echo = quote_argument(indicator)
        raise ValueError(
            f"{echo} is not a known macro alias or a valid FRED series ID. "
            f"Use an alias (e.g. 'cpi', 'unemployment', '10y_treasury') or a raw "
            f"FRED series ID (e.g. 'CPIAUCSL')."
        )
    return candidate


def _request(path: str, params: dict) -> dict:
    """GET a FRED endpoint, surfacing FRED's JSON error body on a bad request.

    A 5xx, or a 2xx whose body is not JSON, raises ``VendorUnavailableError``
    (the shared boundary helpers): FRED answered without data, which the
    router logs without a traceback and degrades from — left to
    ``raise_for_status()`` / a bare ``.json()`` the same events reached its
    generic lane as a bug (#142). Every other status keeps its handling.
    """
    api_params = {**params, "api_key": get_api_key(), "file_type": "json"}
    response = requests.get(f"{FRED_API_BASE}/{path}", params=api_params, timeout=REQUEST_TIMEOUT)
    # FRED returns 400 with a JSON {"error_message": ...} for unknown series IDs
    # or malformed params; turn that into a clear, actionable error. The
    # reason is FRED's text, flattened (uncapped) where it enters the message
    # like every boundary's; the router caps it at the sentinel. A 400 whose
    # body is not that object — not JSON, JSON that is not an object (a
    # WAF's), or one without the key — keeps the body text as the reason.
    if response.status_code == 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        message = (body.get("error_message") if isinstance(body, dict) else None) or response.text
        raise FredRequestError(f"FRED request failed: {sanitize_untrusted(message)}")
    raise_for_http_status(response, "FRED")
    return json_body_or_outage(response, "FRED")


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
            f"FRED series {quote_argument(series_id)} not found. Pass a known alias "
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
    # Series metadata is FRED's own free text and renders into the header's
    # heading and label lines, so it takes the vendor flattening. The default
    # for a missing title is the caller's series id, which is an ARGUMENT and
    # is echoed as one below — so the substitution happens after the flatten,
    # not before, and each value takes the guard its own subject calls for.
    def _meta(*keys) -> str:
        """The first of these metadata fields FRED gave, as it will render."""
        for key in keys:
            shown = sanitize_untrusted(info.get(key) or "", limit=MAX_UNTRUSTED_CHARS)
            if shown:
                return shown
        return ""

    title = _meta("title")
    units = _meta("units_short", "units")
    frequency = _meta("frequency")
    seasonal = _meta("seasonal_adjustment_short")

    observations = _request(
        "series/observations",
        {
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": curr_date,
            "sort_order": "asc",
        },
    ).get("observations", [])

    # A row is ADMITTED on the shape of its RAW value, never repaired into one.
    #
    # Both halves are raw vendor strings that nothing coerces on the way in,
    # and both render inside "|"-separated lines — the observation table's
    # cells, and the Latest/Change summary, which uses "|" as its own field
    # separator — so one "|" or line break forges a column or a whole row
    # (#233). Flattening alone would close that and open something worse:
    # ``sanitize_untrusted`` turns "4.1|" into "4.1", so a value that used to
    # fail ``float()`` and degrade the summary visibly would instead parse and
    # drive a computed macro delta with nothing to say it had been altered —
    # and "2026-06-0#1" becomes "2026-06-0 1", which ``data_lag_note`` cannot
    # read, so the freshness disclosure silently disappears and the report
    # reads as MORE trustworthy for being corrupt.
    #
    # So the raw value is asked to BE a finite number and the raw date to be a
    # date; a row that is neither is dropped and counted. The flattening that
    # remains is a second line of defence, not the guard: a string that parses
    # to a finite float and an ISO date both come through it byte for byte
    # apart from whitespace, so the value judged and the value shown are one
    # value. Same answer ``fear_greed`` already gives its rows, which coerce
    # and raise rather than print whatever arrived.
    points = []
    unusable = 0
    for o in observations:
        raw_value = o.get("value")
        if raw_value == ".":
            # FRED's OWN missing-observation encoding, and the only one: a
            # period means "this period has no reading", which is a fact about
            # the series rather than a fault in the payload. An absent key or
            # an empty string is neither, and used to be waved through here
            # beside it — so a response whose every row was malformed still
            # advised the reader to widen look_back_days.
            continue
        day = normalize_iso_date(o.get("date"))
        shown = sanitize_untrusted(raw_value, limit=MAX_UNTRUSTED_CHARS)
        # Both spellings are asked, which is what makes "judged == shown" true
        # rather than nearly true: the RAW one so flattening cannot repair a
        # value into a number, and the RENDERED one so a value that only the
        # raw form parses — a JSON ``true``, or a number the cap cut — cannot
        # reach the table and then fail the summary's ``float()`` silently.
        if day is None or not (_is_finite_number(raw_value) and _is_finite_number(shown)):
            unusable += 1
            continue
        points.append((day, shown))

    # The series id is the caller's own argument coming back into text the
    # model reads, bare and in running prose, so it takes ``echo_argument``.
    # This module ALREADY echoed the id it REJECTED (``_resolve_series_id``
    # above) while interpolating the accepted one raw — the guard and the hole
    # were two screens apart in one file, which is why #233 groups by subject
    # rather than by module. ``_resolve_series_id`` bounds it to 30 characters
    # with no whitespace, so a line break is already impossible here; "|" and
    # "#" are not, and the bound is a fact about a caller two hundred lines
    # away rather than about this line.
    echoed_series = echo_argument(series_id)
    # A field with nothing left to show is NAMED where it is a subject and
    # OMITTED where it is a label. The heading always has a name slot, so an
    # absent or unrenderable title takes the marker rather than borrowing the
    # series id — which would read exactly like a series FRED titled after
    # itself. A "- Units: " with nothing after it is not a fact at all, so that
    # line simply does not appear; the same rule PR #251 applied to the news
    # report's summary and link lines.
    header_lines = [f"## FRED: {title or TITLE_UNAVAILABLE} ({echoed_series})"]
    if units:
        header_lines.append(f"- Units: {units}")
    if frequency:
        header_lines.append(f"- Frequency: {frequency}{f' ({seasonal})' if seasonal else ''}")
    elif seasonal:
        # Seasonal adjustment rides on the Frequency line only because it
        # usually has one to ride on. It is its own fact about the series, so
        # dropping the label must not drop it too.
        header_lines.append(f"- Seasonal adjustment: {seasonal}")
    header_lines.append(f"- Window: {start_date} to {curr_date}")
    header = "\n".join(header_lines) + "\n"

    if not points:
        # "The series reports less frequently than your window" is the wrong
        # explanation when rows arrived and were dropped as unusable, so this
        # branch names that case rather than letting the cadence sentence stand
        # for both.
        if unusable:
            return header + (
                f"\nNo usable observations for {echoed_series} in this window: FRED "
                f"served {unusable} row(s) whose date or value could not be read."
            )
        return header + (
            f"\nNo observations for {echoed_series} in this window. The series may "
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
        # The lag note renders into the report as its own paragraph, so the id
        # it names is prompt text like the heading's.
        lag_note = data_lag_note(last_date, curr_date, max_lag, f"{echoed_series} observation")
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

    # Dropped rows are disclosed for the reason every omission in these reports
    # is: a table that quietly loses observations is a different series from
    # the one FRED served, and the reader has no way to see the difference.
    unusable_note = (
        f"\n_({unusable} observation(s) omitted: FRED's date or value was not usable)_\n"
        if unusable
        else ""
    )

    return header + summary + lag_note + unusable_note + truncation_note + table
