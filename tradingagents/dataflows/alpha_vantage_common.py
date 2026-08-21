import json
import os
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

from .errors import NoMarketDataError, VendorNotConfiguredError, VendorRateLimitError
from .utils import _parse_day

API_BASE_URL = "https://www.alphavantage.co/query"

# Network timeout (seconds) so a stalled Alpha Vantage request can't hang the
# CLI/agents indefinitely (#990).
REQUEST_TIMEOUT = 30


class AlphaVantageNotConfiguredError(VendorNotConfiguredError):
    """Raised when Alpha Vantage is selected but no API key is configured.

    A VendorNotConfiguredError (and thus still a ValueError), so the routing
    layer's "vendor unavailable" handling and existing ValueError callers both
    keep working.
    """

    pass


def get_api_key() -> str:
    """Retrieve the API key for Alpha Vantage from environment variables."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise AlphaVantageNotConfiguredError(
            "ALPHA_VANTAGE_API_KEY environment variable is not set."
        )
    return api_key


def format_datetime_for_api(date_input) -> str:
    """Convert various date formats to YYYYMMDDTHHMM format required by Alpha Vantage API."""
    if isinstance(date_input, str):
        # If already in correct format, return as-is
        if len(date_input) == 13 and "T" in date_input:
            return date_input
        # Try to parse common date formats
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}") from None
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")


class AlphaVantageRateLimitError(VendorRateLimitError):
    """Raised when the Alpha Vantage API rate limit is exceeded."""

    pass


# Keys Alpha Vantage answers with when it is reporting a problem rather than
# serving data. A body made only of these is a failure envelope, not a payload.
# The single definition of that key list (#68). ``_make_api_request`` handles
# each key's raise-worthy case itself ("Error Message" always raises;
# Information/Note raise only when their text matches a rate-limit or API-key
# pattern); the fundamentals module imports the set to decide whether a body
# that still reached it — an unmatched Information/Note — carries anything
# worth a freshness disclosure.
_AV_ENVELOPE_KEYS = {"Error Message", "Information", "Note"}

# Where a freshness disclosure lives in an Alpha Vantage payload. This vendor
# answers in JSON (yfinance answers in CSV with "# " header lines), so the note
# is carried as a key rather than a prefixed line: the body stays parseable, and
# an underscore-prefixed name is not part of any Alpha Vantage schema this repo
# has seen. That last part is a convention, not a guarantee, so every path that
# serves a body drops a same-named key from it instead of trusting it to be
# absent — including the paths that attach no disclosure of their own, where a
# vendor-written note would stand unopposed. Written first so a disclosure is
# not buried under a long report list (#58). Shared here so every Alpha Vantage
# module annotates through the same carrier (#69).
_FRESHNESS_NOTE_KEY = "_freshness_note"


def _carries_payload(parsed: dict) -> bool:
    """True when a parsed body carries something other than a failure envelope.

    ``_AV_ENVELOPE_KEYS`` is this module's single definition of the failure
    keys (#68); whether the request boundary would have raised on a body has no
    bearing on whether it contains data to disclose about. The freshness key is
    discounted alongside the envelope keys: a body of nothing but a notice plus
    a vendor-written ``_freshness_note`` is still a failure envelope, and
    counting that key as content would let a rate-limit notice be dressed in
    our own freshness disclosure.
    """
    return bool(parsed.keys() - _AV_ENVELOPE_KEYS - {_FRESHNESS_NOTE_KEY})


def _parsed_payload(result) -> dict | None:
    """The response body as a JSON object, or ``None`` when it is not one.

    One decoder for every annotation path: ``_make_api_request`` returns
    response *text*, and an Alpha Vantage endpoint answers with a JSON object
    only when it served data — a rejection can arrive as prose instead.
    Whatever "decodable as a payload" means has to mean the same thing to every
    caller judging one, so it is decided here rather than restated in each.
    """
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None  # not JSON (an error/plain-text body)
    return parsed if isinstance(parsed, dict) else None


def _served_body(result, parsed: dict | None) -> str:
    """The vendor body as served, minus any freshness key the vendor supplied.

    Used by the exits that hand a body over WITHOUT rebuilding it, because a
    vendor-written ``_freshness_note`` reaches the agent looking like a
    system-issued freshness statement — and on these paths there is no real note
    beside it to contradict it. (The statement path rebuilds its body and drops
    the key there instead.) Returns ``result`` untouched when there is nothing to
    strip, so the ordinary no-disclosure answer stays byte-identical to the
    vendor's own text; stripping necessarily re-serializes.
    """
    if parsed is None or _FRESHNESS_NOTE_KEY not in parsed:
        return result
    return json.dumps({k: v for k, v in parsed.items() if k != _FRESHNESS_NOTE_KEY}, indent=2)


def _newest_row_date(rows, field: str):
    """The newest date carried under ``field`` across dict rows, or ``None``.

    One "newest date among rows" reduction for every annotation path (the
    statement and insider notes both need it), so the family cannot grow a
    third parsing style that drifts from the others (#69). Non-dict rows and
    rows without the field are skipped silently — the annotations this feeds
    degrade to silence rather than guess — but a present-and-unparseable date
    goes through ``utils._parse_day``, which logs it: a vendor date-format
    drift must leave a trace instead of switching every disclosure off
    invisibly.
    """
    dates = [
        parsed
        for row in rows
        if isinstance(row, dict) and (parsed := _parse_day(row.get(field), field)) is not None
    ]
    return max(dates) if dates else None


def _with_freshness_note(payload: dict, note: str) -> str:
    """Render ``payload`` with its freshness note first, as JSON text.

    Any same-named key already in the vendor body is dropped rather than merged
    over: a plain ``{key: note, **payload}`` would let the body win, silently
    replacing our disclosure with text the vendor wrote — which the agent would
    then read as a system-issued freshness statement.
    """
    body = {k: v for k, v in payload.items() if k != _FRESHNESS_NOTE_KEY}
    return json.dumps({_FRESHNESS_NOTE_KEY: note, **body}, indent=2)


def _make_api_request(function_name: str, params: dict, subject: str | None = None) -> dict | str:
    """Helper function to make API requests and handle responses.

    ``subject`` is what a rejection is attributed to (the caller knows which of
    its params is the instrument); unset, it falls back to the params'
    ``symbol``/``tickers`` and then to the function name — a last resort that
    reads oddly in the router's no-data sentinel, so callers whose requests
    carry neither key should name their subject.

    Raises:
        AlphaVantageRateLimitError: When API rate limit is exceeded — whether
            reported as an HTTP 429 or as a notice in an HTTP 200 body (#72)
        NoMarketDataError: When the body is an ``Error Message`` rejection
            envelope — Alpha Vantage's "Invalid API call" answer for a symbol
            or parameter it cannot serve — or a JSON body that is not an
            object (no shape this vendor serves data in). Raised here, at the
            one boundary every Alpha Vantage request goes through, because
            such a body otherwise returns to callers looking like a
            successful answer and the router never gets to fall back (#68).
            The no-data type is the decided one: the router tries the next
            vendor, then answers with its no-data sentinel — and the vendor's
            wording rides along in ``detail``, so a parameter mistake stays
            visible there rather than flattened into a bare "no data".
    """
    # Create a copy of params to avoid modifying the original
    api_params = params.copy()
    api_params.update(
        {
            "function": function_name,
            "apikey": get_api_key(),
            "source": "trading_agents",
        }
    )

    # Handle entitlement parameter if present in params or global variable
    current_entitlement = globals().get("_current_entitlement")
    entitlement = api_params.get("entitlement") or current_entitlement

    if entitlement:
        api_params["entitlement"] = entitlement
    elif "entitlement" in api_params:
        # Remove entitlement if it's None or empty
        api_params.pop("entitlement", None)

    response = requests.get(API_BASE_URL, params=api_params, timeout=REQUEST_TIMEOUT)
    # Classify an HTTP 429 before raise_for_status() turns it into a bare
    # requests.HTTPError outside the taxonomy: the notice check below reads
    # only HTTP 200 bodies, so a status-code throttle would otherwise fall
    # into each caller's broad except and never reach the router's rate-limit
    # lane (#72). Other 4xx/5xx keep their HTTPError behaviour.
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        after = f" (Retry-After: {retry_after})" if retry_after else ""
        raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: HTTP 429{after}")
    response.raise_for_status()

    response_text = response.text

    # Error responses are JSON; data responses are usually CSV (or data-keyed
    # JSON). A non-JSON body is normal data.
    try:
        response_json = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text

    # What a rejection is attributed to; see the ``subject`` docstring note.
    rejected_subject = subject or params.get("symbol") or params.get("tickers") or function_name

    # A JSON body that is not an object is no shape Alpha Vantage serves data
    # in (data is CSV text or a keyed JSON object), and the classification
    # below reads keys — so classify it as no-data rather than serving 'null'
    # or '[]' to the agent as a report body. Raising (not returning) keeps the
    # pre-#68 outcome of the router trying the next vendor, minus the crash:
    # calling .get on a list/scalar used to AttributeError into the router's
    # broad handler (or the indicator caller's prose lane).
    if not isinstance(response_json, dict):
        raise NoMarketDataError(
            rejected_subject,
            detail=(
                f"Alpha Vantage answered the {function_name} request with a "
                f"non-object JSON body ({type(response_json).__name__}); "
                f"refusing to serve it as data"
            ),
        )

    # Alpha Vantage reports problems via "Information" / "Note". Classify so a
    # genuine rate limit and an invalid/missing key aren't conflated (#991):
    # rate-limit phrasing is checked first because those notices also mention
    # "API key" ("your API key ... 25 requests per day"). A non-string notice
    # (no such shape has been observed) is unclassifiable and falls through.
    notice = response_json.get("Information") or response_json.get("Note")
    if isinstance(notice, str) and notice:
        low = notice.lower()
        if any(m in low for m in ("rate limit", "requests per day", "call frequency", "premium")):
            raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {notice}")
        if "api key" in low or "apikey" in low:
            # Reuse the existing "not configured" error so a bad key surfaces as
            # a real, actionable failure rather than a mislabeled rate limit (#991).
            raise AlphaVantageNotConfiguredError(
                f"Alpha Vantage API key invalid or missing: {notice}"
            )

    # See the Raises: section for why an "Error Message" body raises the
    # no-data type. Checked after the notice classification so a body carrying
    # both keys keeps its more actionable rate-limit/bad-key verdict. Keyed on
    # presence, not truthiness: a rejection with blank wording is still a
    # rejection, not data.
    if "Error Message" in response_json:
        error_message = response_json["Error Message"] or "(no reason given)"
        raise NoMarketDataError(
            rejected_subject,
            detail=f"Alpha Vantage rejected the {function_name} request: {error_message}",
        )

    return response_text


def _filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str, symbol: str) -> str:
    """
    Filter CSV data to include only rows within the specified date range.

    Args:
        csv_data: CSV string from Alpha Vantage API
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format
        symbol: Requested symbol; required so the classified error is always
            attributed to a real symbol (the router renders it to the agent)

    Returns:
        Filtered CSV string

    Raises:
        NoMarketDataError: When the date range or the CSV body cannot be
            parsed. This filter backs a core (non-optional) data path, so it
            must fail closed: the old fallback returned the UNFILTERED body,
            silently serving out-of-range/future rows to a backtest (#33).
    """
    if not csv_data or csv_data.strip() == "":
        return csv_data

    try:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        if pd.isna(start_dt) or pd.isna(end_dt):
            raise ValueError(f"unparseable date range {start_date}..{end_date}")
    except (TypeError, ValueError) as e:
        raise NoMarketDataError(
            symbol,
            detail=f"unusable date range for the CSV date filter: {e}",
        ) from e

    try:
        df = pd.read_csv(StringIO(csv_data))
        # The first column is the date column (timestamp) on the CSV shape
        # this filter's caller requests (TIME_SERIES_DAILY_ADJUSTED).
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])
    except Exception as e:
        raise NoMarketDataError(
            symbol,
            detail=f"unparseable CSV response; refusing to serve it unfiltered: {e}",
        ) from e

    filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]
    return filtered_df.to_csv(index=False)
