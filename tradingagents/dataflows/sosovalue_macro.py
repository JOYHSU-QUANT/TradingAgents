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
history FAILED — an unknown one is not retried), stale-served on a fetch
failure up to ``MAX_STALE_DAYS`` with a disclosed age, never written on
failure, and discarded when read-side validation fails. The stale cap is the
family's 14 days, but this feed's forward half is only ``AHEAD_DAYS`` wide, so
a stale serve loses one day of schedule per day of age — the report states how
much of the scheduled window the snapshot's age has eaten rather than letting
an age-shortened schedule read as a quiet fortnight.
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
    _coverage_gap_note,
    _days_stale,
    _humanize_age,
    _is_iso_date,
    _iso_now,
    _plural,
    _plural_days,
    _request,
    _sanitize,
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

# Tracked events whose figures are counts in thousands rather than percent
# readings. The report states this unit outright — a bare "-23" payrolls
# print means -23,000 jobs — so the claim is generated from this tuple rather
# than written into the sentence: editing TRACKED_EVENTS (which the unknown
# bucket's log actively invites) must not leave the report asserting a unit
# for an event it no longer carries. A test pins the subset relationship.
THOUSANDS_EVENTS = ("Nonfarm Payrolls", "Initial Jobless Claims")

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

# Soft bound on the calendar: the live ~2-week window holds a handful of rows
# (6 when captured). A provider that widens its published horizon is benign
# evolution, not a contract break, so a longer calendar keeps the EARLIEST
# day-rows and discloses the drop — failing the vendor instead would serve
# stale until the 14-day cap and then kill the category outright. Only a
# payload large enough to be pathological rather than merely wider trips the
# hard bound.
#
# Earliest, not newest: the provider's calendar is anchored to the present
# (it began one day back when captured), so the rows this report renders —
# curr_date to curr_date + AHEAD_DAYS for a live caller — sit at the HEAD of
# the ascending list. Keeping the tail would discard precisely the fortnight
# the scheduled section reads and leave the empty-schedule branch calling
# that absence legitimate.
MAX_CALENDAR_ROWS = 40
MAX_CALENDAR_ROWS_HARD = 400

# The other axis of the same payload. MAX_CALENDAR_ROWS bounds day-rows; the
# NAMES inside one day-row were unbounded, so a provider broadening
# /macro/events from this US-only shape to a global calendar would parse
# cleanly, be written to the snapshot file at unbounded size, and be
# re-validated in full on every subsequent cache read — the render cap bounds
# the prompt, not the cache. Hard, not soft: like MAX_CALENDAR_ROWS_HARD this
# only trips on a payload that is pathological rather than merely wider (the
# live calendar carries a handful of names per day), and failing the vendor
# routes the call through the stale fallback instead of persisting the bloat.
MAX_CALENDAR_EVENTS_HARD = 2000

# Server-controlled strings are LLM-visible report text, so both get a
# printable-ASCII charset check and a length bound at the parse boundary
# (and again read-side from cache). Live names top out well under these.
MAX_EVENT_NAME_CHARS = 60
MAX_VALUE_CHARS = 24

# Row cap for the rendered released-events table, mirroring the family MAX_ROWS.
MAX_ROWS = 40

# Cache lifetimes: the TTL re-pulls a few times a day (event figures change on
# release days only), the short TTL re-tries a partially-failed history sweep,
# and stale serves are capped and disclosed.
#
# 5h, deliberately NOT the ETF module's 6h: the three SoSoValue modules share
# one 20 req/min key, and this module's refresh is 1 + len(TRACKED_EVENTS)
# requests while the ETF module's is ~15. Equal TTLs make the two caches
# expire together, so a single analyst turn that touches both fires ~25
# requests inside one minute and whichever runs second takes a 429 — the
# category that ends up degraded rotating with tool-call order. An offset TTL
# de-phases the two refreshes for the price of one constant. Do not "tidy"
# this back to 6 to match the family.
CACHE_TTL_HOURS = 5
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


