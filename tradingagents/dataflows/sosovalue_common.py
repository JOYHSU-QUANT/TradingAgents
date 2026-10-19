"""Shared plumbing for the SoSoValue vendor modules.

SoSoValue's OpenAPI (https://sosovalue.gitbook.io/soso-value-api-doc) serves
several product modules off one base URL, one ``x-soso-api-key`` header, one
``{"code": 0, "data": [...]}`` response envelope, and one 10 req/min /
100k req/month plan limit (the plan page says 20; the server was measured
at 10, #215). The vendor modules built on it — spot-ETF flows
(``sosovalue.py``), the macro economic calendar (``sosovalue_macro.py``), and
BTC corporate treasuries (``sosovalue_treasuries.py``) — share that plumbing
here: the error taxonomy and vendor name, API-key retrieval, the listing
ticker predicate (``is_valid_ticker``, ETF and treasuries only), the request
helper with its redaction discipline and the shared request budget, and the
per-item sweep with its 429 drain and consecutive-network-failure breaker
(``fetch_each``/``raise_all_failed``, macro and treasuries only; the ETF fund
loop keeps its older semantics), which catches and raises this family's
types and so stays beside them.

The vendor-neutral kernel these modules also use (validators, clock,
cache preamble, formatters, ``load_rolling_snapshot``) lives in
``vendor_common``. Anything module-specific
(parsers, snapshot shapes, cache payloads, TTL policy, report rendering, the
vendor-success threshold) stays in the vendor modules.

The one shared key means unsetting ``SOSOVALUE_API_KEY`` on a deployed box is
a single emergency-disable switch for every SoSoValue-backed category at once:
each module checks the key before consulting its cache, so the flip takes
effect on the very next call.
"""

import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import NamedTuple, NoReturn, TypeGuard

import requests

from .errors import (
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)
from .utils import (
    failure_account,
    generic_failure_words,
    is_unreached,
    json_body_or_outage,
    raise_for_http_status,
    sanitize_untrusted as _sanitize,
)

# ``_sanitize`` is ``utils.sanitize_untrusted``, which this module and the
# family's vendor modules each import under that name: it flattens a fragment
# the vendor did not author so it cannot forge structure. What is specific to
# this family is WHERE it is applied. The vendor modules render into
# markdown tables that reach an LLM verbatim, so a value carrying "|" splits
# its cell into new columns — a macro event named "Widget Index | 9.9% |
# 9.9%" lands fabricated figures in the Forecast and Previous positions of
# its own row. Neutralised where the fragment ENTERS the message rather than
# at the parse boundary, because a raised error reaches the prompt too
# (``route_to_vendor`` hands an optional category's failure to the model as
# ``DATA_UNAVAILABLE: ... ({error})``), while the stored and compared value
# must stay byte-exact — the macro history path sends event names back to
# the API, so flattening them at parse time would break the request path.
# ``limit`` is passed only where the fragment is ISOLATED (an echoed raw row
# in a raised message), never when flattening a whole exception message here
# — the router caps a whole raised message where it enters a sentinel the
# model reads (see ``utils.sanitize_untrusted``), and the log keeps it whole.

logger = logging.getLogger(__name__)

SOSOVALUE_API_BASE = "https://openapi.sosovalue.com/openapi/v1"

