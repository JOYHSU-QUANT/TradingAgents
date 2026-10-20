"""The kernel the rolling-snapshot vendors share, under no vendor's name.

This module holds what the fork's rolling-snapshot vendors import, each
taking what it needs: the trust-boundary predicates, the clock and cache-age
helpers, the cache read preamble and rejecter, the STALE template, the US$m
formatters and the cache/TTL/stale-fallback skeleton
(``load_rolling_snapshot``). They grew
inside ``sosovalue_common``, the first family to need them, and moved out
under public names (dataflows refactor plan 2026-10-16, D1); what is
SoSoValue's — the error taxonomy and the vendor name, the API key, the
request helper and budget, the listing ticker predicate, and the per-item
sweep that catches and raises those types — stays in ``sosovalue_common``.

``_utc_now`` is the one clock every helper here reads, and it stays a
module-level underscore name on purpose: it is the seam the tests patch to
freeze time (``monkeypatch.setattr(vendor_common, "_utc_now", ...)``).

Nothing here knows a vendor's payload shape, TTL policy or report wording;
``load_rolling_snapshot`` takes those as callables and names, the
``vendor`` it prints among them.
"""

import json
import logging
import math
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TypeGuard

from .config import get_config
from .errors import (
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
)
from .utils import failure_account, finite_float, plural_days, quote_argument, wiring_gap


def is_finite_real(x: object) -> TypeGuard[float]:
    """True only for a real, finite number (not a bool, NaN, or Infinity).

    bool is an int subclass, so a JSON ``true`` must not pass as a figure;
    NaN/Infinity would poison downstream sums and render as literal
    "nan"/"inf". An int too large to convert to float makes ``math.isfinite``
    raise OverflowError - such a value can never be a usable figure, and
    letting the exception escape would crash a cache read or parse outside the
    vendor taxonomy.

    All of that is ``utils.finite_float`` now, shared with the other vendors
    that ask the same question. A ``TypeGuard`` so a value that passed reads
    as a number to the type checker at the comparison that follows.
    """
    return finite_float(x) is not None


def is_iso_date(x: object) -> bool:
    """True only for a canonical zero-padded ``YYYY-MM-DD`` date string.

    A non-canonical date would silently mis-order the lexical
    ``date <= curr_date`` lookahead filter (a non-zero-padded "2026-6-5" sorts
    after "2026-07-20"), and a non-date would crash a later ``strptime`` deep
    inside a render, so it is rejected wherever a date crosses a trust
    boundary (API response or cache file).
    """
    if not isinstance(x, str):
        return False
    try:
        return datetime.strptime(x, "%Y-%m-%d").strftime("%Y-%m-%d") == x
    except ValueError:
        return False


def is_safe_text(x: object, max_len: int, *, min_len: int = 1, stripped: bool = True) -> bool:
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


def is_non_negative_int(x: object) -> bool:
    """True only for a real int >= 0 (bool is an int subclass and must not pass).

    The plain non-negative count fields the cache payloads carry (fund /
    calendar / company tallies) share this check — except treasuries'
    ``companies_total``, whose bound is relational (no smaller than the
    selection, which subsumes non-negativity) and stays with its validator.
    Hand-written, the bool exclusion is the clause a new field's author
    forgets, letting a JSON ``true`` validate as 1.
    """
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def valid_dated_rows(rows: object, *, max_rows: int, row_ok: Callable[[dict], bool]) -> bool:
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
    if not all(isinstance(r, dict) and is_iso_date(r.get("date")) and row_ok(r) for r in rows):
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


def days_unobserved(fetched_at: str, curr_dt: datetime) -> int | None:
    """Days of a report's window that postdate the snapshot, or None.

    Not ``_cache_age_hours``: that measures the snapshot against wall-clock
    now for the TTL and the stale cap, while this measures it against the
    report's ``curr_date``, which on a backtest can sit far from now (and before the
    fetch, giving a non-positive result callers are expected to ignore). It is
    what a stale serve's trailing window cannot have seen — no snapshot carries
    what the provider published after it was fetched — so both vendor modules
    can say so instead of leaving the reader to subtract two printed dates.
    """
    fetched_day = fetched_at[:10]
    if not is_iso_date(fetched_day):
        return None
    return (curr_dt - datetime.strptime(fetched_day, "%Y-%m-%d")).days


# The earliest year a window may open in. The family already refuses a
# ``curr_date`` that does not round-trip through ``strftime`` as a canonical
# ``YYYY-MM-DD`` (:func:`is_iso_date`); this is that rule applied to the
# window start, as a fixed year rather than a round-trip because ``strftime``
# pads the year to four digits only on some platforms (glibc writes the year
# 931 as "931") and the vendors' row filters compare those strings lexically,
# so a window opening earlier would silently empty or misfilter the report
# where a refusal is owed.
MIN_WINDOW_YEAR = 1000