def _parse_calendar(data: list) -> tuple[list[dict], int, int, int]:
    """Validate /macro/events rows into ascending ``{"date", "events"}`` rows.

    Strict only where the data would otherwise be unreadable: an empty
    calendar, a malformed row, or a pathologically large payload raise so the
    router degrades instead of serving a half parsed spine. Shape changes that
    stay readable degrade softly and are counted, because a provider widening
    or reshaping its schedule is evolution, not breakage: an unusable event
    *name* is dropped (``unusable``, mirroring the ETF listing's
    unusable-ticker handling), a repeated date has its event lists merged
    (``duplicated`` counting the extra rows, because a merge can also be the
    provider mislabelling two days as one — benign enough not to fail the
    vendor, not benign enough to apply silently; the per-date de-dupe still
    keeps an event from double-listing), and a calendar longer than
    ``MAX_CALENDAR_ROWS`` keeps its EARLIEST day-rows — the present-anchored
    span the report actually renders — with ``truncated`` counting the
    furthest-out ones dropped.
    """
    if not data:
        raise SoSoValueError("SoSoValue returned an empty macro calendar")
    if len(data) > MAX_CALENDAR_ROWS_HARD:
        raise SoSoValueError(
            f"SoSoValue macro calendar has {len(data)} day-rows "
            f"(> {MAX_CALENDAR_ROWS_HARD}); the API contract may have changed"
        )
    by_date: dict[str, list[str]] = {}
    unusable = 0
    duplicated = 0
    for raw in data:
        if (
            not isinstance(raw, dict)
            or not _is_iso_date(raw.get("date"))
            or not isinstance(raw.get("events"), list)
        ):
            raise SoSoValueError(f"Malformed macro calendar row {_sanitize(repr(raw), limit=200)}")
        if raw["date"] in by_date:
            duplicated += 1
            logger.warning(
                "SoSoValue macro calendar repeats %s; merging the day's event "
                "lists and disclosing it — the provider may have split one day "
                "across rows, or mislabelled another day as this one",
                raw["date"],
            )
        names = by_date.setdefault(raw["date"], [])
        for name in raw["events"]:
            if not _is_valid_event_name(name):
                unusable += 1
                logger.warning(
                    "SoSoValue macro calendar entry %.120r has no usable event name; skipping it",
                    name,
                )
            elif name not in names:
                names.append(name)
    rows = [{"date": date, "events": by_date[date]} for date in sorted(by_date)]
    truncated = max(0, len(rows) - MAX_CALENDAR_ROWS)
    if truncated:
        # Keep the HEAD: the provider's calendar is anchored to the present, so
        # the span the scheduled section renders (curr_date forward, for a live
        # caller) sits at the start of the ascending list. Dropping the tail
        # costs only rows further out than this report ever reaches.
        rows = rows[:MAX_CALENDAR_ROWS]
        logger.warning(
            "SoSoValue macro calendar returned %d day-rows (> %d); keeping the "
            "%d earliest and disclosing the drop",
            truncated + MAX_CALENDAR_ROWS,
            MAX_CALENDAR_ROWS,
            MAX_CALENDAR_ROWS,
        )
    total_events = sum(len(r["events"]) for r in rows)
    if total_events > MAX_CALENDAR_EVENTS_HARD:
        raise SoSoValueError(
            f"SoSoValue macro calendar carries {total_events} event names across its "
            f"{len(rows)} kept day-rows (> {MAX_CALENDAR_EVENTS_HARD}); the API "
            f"contract may have changed"
        )
    return rows, unusable, truncated, duplicated


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
            raise SoSoValueError(
                f"Malformed {name!r} history row {_sanitize(repr(raw), limit=200)}"
            )
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
    names, ``calendar_truncated`` the furthest-out day-rows dropped when the provider
    published more than ``MAX_CALENDAR_ROWS``, and ``calendar_duplicated`` the
    repeated day-rows merged into their date). ``histories`` maps
    each successfully-fetched tracked event to its
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
    calendar_truncated: int
    calendar_duplicated: int
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
    # Mirror the parse-side name bound exactly as the day-row cap is mirrored:
    # a file written before this bound existed (or by hand) must cost one
    # refetch rather than be re-validated in full on every read forever.
    if sum(len(r["events"]) for r in calendar) > MAX_CALENDAR_EVENTS_HARD:
        return _reject(f"'calendar' carries more than {MAX_CALENDAR_EVENTS_HARD} event names")
    for key in ("calendar_unusable", "calendar_truncated", "calendar_duplicated"):
        count = payload.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return _reject(f"'{key}' is missing or not a non-negative integer")
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
    calendar, unusable, truncated, duplicated = _parse_calendar(_request("/macro/events", {}))

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
                "SoSoValue macro: rate limit hit on %r (%s); that request and "
                "the %d histories not yet attempted all go to events_failed "
                "(disclosed as incomplete, retried on the short TTL)",
                name,
                e,
                len(skipped) - 1,
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
        # Say which way it went, like the treasuries twin: "could not be
        # fetched" would misdescribe a mass upstream rename, where all nine
        # requests SUCCEEDED and returned empty lists — sending whoever reads
        # this (or the model, once the stale cap is passed and it rides a
        # DATA_UNAVAILABLE line) after a transport problem that never happened.
        raise SoSoValueError(
            f"SoSoValue macro: no usable history for any of the "
            f"{len(TRACKED_EVENTS)} tracked events ({len(events_failed)} failed, "
            f"{len(events_unknown)} unknown to the provider); a schedule without "
            f"figures is not a served signal"
        )
    return {
        "calendar": calendar,
        "calendar_unusable": unusable,
        "calendar_truncated": truncated,
        "calendar_duplicated": duplicated,
        "histories": histories,
        "events_failed": events_failed,
        "events_unknown": events_unknown,
    }