# The family's name in every message, log line and status classification.
VENDOR = "SoSoValue"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# The limit every request on the shared key counts against: 10 per minute,
# per key — MEASURED, not the plan page's 20. On 2026-09-05, from the paper
# box in an idle gap, ten back-to-back GETs answered 200 and the eleventh
# 429 (body code 402901); refused requests did not extend the window, which
# cleared about 60s after the first request; no answer carried a rate-limit
# or Retry-After header. With the page's number in here every ETF and
# treasuries sweep in production ended in a 429 at its 11th-12th request
# and no cache ever completed (#215). (The 100k-per-month tier is not
# budgeted here — a day of every module refreshing on every 4h paper cycle
# stays under 300.) The three vendor modules refresh 10 (macro), ~15 (ETF)
# and 16 (treasuries) requests at a time, so a sweep past its tenth request
# waits out the window once inside the caller's tool call, and any two
# expiring inside one analyst turn wait rather than overrun it (#189). Each
# module's TTL only knows its own traffic; the budget below is the one thing
# that sees all of it, and it spaces requests so the overrun never happens.
# The budget is per PROCESS and spends the whole limit: do not run a second
# process against the same key while the daemon is up (a CLI run on the
# box, say) — both would count to 10, the server would 429 them, and each
# 429 parks the process that took it for a window.
RATE_LIMIT_REQUESTS = 10
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

    Same altitude as the yfinance latch in ``yfinance_common`` — the
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

    def _wait_needed(self, now: float) -> tuple[float, str]:
        """The wait before the next send, and the words for the bound that set it.

        Named here, where both bounds are computed, because the park and
        the count can hold at once and the log line must name whichever
        the caller is actually waiting on — read off ``_blocked_until``
        alone, a count-bound wait was attributed to the 429 (#195). Ties
        go to the park: the server's verdict outranks the local count.
        """
        while self._sent and self._sent[0] <= now - RATE_LIMIT_WINDOW_SECONDS:
            self._sent.popleft()
        wait = self._blocked_until - now
        bound = "the server answered 429, so the window is full whatever this process counted"
        if len(self._sent) >= RATE_LIMIT_REQUESTS:
            counted = self._sent[0] + RATE_LIMIT_WINDOW_SECONDS - now
            if counted > wait:
                wait = counted
                bound = (
                    f"{len(self._sent)} requests already sent in the last "
                    f"{RATE_LIMIT_WINDOW_SECONDS:.0f}s on the shared key"
                )
        return (wait + RATE_LIMIT_SLACK_SECONDS if wait > 0 else 0.0), bound

    def acquire(self, path: str) -> None:
        """Block until a request may be sent."""
        with self._lock:
            now = _monotonic()
            wait, reason = self._wait_needed(now)
            if wait > 0:
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


# A plausible exchange ticker, safe to interpolate into a URL path: the US
# ETF forms like "IBIT" and the international listing forms like "0434.HK",
# "ADE.DE" or Tokyo's numeric "3350" (the SoSoValue listings supplied the
# examples; the grammar is any provider's). Anything not matching is dropped
# — and disclosed via an unusable-entry count — rather than requested. The first
# character must be alphanumeric: URL-quoting leaves "." unescaped (it sits
# in urllib's always-safe set regardless of ``safe=``), so a dot-led token
# like ".." would otherwise survive quoting as a path-traversal-shaped
# segment. Anchored with ``\Z``, not ``$``: ``$`` also matches before a
# trailing newline, so "IBIT\n" would pass and land its raw newline mid-line
# in the report text.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}\Z")


def is_valid_ticker(x: object) -> TypeGuard[str]:
    """True only for a string passing the plausible-ticker filter.

    The one ticker predicate the listing consumers (the ETF and treasuries
    modules) share — each applies it live-parse-side and
    cache-read-side, so the two trust boundaries cannot drift apart on what
    may be interpolated into a URL path or report text.
    """
    return isinstance(x, str) and bool(_TICKER_RE.match(x))


