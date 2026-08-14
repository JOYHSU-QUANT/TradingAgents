"""Shared plumbing for the SoSoValue vendor modules.

SoSoValue's OpenAPI (https://sosovalue.gitbook.io/soso-value-api-doc) serves
several product modules off one base URL, one ``x-soso-api-key`` header, one
``{"code": 0, "data": [...]}`` response envelope, and one 20 req/min /
100k req/month plan limit. The vendor modules built on it — spot-ETF flows
(``sosovalue.py``), the macro economic calendar (``sosovalue_macro.py``), and
BTC corporate treasuries (``sosovalue_treasuries.py``) — share that plumbing
here: the error taxonomy, API-key retrieval and sanitization, the request
helper with its redaction discipline, the value/date/ticker trust-boundary
predicates, the clock and cache-age helpers every rolling-snapshot cache
shares, and the two orchestration skeletons the modules used to carry as
near-verbatim copies — the cache/TTL/stale-fallback discipline
(``load_rolling_snapshot``) and the per-item sweep with its 429 drain and
consecutive-network-failure breaker (``fetch_each``). Anything
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
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import NamedTuple, NoReturn

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


def _stale_caveat(fetched_at: str, closing: str) -> str:
    """The family's STALE header line: age, cause set, provenance, ``closing``.

    One template so the modules cannot drift on the cause enumeration —
    network error, rate limit, and an API contract break are the causes
    ``load_rolling_snapshot``'s fallback absorbs for this family — while
    each module supplies the closing warning its own reader needs (what,
    concretely, may be outdated).
    """
    return (
        f"_STALE by {_humanize_age(fetched_at)}: live refresh failed (network error, "
        f"rate limit, or an API contract break); showing the last cached snapshot "
        f"(fetched {fetched_at}). {closing}_"
    )


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

    The ETF module's fund loop deliberately does NOT use this: it predates
    the 429 drain and keeps its per-item-retry rate-limit semantics (a PR #19
    decision), so folding it in would be a behaviour change, not a refactor.
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
) -> tuple[dict, str, bool]:
    """The family's cache/TTL/stale discipline; returns (payload, fetched_at, stale).

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
            return cached, cached["fetched_at"], False

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
            return cached, fetched_at, True
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
    return payload, fetched_at, False
