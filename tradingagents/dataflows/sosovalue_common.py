"""Shared plumbing for the SoSoValue vendor modules.

SoSoValue's OpenAPI (https://sosovalue.gitbook.io/soso-value-api-doc) serves
several product modules off one base URL, one ``x-soso-api-key`` header, one
``{"code": 0, "data": [...]}`` response envelope, and one 20 req/min /
100k req/month plan limit. The vendor modules built on it — spot-ETF flows
(``sosovalue.py``), the macro economic calendar (``sosovalue_macro.py``), and
BTC corporate treasuries (``sosovalue_treasuries.py``) — share that plumbing
here: the error taxonomy, API-key retrieval and sanitization, the request
helper with its redaction discipline, the value/date/ticker trust-boundary
predicates, and the clock and cache-age helpers every rolling-snapshot cache
shares. Anything module-specific (parsers, snapshot shapes, cache payloads,
report rendering) stays in the vendor modules.

The one shared key means unsetting ``SOSOVALUE_API_KEY`` on a deployed box is
a single emergency-disable switch for every SoSoValue-backed category at once:
each module checks the key before consulting its cache, so the flip takes
effect on the very next call.
"""

import logging
import math
import os
import re
from datetime import datetime, timezone

import requests

from .config import get_config
from .errors import VendorError, VendorNotConfiguredError, VendorRateLimitError

logger = logging.getLogger(__name__)

SOSOVALUE_API_BASE = "https://openapi.sosovalue.com/openapi/v1"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# A plausible exchange ticker from a SoSoValue listing endpoint (US ETF
# tickers like "IBIT", but also the treasuries listing's international forms
# like "0434.HK", "ADE.DE", or Tokyo's numeric "3350"). Tickers are
# interpolated into a URL path, so anything not matching is dropped — and
# disclosed via an unusable-entry count — rather than requested. The first
# character must be alphanumeric: URL-quoting leaves "." unescaped (it sits
# in urllib's always-safe set regardless of ``safe=``), so a dot-led token
# like ".." would otherwise survive quoting as a path-traversal-shaped
# segment. Anchored with ``\Z``, not ``$``: ``$`` also matches before a
# trailing newline, so "IBIT\n" would pass and land its raw newline mid-line
# in the report text.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}\Z")

# Markdown control characters that let a server-controlled fragment forge
# report structure. The vendor modules render into markdown tables that reach
# an LLM verbatim, so a value carrying "|" splits its cell into new columns —
# a macro event named "Widget Index | 9.9% | 9.9%" lands fabricated figures in
# the Forecast and Previous positions of its own row — and "#"/"*"/"`" open
# headings, emphasis and code spans mid-report. Same remedy and rationale as
# the Deribit vendor: neutralize where the fragment ENTERS the message rather
# than at the parse boundary, because a raised error reaches the prompt too
# (``route_to_vendor`` hands an optional category's failure to the model as
# ``DATA_UNAVAILABLE: ... ({error})``), while the stored and compared value
# must stay byte-exact — the macro history path sends event names back to the
# API, so flattening them at parse time would break the request path.
#
# Translated to a SPACE, not deleted: deletion joins the fragments either side
# and can fuse two tokens into a third that reads as legitimate.
_MARKDOWN_CONTROL = str.maketrans(dict.fromkeys("#*`|", " "))

# "_" is handled separately because it also occurs inside ordinary words (an
# event name or a company name may legitimately carry one). Only underscores
# in EMPHASIS position — at a word boundary, where the reports' own
# "_caveat._" lines sit — are removed; one between two alphanumerics stays.
_EMPHASIS_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")


def _sanitize(text: object, *, limit: int | None = None) -> str:
    """Flatten a fragment this vendor did not author so it cannot forge structure.

    The strip runs FIRST and whitespace is collapsed after it, so neither the
    spaces the translation introduces nor the ones already in the fragment can
    survive as a run or rebuild a line break inside a table cell. ``limit``
    caps the result and is passed only where the fragment is ISOLATED (an
    echoed raw row in a raised message), never when flattening a whole
    exception message — most of that string is the module's own diagnostic,
    and capping there would truncate the sentence that carries the meaning.
    """
    stripped = _EMPHASIS_UNDERSCORE.sub("", str(text).translate(_MARKDOWN_CONTROL))
    stripped = " ".join(stripped.split())
    if limit is not None and len(stripped) > limit:
        stripped = stripped[:limit].rstrip() + "..."
    return stripped


class SoSoValueError(VendorError):
    """SoSoValue was unreachable, returned an error, or its response shape changed.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing
    layer reacts by behaviour rather than by vendor and an optional
    SoSoValue-backed category degrades down the chain instead of aborting.
    """


