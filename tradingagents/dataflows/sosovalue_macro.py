"""SoSoValue US macro economic-calendar vendor.

Serves a forward-looking economic-event report from SoSoValue's OpenAPI macro
module (shared plumbing in ``sosovalue_common``): scheduled US releases inside
a two-week look-ahead with their consensus forecasts, and recent releases
inside a trailing window with actual-vs-forecast surprises. It is a news-side
regime/risk-modifier feed — event risk contextualizes sizing and timing — not
a directional signal, and the report says so in a fixed line.

Live-verified API facts this module is built on (2026-08-11):

- ``GET /macro/events`` takes no parameters and returns a ~2-week forward
  calendar anchored to the present (it reached one day back and two weeks
  ahead when captured): ``{"date": "yyyy-MM-dd", "events": [names...]}``.
- ``GET /macro/events/{event}/history`` accepts the exact name string the
  calendar uses, serves deep history (2018+ for CPI) newest-first under a
  ``limit`` capped at 100 (values above are silently clamped), and represents
  a scheduled-but-unreleased print as a row whose ``actual`` is the empty
  string — never null. An unknown event name returns HTTP 200 with an empty
  ``data`` list, not a 404, so a renamed event surfaces as an empty history.
- ``actual``/``forecast``/``previous`` are strings with embedded units:
  percent forms ("3.5%") and plain numbers (Nonfarm Payrolls' thousands,
  where a contraction month arrives as a negative string like "-23").
- The provider carries no Fed rate-decision event under any probed name (it
  exposes a separate FOMC-probabilities endpoint instead, out of scope here),
  so the report labels that hole explicitly — an analyst must not read "no
  FOMC below" as a quiet Fed schedule.

Importance filtering is a curated whitelist (``TRACKED_EVENTS``, exact
live-verified names): the API has no importance field, so the whitelist IS the
importance filter, and it bounds the fan-out to 1 + len(TRACKED_EVENTS)
requests per refresh against the shared 20 req/min plan limit. Calendar names
outside the whitelist still appear in the scheduled section as name-only lines
(zero extra requests), so coverage beyond the whitelist stays visible even
though it carries no figures.

Vendor success is the calendar fetch (the report's spine) plus at least one
tracked history — symmetric with the treasuries module's rule, because a
figure-less schedule overwriting a complete cached snapshot would destroy
good data while presenting itself as fresh; with every history failed the
call raises instead, so the stale-fallback path preserves and discloses the
previous snapshot. Below that threshold each event's history is fetched
non-fatally, with the ETF module's consecutive-network-failure breaker so a
hanging network cannot burn a full timeout per event. A 429 goes further
than the breaker: the plan limit is per-key and per-minute, so the first one
proves every remaining request in this sweep would also 429 — the rest are
drained into ``events_failed`` unattempted (this deliberately diverges from
the ETF module's decided keep-trying-after-429 behaviour, which predates the
key being shared by three fan-outs). A tracked name whose history comes back
*empty* is a different failure from either: the API answers an unknown name
with 200 + an empty list, so emptiness means the name was renamed or dropped
upstream and will not heal by retrying — those land in ``events_unknown``,
disclosed in the report but NOT shortening the cache TTL (same rationale as
the ETF module's unusable listing entries), because an hourly re-sweep can
never fix what needs a code edit. A rejected or unset key raises out of any
request so the emergency-disable flip (unset ``SOSOVALUE_API_KEY``) is never
papered over by a cache or a stale serve.

Three lookahead disciplines keep a backtest honest. Released figures
(``actual``) render only for rows dated on or before ``curr_date``; the
scheduled section renders forecast/previous but never an actual — even when
the provider has since filled it in — so a historical ``curr_date`` sees
scheduled prints exactly as a live caller would have (they come from the deep
event histories, not from the present-anchored calendar, which cannot reach
back). Second, forecast and previous are the provider's *current* figures, not
point-in-time snapshots — the report carries that caveat rather than
pretending otherwise. Third, a release dated ``curr_date`` itself was
published at some unknown point during that day (the API has no time-of-day),
which the report flags whenever such a row is shown.

Caching mirrors the family pattern: one rolling snapshot file, refreshed past
``CACHE_TTL_HOURS`` (or ``INCOMPLETE_CACHE_TTL_HOURS`` while any tracked
history is missing), stale-served on a fetch failure up to ``MAX_STALE_DAYS``
with a disclosed age, never written on failure, and discarded when read-side
validation fails.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import NamedTuple
from urllib.parse import quote

import requests

from .errors import VendorError
from .sosovalue_common import (
    SoSoValueError,
    SoSoValueNotConfiguredError,
    SoSoValueRateLimitError,
    _cache_age_hours,
    _cache_dir,
    _days_stale,
    _humanize_age,
    _is_iso_date,
    _iso_now,
    _plural,
    _request,
    get_api_key,
)

logger = logging.getLogger(__name__)

# Exact event-name strings, live-verified against /macro/events and per-name
# history probes on 2026-08-11. The API takes the name verbatim in the history
# path, so these must match byte-for-byte; a renamed event comes back as an
# empty history and lands in ``events_unknown`` (disclosed, and deliberately
# not retried on the short TTL — only editing this tuple fixes it), never in
# bad data.
# Probed-and-absent (documented so nobody re-adds them blind): every FOMC /
# Fed-rate-decision variant, "Unemployment Rate", "ISM Manufacturing PMI",
# "Michigan Consumer Sentiment", "Core PCE (MoM)", "PCE (YoY)",
# "GDP Growth Rate (QoQ)".
TRACKED_EVENTS = (
    "CPI (YoY)",
    "CPI (MoM)",
    "Core CPI (YoY)",
    "Core CPI (MoM)",
    "Nonfarm Payrolls",
    "Initial Jobless Claims",
    "Core PCE Price Index (MoM)",
    "GDP (QoQ)",
    "Retail Sales (MoM)",
)

# Forward horizon (calendar days after curr_date) for the scheduled section.
# Matches the ~2-week window the live calendar serves, so the two sources the
# section merges (deep histories and the present-anchored calendar) cover the
# same span for a live caller.
AHEAD_DAYS = 14

# Default trailing window for the released section when the caller does not
# specify one; mirrors the family default.
DEFAULT_LOOKBACK_DAYS = 30

# The documented per-request row cap (values above are silently clamped).
# 100 rows is 8+ years of a monthly event and ~2 years of a weekly one —
# deep enough for any backtest window this report serves.
HISTORY_LIMIT = 100

# Structural bound on the calendar: the live ~2-week window holds a handful
# of rows (6 when captured), so a calendar past this many day-rows means the
# endpoint changed shape and the strict parser refuses it.
MAX_CALENDAR_ROWS = 40

# Server-controlled strings are LLM-visible report text, so both get a
# printable-ASCII charset check and a length bound at the parse boundary
# (and again read-side from cache). Live names top out well under these.
MAX_EVENT_NAME_CHARS = 60
MAX_VALUE_CHARS = 24

# Row cap for the rendered released-events table, mirroring the family MAX_ROWS.
MAX_ROWS = 40

# Cache lifetimes, mirroring the ETF module: 6h TTL re-pulls a few times a
# day (event figures change on release days only), the short TTL re-tries a
# partially-failed history sweep, and stale serves are capped and disclosed.
CACHE_TTL_HOURS = 6
INCOMPLETE_CACHE_TTL_HOURS = 1
MAX_STALE_DAYS = 14

# Consecutive transport-level failures in the history loop before the
# remaining events are skipped into ``events_failed`` unattempted — same
# rationale and value as the ETF module's fund-history breaker.
MAX_CONSECUTIVE_NETWORK_FAILURES = 3

# A parseable numeric value string: optional sign, digits (with or without
# comma grouping), optional decimals, optional unit (percent or a magnitude
# letter). Used only to compute surprises; a string that does not match is
# still rendered verbatim (it passed the charset check), just never
# arithmetic'd on.
_VALUE_NUM_RE = re.compile(r"^([+-]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%|[KMBT])?$")


def _is_valid_value(x: object) -> bool:
    """True for a bounded, printable-ASCII value string (empty string legal).

    ``actual`` is the empty string until a print is released (live-verified),
    so emptiness is a meaning, not a defect. Anything unprintable or oversized
    is rejected at the trust boundary: these strings render verbatim into
    LLM-visible report text.
    """
    return isinstance(x, str) and len(x) <= MAX_VALUE_CHARS and all(32 <= ord(c) <= 126 for c in x)


def _is_valid_event_name(x: object) -> bool:
    """True for a plausible event name: non-empty, bounded, printable ASCII.

    Names are interpolated (URL-quoted) into history request paths and render
    verbatim in report text, so the same one predicate guards both the live
    parse and the cache read, like the family's ticker filter.
    """
    return (
        isinstance(x, str)
        and 1 <= len(x) <= MAX_EVENT_NAME_CHARS
        and x == x.strip()
        and all(32 <= ord(c) <= 126 for c in x)
    )


def _parse_calendar(data: list) -> tuple[list[dict], int]:
    """Validate /macro/events rows into ascending ``{"date", "events"}`` rows.

    Strict where the calendar is the vendor-success criterion: an empty
    calendar, a malformed row, a duplicated date, or an implausibly large
    row count all raise so the router degrades instead of serving a half
    parsed spine. Individual event *names* degrade softly — an unusable name
    is dropped and counted (``unusable``) so the report can disclose the
    shrunken universe, mirroring the ETF listing's unusable-ticker handling.
    """
    if not data:
        raise SoSoValueError("SoSoValue returned an empty macro calendar")
    if len(data) > MAX_CALENDAR_ROWS:
        raise SoSoValueError(
            f"SoSoValue macro calendar has {len(data)} day-rows "
            f"(> {MAX_CALENDAR_ROWS}); the API contract may have changed"
        )
    rows = []
    unusable = 0
    for raw in data:
        if (
            not isinstance(raw, dict)
            or not _is_iso_date(raw.get("date"))
            or not isinstance(raw.get("events"), list)
        ):
            raise SoSoValueError(f"Malformed macro calendar row {str(raw)[:200]!r}")
        names = []
        for name in raw["events"]:
            if not _is_valid_event_name(name):
                unusable += 1
                logger.warning(
                    "SoSoValue macro calendar entry %.120r has no usable event name; skipping it",
                    name,
                )
            elif name not in names:
                names.append(name)
        rows.append({"date": raw["date"], "events": names})
    rows.sort(key=lambda r: r["date"])
    dates = [r["date"] for r in rows]
    if len(set(dates)) != len(dates):
        raise SoSoValueError(
            "SoSoValue macro calendar repeats a date; refusing to double-list the day"
        )
    return rows, unusable


def _parse_event_rows(data: list, name: str) -> list[dict]:
    """Validate one event's history into ascending rows.

    Raises on anything malformed so the caller counts the event as failed
    (per-event failures are non-fatal by design). An empty ``data`` raises
    too, as a defensive backstop — ``_fetch_one_event`` pre-checks emptiness
    and routes it into the ``events_unknown`` bucket before this parser runs.
    Duplicate dates are KEPT, both rows: the live NFP history carries two
    prints dated 2025-12-16 (a delayed release and its catch-up on one day),
    and a last-wins collapse would silently drop a real release plus its
    surprise. Ascending sort, stable within a date.
    """
    if not data:
        raise SoSoValueError(
            f"SoSoValue returned no history rows for macro event {name!r} "
            f"(an unknown or renamed event name returns an empty list)"
        )
    rows = []
    for raw in data:
        if not (
            isinstance(raw, dict)
            and _is_iso_date(raw.get("date"))
            and all(_is_valid_value(raw.get(k)) for k in ("actual", "forecast", "previous"))
        ):
            raise SoSoValueError(f"Malformed {name!r} history row {str(raw)[:200]!r}")
        rows.append(
            {
                "date": raw["date"],
                "actual": raw["actual"],
                "forecast": raw["forecast"],
                "previous": raw["previous"],
            }
        )
    rows.sort(key=lambda r: r["date"])
    return rows


class _MacroSnapshot(NamedTuple):
    """What ``_load_snapshot`` resolved.

    ``calendar`` is the present-anchored ~2-week schedule (ascending unique
    dates, validated names only, with ``calendar_unusable`` counting dropped
    names). ``histories`` maps each successfully-fetched tracked event to its
    ascending rows; ``events_failed`` holds the tracked events whose history
    fetch failed (retried on the short TTL), and ``events_unknown`` those the
    provider answered with an empty history (renamed/dropped upstream — a
    code edit, not a retry, fixes those, so they do not shorten the TTL).
    The three always partition ``TRACKED_EVENTS``, an invariant the cache
    validator re-checks read-side; ``histories`` is never empty (all-failed
    is a vendor failure by decision).
    """

    calendar: list[dict]
    calendar_unusable: int
    histories: dict[str, list[dict]]
    events_failed: list[str]
    events_unknown: list[str]
    fetched_at: str
    stale: bool


def _cache_path() -> str:
    """Path of the single rolling macro snapshot (no per-asset dimension)."""
    return os.path.join(_cache_dir(), "sosovalue_macro.json")


def _valid_history_rows(rows: object) -> bool:
    if not (isinstance(rows, list) and rows):
        return False
    if not all(
        isinstance(r, dict)
        and _is_iso_date(r.get("date"))
        and all(_is_valid_value(r.get(k)) for k in ("actual", "forecast", "previous"))
        for r in rows
    ):
        return False
    dates = [r["date"] for r in rows]
    # Non-descending, not strictly ascending: duplicate dates are legal (two
    # prints released on one day) and preserved by the parser.
    return all(a <= b for a, b in zip(dates, dates[1:], strict=False))


def _read_cache(path: str) -> dict | None:
    """Return a fully-validated cached payload, or None if untrusted.

    Same boundary philosophy as the family's other caches: every rejection is
    logged with its own reason, a rejected cache costs one re-fetch and never
    bad data, and the cross-field invariant ``_fetch_all`` always writes —
    histories and failures partitioning ``TRACKED_EVENTS`` — is re-checked so
    a cache written by a different whitelist (an older code version, a hand
    edit) is refetched rather than rendered with missing or orphaned events.
    """

    def _reject(reason: str) -> None:
        logger.warning("Ignoring SoSoValue macro cache %s: %s", path, reason)
        return None

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return _reject(f"unreadable ({e})")
    if not isinstance(payload, dict):
        return _reject(f"top-level JSON is a {type(payload).__name__}, expected an object")
    calendar = payload.get("calendar")
    if not (
        isinstance(calendar, list)
        and calendar
        and len(calendar) <= MAX_CALENDAR_ROWS
        and all(
            isinstance(r, dict)
            and _is_iso_date(r.get("date"))
            and isinstance(r.get("events"), list)
            and all(_is_valid_event_name(n) for n in r["events"])
            for r in calendar
        )
    ):
        return _reject("'calendar' is missing, empty, oversized, or malformed")
    dates = [r["date"] for r in calendar]
    if any(a >= b for a, b in zip(dates, dates[1:], strict=False)):
        return _reject("'calendar' dates are not strictly ascending")
    unusable = payload.get("calendar_unusable")
    if not isinstance(unusable, int) or isinstance(unusable, bool) or unusable < 0:
        return _reject("'calendar_unusable' is missing or not a non-negative integer")
    histories = payload.get("histories")
    if not (
        isinstance(histories, dict)
        and all(
            _is_valid_event_name(n) and _valid_history_rows(rows) for n, rows in histories.items()
        )
    ):
        return _reject("'histories' is missing or contains a malformed event history")
    if not histories:
        # _fetch_all raises rather than writing an all-failed payload, so an
        # empty histories dict can only be a foreign or corrupted file.
        return _reject("'histories' is empty")
    buckets = {}
    for key in ("events_failed", "events_unknown"):
        names = payload.get(key)
        if not isinstance(names, list) or not all(_is_valid_event_name(n) for n in names):
            return _reject(f"'{key}' is missing or not a list of event names")
        buckets[key] = set(names)
        if len(buckets[key]) != len(names) or buckets[key] & histories.keys():
            return _reject(f"'{key}' repeats an event or overlaps 'histories'")
    if buckets["events_failed"] & buckets["events_unknown"]:
        return _reject("'events_failed' overlaps 'events_unknown'")
    if histories.keys() | buckets["events_failed"] | buckets["events_unknown"] != set(
        TRACKED_EVENTS
    ):
        # Also covers a cache written under a different TRACKED_EVENTS: the
        # report claims whitelist-wide coverage, so a mismatched universe must
        # cost one refetch, not render with silently missing events.
        return _reject("'histories' + failure buckets do not partition TRACKED_EVENTS")
    if not isinstance(payload.get("fetched_at"), str) or not payload["fetched_at"]:
        return _reject("'fetched_at' is missing or not a non-empty string")
    return payload


def _fetch_one_event(name: str) -> list[dict] | str | None:
    """Fetch and parse one tracked event's history.

    Returns the rows, the string ``"unknown"`` for an empty history (the API
    answers an unknown or renamed name with 200 + an empty list — a code
    edit, not a retry, fixes that), or ``None`` on a non-fatal failure worth
    retrying. A rejected key and a 429 both propagate: the key because config
    breakage must reach the router even mid-batch, the 429 because the
    per-minute quota makes the rest of the sweep pointless (the caller drains
    it). A structural break is logged at ERROR with a traceback, a transient
    stays a warning, and a transport-level failure is re-raised after logging
    so the caller's consecutive-failure breaker can count the streak.
    """
    try:
        data = _request(f"/macro/events/{quote(name, safe='')}/history", {"limit": HISTORY_LIMIT})
        if not data:
            logger.warning(
                "SoSoValue macro event %r has an empty history — the name is "
                "unknown or renamed upstream and retrying cannot heal it; "
                "update TRACKED_EVENTS",
                name,
            )
            return "unknown"
        return _parse_event_rows(data, name)
    except (requests.RequestException, SoSoValueError) as e:
        if isinstance(e, SoSoValueError):
            logger.error(
                "SoSoValue macro event %r history failed structurally (its rows "
                "will be missing from the report) — the client likely needs a "
                "fix: %s",
                name,
                e,
                exc_info=True,
            )
        else:
            logger.warning(
                "SoSoValue macro event %r history failed (its rows will be "
                "missing from the report): %s",
                name,
                e,
            )
        if isinstance(e, requests.RequestException):
            raise
        return None


def _fetch_all() -> dict:
    """One full refresh: calendar, then each tracked event's history.

    Returns a cache payload (without ``fetched_at``). Raises on a calendar
    failure, on a rejected key from ANY request, and when every tracked
    history failed (vendor success = calendar + at least one history, by
    decision): a figure-less schedule must not overwrite a complete cached
    snapshot while presenting itself as fresh — raising instead routes the
    call through the stale fallback, which preserves and discloses the
    previous snapshot. Below that threshold, a transient failure lands in
    ``events_failed`` (with the family's consecutive-network-failure breaker,
    and an immediate drain on the first 429 — the per-minute quota is shared,
    so the rest of the sweep would only burn more 429s), and an
    empty-history name lands in ``events_unknown`` (renamed upstream; not
    retried on the short TTL because retrying cannot heal it).
    """
    calendar, unusable = _parse_calendar(_request("/macro/events", {}))

    histories: dict[str, list[dict]] = {}
    events_failed: list[str] = []
    events_unknown: list[str] = []
    rate_limited: SoSoValueRateLimitError | None = None
    consecutive_network = 0
    remaining = iter(TRACKED_EVENTS)
    for name in remaining:
        try:
            rows = _fetch_one_event(name)
        except SoSoValueRateLimitError as e:
            rate_limited = e
            # The 20 req/min limit is per-key and per-minute: this 429 proves
            # every further request in this sweep would 429 too, so drain the
            # rest into events_failed (short-TTL retry) instead of burning a
            # quota call per remaining event.
            skipped = [name, *remaining]
            events_failed.extend(skipped)
            logger.warning(
                "SoSoValue macro: rate limit hit (%s); skipping the remaining "
                "%d event histories (disclosed as incomplete, retried on the "
                "short TTL)",
                e,
                len(skipped),
            )
            break
        except requests.RequestException:
            events_failed.append(name)
            consecutive_network += 1
            if consecutive_network >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                skipped = list(remaining)
                if skipped:
                    events_failed.extend(skipped)
                    logger.warning(
                        "SoSoValue macro: %d consecutive network failures; "
                        "skipping the remaining %d event histories "
                        "(disclosed as incomplete, retried on the short TTL)",
                        consecutive_network,
                        len(skipped),
                    )
                break
            continue
        consecutive_network = 0
        if rows == "unknown":
            events_unknown.append(name)
        elif rows is None:
            events_failed.append(name)
        else:
            histories[name] = rows
    if not histories:
        # Keep the taxonomy honest when a 429 drained the whole sweep: the
        # router and _load_snapshot classify by type, and a quota trip must
        # not masquerade as structural breakage (ERROR + traceback logs).
        if rate_limited is not None:
            raise SoSoValueRateLimitError(
                f"SoSoValue macro: rate limited before any tracked event "
                f"history could be fetched: {rate_limited}"
            ) from rate_limited
        raise SoSoValueError(
            f"SoSoValue macro: none of the {len(TRACKED_EVENTS)} tracked event "
            f"histories could be fetched; a schedule without figures is not a "
            f"served signal"
        )
    return {
        "calendar": calendar,
        "calendar_unusable": unusable,
        "histories": histories,
        "events_failed": events_failed,
        "events_unknown": events_unknown,
    }


def _snapshot_from(payload: dict, fetched_at: str, stale: bool) -> _MacroSnapshot:
    return _MacroSnapshot(
        calendar=payload["calendar"],
        calendar_unusable=payload["calendar_unusable"],
        histories=payload["histories"],
        events_failed=payload["events_failed"],
        events_unknown=payload["events_unknown"],
        fetched_at=fetched_at,
        stale=stale,
    )


def _load_snapshot() -> _MacroSnapshot:
    """Return the macro snapshot, via the family's cache/stale discipline.

    Key first (the emergency-disable flip must not wait out a fresh cache);
    a cache younger than its TTL is served as-is (missing event histories
    earn the shorter ``INCOMPLETE_CACHE_TTL_HOURS``); otherwise fetch and
    overwrite the rolling file; on a fetch failure fall back to the cached
    snapshot (``stale=True``) up to ``MAX_STALE_DAYS``. A failed fetch is
    never written to cache, and ``SoSoValueNotConfiguredError`` is never
    absorbed by the stale fallback.
    """
    get_api_key()

    path = _cache_path()
    cached = _read_cache(path)
    if cached:
        age_h = _cache_age_hours(cached["fetched_at"])
        ttl = INCOMPLETE_CACHE_TTL_HOURS if cached["events_failed"] else CACHE_TTL_HOURS
        if age_h is not None and 0 <= age_h < ttl:
            return _snapshot_from(cached, cached["fetched_at"], stale=False)

    try:
        payload = _fetch_all()
    except SoSoValueNotConfiguredError:
        raise
    except (requests.RequestException, VendorError) as e:
        # Keep the exact vendor-taxonomy type through the context-adding wrap
        # (the router classifies by type); only a network error becomes the
        # generic SoSoValueError. Same rationale as the ETF module.
        wrap_cls = type(e) if isinstance(e, VendorError) else SoSoValueError
        if cached:
            fetched_at = cached["fetched_at"]
            age = _days_stale(fetched_at)
            if age is None or age > MAX_STALE_DAYS:
                stale_desc = (
                    "has an unparseable or future-dated fetch date"
                    if age is None
                    else f"is {age} days stale"
                )
                raise wrap_cls(
                    f"SoSoValue macro fetch failed and the newest cache {stale_desc} "
                    f"(> {MAX_STALE_DAYS}-day cap): {e}"
                ) from e
            age_str = _humanize_age(fetched_at)
            if isinstance(e, SoSoValueError):
                logger.error(
                    "SoSoValue macro refresh failed structurally (%s); serving "
                    "stale cache (%s old) — the client likely needs a fix",
                    e,
                    age_str,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "SoSoValue macro refresh failed (%s); using stale cache (%s old)",
                    e,
                    age_str,
                )
            return _snapshot_from(cached, fetched_at, stale=True)
        raise wrap_cls(f"SoSoValue macro unavailable and no usable cache exists: {e}") from e

    fetched_at = _iso_now()
    payload["fetched_at"] = fetched_at
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        logger.warning(
            "Could not write SoSoValue macro cache %s: %s — the fetch throttle "
            "stays disabled until a write succeeds, so further calls will each "
            "re-fetch",
            path,
            e,
        )
    return _snapshot_from(payload, fetched_at, stale=False)


def _parse_value(s: str) -> tuple[float, str] | None:
    """Parse a value string into ``(number, unit)``; None when not numeric.

    ``unit`` is ``"%"``, a magnitude letter (K/M/B/T), or ``""`` for a plain
    number. Comma grouping is accepted; anything else — including an empty
    string — is None, and the caller renders the original string instead of
    inventing a number.
    """
    m = _VALUE_NUM_RE.match(s.strip())
    if not m:
        return None
    sign, whole, frac, unit = m.groups()
    value = float(f"{sign}{whole.replace(',', '')}{frac or ''}")
    return value, unit or ""


def _surprise_cell(actual: str, forecast: str) -> str:
    """Actual-minus-forecast for the released table, or "n/a".

    Computed only when both sides parse AND carry the same unit — "3.5%" minus
    "250K" is not a number, and rendering one would be fabrication. A percent
    difference is labelled "pp" (percentage points): the difference of two
    percent readings is not itself a percent of anything.
    """
    a, f = _parse_value(actual), _parse_value(forecast)
    if a is None or f is None or a[1] != f[1]:
        return "n/a"
    unit = "pp" if a[1] == "%" else a[1]
    diff = round(a[0] - f[0], 4)
    # Grouped fixed-point, never ``g``: a raw-count event (claims filed in
    # thousands or units) would otherwise render "+1e+06"-style scientific
    # notation in a column whose other rows are plain integers.
    text = f"{int(diff):+,}" if diff == int(diff) else f"{diff:+,}"
    return f"{text}{unit}"


def get_economic_calendar_data(curr_date: str, look_back_days: int | None = None) -> str:
    """Fetch the US macro economic calendar as a markdown report.

    Args:
        curr_date: Anchor date (yyyy-mm-dd). Scheduled events are shown for
            the ``AHEAD_DAYS`` after it (forecast/previous only, never an
            actual); released figures only on or before it.
        look_back_days: Trailing window for the released section; ``None``
            uses DEFAULT_LOOKBACK_DAYS.

    Returns:
        A markdown report: a scheduled-events table (tracked events with
        forecasts, other calendar names without figures), a released-events
        table with surprises, and fixed caveats — including that this feed
        carries no Fed rate decisions.
    """
    if look_back_days is None or look_back_days <= 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Normalise curr_date BEFORE any lexical date comparison: strptime accepts
    # non-zero-padded input ("2026-6-5"), which compares wrong against
    # canonical ISO row dates and would silently admit future rows.
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_dt.strftime("%Y-%m-%d")
    ahead_end = (curr_dt + timedelta(days=AHEAD_DAYS)).strftime("%Y-%m-%d")
    window_start = (curr_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    snapshot = _load_snapshot()

    # ---- scheduled section rows -------------------------------------------
    # Tracked figures come from the deep histories (they reach any backtest
    # date), never from the present-anchored calendar; a scheduled row NEVER
    # renders an actual, even when the provider has since filled it in.
    scheduled = []
    covered: dict[str, list[datetime]] = {}
    for name in TRACKED_EVENTS:
        for row in snapshot.histories.get(name, ()):
            if curr_date < row["date"] <= ahead_end:
                scheduled.append(
                    (row["date"], name, row["forecast"] or "—", row["previous"] or "—")
                )
                covered.setdefault(name, []).append(datetime.strptime(row["date"], "%Y-%m-%d"))
    # Calendar entries ride along name-only unless a figures row for the SAME
    # print is already shown. "Same print" is per (name, date +/- 1 day) —
    # the calendar date sits a day off its history row for some events
    # (live-observed for CPI) — and deliberately NOT per name alone: a weekly
    # event (Initial Jobless Claims) has two or three prints inside the
    # 14-day window, and a name-keyed dedupe would silently swallow every
    # occurrence after the first, under-stating the very event risk this
    # report exists to surface.
    for cal in snapshot.calendar:
        if curr_date < cal["date"] <= ahead_end:
            cal_dt = datetime.strptime(cal["date"], "%Y-%m-%d")
            for name in cal["events"]:
                shown = covered.get(name, ())
                if any(abs((cal_dt - dt).days) <= 1 for dt in shown):
                    continue
                scheduled.append((cal["date"], name, "—", "—"))
    scheduled.sort(key=lambda r: (r[0], r[1]))

    # ---- released section rows --------------------------------------------
    released = []
    for name in TRACKED_EVENTS:
        for row in snapshot.histories.get(name, ()):
            if window_start <= row["date"] <= curr_date:
                released.append((row["date"], name, row))
    released.sort(key=lambda r: (r[0], r[1]))
    same_day = any(d == curr_date and row["actual"] for d, _n, row in released)

    # ---- header ------------------------------------------------------------
    header_lines = ["## US Economic Calendar — scheduled events & releases (SoSoValue)"]

    if snapshot.stale:
        age_str = _humanize_age(snapshot.fetched_at)
        header_lines.append(
            f"_STALE by {age_str}: live refresh failed (network error, rate limit, or an "
            f"API contract break); showing the last cached snapshot (fetched "
            f"{snapshot.fetched_at}). Scheduled dates and figures may be outdated._"
        )

    if snapshot.events_failed:
        fetched = len(snapshot.histories)
        header_lines.append(
            f"_Tracked-event coverage incomplete ({fetched}/{len(TRACKED_EVENTS)}): "
            f"histories for {', '.join(sorted(snapshot.events_failed))} could not be "
            f"fetched, so their scheduled prints and releases are missing from both "
            f"tables below._"
        )

    if snapshot.events_unknown:
        n = len(snapshot.events_unknown)
        header_lines.append(
            f"_{n} tracked {_plural(n, 'event is', 'events are')} unknown to the "
            f"provider ({', '.join(sorted(snapshot.events_unknown))}): the name was "
            f"renamed or dropped upstream, so {_plural(n, 'its', 'their')} rows are "
            f"missing below until the tracked-name list is updated in code._"
        )

    if snapshot.calendar_unusable:
        n = snapshot.calendar_unusable
        header_lines.append(
            f"_{n} calendar {_plural(n, 'entry', 'entries')} had no usable event name "
            f"and {_plural(n, 'was', 'were')} skipped._"
        )

    header_lines.append(
        "_No Fed rate decisions: this feed carries no FOMC event at all, so their "
        "absence below is a coverage gap of the source, not a quiet Fed schedule._"
    )
    header_lines.append(
        "_Forecast and previous values are the provider's current figures, not "
        "point-in-time snapshots; a surprise is shown only where actual and forecast "
        "share a unit._"
    )
    if same_day:
        header_lines.append(
            f"_A release dated {curr_date} was published at some point during that "
            f"day (this feed has no time-of-day), so it may postdate an intraday "
            f"decision time._"
        )
    header_lines.append(
        "_Treat event risk as a regime / risk modifier for sizing and timing, not a "
        "directional trade signal._"
    )

    cal_span = f"{snapshot.calendar[0]['date']} → {snapshot.calendar[-1]['date']}"
    header_lines.append(
        f"- Source: SoSoValue OpenAPI (US macro calendar; served span {cal_span}) | "
        f"Tracked: {', '.join(TRACKED_EVENTS)} | Window ending {curr_date}"
    )
    header = "\n\n".join(header_lines) + "\n"

    # ---- scheduled table ----------------------------------------------------
    if scheduled:
        lines = ["\n| Date | In | Event | Forecast | Previous |", "| --- | --- | --- | --- | --- |"]
        for date, name, forecast, previous in scheduled:
            days = (datetime.strptime(date, "%Y-%m-%d") - curr_dt).days
            lines.append(f"| {date} | {days}d | {name} | {forecast} | {previous} |")
        scheduled_block = (
            f"\n**Scheduled (next {AHEAD_DAYS} days):** figures are consensus "
            f"forecast vs the prior print; a — row is on the provider's calendar "
            f"but carries no tracked figures\n" + "\n".join(lines) + "\n"
        )
    else:
        scheduled_block = (
            f"\n**Scheduled (next {AHEAD_DAYS} days):** none visible for {curr_date} → "
            f"{ahead_end}. The provider's calendar is anchored to the present and "
            f"scheduled prints exist only as far as it publishes them, so a backtest "
            f"date far from today legitimately shows none.\n"
        )

    # ---- released table -----------------------------------------------------
    if released:
        shown = released[-MAX_ROWS:]
        note = (
            f"\n_(showing the most recent {MAX_ROWS} of {len(released)} releases in the window)_\n"
            if len(released) > MAX_ROWS
            else ""
        )
        lines = [
            "\n| Date | Event | Actual | Forecast | Surprise | Previous |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for date, name, row in shown:
            if row["actual"]:
                actual = row["actual"]
                surprise = _surprise_cell(row["actual"], row["forecast"])
            else:
                # The scheduled date has passed with no published figure —
                # a pending print, not a zero.
                actual = "not yet released"
                surprise = "—"
            lines.append(
                f"| {date} | {name} | {actual} | {row['forecast'] or '—'} | "
                f"{surprise} | {row['previous'] or '—'} |"
            )
        released_block = (
            f"\n**Released (last {look_back_days} days):**\n" + "\n".join(lines) + "\n" + note
        )
    else:
        released_block = (
            f"\n**Released (last {look_back_days} days):** no tracked releases in the "
            f"window ending {curr_date}.\n"
        )

    return header + scheduled_block + released_block