_WINDOW_TOO_LARGE = (
    "look_back_days is too large: the window would open before the year 1000, "
    "the earliest the report's dates can write"
)


def look_back_window(
    look_back_days: int | None, *, default: int, error_cls: type[Exception]
) -> int:
    """The caller's trailing window in days: ``default`` for None or non-positive, ``error_cls`` for a non-integer.

    One check for every vendor that opens a window from the caller's
    ``look_back_days`` (#380). ``bool`` counts as a non-integer: ``True``
    would silently mean a one-day window. The refusal echoes the value the
    way the family echoes a caller's argument, quoted, flattened and capped,
    and reaches the router as the vendor's own class rather than as the bare
    ``TypeError`` or ``ValueError`` a ``<=`` or ``timedelta`` would have
    raised over it.
    """
    if look_back_days is None:
        return default
    if isinstance(look_back_days, bool) or not isinstance(look_back_days, int):
        raise error_cls(f"look_back_days {quote_argument(look_back_days)} is not an integer")
    return default if look_back_days <= 0 else look_back_days


def window_start_dt(
    curr_dt: datetime, look_back_days: int, *, error_cls: type[Exception]
) -> datetime:
    """``curr_dt`` less ``look_back_days`` days, or ``error_cls`` when the calendar cannot hold the window.

    The count has passed :func:`look_back_window`, so it is a positive int;
    what remains is its size (#380). Past roughly 739,000 days from a
    present-day ``curr_dt`` the subtraction leaves ``datetime``'s range and
    past a billion ``timedelta`` refuses the count outright, both as
    ``OverflowError``; from about 375,000 the window opens before
    :data:`MIN_WINDOW_YEAR`, where its ISO string stops being four digits on
    every platform. All three are refused with one sentence as the vendor's
    own class, the ``OverflowError`` chained where there was one. The value
    is not echoed: an integer that large has no useful spelling, and ``str``
    refuses one past the interpreter's digit limit.
    """
    try:
        start = curr_dt - timedelta(days=look_back_days)
    except OverflowError as e:
        raise error_cls(_WINDOW_TOO_LARGE) from e
    if start.year < MIN_WINDOW_YEAR:
        raise error_cls(_WINDOW_TOO_LARGE)
    return start


def _humanize_span(hours: float) -> str:
    """A cap in its own units: hours under a day, days beyond.

    The stale cap used to be a whole number of days and could say so; now that
    it is hours, "0.25-day" would be a worse way to write six.
    """
    return f"{hours:g}-hour" if hours < 24 else f"{hours / 24:g}-day"


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
    return f"{days} {plural_days(days)}"


