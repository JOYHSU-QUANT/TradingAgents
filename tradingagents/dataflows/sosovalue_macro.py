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
  calendar uses, serves deep history (2018+ for CPI) with DATES newest-first
  — but two prints sharing one date arrive OLDEST-first (NFP's captured
  2025-12-16 pair, whose ``previous`` chain settles the direction) — under a
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
back). Second, every served value — actual included, since macro actuals are
routinely revised — is the provider's *current* figure rather than a
point-in-time snapshot, and the report carries that caveat rather than
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
    _days_unobserved,
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

# Forward horizon (calendar days after curr_date) for the scheduled section,
# sized to the ~2-week window the live calendar serves.
#
# It is NOT a claim that the two sources the section merges cover the same
# span: the deep histories reach the full 14 days, while the calendar's last
# EVENT-BEARING day-row sat 13 days out in the live capture — because the feed
# emits a day-row only where it has events, so the measured reach falls short
# whenever NO day-row at or beyond day 14 carries any (not merely when day 14
# itself is quiet: the reach is measured off the last dated row globally, which
# a wider horizon or a historical curr_date can push well past the window).
# The reach note below therefore fires on ordinary serves and is worded not to
# resolve which of the two causes it is.
AHEAD_DAYS = 14

# Default trailing window for the released section when the caller does not
# specify one; mirrors the family default.
DEFAULT_LOOKBACK_DAYS = 30

# The documented per-request row cap (values above are silently clamped).
# 100 rows is 8+ years of a monthly event and ~2 years of a weekly one —
# deep enough for any backtest window this report serves.
HISTORY_LIMIT = 100

# The other unbounded payload axis, same reasoning as MAX_CALENDAR_EVENTS_HARD
# below: HISTORY_LIMIT is a request PARAMETER the server clamps, not a bound
# this client enforces, so a provider that stops honouring it would write an
# unbounded snapshot file that every subsequent cache read re-validates in
# full — the render caps bound the prompt, not the cache. Hard rather than
# soft, and set far above the served depth, so only a payload that is
# pathological rather than merely deeper trips it; failing routes the call
# through the stale fallback instead of persisting the bloat.
MAX_HISTORY_ROWS_HARD = 1000

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

# Malformed day-rows are logged one line each, but only up to here. A wholly
# reshaped payload can carry MAX_CALENDAR_ROWS_HARD of them, and 400 WARNINGs
# each echoing a 200-character sanitized row buries the rest of the cycle. The
# disclosed count carries the magnitude; the log only has to show the shape.
MAX_MALFORMED_LOG_ROWS = 10

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