class SoSoValueError(VendorError):
    """SoSoValue answered with an error envelope, or its response shape changed.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing
    layer reacts by behaviour rather than by vendor and an optional
    SoSoValue-backed category degrades down the chain instead of aborting.
    The family reads this type as STRUCTURAL — the client likely needs a
    fix — and logs it at ERROR with a traceback, so it is not raised for
    the vendor being down: that is ``SoSoValueUnavailableError``, which
    ``request`` raises for an unreached vendor too. One answer that is
    not the client's to fix still lands here: a 4xx with a body that is
    not JSON (a WAF's 403 page, a renamed endpoint's 404 page) is the
    vendor refusing or not knowing this request, kept structural because
    it is deterministic — retrying and stale-serving it as an outage
    would hide it for the whole stale cap — so the log line says "needs
    a fix, or the vendor is refusing it" (#217). So does a ``requests``
    failure raised before any network — a malformed URL or header —
    which is deterministic for the same reason and no outage.
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
    """The 10 req/min / 100k req/month limit was hit (HTTP 429).

    Reaches the router only when the throttled call ALSO had no usable cache
    (see ``load_rolling_snapshot``): the sibling tools, each with its own cache
    file, answer the same throttle with a stale-cache report. So the raise is
    not a statement about this client's standing, and the router must not
    keep the vendor unasked over it — that would turn those reports into
    DATA_UNAVAILABLE for the rest of the window, a different verdict rather
    than a cheaper one (#114).
    """

    latches_vendor = False


class SoSoValueUnavailableError(VendorUnavailableError):
    """SoSoValue was down: a 5xx it did not explain, a non-JSON 2xx/5xx body, or unreached.

    Raised by ``request`` for all three — the one type the family's
    transport lane carries, so every per-item handler and the cache lane
    catch it alone rather than a tuple keyed on the ``requests`` exception
    (#217) — and by ``raise_all_failed`` for a sweep that died purely of
    it. The message carries the status and path, or the exception's
    class, never a body or a ``requests`` message: the former is the
    gateway's error page, the latter quotes the request URL, and the text
    travels into a sentinel the model reads. Deliberately NOT a
    ``SoSoValueError``: every per-item handler and cache lane in the
    family reads that type as structural breakage (ERROR, traceback,
    "the client likely needs a fix"), and a gateway that is down is none
    of that — it takes the transport lane instead, like the rate-limit
    type takes its own.

    The unreached flavour is the subclass below, so a sweep can tally the
    two apart while every handler still catches this one type (#217).
    """


class SoSoValueUnreachedError(SoSoValueUnavailableError):
    """SoSoValue could not be reached at all: a timeout, a reset, a redirect loop.

    The one flavour of the transport lane ``request`` can tell apart, kept
    as a subclass — the family's way of typing a flavour (Farside, Fear &
    Greed) — so every handler still catches the parent alone, and the fact
    survives the cache lane's re-wrap by ``type(e)`` without a flag to
    forward (#217). Read only by ``fetch_each``'s tally. A sweep verdict
    (``raise_all_failed``) is the parent whatever the sweep counted: it
    carries the tally in its text, not one flavour in its type.
    """


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


def request(path: str, params: dict) -> list:
    """GET a SoSoValue endpoint and return its ``data`` list.

    Raises the vendor taxonomy: 401 -> not-configured (bad key is config
    breakage, not an outage), 429 -> rate-limited, a 5xx the envelope does
    not explain, a body that is not JSON at a 2xx or 5xx, or a vendor that
    could not be reached -> unavailable (the vendor is down, #172), anything
    else that is not a clean ``{"code": 0, "data": [...]}`` ->
    ``SoSoValueError``. "Explain" is Deribit's rule: an envelope with a
    non-zero ``code`` is the vendor answering about this request whatever
    status it rode in on (the live over-window error is an HTTP 403 with
    code 400301), so it stays the structural type; a 5xx with no such
    envelope is the gateway, not the API. A 4xx with a body that is not
    JSON — a renamed endpoint's 404 page, a WAF's 403 — is the vendor
    answering about this request too, and stays structural, as a 4xx the
    boundary leaves alone does at Farside and Fear & Greed.

    The transport lane is typed HERE, at the one place the family talks to
    the network, so every per-item handler and the cache lane catch one
    type — ``SoSoValueUnavailableError`` — instead of a tuple keyed on the
    library's exception that a new transport verdict would have to visit
    six times (#217). An unreached vendor (``is_unreached``: a timeout, a
    reset, and by that predicate's decision a redirect loop or an
    exhausted retry) is the outage type, worded by class only, never by
    message, which quotes the request URL (#171). The
    ``ValueError``-flavoured ``requests`` exceptions — a malformed URL or
    header, raised before any network — are a bug or config breakage
    rather than a vendor down and take the structural type, the verdict
    the cache lane used to reach on its own. The two outage verdicts on
    an answered request reuse the shared boundary helpers
    (``json_body_or_outage``, ``raise_for_http_status``) so their words
    are authored once for every vendor, re-raised as this family's type
    with the path appended. They judge only an answer outside the 4xx
    range: a 4xx is the vendor answering about this request whatever its
    body — the key verdict, the throttle, an error envelope, a WAF's page
    that is not JSON — and every verdict on one is the family's own.

    Every call passes through the shared ``_BUDGET`` first — after the key
    check, so an unset key still fails instantly rather than after a wait —
    and a 429 parks that budget for a window before it is raised.
    """
    api_key = get_api_key()
    _BUDGET.acquire(path)
    try:
        response = requests.get(
            f"{SOSOVALUE_API_BASE}{path}",
            params=params,
            headers={"x-soso-api-key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        words = f"SoSoValue {generic_failure_words(e)} on {path}"
        if is_unreached(e):
            raise SoSoValueUnreachedError(words) from e
        raise SoSoValueError(words) from e
    status = response.status_code
    if 400 <= status < 500:
        # The vendor answering about this request whatever the body
        # carries — the key verdict, the throttle, an error envelope, a
        # WAF's page — so the body is read leniently and every verdict
        # below is the family's own; none of them is an outage.
        try:
            body = response.json()
        except ValueError:
            body = None
    else:
        # The outage verdicts, in the shared helpers' words and carrying no
        # body text (the body is whatever page the gateway served): a body
        # that is not JSON, or a 5xx the envelope does not explain — an
        # envelope with a non-zero code is the API answering, whatever
        # status it rode in on. Below 400 the status helper is a no-op.
        try:
            body = json_body_or_outage(response, VENDOR)
            if not (isinstance(body, dict) and "code" in body and body["code"] != 0):
                raise_for_http_status(response, VENDOR)
        except VendorUnavailableError as e:
            raise SoSoValueUnavailableError(f"{e} on {path}") from e
    if status == 401:
        # The remedy ahead of the vendor's text: the body is capped at 300
        # and the router caps a whole message at 200, so a remedy appended
        # after the body was the first thing a cap dropped (#203). The router
        # renders this type by a fixed phrase for the model, and its lane
        # logs the message whole; the order is for any reader that caps it.
        raise SoSoValueNotConfiguredError(
            f"SoSoValue rejected the API key (HTTP 401); verify SOSOVALUE_API_KEY — until "
            f"then the chain falls to the next vendor. Body: {_error_message(body, api_key)}"
        )
    if status == 429:
        _BUDGET.note_rate_limited()
        raise SoSoValueRateLimitError(
            f"SoSoValue rate limit hit (HTTP 429) on {path}: {_error_message(body, api_key)}"
        )
    # Envelope errors can ride on any HTTP status (a missing-param error is
    # HTTP 400 with code 1; the over-window error is HTTP 403 with code
    # 400301), so judge the body, not just the status.
    if status != 200 or not isinstance(body, dict) or body.get("code") != 0:
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
    * ``last_network`` — the last failure on the transport lane, which is
      one type (``SoSoValueUnavailableError``: an unreached vendor, a 5xx
      the envelope did not explain, a non-JSON body — ``request`` types
      them all, #217), kept because without it a pure outage could only
      surface as structural breakage.
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
    * ``unreached`` / ``answered_without_data`` — the transport lane's two
      flavours counted, by type (``SoSoValueUnreachedError`` or not, #217),
      for the all-failed message that used to quote only ``last_network``:
      a sweep that mixed a gateway 502 with a timeout showed one of them.
    """

    results: dict
    failed: list[str]
    flagged: list[str]
    rate_limited: SoSoValueRateLimitError | None
    last_network: SoSoValueUnavailableError | None
    structural_failure: bool
    breaker_skipped: bool
    attempted: int
    unreached: int
    answered_without_data: int

    def transport_tally(self) -> str:
        """The transport failures counted by flavour, for an all-failed message (#217).

        ``(last: ...)`` quoted one failure for a sweep that may have mixed a
        gateway 5xx with a timeout; the counts say how many of each the
        sweep met, and the last one's words still ride along — in
        ``failure_account``'s words, which ``request`` authored by status
        or class, never from a ``requests`` message. A sweep that met no
        transport failure has no last one to quote and says so by the
        counts alone.
        """
        counts = f"{self.unreached} unreached, {self.answered_without_data} answered without data"
        if self.last_network is None:
            return counts
        return f"{counts}; last: {failure_account(self.last_network)}"

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
      10 req/min limit is per-key and per-minute), so the rest is drained
      into ``failed`` (short-TTL retry) instead of burning a quota call per
      remaining item.
    * ``max_consecutive_network`` transport failures trip the breaker: a
      network that hangs instead of failing fast would otherwise turn one
      refresh into sequential full timeouts inside a single analyst tool
      call, and the post-breaker outcome (disclosed-incomplete, short TTL)
      is identical to riding the brownout out. The lane is one type,
      ``SoSoValueUnavailableError``, which ``request`` raises for an
      unreached vendor and for an outage answer — a 5xx the envelope does
      not explain, a non-JSON body — alike (#172, #217): the item goes to
      ``failed`` and the streak counts it, since a gateway that is down
      costs the same per remaining item as a network that hangs. Any
      completed request resets the streak — the server answered, so each
      remaining item is still worth its own try.

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
        # request per character instead of failing loudly. The list is the
        # caller's to build, so this is our breakage and takes the type that
        # keeps the router from reading it as SoSoValue's library (#219).
        raise WiringGapError("fetch_each items must be a collection of items, not a bare string")
    results: dict = {}
    failed: list[str] = []
    flagged: list[str] = []
    rate_limited: SoSoValueRateLimitError | None = None
    last_network: SoSoValueUnavailableError | None = None
    unreached = answered_without_data = 0
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
        except SoSoValueUnavailableError as e:
            last_network = e
            if isinstance(e, SoSoValueUnreachedError):
                unreached += 1
            else:
                answered_without_data += 1
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
        unreached=unreached,
        answered_without_data=answered_without_data,
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
    * A sweep that died PURELY of transport — unreached, or answered with a
      gateway page (``SoSoValueUnavailableError``) — is the vendor down, and
      raises the outage type: the cache lane's warning branch, where network
      failures already land, and the router's no-traceback lane that counts
      the vendor as down (#172) — not a bare ``SoSoValueError``, which reads
      as "the client likely needs a fix" for an outage no code change can
      heal. One type for both flavours, so the verdict cannot depend on
      which of them happened to come last. "Purely" is the gate: a flagged
      item (the provider answered and served nothing) or a swallowed parse
      break means something structural is in the mix, and the generic error
      stays the honest answer.

    The callables supply each module's message — worded over its own universe
    and counts — so the shared predicate cannot drift between modules while
    the prose stays theirs.
    """
    if sweep.rate_limited is not None:
        raise SoSoValueRateLimitError(on_rate_limited()) from sweep.rate_limited
    if sweep.last_network is not None and not sweep.structural_failure and not sweep.flagged:
        raise SoSoValueUnavailableError(on_transport()) from sweep.last_network
    raise SoSoValueError(on_structural())
