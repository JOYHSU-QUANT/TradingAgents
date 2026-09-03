"""Shared plumbing for the SoSoValue vendor modules.

SoSoValue's OpenAPI (https://sosovalue.gitbook.io/soso-value-api-doc) serves
several product modules off one base URL, one ``x-soso-api-key`` header, one
``{"code": 0, "data": [...]}`` response envelope, and one 20 req/min /
100k req/month plan limit. The vendor modules built on it — spot-ETF flows
(``sosovalue.py``), the macro economic calendar (``sosovalue_macro.py``), and
BTC corporate treasuries (``sosovalue_treasuries.py``) — share that plumbing
here: the error taxonomy, API-key retrieval and sanitization, the request
helper with its redaction discipline, the value/date/ticker trust-boundary
predicates (including the dated-history-row shape skeleton
``_valid_dated_rows``, macro and treasuries), the clock and cache-age helpers
every rolling-snapshot cache shares, the cache-file read preamble every
``_read_cache`` opens with (``_read_cache_preamble``, all three — plus the
Farside vendor, which adopts the read preamble, the rejecter factory, and
the parameterized STALE template while keeping its own fetch/cache
discipline), and the two
orchestration skeletons the modules used to carry as near-verbatim copies —
the cache/TTL/stale-fallback discipline (``load_rolling_snapshot``, all
three) and the per-item sweep with its 429 drain and
consecutive-network-failure breaker (``fetch_each``, macro and treasuries
only; the ETF fund loop keeps its older semantics). Anything
module-specific (parsers, snapshot shapes, cache payloads, TTL policy,
report rendering, the vendor-success threshold) stays in the vendor modules.

The one shared key means unsetting ``SOSOVALUE_API_KEY`` on a deployed box is
a single emergency-disable switch for every SoSoValue-backed category at once:
each module checks the key before consulting its cache, so the flip takes
effect on the very next call.
"""

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import NamedTuple, NoReturn

import requests

from .config import get_config
from .errors import VendorError, VendorNotConfiguredError, VendorRateLimitError
from .utils import sanitize_untrusted

logger = logging.getLogger(__name__)

SOSOVALUE_API_BASE = "https://openapi.sosovalue.com/openapi/v1"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# The plan limit every request on the shared key counts against: 20 per
# minute, per key. (The 100k-per-month tier is not budgeted here — a day of
# every module refreshing on every 4h paper cycle stays under 300.) The three
# vendor modules refresh 10 (macro), ~15 (ETF) and 16 (treasuries) requests
# at a time, so any two expiring inside one analyst turn overrun the minute
# and whichever runs second takes a 429 for the rest of it (#189). Each
# module's TTL only knows its own traffic; the budget below is the one thing
# that sees all of it, and it spaces requests so the overrun never happens.
# The budget is per PROCESS and spends the whole plan limit: do not run a
# second process against the same key while the daemon is up (a CLI run on
# the box, say) — both would count to 20, the server would 429 them, and
# each 429 parks the process that took it for a window.
RATE_LIMIT_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60.0
# Added to every wait: the server's window edge is not observable from here,
# and a request that leaves this process at second 60.0 by our clock can
# still land inside the server's previous window. Two seconds covers
# request-latency jitter without making a full wait feel like a timeout.
RATE_LIMIT_SLACK_SECONDS = 2.0

# Module attributes rather than bare ``time`` calls so the tests can step a
# fake clock instead of sleeping: the budget's whole behaviour is "wait
# until", which is untestable against wall time.
_monotonic = time.monotonic
_sleep = time.sleep