def _parse_calendar(data: list) -> tuple[list[dict], int, int, int, int, list[str]]:
    """Validate /macro/events rows into ascending ``{"date", "events"}`` rows.

    Strict only where the data would otherwise be unreadable: an empty
    calendar, a pathologically large payload, or a calendar left with no
    readable day-row at all raise so the router degrades instead of serving a
    half parsed spine. Shape changes that stay readable degrade softly and are
    counted, because a provider widening or reshaping its schedule is
    evolution, not breakage: a malformed day-row is dropped (``malformed``,
    mirroring the treasuries listing's unusable-entry handling — this parser
    runs BEFORE any history request, so failing the whole calendar over one bad
    row would discard all nine tracked-event histories and the released table
    along with the schedule), an unusable event
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
    malformed = 0
    # Dates recovered from rows that were otherwise unreadable. A row whose
    # ``events`` is not a list still carries a perfectly good ``date``, and the
    # report can then name the day it lost instead of printing a bare count. A
    # row that lost its date too contributes nothing here, so the caveat
    # downstream has to read correctly both with and without this set.
    malformed_dates: set[str] = set()
    for raw in data:
        if (
            not isinstance(raw, dict)
            or not _is_iso_date(raw.get("date"))
            or not isinstance(raw.get("events"), list)
        ):
            malformed += 1
            # ``.get``, not ``[...]``: reaching this branch does not imply the
            # key exists — a dict with no ``date`` at all is one of the shapes
            # that lands here, and a subscript would raise a KeyError from
            # outside the vendor taxonomy, straight past the stale fallback.
            if isinstance(raw, dict) and _is_iso_date(raw.get("date")):
                malformed_dates.add(raw["date"])
            # One line per row up to a cap. A wholly reshaped payload can carry
            # MAX_CALENDAR_ROWS_HARD rows, and 400 WARNINGs each echoing 200
            # sanitized characters buries every other line of the cycle. The
            # count in the caveat and in the all-unreadable raise below is what
            # carries the magnitude; the log only has to show the shape.
            if malformed <= MAX_MALFORMED_LOG_ROWS:
                logger.warning(
                    "SoSoValue macro calendar row %s is malformed; dropping it and "
                    "disclosing the drop",
                    _sanitize(repr(raw), limit=200),
                )
            continue
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
    if malformed > MAX_MALFORMED_LOG_ROWS:
        # Emitted once, after the count is final. Inside the loop the last
        # logged line cannot know whether another malformed row follows, so
        # appending "further rows were not logged" there claimed a remainder
        # on a payload holding exactly MAX_MALFORMED_LOG_ROWS of them.
        skipped = malformed - MAX_MALFORMED_LOG_ROWS
        logger.warning(
            "SoSoValue macro calendar had %d further malformed day-%s beyond the first "
            "%d logged; they are counted and disclosed, not logged",
            skipped,
            _plural(skipped, "row", "rows"),
            MAX_MALFORMED_LOG_ROWS,
        )
    if not by_date:
        # Every row unreadable is a contract break, not evolution: there is no
        # calendar spine left to render, and the empty-payload raise above
        # already routes that shape through the stale fallback. Mirrors the
        # treasuries listing, which drops entries but raises on an empty result.
        raise SoSoValueError(
            f"SoSoValue macro calendar has {malformed} "
            f"day-{_plural(malformed, 'row', 'rows')} and "
            f"{_plural(malformed, 'it is not readable', 'none of them is readable')}; "
            f"the API contract may have changed"
        )
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
    # The dates come last and sorted: they are a subset of what ``malformed``
    # counts (a row that lost its date too cannot contribute one), so the count
    # stays the authority on how much was dropped and the dates only name as
    # much of it as survived.
    # Dates the report still carries are subtracted. A malformed row and a good
    # row can share a date — the malformed branch ``continue``s before the
    # by_date merge, so both are processed — and naming that date would tell
    # the reader an event on it is "missing from this report" while the
    # scheduled table lists one three lines below. Measured against the kept
    # rows, not by_date, so a date lost to truncation still counts as lost.
    carried = {r["date"] for r in rows}
    return rows, unusable, truncated, duplicated, malformed, sorted(malformed_dates - carried)


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
    if len(data) > MAX_HISTORY_ROWS_HARD:
        raise SoSoValueError(
            f"SoSoValue served {len(data)} history rows for macro event {name!r} "
            f"(> {MAX_HISTORY_ROWS_HARD}, against a requested limit of {HISTORY_LIMIT}); "
            f"the API contract may have changed"
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
    published more than ``MAX_CALENDAR_ROWS``, ``calendar_duplicated`` the
    repeated day-rows merged into their date, ``calendar_malformed`` the
    day-rows dropped as unreadable and ``calendar_malformed_dates`` those of
    them that still carried a readable date — a subset, never a substitute for
    the count). ``histories`` maps
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
    calendar_malformed: int
    calendar_malformed_dates: list[str]
    histories: dict[str, list[dict]]
    events_failed: list[str]
    events_unknown: list[str]
    fetched_at: str
    stale: bool


def _cache_path() -> str:
    """Path of the single rolling macro snapshot (no per-asset dimension)."""
    return os.path.join(_cache_dir(), "sosovalue_macro.json")


def _valid_history_rows(rows: object) -> bool:
    # The row-count bound is mirrored read-side like every other parse-boundary
    # bound: a file written before it existed (or by hand) must cost one
    # refetch rather than be re-validated in full on every read forever.
    if not (isinstance(rows, list) and rows and len(rows) <= MAX_HISTORY_ROWS_HARD):
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
    # Mirror the parser's per-date de-dupe (``elif name not in names``). The
    # scheduled builder iterates a day-row's names with no dedupe of its own —
    # the ``covered`` guard only suppresses names already shown in the RELEASED
    # table — so a repeat that only a cache file can carry renders the same
    # event twice in the schedule, overstating event risk and spending two of
    # the table's MAX_ROWS slots on one print.
    if any(len(set(r["events"])) != len(r["events"]) for r in calendar):
        return _reject("'calendar' repeats an event name inside one day-row")
    # Mirror the parse-side name bound exactly as the day-row cap is mirrored:
    # a file written before this bound existed (or by hand) must cost one
    # refetch rather than be re-validated in full on every read forever.
    if sum(len(r["events"]) for r in calendar) > MAX_CALENDAR_EVENTS_HARD:
        return _reject(f"'calendar' carries more than {MAX_CALENDAR_EVENTS_HARD} event names")
    for key in (
        "calendar_unusable",
        "calendar_truncated",
        "calendar_duplicated",
        "calendar_malformed",
    ):
        count = payload.get(key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return _reject(f"'{key}' is missing or not a non-negative integer")
    # Mirror the parse-side relationship, not just the type: the dates are the
    # subset of dropped day-rows that still had a readable date, so more dates
    # than drops is impossible and any date at all with zero drops is
    # impossible. Unmirrored, a hand-written file can make the caveat name days
    # nothing was dropped on — the same class of cache-side hole the negative
    # holdings and the duplicate-date checks closed.
    malformed_dates = payload.get("calendar_malformed_dates")
    if not isinstance(malformed_dates, list) or not all(_is_iso_date(d) for d in malformed_dates):
        return _reject("'calendar_malformed_dates' is missing or not a list of ISO dates")
    if len(set(malformed_dates)) != len(malformed_dates):
        return _reject("'calendar_malformed_dates' repeats a date")
    if len(malformed_dates) > payload["calendar_malformed"]:
        return _reject("'calendar_malformed_dates' holds more dates than day-rows were dropped")
    # The parser subtracts dates the report still carries, so a stored date
    # that IS in the calendar is a shape it cannot write. Served, the caveat
    # says an event on that date is "missing from this report" while the
    # scheduled table lists one a few lines below.
    if {r["date"] for r in calendar} & set(malformed_dates):
        return _reject("'calendar_malformed_dates' names a date the calendar still carries")
    # Same cache-mirror family: the parser only drops day-rows once the cap is
    # reached, so a positive count implies a calendar sitting exactly at it.
    # Unmirrored, a hand-edited file prints "the provider published N more
    # calendar day-rows than this client keeps" over a two-row calendar, and
    # forces the empty-schedule branch into its snapshot-artefact wording for a
    # calendar that was never truncated.
    if payload["calendar_truncated"] and len(calendar) != MAX_CALENDAR_ROWS:
        return _reject("'calendar_truncated' is positive but the calendar is not at the row cap")
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
    calendar, unusable, truncated, duplicated, malformed, malformed_dates = _parse_calendar(
        _request("/macro/events", {})
    )

    histories: dict[str, list[dict]] = {}
    events_failed: list[str] = []
    events_unknown: list[str] = []
    rate_limited: SoSoValueRateLimitError | None = None
    # The transport counterpart of ``rate_limited``, kept for the same reason:
    # every RequestException is absorbed per event, so without it an all-failed
    # sweep can only raise a bare SoSoValueError.
    last_network: requests.RequestException | None = None
    # Set when an event fails for a reason no retry can heal — _fetch_one_event
    # returns None only after swallowing a SoSoValueError (a parse/contract
    # break) — so the transport classification below cannot claim a pure
    # outage while a real structural break is in the same sweep.
    structural_failure = False
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
        except requests.RequestException as e:
            last_network = e
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
            structural_failure = True
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
        # The transport sibling of the rate-limit rule directly above. Every
        # RequestException is absorbed per event, so a total outage would
        # otherwise surface here as a bare SoSoValueError — which _load_snapshot
        # classifies as structural breakage and logs at ERROR with a traceback
        # and "the client likely needs a fix", for an outage no code change can
        # heal. Raising the transport class routes it to the warning branch,
        # where the ETF module's network failures already land. Only when the
        # sweep died PURELY of transport: an unknown event or a swallowed parse
        # break means something structural is in the mix, and the generic error
        # below stays the honest answer.
        if last_network is not None and not structural_failure and not events_unknown:
            raise requests.RequestException(
                f"SoSoValue macro: no usable history for any of the "
                f"{len(TRACKED_EVENTS)} tracked events; every attempt failed at the "
                f"transport layer (last: {last_network})"
            ) from last_network
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
        "calendar_malformed": malformed,
        "calendar_malformed_dates": malformed_dates,
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
        calendar_malformed=payload["calendar_malformed"],
        calendar_malformed_dates=payload["calendar_malformed_dates"],
        histories=payload["histories"],
        events_failed=payload["events_failed"],
        events_unknown=payload["events_unknown"],
        fetched_at=fetched_at,
        stale=stale,
    )


def _load_snapshot() -> _MacroSnapshot:
    """Return the macro snapshot, via the family's cache/stale discipline.

    Key first (the emergency-disable flip must not wait out a fresh cache);
    a cache younger than its TTL is served as-is (a *failed* history or a
    *dropped* calendar day-row earns the shorter ``INCOMPLETE_CACHE_TTL_HOURS``
    because a re-fetch can heal both; an unknown event, a dropped name, a
    truncation and a merged duplicate cannot be healed that way, so they do
    not — the TTL site itself argues each case); otherwise fetch and
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
        # Every degradation bucket is named here and every exclusion is argued
        # here, following the ETF module's TTL site — the enumeration is what
        # stops a new bucket being wired into the report and forgotten by the
        # refresh. Shortened for the two a re-fetch can actually heal:
        #   events_failed  — 429 / network / breaker, transient by definition.
        #   calendar_malformed — a dropped day-row costs a whole DATE, and
        #     nothing says the row is permanently bad; a null events array or a
        #     half-written row in a present-anchored calendar is exactly what an
        #     hour later returns clean. Left on the full TTL it is re-served for
        #     five hours with a hole the report has to keep disclosing.
        # NOT shortened, each for its own reason:
        #   events_unknown — proven permanent (200 + empty list = renamed
        #     upstream); only a TRACKED_EVENTS edit heals it.
        #   calendar_unusable — a name failing the charset filter is
        #     deterministic, same as the ETF listing's unusable tickers.
        #   calendar_truncated — deterministic: a re-fetch re-truncates
        #     identically, and the kept head is the span this report renders.
        #   calendar_duplicated — the merge loses no day-row and no name; it
        #     costs date confidence, not data.
        ttl = (
            INCOMPLETE_CACHE_TTL_HOURS
            if (cached["events_failed"] or cached["calendar_malformed"])
            else CACHE_TTL_HOURS
        )
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

    Raises:
        SoSoValueError: if ``curr_date`` is not a yyyy-mm-dd date or
            ``look_back_days`` is not an integer (a caller's malformed argument
            is reported as this vendor's error class rather than left to escape
            as a raw ``ValueError``/``TypeError``, which the router would render
            into the model-visible sentinel with the argument echoed
            verbatim); or if the live fetch fails — including on a pure network
            error — and no cache is usable, because none exists or the newest
            is past ``MAX_STALE_DAYS``.
        SoSoValueRateLimitError: same no-usable-cache case when the sweep died
            on a 429; the type is preserved through the wrap so the router can
            classify by behaviour.
        SoSoValueNotConfiguredError: if the key is unset or rejected. Never
            absorbed by the stale fallback, so the emergency-disable flip takes
            effect on the next call.

    A degraded serve is what happens INSTEAD of raising only while a usable
    cache exists; the disabled/unavailable note the caller sees in the other
    cases is produced by ``route_to_vendor`` catching these, downstream of the
    raise rather than in place of it.
    """
    # Guarded like curr_date below, and for the same reason: look_back_days is
    # a caller-supplied tool argument, and a non-int reaches ``<=`` first,
    # where the TypeError escapes the vendor taxonomy into the router's bare
    # except and is rendered into the model-visible sentinel. bool is rejected
    # explicitly even though it passes isinstance(x, int) — True would silently
    # mean a one-day window rather than the default.
    if look_back_days is not None and (
        isinstance(look_back_days, bool) or not isinstance(look_back_days, int)
    ):
        raise SoSoValueError(
            f"look_back_days {_sanitize(repr(look_back_days), limit=120)} is not an integer"
        )
    if look_back_days is None or look_back_days <= 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Normalise curr_date BEFORE any lexical date comparison: strptime accepts
    # non-zero-padded input ("2026-6-5"), which compares wrong against
    # canonical ISO row dates and would silently admit future rows.
    #
    # Guarded like deribit's twin: curr_date is an LLM-written tool argument,
    # and strptime's own ValueError echoes it verbatim ("time data '...' does
    # not match format"). ValueError/TypeError are outside the vendor taxonomy,
    # so that message escapes to the router's bare except and is rendered into
    # the model-visible DATA_UNAVAILABLE sentinel unsanitized and unbounded —
    # a caller's malformed argument dressed up as a SoSoValue outage.
    try:
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    except (ValueError, TypeError) as e:
        # The exception's own message only repeats curr_date and the format
        # string, so echoing it prints the caller's argument a SECOND time —
        # and _sanitize's ``limit`` is documented for isolated fragments, never
        # for flattening a whole exception message. The value is echoed once,
        # capped, with the type name carrying what the TypeError case would
        # otherwise have contributed.
        detail = _sanitize(curr_date, limit=200)
        raise SoSoValueError(
            f"curr_date {detail!r} ({type(curr_date).__name__}) is not a yyyy-mm-dd date"
        ) from e
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
    # Per-event view of what this curr_date can actually see, mirroring the
    # treasuries module's ``visible_by_company``. A fetched event is not
    # automatically a contributing one: for a curr_date older than the served
    # depth every print postdates the window, and the event vanishes from both
    # tables while every failure bucket stays empty.
    visible_by_event: dict[str, list[dict]] = {}
    no_disclosure: list[str] = []
    for name, rows in snapshot.histories.items():
        visible = [row for row in rows if row["date"] <= curr_date]
        if visible:
            visible_by_event[name] = visible
        else:
            no_disclosure.append(name)

    # Served-history-depth honesty, the counterpart of the treasuries module's
    # ``shallow`` note and the ETF module's window clamp: the provider serves
    # at most HISTORY_LIMIT rows per event, so 100 rows reach 8+ years for a
    # monthly event but only ~2 for a weekly one. A window that outruns an
    # event's served depth loses its prints with nothing else in the report
    # saying so — events_failed/events_unknown are both empty in that case, so
    # the coverage-gap note cannot cover it either.
    # ``len(rows) >= HISTORY_LIMIT`` is what makes the claim true: only a
    # history the per-request cap actually truncated can be hiding earlier
    # prints. A short history that simply starts inside the window — a newly
    # published series, or one the provider has only lately begun serving —
    # has nothing older to show, and asserting otherwise would invent a gap.
    # Driven off what this curr_date can actually see, not the raw history:
    # rows are ascending, so the visible slice is a prefix, but an event whose
    # every print POSTDATES curr_date has no visible slice at all. Measuring
    # the start date on the raw history would call that event's window "short,
    # not empty" when it is total — the opposite correction. Depth still comes
    # from the full history: the per-request cap drops the OLDEST rows,
    # independently of this filter. Same split as the treasuries twin.
    shallow = sorted(
        name
        for name, visible in visible_by_event.items()
        if len(snapshot.histories[name]) >= HISTORY_LIMIT and visible[0]["date"] > window_start
    )
    # One union for both empty-section notes: _coverage_gap_note centralizes
    # the sentence precisely so a new failure bucket cannot be wired into one
    # branch and forgotten in the other — building the input twice would hand
    # that drift straight back. Every bucket that contributed nothing, not just
    # the failures: an event fetched whole whose every print postdates
    # curr_date leaves the same hole a failed fetch does.
    missing_events = set(snapshot.events_failed) | set(snapshot.events_unknown) | set(no_disclosure)
    shallow_note = (
        f"\n_The provider's served history for {', '.join(shallow)} starts inside this "
        f"window (at most {HISTORY_LIMIT} rows per event), so earlier prints exist that "
        f"this snapshot cannot show; the window is short for "
        f"{_plural(len(shallow), 'that event', 'those events')}, not empty._\n"
        if shallow
        else ""
    )

    # ---- header ------------------------------------------------------------
    # The day-rows that can still contribute: a row whose every name was
    # dropped as unusable survives with an empty list, and counting it would
    # overstate the calendar's reach. Shared by EVERY site that measures the
    # calendar's extent — the reach line, the window-overlap disclosure,
    # the Source span and the empty-schedule narration — so they cannot drift;
    # the last of those four was the one site this rule had already been
    # written down for and not applied to. A new such site belongs here too.
    cal_dated = [r["date"] for r in snapshot.calendar if r["events"]]
    # Does the calendar contribute anything at all to THIS window? Its only
    # consumer is the zero-coverage note below, so emptiness is all that is
    # read off it — the two PARTIAL-coverage notes are bounded by the
    # calendar's own endpoints instead (`cal_dated[0]` for the front gap,
    # `cal_dated[-1]` for the reach), because a dateless day inside that span
    # is one the provider covered and simply had nothing to list.
    in_window = [d for d in cal_dated if curr_date <= d <= ahead_end]
    # "This snapshot does not hold the whole calendar the provider published
    # for this date." One boolean, two consumers — the bracketed
    # covered-and-quiet note below and the empty-schedule narration further
    # down — because both make a claim that is only safe on a complete
    # snapshot, and a bucket wired into one and forgotten in the other is
    # exactly how the two came to contradict each other before.
    # calendar_duplicated is deliberately NOT a member: a merge loses no
    # day-row and no name, it only makes a DATE less certain.
    # The fetch-date skew (blind > 0) is deliberately not a member either, and
    # it is a different class rather than an oversight: those days are not
    # missing from what the provider published for this date, the snapshot
    # simply predates the date. Folding it in would also make the
    # empty-schedule chain's "a curr_date sitting far from that fetch date"
    # arm unreachable — it is the alternative the gate would swallow. Each
    # consumer that needs currency takes ``blind`` as its own separate term.
    snapshot_incomplete = bool(
        snapshot.stale
        or snapshot.calendar_truncated
        or snapshot.calendar_unusable
        or snapshot.calendar_malformed
    )
    header_lines = ["## US Economic Calendar — scheduled events & releases (SoSoValue)"]

    if snapshot.stale:
        age_str = _humanize_age(snapshot.fetched_at)
        header_lines.append(
            f"_STALE by {age_str}: live refresh failed (network error, rate limit, or an "
            f"API contract break); showing the last cached snapshot (fetched "
            f"{snapshot.fetched_at}). Scheduled dates and figures may be outdated._"
        )
    # Hoisted above the forward-reach note because BOTH halves of the
    # arithmetic need it: the backward note prints the blind tail, and the
    # forward note has to know the calendar was anchored to an earlier day
    # before it can claim the shortfall's cause is unknowable.
    fetched_day = snapshot.fetched_at[:10]
    blind = _days_unobserved(snapshot.fetched_at, curr_dt)
    # Forward reach, and NOT gated on staleness — the sibling of the backward
    # unobserved-tail sentence below, un-gated for the same reason. The
    # provider's calendar is anchored to when it was FETCHED, so a snapshot
    # fetched on an earlier day already reaches less far than the scheduled
    # section's title claims, stale or not; at a 5h TTL that is an ordinary
    # serve. Staleness only makes it worse (a day of reach per day of age), and
    # the STALE line above already attributes that. The family's 14-day cap is
    # kept (user decision); what the shortfall costs is stated instead of left
    # for the reader to infer. Measured from the last day-row that actually
    # carries names: a row whose every name was dropped as unusable survives
    # with an empty list, and counting it would overstate what the calendar can
    # still contribute.
    reach = (datetime.strptime(cal_dated[-1], "%Y-%m-%d") - curr_dt).days if cal_dated else None
    if reach is None or reach < AHEAD_DAYS:
        if reach is None:
            extent = "names no event on any day-row it carries"
        elif reach < 0:
            extent = "ends before this date, so it can contribute no schedule at all"
        elif reach == 0:
            # Not "no forward schedule": the calendar sweep starts at
            # curr_date INCLUSIVE, so a calendar ending today still
            # contributes today's entries — the table below can show rows
            # this sentence would otherwise deny.
            extent = "ends on this date, so it can contribute today's entries but nothing beyond"
        else:
            extent = f"reaches only {reach} {_plural_days(reach)} past it"
        # The subject is the CALENDAR's reach, never "the scheduled section is
        # short": that section is fed by forward-dated tracked-history rows as
        # well as calendar rows, and histories reach ahead_end independently of
        # the calendar — so a table full to the window edge can sit directly
        # under this sentence. What is actually true past the calendar's last
        # dated entry is narrower: only tracked events can still appear there.
        # With no dated entry at all that anchor points at nothing, so the
        # stronger, unanchored form is used instead.
        # The cause of a thin tail is left OPEN, but only where it genuinely
        # is. The feed emits a day-row only where it has events, so "the
        # provider's horizon stopped at cal_dated[-1]" and "the provider
        # covered the rest and had nothing to list" produce byte-identical
        # payloads — naming the first as fact is the same
        # absence-is-not-uncoverage error the front-gap note was corrected for.
        # It matters here because the live capture's last event-bearing day
        # sits 13 days out, so this note fires on an ordinary serve rather than
        # on an edge case.
        #
        # Two states DO name the cause, and "cannot say" contradicts a caveat
        # printed in this same header whenever one of them holds:
        #   - this client dropped calendar content of its own. Truncation is
        #     the sharpest case (it keeps the HEAD, so the rows it drops are
        #     exactly the furthest-out ones), but a malformed day-row or a
        #     day-row whose every name was dropped can also sit in the stretch
        #     — and the bucket caveats name that drop outright. They are
        #     appended BELOW this line, so the pointer says "below": every
        #     other "see the caveats above" in this module lives in the
        #     scheduled block, which renders after the whole header.
        #   - the snapshot was fetched before curr_date. A calendar is anchored
        #     to its fetch, so one fetched `blind` days ago reaches that much
        #     less far forward than one fetched today. That is a FACT about the
        #     snapshot, and it is all this arm may claim: it does not establish
        #     that the fetch date explains the shortfall, which can be far
        #     larger than blind (a quiet calendar three days out, read one day
        #     after the fetch, is 11 days short of the window for reasons the
        #     skew cannot account for). So the arm ADDS the fact and keeps the
        #     "cannot say" it would otherwise have replaced — an earlier draft
        #     of this branch asserted the cause outright and was false on the
        #     ordinary first-read-after-midnight serve.
        #     The condition is blind > 0, NOT stale: a snapshot fetched at
        #     00:30 and read at 06:00 is stale at a 5h TTL yet was fetched
        #     today, so its horizon is today's and nothing is owed.
        # Order is client-loss first: it is the more specific claim, and when
        # both hold it is the one the reader can act on.
        stretch = "the window" if reach is None else "that stretch"
        if snapshot.calendar_truncated or snapshot.calendar_malformed or snapshot.calendar_unusable:
            cause = (
                f"and this snapshot dropped calendar content of its own (see the caveats "
                f"below), so {stretch} may be neither unpublished nor quiet"
            )
        elif blind is not None and blind > 0:
            cause = (
                f"and this snapshot cannot say whether {stretch} is unpublished or simply "
                f"quiet — though its calendar was fetched {blind} {_plural_days(blind)} "
                f"before this date, so it reaches that much less far than one fetched today"
            )
        else:
            cause = f"and this snapshot cannot say whether {stretch} is unpublished or simply quiet"
        scope = (
            f"so the schedule below can only carry the {len(TRACKED_EVENTS)} tracked events at all"
            if reach is None
            else f"so beyond the calendar's last dated entry the schedule below can only "
            f"carry the {len(TRACKED_EVENTS)} tracked events"
        )
        consequence = f"{scope}, {cause}"
        header_lines.append(
            f"_Measured from {curr_date}, this snapshot's calendar {extent}, short of the "
            f"{AHEAD_DAYS}-day window the scheduled section covers — {consequence}._"
        )
    # The backward half of the same arithmetic, and NOT gated on staleness: the
    # released table is labelled by curr_date, but no snapshot can carry what
    # the provider published after it was fetched, so the most recent stretch
    # of that window is empty by construction rather than quiet — the reading
    # most likely to be taken as "nothing printed". A within-TTL snapshot has
    # exactly the same blind tail whenever it was fetched on an earlier day,
    # which at a 5h TTL is an ordinary serve, not an edge case. The guard is
    # the fact itself (blind > 0), so on the production default
    # curr_date == fetched_at[:10] the sentence stays silent.
    if blind is not None and blind > 0:
        # Clamped to the window: look_back_days is a caller-supplied tool
        # argument, so an age larger than it would claim a tail longer than
        # the window it describes ("the most recent 10 days" of a 5-day
        # window). When the age swallows the window the true statement is
        # the stronger one, not a truncated version of the weaker one.
        # Strict >, not >=: the window is inclusive at both ends, so it
        # spans look_back_days + 1 days while the unobserved stretch
        # (fetched_day, curr_date] is exactly blind. At equality
        # fetched_day IS window_start, whose own prints are observable —
        # the whole-window claim needs the fetch to precede the window.
        unseen = min(blind, look_back_days)
        extent = (
            "the whole of that window is"
            if blind > look_back_days
            else f"the most recent {unseen} {_plural_days(unseen)} of that window "
            f"{_plural(unseen, 'is', 'are')}"
        )
        # "empty of published figures", not "empty": a forward-dated row
        # carrying only a forecast was already in the snapshot at fetch
        # time and still renders here as "not yet released", so the
        # stretch is not empty of ROWS — only of prints.
        header_lines.append(
            f"_The released window below is bounded by the fetch date: this snapshot "
            f"cannot carry anything published after {fetched_day}, so {extent} empty of "
            f"published figures by construction, not quiet._"
        )

    if not in_window:
        # With at least one dated row but none inside the window, exactly three
        # states are reachable — the calendar ends before the window, starts
        # after it, or BRACKETS it — because any dated row in
        # [curr_date, ahead_end] would have landed in in_window (which also
        # makes the two comparisons below strict: equality at either endpoint
        # is unreachable here). A fourth state, no dated row at all, takes the
        # else with the rest.
        #
        # Only the bracketed case tells us the provider covered these days. The
        # feed emits a day-row only where it has events, so a window sitting
        # inside [cal_dated[0], cal_dated[-1]] with no row of its own is
        # covered and quiet, and an off-list event there is genuinely ABSENT.
        # Saying "missing rather than absent" there contradicts both the Source
        # line's own span and the benign empty-schedule branch below, which
        # calls the same state a window that genuinely carries no entries.
        # This is the front-gap correction applied at the other end of the same
        # ambiguity: absence of a row is not absence of coverage.
        #
        # Gated on snapshot_incomplete for the same reason the empty-schedule
        # narration is, and it shares the boolean so the two cannot drift: the
        # bracketing proves the PROVIDER covered these days, not that this
        # snapshot still holds what it published for them. The tightest case is
        # calendar_unusable, which is also the most natural way into this
        # branch — a day-row INSIDE the window whose every name was dropped
        # survives with an empty list, is filtered out of cal_dated, and so
        # empties in_window while the rows either side of it remain. Calling
        # that "genuinely quiet" denies precisely the off-list event this
        # client dropped, two lines above the caveat that admits dropping it.
        #
        # calendar_duplicated is checked HERE but is not a member of
        # snapshot_incomplete, and the asymmetry is the point: in the
        # empty-schedule chain duplication has its own branch to speak in, so
        # folding it into the shared boolean would make that branch unreachable
        # and assert an incompleteness the merge disproves. This note has no
        # such alternative — its only other arm is the conservative "missing
        # rather than absent" — and a merge can put an in-window entry on an
        # out-of-window date, which is exactly what "absent from this window"
        # would deny. Same bucket, opposite handling, because the two consumers
        # offer different alternatives.
        #
        # ``not scheduled`` is part of the gate, not a nicety. The calendar is
        # only ONE of the two feeds behind the table below; forward-dated
        # tracked-history rows reach ahead_end on their own, and with
        # in_window empty every row in ``scheduled`` came from a history. So
        # without this guard "these days ... are genuinely quiet" can print
        # three lines above a table listing the events that make them noisy —
        # the same wrong-subject error the reach note's own comment warns
        # about, made here instead of avoided.
        #
        # "an event this feed carries", not "an event outside the N tracked
        # ones": FOMC is outside the tracked list and the unconditional caveat
        # further down says this feed carries no Fed decision AT ALL, so their
        # absence is a source gap, not a quiet Fed. The wider phrasing licensed
        # exactly the reading that caveat forbids.
        #
        # ``not blind`` is the currency half of the same claim and is a term of
        # its own, not a snapshot_incomplete member (see that boolean): a
        # calendar fetched yesterday can bracket this window and still predate
        # an entry the provider added today, so "genuinely quiet" would assert
        # currency the snapshot does not have. On the production default
        # curr_date == fetched_at this costs nothing.
        # Truthiness, so a NEGATIVE blind suppresses the claim too — a
        # backtest whose curr_date precedes the fetch. That direction is
        # over-conservative rather than wrong (a calendar fetched later covers
        # the window at least as well), and it is left that way deliberately:
        # the claim is the strongest one this header makes, so the arm that
        # declines it is the safe place to be imprecise.
        if (
            cal_dated
            and not snapshot_incomplete
            and not snapshot.calendar_duplicated
            and not scheduled
            and not blind
            and cal_dated[0] < curr_date
            and cal_dated[-1] > ahead_end
        ):
            header_lines.append(
                f"_The provider's calendar spans this whole window ({cal_dated[0]} → "
                f"{cal_dated[-1]}) and lists no entry between {curr_date} and {ahead_end}: "
                f"these days were covered and are genuinely quiet, so an event this feed "
                f"carries is absent from this window rather than missing from it._"
            )
        else:
            # Unchanged wording, for every other state: the window is not
            # covered, or the calendar carries no dated row at all, or the
            # snapshot is incomplete / its dates are unverified. "Missing
            # rather than absent" is the conservative reading and stays right
            # in all of them. No span is named — one exists in most of these
            # states but naming it would imply the coverage claim this arm is
            # precisely declining to make.
            header_lines.append(
                f"_No calendar entry in this snapshot falls between {curr_date} and "
                f"{ahead_end}, so the schedule below carries only the {len(TRACKED_EVENTS)} "
                f"tracked events: an event outside that list is missing from this window "
                f"rather than absent from it._"
            )
    elif cal_dated[0] > curr_date:
        # Partial coverage, the backward half — bounded by the FIRST day-row
        # the calendar carries names on anywhere, never by the first such day
        # INSIDE the window. Those are different dates, and only the former
        # marks unfetched days: the
        # provider emits a day-row only where it has events, so inside
        # [cal_dated[0], cal_dated[-1]] a dateless day is one the calendar DID
        # cover and simply had nothing to list. The live capture spans 15 days
        # with day-rows on 6, so keying this on in_window[0] would tell the
        # reader an ordinary quiet weekday is unknowable — and contradict the
        # Source line, which prints cal_dated[0] as the real start. Mirrors the
        # reach line, which measures the far end with cal_dated[-1]. Only the
        # front is reported here: a gap past the calendar's end is that line's
        # subject, and saying it twice would put two spans on one hole.
        gap_end = (datetime.strptime(cal_dated[0], "%Y-%m-%d") - timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        blind_days = (datetime.strptime(cal_dated[0], "%Y-%m-%d") - curr_dt).days
        # A one-day gap collapses to the single date: "X → X" reads as a broken
        # range. The arrow is the header's house style for a span (the Source
        # line uses it), and an en-dash between two hyphen-bearing ISO dates is
        # hard to parse. "predates the calendar", not "predate it": the nearest
        # antecedent of "it" is "this window", under which the clause is false —
        # those days OPEN the window rather than predating it.
        span = curr_date if blind_days == 1 else f"{curr_date} → {gap_end}"
        header_lines.append(
            f"_This snapshot's calendar begins {cal_dated[0]}, after this window opens, so "
            f"{span} ({blind_days} {_plural_days(blind_days)}) "
            f"{_plural(blind_days, 'predates', 'predate')} the calendar: over that stretch "
            f"the schedule below carries only the {len(TRACKED_EVENTS)} tracked events, and "
            f"an event outside that list is missing from "
            f"{_plural(blind_days, 'that day', 'those days')} rather than absent from "
            f"{_plural(blind_days, 'it', 'them')}._"
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

    # Unconditional, mirroring the treasuries twin. The coverage-gap notes that
    # also carry this bucket reach the reader only when a SECTION renders
    # nothing, so an event that contributed nothing while both tables are
    # non-empty disappears from the report entirely while the Tracked line
    # above still advertises it — and the shallow note cannot cover it either,
    # since an event with no visible slice is excluded from that check by
    # construction. Deliberately weaker than the treasuries wording ("excluded
    # from every figure"): these events keep their forward-dated rows, so one
    # can still be visible in the scheduled table below.
    if no_disclosure:
        n = len(no_disclosure)
        header_lines.append(
            f"_{n} tracked {_plural(n, 'event has', 'events have')} no print dated on or "
            f"before {curr_date} in this snapshot ({', '.join(sorted(no_disclosure))}): "
            f"every served print for {_plural(n, 'it', 'them')} postdates this date, so "
            f"{_plural(n, 'it contributes', 'they contribute')} no released figure to the "
            f"window below — either the served history stops short of this date, or the "
            f"series had not yet printed by then._"
        )

    if snapshot.calendar_unusable:
        n = snapshot.calendar_unusable
        header_lines.append(
            f"_{n} calendar {_plural(n, 'entry', 'entries')} had no usable event name "
            f"and {_plural(n, 'was', 'were')} skipped._"
        )

    if snapshot.calendar_malformed:
        # Unlike a dropped NAME, a dropped day-row takes its whole date with it,
        # so this bucket costs coverage rather than detail: the calendar this
        # report renders may skip a date the provider did publish, and the
        # quiet-vs-uncovered reasoning the other calendar notes rely on does
        # not hold across it.
        n = snapshot.calendar_malformed
        dates = snapshot.calendar_malformed_dates
        # Name the days, because a bare count cannot be acted on and a row
        # whose only fault was its ``events`` field still carried a usable
        # date. Two phrasings, and neither may claim a row count: the dates are
        # a SET, so three rows all dated the same day contribute one entry, and
        # "N of them dated ..." would then assert that the other two lost their
        # dates when all three had one. Only the equal-length case is provably
        # exhaustive (n distinct, uncarried dates for n rows); everything else
        # says "include" and under-claims on purpose.
        listed = ", ".join(dates)
        named = ""
        if dates:
            named = f" ({listed})" if len(dates) == n else f" (dropped dates include {listed})"
        # Not "the span below": when every dated row is gone the Source line
        # names no span at all and the phrase points at nothing. The claim is
        # about the calendar this report carries, which exists either way.
        # The endpoint clause mirrors the truncation caveat — a dropped row at
        # either end moves the rendered span inward, so the span on the Source
        # line is not the provider's own. It is dropped where the report can
        # rule that out: every drop named, and every named date strictly inside
        # the rendered span, means the endpoints provably did not move. An
        # unnamed drop could have sat anywhere, so it keeps the clause.
        span_moved_possible = not (
            cal_dated and len(dates) == n and all(cal_dated[0] < d < cal_dated[-1] for d in dates)
        )
        span_clause = (
            ", and its span may start later or end earlier than the provider's own"
            if span_moved_possible
            else ""
        )
        header_lines.append(
            f"_{n} calendar day-{_plural(n, 'row', 'rows')} could not be read and "
            f"{_plural(n, 'was', 'were')} dropped{named}, so a date the provider published "
            f"may be missing from the calendar below entirely{span_clause} — an event on "
            f"such a date is missing from this report rather than absent from the "
            f"provider's calendar._"
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
    # "Actual" belongs in this list too: the history endpoint serves whatever
    # the provider holds for a date NOW, and macro actuals are routinely
    # revised (payroll benchmarks, CPI seasonal factors). Naming only forecast
    # and previous reads as a deliberate exclusion — i.e. as a promise that the
    # actual IS the print as first published — which is the opposite of true,
    # and it is the actual that feeds the Surprise column.
    header_lines.append(
        "_Actual, forecast and previous are all the provider's current figures, not "
        "point-in-time snapshots: a revised actual is served in place of the print as "
        "first published. A surprise is shown only where actual and forecast share a unit._"
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

    # Measured from the day-rows that actually carry names, matching the
    # reach line above: a row whose every name was dropped as unusable
    # survives with an empty list, and spanning it here would claim coverage
    # the calendar cannot contribute — two contradictory spans in one header.
    cal_span = (
        f"covers {cal_dated[0]} → {cal_dated[-1]}"
        if cal_dated
        else "names no event on any day-row it carries"
    )
    # "Calendar in this snapshot", not "Provider calendar". Both arms describe
    # post-loss state: the endpoints are this client's kept rows (truncation
    # keeps the head, a malformed or wholly-unusable row drops out of
    # cal_dated), and the empty arm fires precisely when THIS client dropped
    # every name. Labelling either as the provider's reads client-side loss as
    # provider absence — the one thing every caveat above is worded to avoid,
    # and this was the last extent-site still asserting it.
    header_lines.append(
        f"- Source: SoSoValue OpenAPI (US macro) | Snapshot fetched "
        f"{snapshot.fetched_at} | Calendar in this snapshot {cal_span}; the released "
        f"figures below come from per-event histories that reach further back | "
        f"Tracked: {', '.join(TRACKED_EVENTS)} | Window ending {curr_date}"
    )
    header = "\n\n".join(header_lines) + "\n"

    # ---- scheduled table ----------------------------------------------------
    if scheduled:
        # Bound the table, but never let a row carrying no figures evict one
        # that does (user decision). Nothing bounds names-per-day at the parse
        # boundary (MAX_CALENDAR_ROWS bounds day-rows, not the events inside
        # one), so a provider that broadened /macro/events from this US-only
        # shape to a global calendar would otherwise pour every name of every
        # day straight into the prompt — and under a plain nearest-40 cut a
        # couple of dense foreign days would push every CPI/NFP forecast out
        # of the table, losing precisely what this section exists to carry.
        # Priority rows are kept first, then the nearest of the rest fill what
        # is left. A row dated curr_date is priority even with no figures: the
        # decision to fold today's calendar entries into this table exists
        # because they reach the reader through no other path, and the intraday
        # caveat below promises a row for them — ranking a fortnight-out
        # forecast above today's event would re-open exactly that hole.
        # Sorted on (date, name) only, like the sort that built ``scheduled``:
        # a plain tuple sort falls through to the figures and would reorder two
        # same-date prints out of the provider's own sequence.
        def _is_priority(r: tuple) -> bool:
            return r[0] == curr_date or r[2] != "—" or r[3] != "—"

        priority = [r for r in scheduled if _is_priority(r)]
        rest = [r for r in scheduled if not _is_priority(r)]
        shown_scheduled = sorted(
            priority[:MAX_ROWS] + rest[: max(0, MAX_ROWS - len(priority))],
            key=lambda r: (r[0], r[1]),
        )
        dropped = len(scheduled) - len(shown_scheduled)
        sched_note = (
            f"\n_(showing {len(shown_scheduled)} of {len(scheduled)} scheduled rows in "
            f"the window: rows dated {curr_date} and rows carrying figures take priority "
            f"over name-only calendar entries further out, and within each group the "
            f"nearest are kept)_\n"
            if dropped
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
            f"{curr_date}; a row showing — for both figures is a calendar entry outside "
            f"the tracked list, a tracked print whose figures the provider has not filed, "
            f"or a tracked event whose history this snapshot does not carry at all — a "
            f"failed fetch or an upstream rename, both named in the coverage caveats "
            f"above, and in either case the provider may well have filed a forecast this "
            f"snapshot simply never received\n" + "\n".join(lines) + "\n" + sched_note
        )
    else:
        # Why the schedule is empty is NOT always benign: the provider's
        # calendar is present-anchored, so an empty forward window is expected
        # only when it still reaches past curr_date. A calendar that ends on or
        # before curr_date means it stopped publishing forward — a coverage
        # failure that must not be narrated as a quiet fortnight.
        # The fourth consumer of the calendar's extent, and the last one to be
        # moved off the raw day-rows. _parse_calendar keeps a day-row whose
        # every name was dropped, and the provider can send an empty ``events``
        # list itself, so ``calendar[-1]`` can date the calendar past anything
        # it is able to contribute: that both prints an end date contradicting
        # the Source span and the overlap disclosure, and — worse — selects the
        # benign branch below off a row that schedules nothing.
        cal_end = cal_dated[-1] if cal_dated else None
        # Staleness, truncation and dropped names are tested FIRST, ahead of
        # the calendar's own end date: each means this snapshot does not hold
        # the whole calendar the provider published for this date, so neither
        # the provider-blaming branch nor the benign one may speak. A stale
        # calendar ending before curr_date has aged out of its forward reach —
        # saying the provider "is publishing no forward schedule" would blame
        # the source for the snapshot's age; and when this client dropped the
        # names itself, "genuinely carries no scheduled entries" is false.
        ends = f"ends {cal_end}" if cal_end else "names no event on any day-row it carries"
        # Shares snapshot_incomplete with the covered-and-quiet note in the
        # header (see its definition): calendar_malformed is a member for the
        # same reason truncation and dropped names are, and DUPLICATION is not.
        if snapshot_incomplete:
            why = (
                f"The provider's calendar in this snapshot {ends}, but the "
                f"snapshot does not hold a complete forward view of what the provider "
                f"publishes for this date (see the caveats above), so read the empty "
                f"schedule as an artefact of the snapshot rather than a quiet window."
            )
        elif cal_end is None or cal_end <= curr_date:
            # Its own phrasing, not the shared ``ends`` above: this branch has
            # to say the end date is on or before curr_date, which is the whole
            # reason it fires.
            # Two phrasings, because the no-date arm has no date to point at
            # AND a different cause. The incompleteness gate above already took
            # every stale/truncated/dropped-name snapshot, and _parse_calendar
            # raises on an empty payload while _read_cache requires a non-empty
            # calendar list — so cal_end is None here means exactly one thing:
            # the PROVIDER sent day-rows carrying no event names. The calendar
            # was received; saying it never was would be false, and the
            # "a day-row only where it has events" premise is falsified by the
            # very state that selects this arm. What is left is the plain fact.
            why = (
                f"The provider's calendar ends {cal_end}, on or before this date, so this "
                f"snapshot carries no dated entry beyond it — which cannot distinguish a "
                f"calendar that stops there from a fortnight the provider covered without "
                f"having anything to list."
                if cal_end
                else "The provider's calendar in this snapshot names no event on any day-row "
                "it carries, so it can place nothing in this window — and with no dated "
                "entry to measure from, this snapshot cannot say whether the window is "
                "unpublished or simply quiet."
            )
        elif snapshot.calendar_duplicated:
            # Duplication belongs here too — the benign "genuinely carries no
            # scheduled entries" must not speak over it — but it earns its own
            # sentence rather than the incompleteness one above. _parse_calendar
            # MERGES same-date rows and de-dupes names within a date, losing no
            # day-row and no name, and truncation is computed after the merge,
            # so nothing is missing. What duplication costs is confidence in the
            # DATES: the caveat above says an entry may have been labelled with
            # the wrong date, so one belonging to this window may be sitting on
            # a date outside it. Claiming an incomplete forward view here would
            # assert a gap the merge caveat two lines up denies.
            why = (
                f"The provider's calendar in this snapshot {ends} and holds every day-row "
                f"it sent, but it repeated dates (see the caveat above), so an entry "
                f"belonging to this window may carry a date outside it — read the empty "
                f"schedule as dates worth double-checking rather than a quiet window."
            )
        else:
            # Not "a date far from the fetch date": that is only one of the two
            # ways this branch is reached, and it is false whenever curr_date
            # IS the fetch date and the calendar's day-rows simply carry no
            # names in the window.
            why = (
                f"The provider's calendar reaches {cal_end} and is anchored to when this "
                f"snapshot was fetched, so this is a window that genuinely carries no "
                f"scheduled entries, or one whose every entry echoes a print already "
                f"listed as released below, or a {curr_date} sitting far from that fetch "
                f"date."
            )
        # The window can also be empty because tracked histories are missing —
        # for a historical curr_date the calendar cannot reach back, so the
        # scheduled rows can ONLY come from histories. Same disclosure the
        # released branch already makes.
        gap = _coverage_gap_note(missing_events, "contributed nothing", "nothing is scheduled")
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
        # "no release in this window", not the scheduled branch's blanket
        # "contributed nothing": an event whose only served rows sit ahead of
        # curr_date is in no_disclosure yet can be visible in the scheduled
        # table above, so the stronger phrase would deny a row the reader sees.
        gap = _coverage_gap_note(
            missing_events, "contributed no release to this window", "nothing printed"
        )
        released_block = (
            f"\n**Released (last {look_back_days} days):** no tracked releases in the "
            f"window ending {curr_date}." + (f" {gap}" if gap else "") + "\n"
        )

    # ``shallow_note`` is unconditional, so it rides the return rather than
    # being appended in each released branch — a third branch cannot forget it.
    return header + scheduled_block + released_block + shallow_note