def stale_caveat(
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


def cache_dir() -> str:
    """The snapshot cache directory, shared by every vendor cached this way.

    Both statements under ``wiring_gap``: a missing key is this project's
    wiring and must not come back as a line of report text (#219), and so is
    a key set to something that is not a path — ``makedirs`` answers that
    with a ``TypeError``, which outside the guard would read as the vendor's
    library. ``makedirs``' own OSError is a different thing, a cache the
    process cannot write, and passes through the guard untouched to the
    lane the router already reads as transport (#116). Farside imports this
    rather than keeping its own copy — the key, the guard's wording and what
    sits inside it were three things to keep in step.
    """
    with wiring_gap("cache configuration"):
        directory = get_config()["data_cache_dir"]
        os.makedirs(directory, exist_ok=True)
    return directory


def cache_rejecter(path: str, *, cache_name: str, log: logging.Logger) -> Callable[[str], None]:
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


def read_cache_preamble(path: str, *, reject: Callable[[str], None]) -> dict | None:
    """Open/parse ``path`` and hand back the payload dict, or None if unusable.

    The preamble every ``_read_cache`` validator (the family's three, and the
    Farside vendor's) opens with, before its module-specific field validation: a missing file is an
    ordinary miss (no cache yet, nothing to log), an unreadable or non-JSON
    file is rejected through the module's ``reject`` (see ``cache_rejecter``),
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


# One place for the US$m rendering every vendor that reports notionals uses.
# The dust branch is the whole reason it is shared: a figure that rounds to
# "0.0" beside a line asserting the thing is non-zero is two cells the reader
# is told cannot both be true (a treasury filing under $50k printing "+0.0"
# beside an Implied US$/BTC cell computed from the unrounded figure), and that
# rule took real thought to get right once. A second hand-copy is how it
# starts drifting.
USD_PER_MILLION = 1e6


def fmt_usd_m(value: float) -> str:
    """A notional in US$m, tenth-of-a-million granularity.

    A non-zero figure that would round to zero keeps three decimals instead, so
    a dust position never prints as "0.0" beside a line saying it is held.
    """
    millions = value / USD_PER_MILLION
    rounded = round(millions, 1)
    if rounded == 0 and millions != 0:
        return f"{millions:,.3f}"
    return f"{rounded + 0.0:,.1f}"


def fmt_signed_usd_m(value: float) -> str:
    """The signed twin of :func:`fmt_usd_m`, for a change or a filed cost.

    ``+ 0.0`` normalizes a negative zero on the coarse path, which a sub-tick
    move would otherwise render as "-0.0". The residual: a value under $500
    renders "+0.000"/"-0.000" — not reachable at the universes these callers
    report on, and an unbounded precision escape would make a column
    unreadable.
    """
    millions = value / USD_PER_MILLION
    rounded = round(millions, 1)
    if rounded == 0 and millions != 0:
        return f"{millions:+,.3f}"
    return f"{rounded + 0.0:+,.1f}"


def concentration_share_str(share: float) -> str:
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


def sign(value: float) -> int:
    return (value > 0) - (value < 0)


def plural(count: int, singular: str, plural_form: str) -> str:
    return singular if count == 1 else plural_form


def coverage_gap_note(missing: set[str] | list[str], did_what: str, otherwise: str) -> str:
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


def load_rolling_snapshot(
    *,
    path: str,
    read_cache: Callable[[str], dict | None],
    fetch_all: Callable[[dict | None], dict],
    ttl_hours: Callable[[dict], float],
    label: str,
    cache_name: str,
    max_stale_hours: float,
    log: logging.Logger,
    vendor: str,
    precheck: Callable[[], object] | None = None,
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

    ``vendor`` and ``precheck`` are what make this the FAMILY's skeleton
    rather than SoSoValue's. ``vendor`` names the vendor in every message and
    log line, and has no default: a default would file a caller that forgot
    it under another vendor's name. ``precheck``, when given, runs before
    anything else and may raise. For the SoSoValue modules it is
    ``get_api_key``: an unset key must raise even when a fresh cache could
    serve the call, or the emergency-disable flip (unset the key on the
    server) would be delayed by up to the cache TTL. A keyless vendor passes none.

    ``max_stale_hours``, not days. The bound used to be a whole number of days
    because the three SoSoValue feeds publish daily, which made a vendor whose
    honest cap is SHORTER than a day unable to use this at all: the
    whale-positioning module argues that a report headed "live snapshot" must
    not serve a quarter-day-old book, and a day-granular bound cannot express
    six hours. Hours cost the existing callers a ``* 24`` and let the skeleton
    hold vendors its units previously excluded.

    What still keeps the two hand-rolled copies out, measured rather than
    assumed, so the next attempt does not rediscover it:

    * ``farside._load_flows`` would inherit THIS module's clock for its TTL and
      staleness decisions while its rendered age keeps reading its own
      ``_utc_now``. Identical in production (both are the UTC wall clock) but
      two seams where the vendor had one, which is the split its own STALE
      caveat takes a ``humanize`` hook to avoid. A clock hook here would close
      it, at the cost of threading one through this module's three time
      helpers - and so through every feed already using them.
    * ``hyperliquid_whales._load_snapshot`` keeps ``fetched_at`` inside its
      ``current`` block, while this function stamps and reads it at the top
      level. Adopting means either a cache-schema change for a vendor that
      ships disabled, or a second pair of hooks to read and write the stamp -
      on a function whose argument list is already long.

    The discipline itself is the one Farside's loader set: a
    cache younger than its TTL is served as-is — the TTL is the module's
    policy call (``ttl_hours(cached)``), where each degradation bucket's
    shortened-or-not argument lives — otherwise ``fetch_all(cached)`` runs
    and overwrites the rolling file; on a fetch failure the call falls back
    to the cached snapshot (``stale=True``) up to ``max_stale_hours``. A
    failed fetch is never written to cache. ``VendorNotConfiguredError`` (the
    SoSoValue modules' unset or rejected key) is deliberately NOT absorbed by
    the stale fallback: config breakage should surface to the router
    immediately, not be papered over for two weeks.

    A vendor-taxonomy error keeps its exact type through the context-adding
    wraps: the router classifies by type (a rate limit is a routine quiet
    fall-through; a plain ``VendorError`` is unexpected breakage, logged
    with a traceback and surfaced as the chain's first error), so the wrap
    must add context without re-classifying. Nothing below the taxonomy
    reaches here: ``fetch_all`` types its own transport failures at the boundary (#217).

    A stale serve on an outage is a WARNING — a brownout of minutes must
    not read as an incident — until the snapshot's age (the cap's own
    measure, ``_cache_age_hours``) exceeds half of ``max_stale_hours``
    — past the seventh day of a 14-day cap — when each refresh logs one ERROR line
    instead (no traceback: the type says it is not a bug). An endpoint that has moved
    behind a gateway's error page thereby shows in the ERROR log a week
    before the cap expires the cache, rather than only when it does.
    Judged on the snapshot's age, not on a count of stale serves, so
    there is no counter to reset on a restart and no per-process view of
    it (#217). A throttle is never escalated: it is the vendor answering.
    """
    if precheck is not None:
        precheck()

    cached = read_cache(path)
    if cached:
        age_h = _cache_age_hours(cached["fetched_at"])
        # 0 <= age guards against a future-dated stamp being treated as fresh.
        if age_h is not None and 0 <= age_h < ttl_hours(cached):
            return cached, cached["fetched_at"], False, False

    try:
        payload = fetch_all(cached)
    except VendorNotConfiguredError:
        raise
    except VendorError as e:
        # A typed failure keeps its type. The transport lane arrives typed
        # too: the vendor's fetch judges an unreached vendor at the boundary (#217).
        wrap_cls = type(e)
        if cached:
            fetched_at = cached["fetched_at"]
            age = _cache_age_hours(fetched_at)
            # Unknown age (unparseable stamp) or a future-dated one is treated
            # as beyond the cap — the case where an unbounded-age serve is most
            # likely — rather than served with a nonsense age caveat. Hours
            # rather than whole days, so a cap under a day is expressible; the
            # future-dated case is the explicit sign test below.
            if age is None or age < 0 or age > max_stale_hours:
                stale_desc = (
                    "has an unparseable or future-dated fetch date"
                    if age is None or age < 0
                    else f"is {_humanize_age(fetched_at)} stale"
                )
                raise wrap_cls(
                    f"{vendor} {label} fetch failed and the newest cache {stale_desc} "
                    f"(> {_humanize_span(max_stale_hours)} cap): "
                    f"{failure_account(e, limit=None)}"
                ) from e
            # A structural failure here is a contract/parse break (a code fix is
            # likely needed) and must not hide among network-blip warnings for
            # up to the stale cap; a rate limit stays a warning, and so does
            # an outage until the snapshot has aged past half the cap.
            age_str = _humanize_age(fetched_at)
            # "Structural" by EXCLUSION rather than by this family's own type:
            # a failure that is neither the vendor being down nor the vendor
            # throttling us is the client or the contract breaking, whoever
            # raised it. ``isinstance(e, SoSoValueError)`` said exactly that
            # for one family - its outage and rate-limit types deliberately
            # sit outside that base - but it read as a rule about SoSoValue,
            # and a second vendor adopting this skeleton would have gone
            # silently down the warning lane with a broken parser.
            if not isinstance(e, (VendorUnavailableError, VendorRateLimitError)):
                log.error(
                    "%s %s refresh failed structurally (%s); serving stale "
                    "cache (%s old) — the client likely needs a fix, or the vendor "
                    "is refusing it",
                    vendor,
                    label,
                    e,
                    age_str,
                    exc_info=True,
                )
            elif isinstance(e, VendorUnavailableError) and age * 2 > max_stale_hours:
                log.error(
                    "%s %s refresh failed (%s); serving stale cache %s old, past "
                    "half the %s stale cap — a persistent failure, not a brownout",
                    vendor,
                    label,
                    e,
                    age_str,
                    _humanize_span(max_stale_hours),
                )
            else:
                log.warning(
                    "%s %s refresh failed (%s); using stale cache (%s old)",
                    vendor,
                    label,
                    e,
                    age_str,
                )
            return cached, fetched_at, True, False
        # "usable": the file may exist but have failed read-side validation.
        # The cause is quoted through ``failure_account``: the typed error's
        # own text, flattened but not capped here — the router caps its
        # slot, and a sweep verdict's ``(...; last: ...)`` tally must survive
        # into this module's log line. An unreached vendor arrives already worded
        # by class at the vendor's boundary (#217): never the requests message, which
        # quotes the request URL, and this line is LLM-visible (#203).
        raise wrap_cls(
            f"{vendor} {label} unavailable and no usable cache exists: "
            f"{failure_account(e, limit=None)}"
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