class SoSoValueNotConfiguredError(VendorNotConfiguredError):
    """Raised when SoSoValue is selected but no usable API key is configured.

    Covers an unset ``SOSOVALUE_API_KEY``, a set key that fails the
    header-safety check in ``get_api_key``, and a key the server rejects
    (HTTP 401): all are configuration breakage, not an outage, so none is
    papered over with a stale cache — the router falls to the next vendor at
    once, which is what makes unsetting the key an emergency-disable switch.
    """


class SoSoValueRateLimitError(VendorRateLimitError):
    """The 20 req/min / 100k req/month plan limit was hit (HTTP 429)."""


def get_api_key() -> str:
    """Retrieve and sanitize the SoSoValue API key from the environment.

    Surrounding whitespace is stripped rather than rejected: a key deployed
    through a Windows env file routinely gains a trailing CRLF, and failing
    the deploy over line endings helps nobody. What remains must be printable
    ASCII with no embedded whitespace — anything else can never be a valid
    key, and letting it reach the request header makes ``requests`` raise a
    pre-network error whose message embeds the full key verbatim (leaking it
    into logs and the LLM-visible DATA_UNAVAILABLE text) on a path classified
    as a network outage, which would stale-serve config breakage for up to
    the stale cap. The rejection message deliberately never echoes the key.
    """
    api_key = (os.getenv("SOSOVALUE_API_KEY") or "").strip()
    if not api_key:
        raise SoSoValueNotConfiguredError(
            "SOSOVALUE_API_KEY environment variable is not set. Get a free Demo "
            "key from the sosovalue.com developer dashboard and set "
            "SOSOVALUE_API_KEY; without it a SoSoValue-backed category falls "
            "through to the next vendor in its chain."
        )
    if not all(33 <= ord(c) <= 126 for c in api_key):
        raise SoSoValueNotConfiguredError(
            "SOSOVALUE_API_KEY contains characters that cannot travel in an "
            "HTTP header (embedded whitespace, control bytes, or non-ASCII "
            "text; the key itself is not echoed here). Re-set it with the raw "
            "key from the sosovalue.com developer dashboard."
        )
    return api_key


def _error_message(body: object, api_key: str) -> str:
    """Best-effort human message from an error body, with the key redacted.

    SoSoValue has two live-verified error shapes: structured
    ``{"code": 400xxx, "message": ...}`` and a gateway shape
    ``{"code": 1, "msg": ...}`` — note ``msg``, not ``message``. A body that
    failed JSON parsing arrives here as ``None`` and renders a fixed
    placeholder instead of the literal string "None". The body is
    server-controlled text that ends up in raised messages (and from there in
    logs and the router's LLM-visible DATA_UNAVAILABLE string), and an error
    body — a 401 especially — is exactly where a server may echo the
    submitted credential back, so the key is scrubbed before the text leaves
    this module.
    """
    text = "(non-JSON response body)" if body is None else body
    if isinstance(body, dict):
        text = body.get("message") or body.get("msg") or body
    # Redact BEFORE truncating (cutting first could slice the key across the
    # boundary and leave its head visible), and truncate always — a structured
    # message is exactly as server-controlled as the raw-body fallback. The
    # flattening matters as much as the truncation: this text rides a raised
    # message into the router's LLM-visible ``DATA_UNAVAILABLE: ... ({error})``
    # string, where a "|" or "##" could forge report structure.
    return _sanitize(str(text).replace(api_key, "[redacted]"), limit=300)