class _RequestBudget:
    """A process-wide sliding-window budget over the shared per-minute limit.

    Every request records its send instant; when ``RATE_LIMIT_REQUESTS``
    instants sit inside the trailing window the next request waits until
    the oldest of them ages out. A 429 is the server's proof that the window
    is full regardless of what this process counted (another process on the
    same key, a server window that does not match ours), so
    ``note_rate_limited`` parks the budget for one full window: the sweep
    the 429 landed in still drains (``fetch_each`` keeps that decision), but
    the NEXT module's sweep in the same analyst turn waits instead of
    burning its own quota on a second round of 429s. That park is
    ``throttle.ThrottleLatch`` in miniature rather than a reuse of it: the
    latch's TTL is fixed at five minutes, this one is exactly the window.

    Same altitude as the yfinance latch in ``stockstats_utils`` — the
    network boundary BEHIND the caches, so a cache-served call never touches
    the budget and a park cannot turn a sibling's stale-cache report into
    DATA_UNAVAILABLE (#114) — but the opposite verdict for an in-window
    call: yfinance fast-fails so the router can fall through, this WAITS,
    because a SoSoValue chain has no next vendor to fall through to
    (treasuries has none at all) and a minute of wall time buys the data a
    fast fail would forfeit.

    The wait happens inside the caller's tool call, holding the lock, so
    concurrent callers queue in order rather than racing the count. One wait
    is at most a window plus slack. How many waits one tool call can pay is
    the sweeps' business, not the budget's: every module's per-item loop
    drains the rest of its sweep on a 429 (``fetch_each``, and the ETF fund
    loop since #189), so a park is paid at most once per sweep — without
    that, a persistently rate-limited key would cost a full window per
    remaining item.
    """

    def __init__(self):
        self._sent: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def _wait_needed(self, now: float) -> float:
        while self._sent and self._sent[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            self._sent.popleft()
        wait = self._blocked_until - now
        if len(self._sent) >= RATE_LIMIT_REQUESTS:
            wait = max(wait, self._sent[0] + RATE_LIMIT_WINDOW_SECONDS - now)
        return wait + RATE_LIMIT_SLACK_SECONDS if wait > 0 else 0.0

    def acquire(self, path: str) -> None:
        """Block until a request may be sent."""
        with self._lock:
            now = _monotonic()
            if (wait := self._wait_needed(now)) > 0:
                reason = (
                    "the server answered 429, so the window is full whatever this process counted"
                    if self._blocked_until > now
                    else f"{len(self._sent)} requests already sent in the last "
                    f"{RATE_LIMIT_WINDOW_SECONDS:.0f}s on the shared key"
                )
                logger.info(
                    "SoSoValue request budget: %s; waiting %.1fs before %s", reason, wait, path
                )
                _sleep(wait)
                now = _monotonic()
            self._sent.append(now)

    def note_rate_limited(self) -> None:
        """Park the budget for a full window: the server said it is full.

        A ``Retry-After`` header is not consulted: whether the vendor sends
        one is not live-verified, so the window is the only number there is.
        """
        with self._lock:
            self._blocked_until = _monotonic() + RATE_LIMIT_WINDOW_SECONDS


_BUDGET = _RequestBudget()

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


def _sanitize(text: object, *, limit: int | None = None) -> str:
    """Flatten a fragment this vendor did not author so it cannot forge structure.

    The flattening is the shared ``utils.sanitize_untrusted``; what is specific
    to this family is WHERE it is applied. The vendor modules render into
    markdown tables that reach an LLM verbatim, so a value carrying "|" splits
    its cell into new columns — a macro event named "Widget Index | 9.9% |
    9.9%" lands fabricated figures in the Forecast and Previous positions of
    its own row. Neutralised where the fragment ENTERS the message rather than
    at the parse boundary, because a raised error reaches the prompt too
    (``route_to_vendor`` hands an optional category's failure to the model as
    ``DATA_UNAVAILABLE: ... ({error})``), while the stored and compared value
    must stay byte-exact — the macro history path sends event names back to
    the API, so flattening them at parse time would break the request path.
    ``limit`` is passed only where the fragment is ISOLATED (an echoed raw row
    in a raised message), never when flattening a whole exception message.
    """
    return sanitize_untrusted(text, limit=limit)


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
    """The 20 req/min / 100k req/month plan limit was hit (HTTP 429).

    Reaches the router only when the throttled call ALSO had no usable cache
    (see ``load_rolling_snapshot``): the sibling tools, each with its own cache
    file, answer the same throttle with a stale-cache report. So the raise is
    not a statement about this client's standing, and the router must not
    keep the vendor unasked over it — that would turn those reports into
    DATA_UNAVAILABLE for the rest of the window, a different verdict rather
    than a cheaper one (#114).
    """

    latches_vendor = False


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

    Every call passes through the shared ``_BUDGET`` first — after the key
    check, so an unset key still fails instantly rather than after a wait —
    and a 429 parks that budget for a window before it is raised.
    """
    api_key = get_api_key()
    _BUDGET.acquire(path)
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
        _BUDGET.note_rate_limited()
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


def _is_safe_text(x: object, max_len: int, *, min_len: int = 1, stripped: bool = True) -> bool:
    """True for a bounded, printable-ASCII string (the family's charset gate).

    The predicate behind the free-text fields the family gates this way
    (macro value strings and event names, treasuries company names; the ETF
    module's fund names predate it and keep their own weaker
    isinstance-and-truncate check — fine because that name is stored for
    diagnosability and never rendered): these strings render verbatim into
    LLM-visible report text, so anything
    unprintable or oversized is rejected at the trust boundary rather than
    escaped. The defaults sit at the strict end (non-empty, no surrounding
    whitespace) so a future call site that forgets the keywords fails closed
    rather than letting "" or padded strings into LLM-visible text; a caller
    for whom emptiness is a meaning, not a defect, opts down explicitly with
    ``min_len=0, stripped=False`` (a macro ``actual`` is "" until the print
    is released).
    """
    return (
        isinstance(x, str)
        and min_len <= len(x) <= max_len
        and (not stripped or x == x.strip())
        and all(32 <= ord(c) <= 126 for c in x)
    )


def _is_non_negative_int(x: object) -> bool:
    """True only for a real int >= 0 (bool is an int subclass and must not pass).

    The plain non-negative count fields the cache payloads carry (fund /
    calendar / company tallies) share this check — except treasuries'
    ``companies_total``, whose bound is relational (no smaller than the
    selection, which subsumes non-negativity) and stays with its validator.
    Hand-written, the bool exclusion is the clause a new field's author
    forgets, letting a JSON ``true`` validate as 1.
    """
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def _valid_dated_rows(rows: object, *, max_rows: int, row_ok: Callable[[dict], bool]) -> bool:
    """The family's dated-history-row shape check; ``row_ok`` holds the fields.

    The skeleton the macro and treasuries cache validators used to carry as
    near-verbatim copies: a non-empty list within the module's read-side row
    cap (a file written before the cap existed, or by hand, must cost one
    refetch rather than be re-validated in full on every read forever), each
    row a dict with a canonical ISO ``date``, and the dates non-descending —
    NOT strictly ascending: duplicate dates are legal (two prints or
    disclosures released on one day) and preserved by the parsers. The ETF
    module's ``summary_rows`` are deliberately NOT on this skeleton: its
    render sums rows and reads ``visible[-1]`` as the latest day, so its
    validator demands strictly-ascending dates — folding it in here would
    re-admit the duplicate-date double-count that check exists to reject.
    Only the per-field rules differ between the callers, so they arrive as
    ``row_ok``, which runs after the dict and date checks — it may assume a
    dict carrying a valid ``date`` and nothing more: probe every other field
    with ``.get()`` or a presence check, or a cache file missing that field
    would raise a raw KeyError here instead of costing one refetch.
    """
    if not (isinstance(rows, list) and rows and len(rows) <= max_rows):
        return False
    if not all(isinstance(r, dict) and _is_iso_date(r.get("date")) and row_ok(r) for r in rows):
        return False
    dates = [r["date"] for r in rows]
    return all(a <= b for a, b in zip(dates, dates[1:], strict=False))


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


def _days_unobserved(fetched_at: str, curr_dt: datetime) -> int | None:
    """Days of a report's window that postdate the snapshot, or None.

    Not ``_days_stale``: that measures the snapshot against wall-clock now for
    the TTL and the stale cap, while this measures it against the report's
    ``curr_date``, which on a backtest can sit far from now (and before the
    fetch, giving a non-positive result callers are expected to ignore). It is
    what a stale serve's trailing window cannot have seen — no snapshot carries
    what the provider published after it was fetched — so both vendor modules
    can say so instead of leaving the reader to subtract two printed dates.
    """
    fetched_day = fetched_at[:10]
    if not _is_iso_date(fetched_day):
        return None
    return (curr_dt - datetime.strptime(fetched_day, "%Y-%m-%d")).days


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


def _stale_caveat(
    fetched_at: str,
    closing: str,
    *,
    causes: str = "network error, rate limit, or an API contract break",
    humanize: Callable[[str], str] | None = None,
) -> str:
    """The family's STALE header line: age, cause set, provenance, ``closing``.

    One template so the vendors cannot drift on the sentence shape. The
    default ``causes`` enumeration is what ``load_rolling_snapshot``'s
    fallback absorbs for the SoSoValue modules; a vendor whose failure modes
    differ (Farside is a keyless scraper — no rate limit, no API contract)
    passes its own set, and each module supplies the closing warning its own
    reader needs (what, concretely, may be outdated). ``humanize`` exists for
    the same reason: the displayed age must read the SAME clock the vendor's
    TTL/staleness decisions read (tests patch each vendor's ``_utc_now``), so
    a vendor with its own clock passes its own age formatter instead of
    silently getting this module's.
    """
    age = (humanize or _humanize_age)(fetched_at)
    return (
        f"_STALE by {age}: live refresh failed ({causes}); "
        f"showing the last cached snapshot "
        f"(fetched {fetched_at}). {closing}_"
    )


def _cache_dir() -> str:
    cache_dir = get_config()["data_cache_dir"]
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _cache_rejecter(path: str, *, cache_name: str, log: logging.Logger) -> Callable[[str], None]:
    """Build a module's cache-rejection logger: warn with its cache name, return None.

    The one owner of the rejection log format — each module used to carry its
    own near-verbatim closure (identical but for its hardcoded cache display
    name), so a wording change (or a new diagnostic field) meant three edits,
    and a missed one left operators grepping for a format
    only some caches emit. ``cache_name`` is the module's display name for
    this cache — for the SoSoValue modules, the same name they pass to
    ``load_rolling_snapshot``, so the read and write sides of one cache file
    name it identically; ``log`` keeps per-module attribution.
    """

    def reject(reason: str) -> None:
        log.warning("Ignoring %s %s: %s", cache_name, path, reason)
        return None

    return reject


def _read_cache_preamble(path: str, *, reject: Callable[[str], None]) -> dict | None:
    """Open/parse ``path`` and hand back the payload dict, or None if unusable.

    The preamble every ``_read_cache`` validator (the family's three, and the
    Farside vendor's) opens with, before its module-specific field validation: a missing file is an
    ordinary miss (no cache yet, nothing to log), an unreadable or non-JSON
    file is rejected through the module's ``reject`` (see ``_cache_rejecter``),
    and a top-level value that is not a JSON object is rejected the same way —
    every later field check subscripts a dict. Both rejection paths return
    None structurally rather than trusting ``reject``'s return value, so a
    rejected file can never come back as a payload.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        reject(f"unreadable ({e})")
        return None
    if not isinstance(payload, dict):
        reject(f"top-level JSON is a {type(payload).__name__}, expected an object")
        return None
    return payload


def _concentration_share_str(share: float) -> str:
    """Render a concentration share where "100" may only mean "the whole".

    Any rounding format breaks that on its own boundary — ``.0f`` fails from
    99.5 and ``.1f`` fails again from 99.95 — so the near-100 band is
    truncated toward zero instead, which can never round up into the claim
    that one contributor is the entire multi-contributor total. (The
    treasuries module's decided fix, shared so the ETF breadth shares get
    the same guarantee.)
    """
    if share >= 100:
        return "100%"
    if round(share) >= 100:
        return f"{math.floor(share * 10) / 10:.1f}%"
    return f"{share:.0f}%"


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


class FetchSweep(NamedTuple):
    """What one ``fetch_each`` sweep produced, and why it stopped short.

    The classification fields exist because the failures the sweep owns are
    absorbed per-item (a rejected key still propagates out untouched), so
    only the sweep itself can say what an all-failed outcome was — the
    caller's vendor-success check reads them to raise the honest type
    instead of a bare structural error:

    * ``rate_limited`` — the 429 that drained the sweep, or None. Persisted
      by callers (not just logged) because the drain fills ``failed`` with
      items this client never asked for, and by render time the local that
      knew why is long gone.
    * ``last_network`` — the last transport failure, kept because without it
      a pure outage could only surface as structural breakage.
    * ``structural_failure`` — True when ``fetch_one`` returned None (it
      swallowed a parse/contract break), so the transport classification
      cannot claim a pure outage while a real structural break is in the
      same sweep.
    * ``breaker_skipped`` — True only when the breaker left items
      unattempted (it also trips on the last item, where nothing went
      unattempted and there is nothing to disclose); the report must not let
      the proven failures stand in for the skipped ones.
    * ``attempted`` — only the requests this sweep actually made, so an
      all-failed message cannot turn a breaker-length streak of observed
      failures into a whole-list claim.
    """

    results: dict
    failed: list[str]
    flagged: list[str]
    rate_limited: SoSoValueRateLimitError | None
    last_network: requests.RequestException | None
    structural_failure: bool
    breaker_skipped: bool
    attempted: int

    def persisted_flags(self) -> dict:
        """The sweep-outcome flags every cache payload persists, not just logs.

        Persisted because the drain and the breaker both fill ``failed`` with
        items this client never asked for, and by the time the report is
        rendered the local that knew why is long gone. One method so a flag
        added here reaches every module's payload without a hand-edit per
        module — on the write side only: each module's cache-read validator
        and report renderer name these flags individually and need their own
        edits before a new flag is consumed anywhere (and the read validators
        reject missing keys, not unknown ones), so adding a flag here is the
        start of a per-module change, not the whole of it.
        """
        return {
            "rate_limited": self.rate_limited is not None,
            "breaker_skipped": self.breaker_skipped,
        }


def fetch_each(
    items: Iterable,
    fetch_one: Callable,
    *,
    key: Callable[[object], str],
    describe: Callable[[object], str],
    label: str,
    failed_bucket: str,
    noun: str,
    max_consecutive_network: int,
    log: logging.Logger,
) -> FetchSweep:
    """The family's per-item sweep: 429 drain, network breaker, three buckets.

    ``fetch_one(item)`` follows the family's per-item contract: it returns
    parsed data on success, a string sentinel for an item the provider
    answered about but served nothing for (``flagged``; a code edit or the
    provider, not a retry, resolves those), or ``None`` after swallowing a
    structural break (``failed``). Any string return lands in ``flagged`` —
    each module has exactly one such sentinel, so a second sentinel with a
    different meaning must not be introduced without splitting the bucket.
    A rejected key propagates out of the sweep untouched (config breakage
    must reach the router even mid-batch), and so do the two flow-control
    failures this loop owns:

    * A 429 proves every further request in this sweep would 429 too (the
      20 req/min limit is per-key and per-minute), so the rest is drained
      into ``failed`` (short-TTL retry) instead of burning a quota call per
      remaining item.
    * ``max_consecutive_network`` transport failures trip the breaker: a
      network that hangs instead of failing fast would otherwise turn one
      refresh into sequential full timeouts inside a single analyst tool
      call, and the post-breaker outcome (disclosed-incomplete, short TTL)
      is identical to riding the brownout out. Any completed request resets
      the streak — the server answered, so each remaining item is still
      worth its own try.

    ``label``/``failed_bucket``/``noun`` only shape the two log lines;
    ``key`` names an item in the buckets and ``describe`` renders it in logs
    (the macro module logs reprs, the treasuries module bare tickers).
    ``log`` is the calling module's logger: log attribution stays per-module
    so operators (and the tests) can keep filtering by the vendor's name.

    The ETF module's fund loop does NOT use this helper — it predates it and
    keeps its own breaker loop — but since #189 it drains on a 429 the same
    way (the PR #19 per-fund retry would cost a budget window per remaining
    fund on a throttled key); folding it in is now a refactor, not a
    behaviour change.
    """
    if isinstance(items, str):
        # A bare string is iterable too — it would silently sweep one HTTP
        # request per character instead of failing loudly.
        raise TypeError("fetch_each items must be a collection of items, not a bare string")
    results: dict = {}
    failed: list[str] = []
    flagged: list[str] = []
    rate_limited: SoSoValueRateLimitError | None = None
    last_network: requests.RequestException | None = None
    structural_failure = False
    consecutive_network = 0
    breaker_skipped = False
    attempted = 0
    remaining = iter(items)
    for item in remaining:
        attempted += 1
        try:
            result = fetch_one(item)
        except SoSoValueRateLimitError as e:
            rate_limited = e
            skipped = [key(item), *(key(i) for i in remaining)]
            failed.extend(skipped)
            log.warning(
                "SoSoValue %s: rate limit hit on %s (%s); that request and "
                "the %d %s not yet attempted all go to %s "
                "(disclosed as incomplete, retried on the short TTL)",
                label,
                describe(item),
                e,
                len(skipped) - 1,
                noun,
                failed_bucket,
            )
            break
        except requests.RequestException as e:
            last_network = e
            failed.append(key(item))
            consecutive_network += 1
            if consecutive_network >= max_consecutive_network:
                skipped = [key(i) for i in remaining]
                if skipped:
                    failed.extend(skipped)
                    breaker_skipped = True
                    log.warning(
                        "SoSoValue %s: %d consecutive network failures; "
                        "skipping the remaining %d %s "
                        "(disclosed as incomplete, retried on the short TTL)",
                        label,
                        consecutive_network,
                        len(skipped),
                        noun,
                    )
                break
            continue
        consecutive_network = 0
        if isinstance(result, str):
            flagged.append(key(item))
        elif result is None:
            structural_failure = True
            failed.append(key(item))
        else:
            results[key(item)] = result
    return FetchSweep(
        results=results,
        failed=failed,
        flagged=flagged,
        rate_limited=rate_limited,
        last_network=last_network,
        structural_failure=structural_failure,
        breaker_skipped=breaker_skipped,
        attempted=attempted,
    )


def raise_all_failed(
    sweep: FetchSweep,
    *,
    on_rate_limited: Callable[[], str],
    on_transport: Callable[[], str],
    on_structural: Callable[[], str],
) -> NoReturn:
    """Raise the honest type for a sweep that produced no results at all.

    The judgment lives here — once, next to the sweep that produced the
    evidence — because the router and ``load_rolling_snapshot`` classify by
    type, and each misclassification tells a lie with a distinct cost:

    * A 429 that drained the whole sweep must not masquerade as structural
      breakage (ERROR + traceback logs for a routine quota trip).
    * A sweep that died PURELY of transport must raise the transport class,
      routing it to the warning branch where network failures already land —
      not a bare ``SoSoValueError``, which reads as "the client likely needs
      a fix" for an outage no code change can heal. "Purely" is the gate: a
      flagged item (the provider answered and served nothing) or a swallowed
      parse break means something structural is in the mix, and the generic
      error stays the honest answer.

    The callables supply each module's message — worded over its own universe
    and counts — so the shared predicate cannot drift between modules while
    the prose stays theirs.
    """
    if sweep.rate_limited is not None:
        raise SoSoValueRateLimitError(on_rate_limited()) from sweep.rate_limited
    if sweep.last_network is not None and not sweep.structural_failure and not sweep.flagged:
        raise requests.RequestException(on_transport()) from sweep.last_network
    raise SoSoValueError(on_structural())


def load_rolling_snapshot(
    *,
    path: str,
    read_cache: Callable[[str], dict | None],
    fetch_all: Callable[[dict | None], dict],
    ttl_hours: Callable[[dict], float],
    label: str,
    cache_name: str,
    max_stale_days: int,
    log: logging.Logger,
) -> tuple[dict, str, bool, bool]:
    """The family's cache/TTL/stale discipline; returns (payload, fetched_at, stale, refetched).

    ``refetched`` is True only when THIS call ran ``fetch_all``: both cache
    serves — the TTL-fresh hit and the stale fallback — replay a payload
    computed at some earlier real fetch, so anything derived-at-fetch-time it
    carries (the ETF module's refresh-diff disclosures) describes that fetch,
    not this call, and ``stale`` alone cannot say so (it is only the subset
    of cache serves whose refresh attempt failed).

    ``log`` is the calling module's logger — the stale-serve warnings and
    errors keep their per-module attribution, so operators (and the tests)
    can keep filtering by the vendor's name.

    Key first: an unset key must raise even when a fresh cache could serve
    the call, or the emergency-disable flip (unset the key on the server)
    would be delayed by up to the cache TTL. Then the Farside pattern: a
    cache younger than its TTL is served as-is — the TTL is the module's
    policy call (``ttl_hours(cached)``), where each degradation bucket's
    shortened-or-not argument lives — otherwise ``fetch_all(cached)`` runs
    and overwrites the rolling file; on a fetch failure the call falls back
    to the cached snapshot (``stale=True``) up to ``max_stale_days``. A
    failed fetch is never written to cache. ``SoSoValueNotConfiguredError``
    (unset or rejected key) is deliberately NOT absorbed by the stale
    fallback: config breakage should surface to the router immediately, not
    be papered over for two weeks.

    A vendor-taxonomy error keeps its exact type through the context-adding
    wraps: the router classifies by type (a rate limit is a routine quiet
    fall-through; a plain ``SoSoValueError`` is unexpected breakage, logged
    with a traceback and surfaced as the chain's first error), so the wrap
    must add context without re-classifying. Only a network error
    (``requests.RequestException``) becomes the generic ``SoSoValueError``.
    """
    get_api_key()

    cached = read_cache(path)
    if cached:
        age_h = _cache_age_hours(cached["fetched_at"])
        # 0 <= age guards against a future-dated stamp being treated as fresh.
        if age_h is not None and 0 <= age_h < ttl_hours(cached):
            return cached, cached["fetched_at"], False, False

    try:
        payload = fetch_all(cached)
    except SoSoValueNotConfiguredError:
        raise
    except (requests.RequestException, VendorError) as e:
        wrap_cls = type(e) if isinstance(e, VendorError) else SoSoValueError
        if cached:
            fetched_at = cached["fetched_at"]
            age = _days_stale(fetched_at)
            # Unknown age (unparseable or future-dated stamp) is treated as
            # beyond the cap — the case where an unbounded-age serve is most
            # likely — rather than served with a nonsense age caveat.
            if age is None or age > max_stale_days:
                stale_desc = (
                    "has an unparseable or future-dated fetch date"
                    if age is None
                    else f"is {age} days stale"
                )
                raise wrap_cls(
                    f"SoSoValue {label} fetch failed and the newest cache {stale_desc} "
                    f"(> {max_stale_days}-day cap): {_sanitize(e)}"
                ) from e
            # A SoSoValueError here is a contract/parse break (a code fix is
            # likely needed) and must not hide among network-blip warnings for
            # up to the stale cap; an outage or rate-limit stays a warning.
            age_str = _humanize_age(fetched_at)
            if isinstance(e, SoSoValueError):
                log.error(
                    "SoSoValue %s refresh failed structurally (%s); serving stale "
                    "cache (%s old) — the client likely needs a fix",
                    label,
                    e,
                    age_str,
                    exc_info=True,
                )
            else:
                log.warning(
                    "SoSoValue %s refresh failed (%s); using stale cache (%s old)",
                    label,
                    e,
                    age_str,
                )
            return cached, fetched_at, True, False
        # "usable": the file may exist but have failed read-side validation.
        # Not capped, only flattened: most of this string is the module's own
        # diagnostic, and a foreign requests.RequestException can carry a
        # server-influenced URL into the same LLM-visible line.
        raise wrap_cls(
            f"SoSoValue {label} unavailable and no usable cache exists: {_sanitize(e)}"
        ) from e

    fetched_at = _iso_now()
    payload["fetched_at"] = fetched_at
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        log.warning(
            "Could not write %s %s: %s — the fetch throttle stays disabled "
            "until a write succeeds, so further calls will each re-fetch",
            cache_name,
            path,
            e,
        )
    return payload, fetched_at, False, True