def _snapshot_from(payload: dict, fetched_at: str, stale: bool) -> _MacroSnapshot:
    return _MacroSnapshot(
        calendar=payload["calendar"],
        calendar_unusable=payload["calendar_unusable"],
        calendar_truncated=payload["calendar_truncated"],
        calendar_duplicated=payload["calendar_duplicated"],
        histories=payload["histories"],
        events_failed=payload["events_failed"],
        events_unknown=payload["events_unknown"],
        fetched_at=fetched_at,
        stale=stale,
    )


def _load_snapshot() -> _MacroSnapshot:
    """Return the macro snapshot, via the family's cache/stale discipline.

    Key first (the emergency-disable flip must not wait out a fresh cache);
    a cache younger than its TTL is served as-is (a *failed* history earns the
    shorter ``INCOMPLETE_CACHE_TTL_HOURS`` — an unknown one cannot be healed by
    retrying, so it does not); otherwise fetch and
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
                    f"(> {MAX_STALE_DAYS}-day cap): {_sanitize(e)}"
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
        # Not capped, only flattened: most of this string is the module's own
        # diagnostic, and a foreign requests.RequestException can carry a
        # server-influenced URL into the same LLM-visible line.
        raise wrap_cls(
            f"SoSoValue macro unavailable and no usable cache exists: {_sanitize(e)}"
        ) from e

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
    # Rows on (or one day before) curr_date also count as covered without
    # being scheduled: a print released today renders in the released table,
    # and its calendar entry — which can sit a day later than the history row,
    # or a day earlier, the live-observed skew running both ways — would
    # otherwise re-render as a phantom name-only scheduled row for the same
    # print now that the calendar sweep starts at curr_date inclusive.
    cover_floor = (curr_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    for name in TRACKED_EVENTS:
        for row in snapshot.histories.get(name, ()):
            if curr_date < row["date"] <= ahead_end:
                scheduled.append(
                    (row["date"], name, row["forecast"] or "—", row["previous"] or "—")
                )
                covered.setdefault(name, []).append(datetime.strptime(row["date"], "%Y-%m-%d"))
            elif cover_floor <= row["date"] <= curr_date:
                covered.setdefault(name, []).append(datetime.strptime(row["date"], "%Y-%m-%d"))
    # Calendar entries ride along name-only unless a figures row for the SAME
    # print is already shown. "Same print" is per (name, date +/- 1 day) —
    # the calendar date sits a day off its history row for some events
    # (live-observed for CPI) — and deliberately NOT per name alone: a weekly
    # event (Initial Jobless Claims) has two or three prints inside the
    # 14-day window, and a name-keyed dedupe would silently swallow every
    # occurrence after the first, under-stating the very event risk this
    # report exists to surface.
    #
    # From curr_date INCLUSIVE, not the day after (user decision): an event
    # scheduled for today is the most decision-relevant row this report can
    # carry, and it reaches the reader through no other path — the released
    # table is built from tracked histories alone, so a calendar-only name
    # dated today used to vanish from the report entirely while the intraday
    # caveat still promised "a figure shown below as not yet released". This
    # inclusive start is also what makes ``cover_floor``'s day of slack bite:
    # a tracked print whose history row sits on curr_date - 1 while its
    # calendar entry sits on curr_date is now one match away from being
    # double-listed.
    for cal in snapshot.calendar:
        if curr_date <= cal["date"] <= ahead_end:
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
    # The intraday caveat exists for anything dated today, published or not:
    # the "not yet released" case is exactly when a reader most needs telling
    # that this feed carries no time-of-day, because the figure may already be
    # public. Calendar-only entries for today count too — they never reach the
    # released rows.
    same_day = any(d == curr_date for d, _n, _row in released) or any(
        cal["date"] == curr_date and cal["events"] for cal in snapshot.calendar
    )
    # Served-history-depth honesty, the counterpart of the treasuries module's
    # ``shallow`` note and the ETF module's window clamp: the provider serves
    # at most HISTORY_LIMIT rows per event, so 100 rows reach 8+ years for a
    # monthly event but only ~2 for a weekly one. A window that outruns an
    # event's served depth loses its prints with nothing else in the report
    # saying so — events_failed/events_unknown are both empty in that case, so
    # the coverage-gap note cannot cover it either.
    shallow = sorted(
        name for name, rows in snapshot.histories.items() if rows[0]["date"] > window_start
    )
    # One union for both empty-section notes: _coverage_gap_note centralizes
    # the sentence precisely so a new failure bucket cannot be wired into one
    # branch and forgotten in the other — building the input twice would hand
    # that drift straight back.
    missing_events = set(snapshot.events_failed) | set(snapshot.events_unknown)
    shallow_note = (
        f"\n_The provider's served history for {', '.join(shallow)} starts inside this "
        f"window (at most {HISTORY_LIMIT} rows per event), so earlier prints exist that "
        f"this snapshot cannot show; the window is short for "
        f"{_plural(len(shallow), 'that event', 'those events')}, not empty._\n"
        if shallow
        else ""
    )

    # ---- header ------------------------------------------------------------
    header_lines = ["## US Economic Calendar — scheduled events & releases (SoSoValue)"]

    if snapshot.stale:
        age_str = _humanize_age(snapshot.fetched_at)
        header_lines.append(
            f"_STALE by {age_str}: live refresh failed (network error, rate limit, or an "
            f"API contract break); showing the last cached snapshot (fetched "
            f"{snapshot.fetched_at}). Scheduled dates and figures may be outdated._"
        )
        # A stale macro snapshot loses forward reach one day per day: the
        # provider's calendar is anchored to when it was FETCHED, so the
        # scheduled section narrows as the snapshot ages and is empty by
        # construction near the stale cap, while the report still ships under
        # a calendar's title. The family's 14-day cap is kept (user decision);
        # what the age costs is stated instead of left for the reader to infer.
        reach = (datetime.strptime(snapshot.calendar[-1]["date"], "%Y-%m-%d") - curr_dt).days
        if reach < AHEAD_DAYS:
            extent = (
                "ends on or before this date, so it can contribute no forward schedule at all"
                if reach <= 0
                else f"reaches only {reach} {_plural_days(reach)} past it"
            )
            header_lines.append(
                f"_That age also shortens the schedule below: this snapshot's calendar "
                f"{extent}, against the {AHEAD_DAYS}-day window the scheduled section "
                f"otherwise covers, so read a short or empty schedule as the snapshot's "
                f"age rather than a quiet fortnight._"
            )

    if snapshot.events_failed:
        # Count the events this sentence actually names: ``histories`` is also
        # short by the unknown bucket, which the next paragraph explains, so a
        # histories-based numerator would not add up to the names listed here.
        n = len(snapshot.events_failed)
        header_lines.append(
            f"_Tracked-event coverage incomplete ({n} of {len(TRACKED_EVENTS)} tracked "
            f"events): histories for {', '.join(sorted(snapshot.events_failed))} could "
            f"not be fetched, so their figures are missing below; a calendar mention of "
            f"such an event, if any, appears name-only._"
        )

    if snapshot.events_unknown:
        n = len(snapshot.events_unknown)
        header_lines.append(
            f"_{n} tracked {_plural(n, 'event is', 'events are')} unknown to the "
            f"provider ({', '.join(sorted(snapshot.events_unknown))}): the name was "
            f"renamed or dropped upstream, so {_plural(n, 'its', 'their')} figures are "
            f"missing below (a calendar mention, if any, appears name-only) until the "
            f"tracked-name list is updated in code._"
        )

    if snapshot.calendar_unusable:
        n = snapshot.calendar_unusable
        header_lines.append(
            f"_{n} calendar {_plural(n, 'entry', 'entries')} had no usable event name "
            f"and {_plural(n, 'was', 'were')} skipped._"
        )

    if snapshot.calendar_truncated:
        n = snapshot.calendar_truncated
        header_lines.append(
            f"_The provider published {n} more calendar day-{_plural(n, 'row', 'rows')} "
            f"than this client keeps; the furthest-out {_plural(n, 'was', 'were')} "
            f"dropped, so the calendar span below ends earlier than the provider's own "
            f"and any event beyond it is missing rather than absent._"
        )

    if snapshot.calendar_duplicated:
        # The count is extra ROWS, which may be one date repeated many times
        # or many dates each repeated once — the wording must fit both.
        n = snapshot.calendar_duplicated
        header_lines.append(
            f"_The provider sent {n} calendar day-{_plural(n, 'row', 'rows')} whose date "
            f"was already in the payload; {_plural(n, 'it was', 'each was')} merged into "
            f"that date. Usually the provider split a day's schedule across rows, but it "
            f"can also mean another day's events were labelled with the wrong date — treat "
            f"scheduled dates in this report with extra care._"
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
    thousands = [n for n in TRACKED_EVENTS if n in THOUSANDS_EVENTS]
    thousands_clause = (
        f", while {' and '.join(thousands)} "
        f"{_plural(len(thousands), 'is a count', 'are counts')} in THOUSANDS (an actual "
        f"of -23 means -23,000)"
        if thousands
        else ""
    )
    header_lines.append(
        f"_Units are the provider's own and differ by row: percent readings carry "
        f"'%'{thousands_clause}, and a K/M/B/T suffix is the provider's own magnitude "
        f"letter. Surprise is actual minus forecast in that same row's unit ('pp' = "
        f"percentage points, a bare number or magnitude letter = the same units as the "
        f"row); 'n/a' means the two sides could not be subtracted (one did not parse, or "
        f"they carry different units) and '—' that nothing is published yet, neither "
        f"means a surprise of zero. The sign says only whether the print beat or missed "
        f"consensus, NOT whether that is bullish — a hot inflation print and a hot "
        f"payrolls print push risk appetite in opposite directions._"
    )
    if same_day:
        header_lines.append(
            f"_This feed carries no time-of-day, so anything dated {curr_date} may print "
            f"at any hour of that day: a figure shown below as not yet released can "
            f"already be public, and one shown with a value may postdate an intraday "
            f"decision time._"
        )
    header_lines.append(
        "_Treat event risk as a regime / risk modifier for sizing and timing, not a "
        "directional trade signal._"
    )

    cal_span = f"{snapshot.calendar[0]['date']} → {snapshot.calendar[-1]['date']}"
    header_lines.append(
        f"- Source: SoSoValue OpenAPI (US macro) | Snapshot fetched "
        f"{snapshot.fetched_at} | Provider calendar covers {cal_span}; the released "
        f"figures below come from per-event histories that reach further back | "
        f"Tracked: {', '.join(TRACKED_EVENTS)} | Window ending {curr_date}"
    )
    header = "\n\n".join(header_lines) + "\n"

    # ---- scheduled table ----------------------------------------------------
    if scheduled:
        # Keep the NEAREST rows. Nothing bounds names-per-day at the parse
        # boundary (MAX_CALENDAR_ROWS bounds day-rows, not the events inside
        # one), so a provider that broadened /macro/events from this US-only
        # shape to a global calendar would otherwise pour every name of every
        # day straight into the prompt; and a fortnight of event risk is read
        # front to back, so the near rows are the ones worth keeping.
        shown_scheduled = scheduled[:MAX_ROWS]
        sched_note = (
            f"\n_(showing the {MAX_ROWS} nearest of {len(scheduled)} scheduled rows "
            f"in the window)_\n"
            if len(scheduled) > MAX_ROWS
            else ""
        )
        lines = ["\n| Date | In | Event | Forecast | Previous |", "| --- | --- | --- | --- | --- |"]
        for date, name, forecast, previous in shown_scheduled:
            days = (datetime.strptime(date, "%Y-%m-%d") - curr_dt).days
            when = "today" if days == 0 else f"{days}d"
            lines.append(
                f"| {date} | {when} | {_sanitize(name)} | {_sanitize(forecast)} "
                f"| {_sanitize(previous)} |"
            )
        scheduled_block = (
            f"\n**Scheduled ({curr_date} and the next {AHEAD_DAYS} days):** figures are "
            f"consensus forecast vs the prior print; 'In' counts calendar days from "
            f"{curr_date}; a row showing — for both figures is either a calendar entry "
            f"outside the tracked list or a tracked print whose figures the provider "
            f"has not filed\n" + "\n".join(lines) + "\n" + sched_note
        )
    else:
        # Why the schedule is empty is NOT always benign: the provider's
        # calendar is present-anchored, so an empty forward window is expected
        # only when it still reaches past curr_date. A calendar that ends on or
        # before curr_date means it stopped publishing forward — a coverage
        # failure that must not be narrated as a quiet fortnight.
        cal_end = snapshot.calendar[-1]["date"]
        # Staleness and truncation are tested FIRST, ahead of the calendar's
        # own end date: both mean this snapshot is not the calendar the
        # provider published for this date, so neither the provider-blaming
        # branch nor the benign one may speak. A stale calendar ending before
        # curr_date has aged out of its forward reach — saying the provider
        # "is publishing no forward schedule" would blame the source for the
        # snapshot's age.
        if snapshot.stale or snapshot.calendar_truncated:
            why = (
                f"The provider's calendar in this snapshot ends {cal_end}, but the "
                f"snapshot does not hold a complete forward view of what the provider "
                f"publishes for this date (see the caveats above), so read the empty "
                f"schedule as an artefact of the snapshot rather than a quiet window."
            )
        elif cal_end <= curr_date:
            why = (
                f"The provider's calendar ends {cal_end}, on or before this date, so it "
                f"is publishing no forward schedule in this snapshot — read the empty "
                f"schedule as missing coverage, not as a fortnight without events."
            )
        else:
            # Not "a date far from the fetch date": that is only one of the two
            # ways this branch is reached, and it is false whenever curr_date
            # IS the fetch date and the calendar's day-rows simply carry no
            # names in the window.
            why = (
                f"The provider's calendar reaches {cal_end} and is anchored to when this "
                f"snapshot was fetched, so this is either a window that genuinely carries "
                f"no scheduled entries, or a {curr_date} sitting far from that fetch date."
            )
        # The window can also be empty because tracked histories are missing —
        # for a historical curr_date the calendar cannot reach back, so the
        # scheduled rows can ONLY come from histories. Same disclosure the
        # released branch already makes.
        gap = _coverage_gap_note(missing_events, "unavailable", "nothing is scheduled")
        scheduled_block = (
            f"\n**Scheduled ({curr_date} and the next {AHEAD_DAYS} days):** none visible "
            f"from {curr_date} through {ahead_end}. {why}" + (f" {gap}" if gap else "") + "\n"
        )

    # ---- released table -----------------------------------------------------
    if released:
        shown = released[-MAX_ROWS:]
        # The rows are heterogeneous: a scheduled date that has passed with no
        # figure sits here too, so calling the total "releases" would overstate
        # what published. Split the count instead of hiding the mix.
        pending = sum(1 for _d, _n, row in released if not row["actual"])
        note = (
            f"\n_(showing the most recent {MAX_ROWS} of {len(released)} rows in the window)_\n"
            if len(released) > MAX_ROWS
            else ""
        )
        # "scheduled on or before", not "past": a print dated curr_date itself
        # is pending without being late.
        mix = (
            f"{len(released) - pending} published, {pending} scheduled on or before "
            f"{curr_date} with no figure yet"
            if pending
            else f"{len(released)} published"
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
                f"| {date} | {_sanitize(name)} | {_sanitize(actual)} "
                f"| {_sanitize(row['forecast'] or '—')} | {surprise} "
                f"| {_sanitize(row['previous'] or '—')} |"
            )
        released_block = (
            f"\n**Released (last {look_back_days} days):** {mix}\n" + "\n".join(lines) + "\n" + note
        )
    else:
        # An empty window can be the calendar being quiet OR the tracked events
        # this snapshot never fetched; the header discloses the gap separately,
        # so point back at it rather than letting "no releases" read as "nothing
        # happened".
        gap = _coverage_gap_note(missing_events, "unavailable", "nothing printed")
        released_block = (
            f"\n**Released (last {look_back_days} days):** no tracked releases in the "
            f"window ending {curr_date}." + (f" {gap}" if gap else "") + "\n"
        )

    # ``shallow_note`` is unconditional, so it rides the return rather than
    # being appended in each released branch — a third branch cannot forget it.
    return header + scheduled_block + released_block + shallow_note