def _request(path: str, params: dict) -> list:
    """GET a SoSoValue endpoint and return its ``data`` list.

    Raises the vendor taxonomy: 401 -> not-configured (bad key is config
    breakage, not an outage), 429 -> rate-limited, anything else that is not a
    clean ``{"code": 0, "data": [...]}`` -> ``SoSoValueError``. Network errors
    propagate as ``requests.RequestException`` for the caller's stale-cache
    handling, mirroring the Farside vendor.
    """
    api_key = get_api_key()
    response = requests.get(
        f"{SOSOVALUE_API_BASE}{path}",
        params=params,
        headers={"x-soso-api-key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code == 401:
        raise SoSoValueNotConfiguredError(
            f"SoSoValue rejected the API key (HTTP 401): {_error_message(body, api_key)} "
            f"Verify SOSOVALUE_API_KEY; until then the chain falls to the next vendor."
        )
    if response.status_code == 429:
        raise SoSoValueRateLimitError(
            f"SoSoValue rate limit hit (HTTP 429) on {path}: {_error_message(body, api_key)}"
        )
    # Envelope errors can ride on any HTTP status (a missing-param error is
    # HTTP 400 with code 1; the over-window error is HTTP 403 with code
    # 400301), so judge the body, not just the status.
    if response.status_code != 200 or not isinstance(body, dict) or body.get("code") != 0:
        # `code` is only known to be != 0 — an arbitrary JSON value, not
        # necessarily a small int — so it gets the same redact-then-FLATTEN-
        # then-truncate treatment as the message, at a display-width cap. It
        # sits in the same sentence as the message and travels the same way
        # (raised -> logs -> the router's LLM-visible DATA_UNAVAILABLE text),
        # so sanitizing one and slicing the other would leave the pair only
        # half closed: 40 characters is ample for "## Reading: | 9.9 |".
        code = body.get("code") if isinstance(body, dict) else "no-json"
        raise SoSoValueError(
            f"SoSoValue request to {path} failed (HTTP {response.status_code}, "
            f"code {_sanitize(str(code).replace(api_key, '[redacted]'), limit=40)}): "
            f"{_error_message(body, api_key)}"
        )
    data = body.get("data")
    if not isinstance(data, list):
        raise SoSoValueError(
            f"SoSoValue response for {path} has no 'data' list "
            f"(got {type(data).__name__}); the API contract may have changed"
        )
    return data


def _is_finite_number(x: object) -> bool:
    """True only for a real, finite number (not a bool, NaN, or Infinity).

    bool is an int subclass, so a JSON ``true`` must not pass as a figure;
    NaN/Infinity would poison downstream sums and render as literal
    "nan"/"inf". Same boundary rule as the Farside vendor. An int too large
    to convert to float makes ``math.isfinite`` raise OverflowError — such a
    value can never be a usable figure, and letting the exception escape
    would crash a cache read or parse outside the vendor taxonomy.
    """
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _is_iso_date(x: object) -> bool:
    """True only for a canonical zero-padded ``YYYY-MM-DD`` date string.

    A non-canonical date would silently mis-order the lexical
    ``date <= curr_date`` lookahead filter, so it is rejected wherever a date
    crosses a trust boundary (API response or cache file).
    """
    if not isinstance(x, str):
        return False
    try:
        return datetime.strptime(x, "%Y-%m-%d").strftime("%Y-%m-%d") == x
    except ValueError:
        return False


def _is_valid_ticker(x: object) -> bool:
    """True only for a string passing the plausible-ticker filter.

    The one ticker predicate every SoSoValue listing consumer shares — each
    module applies it both live-parse-side and cache-read-side, so the two
    trust boundaries cannot drift apart on what may be interpolated into a
    URL path or report text.
    """
    return isinstance(x, str) and bool(_TICKER_RE.match(x))


def _utc_now() -> datetime:
    """The single UTC clock source (tests patch this one function)."""
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    """Current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` for the cache fetched_at."""
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_age_hours(fetched_at: str) -> float | None:
    """Hours since ``fetched_at``, or None if the stamp cannot be parsed.

    The one parse and one clock every freshness decision shares (TTL, stale
    cap, displayed age), so a stamp is readable by all of them or by none.
    """
    if not fetched_at:
        return None
    try:
        fetched = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (_utc_now() - fetched).total_seconds() / 3600.0


def _days_stale(fetched_at: str) -> int | None:
    """Whole elapsed days since fetched_at; None for unparseable or future stamps.

    A future-dated stamp (clock skew / tampered file) must read as unknown so
    the stale cap refuses it rather than serving it forever.
    """
    hours = _cache_age_hours(fetched_at)
    if hours is None or hours < 0:
        return None
    return int(hours // 24)


def _humanize_age(fetched_at: str) -> str:
    """Human-readable snapshot age for the STALE caveat.

    Hour-granular under a day (an hourly TTL can stale-serve within one UTC
    day, where "0 days" would read as nearly current), day-granular beyond.
    """
    hours = _cache_age_hours(fetched_at)
    if hours is None or hours < 0:
        return "an unknown age"
    if hours < 24:
        return f"{hours:.1f} hours"
    days = int(hours // 24)
    return f"{days} {_plural_days(days)}"


def _cache_dir() -> str:
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _plural_days(count: int) -> str:
    return _plural(count, "day", "days")


def _coverage_gap_note(missing: set[str] | list[str], did_what: str, otherwise: str) -> str:
    """A sentence naming what an empty section might really be, or "".

    An empty table reads as "nothing happened" unless the report says the
    snapshot was short of the things that would have filled it. The union
    guard and the shared clause live here so a module that grows another
    failure bucket cannot fix the wording in one empty branch and forget the
    next: each caller passes every bucket that contributed nothing, plus its
    own two context phrases.
    """
    names = sorted(missing)
    if not names:
        return ""
    return (
        f"Coverage is incomplete in this snapshot ({', '.join(names)} {did_what}), "
        f"so the window may be empty because of that gap rather than because {otherwise}."
    )
