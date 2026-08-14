"""SoSoValue BTC corporate-treasury vendor.

Serves corporate BTC treasury holdings and disclosed holdings changes from
SoSoValue's OpenAPI btc-treasuries module (shared plumbing in
``sosovalue_common``), as a news-side demand-flow signal of the same family as
the spot-ETF flows: who holds how much, and who added or reduced inside the
window. Treasury flow is announcement-driven and lumpy — a medium-term
demand-side narrative, not a timing signal — and the report says so.

Live-verified API facts this module is built on (2026-08-11):

- ``GET /btc-treasuries`` takes no parameters and returned 57 companies
  ordered by holdings, largest first (MSTR, then XXI, Metaplanet, MARA —
  matching the real-world ranking), each ``{"ticker", "name",
  "list_location"}`` with NO holdings figure — so the listing's own order is
  the only way to pick the biggest holders without fetching every history,
  and the ``MAX_COMPANIES`` cut assumes it. International tickers appear
  ("3350", "0434.HK", "ADE.DE"), which the shared ticker filter accepts.
- ``GET /btc-treasuries/{ticker}/purchase-history`` (``limit`` capped at 100)
  serves DATES newest-first. Two rows sharing ONE date were not observed on
  this endpoint — no captured company has a duplicate date — so the parser's
  within-date rule is INFERRED from the sibling ``/macro/events/{event}/history``
  endpoint, whose capture lists a same-date pair oldest-first; that is why the
  parser sorts ascending without reversing (see ``_parse_purchase_rows``).
  Numeric fields arrive as STRINGS ("840447",
  "-1690"); ``btc_acq`` is negative for disposals (MSTR was reducing when
  captured) and — with ``acq_cost`` — can be MISSING entirely: MARA's newest
  row carries only ``btc_holding``, its ~17.5k BTC reduction visible only as
  a holdings drop. Some companies disclose per-purchase, others only monthly
  or quarterly snapshots. ``avg_btc_cost`` is unusable (0 or 0.09 where
  ~$64k/BTC is implied by cost/quantity) and is neither stored nor rendered;
  a per-row implied price is computed from ``acq_cost / btc_acq`` instead.
- History depth varies and is bounded (MSTR's reached ~19 months when
  captured), so a look-back window can outrun the served history — the
  report discloses that instead of reading absence as inactivity.

The fan-out is capped at ``MAX_COMPANIES`` histories (1 + N requests against
the shared 20 req/min plan limit), taken in listing order — i.e. the largest
holders. Vendor success needs the listing AND at least one company history
(decision Q3): unlike the ETF module there is no aggregate endpoint, the
signal lives entirely in the histories, and a report of bare company names
would be no signal at all. Individual history failures below that threshold
are disclosed and retried on the short incomplete-TTL, with the family's
consecutive-network-failure breaker — and an immediate drain on the first
429: the plan limit is per-key and per-minute, so once it trips every
remaining request this sweep would 429 too (a deliberate divergence from the
ETF module's decided keep-trying-after-429 behaviour, which predates the key
being shared by three fan-outs). The holdings-order assumption is verified
after each fetch — the fetched companies' latest holdings must be
non-increasing in listing order — and a violation downgrades the report's
"largest holders" claim to "ordering unverified" instead of asserting a
ranking the data contradicts. A rejected or unset key raises out of any
request so the emergency-disable flip stays immediate.

Asset scope: the module serves BTC only (that is the product). ``BTC`` gets
the native report; another recognized crypto risk asset (including ETH, which
has no treasuries module here) gets the same data labelled as a market-wide
demand proxy; a stablecoin or unrecognized symbol gets a no-signal note —
mirroring the ETF module's classification, with ETH on the proxy side this
time.

Caching mirrors the family: one rolling snapshot file on a 24h TTL (treasury
disclosures are event-frequency; a day-scale TTL also keeps this module's
16-request refresh from crowding the ETF and macro modules' shares of the
rate limit), the short TTL while any selected history is missing, stale
serves capped at ``MAX_STALE_DAYS`` and disclosed, failures never written,
and a cache that fails read-side validation discarded. All numeric strings
are normalized to floats at the parse boundary, so the cache stores numbers.
"""

import json
import logging
import math
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
    _is_finite_number,
    _is_iso_date,
    _is_valid_ticker,
    _iso_now,
    _plural,
    _plural_days,
    _request,
    _sanitize,
    get_api_key,
)
from .symbol_utils import CRYPTO_BASES, normalize_symbol

logger = logging.getLogger(__name__)

# Only BTC has a treasuries module; every other recognized risk asset is
# served the BTC data as a labelled market-wide proxy (see module docstring).
SUPPORTED_ASSETS = {"BTC"}

# Histories fetched per refresh, taken in listing order — the provider lists
# by holdings, largest first (live-verified), so this is a top-holders cut.
# 1 + 15 requests fits a single refresh inside the shared 20 req/min limit.
MAX_COMPANIES = 15

# Default trailing window for the activity section. Treasury disclosures are
# sparse (some companies file monthly or quarterly), so the family's 30-day
# default would routinely show an empty table; a quarter captures at least
# one disclosure cycle for most filers.
DEFAULT_LOOKBACK_DAYS = 90

# The documented per-request row cap (values above are silently clamped).
HISTORY_LIMIT = 100

# Client-side hard bound on a company's served history, the twin of the macro
# module's MAX_HISTORY_ROWS_HARD: HISTORY_LIMIT is a request parameter the
# server clamps, not a bound this client enforces, so a provider that stopped
# honouring it would write an unbounded snapshot file that every later cache
# read re-validates in full. Set far above the served depth so only a
# pathological payload trips it; a trip raises, which lands the company in
# companies_failed (or routes the call through the stale fallback) rather than
# persisting the bloat.
MAX_HISTORY_ROWS_HARD = 1000

# Row cap for the rendered activity table, mirroring the family MAX_ROWS.
MAX_ROWS = 40

# Companies shown in the top-holders line.
TOP_HOLDERS = 5

# Bound on the listing's free-text company name. Unlike the ETF module's
# stored-only fund names these ARE rendered (the top-holders line), so the
# charset is restricted to printable ASCII as well as bounded — a name that
# fails is dropped to "" and the report falls back to the ticker alone.
MAX_COMPANY_NAME_CHARS = 60

# Cache lifetimes. 24h TTL: disclosures are event-frequency (announcement
# driven), and the longer interval keeps this module's 16-request refresh
# from crowding the ETF/macro modules on the shared 20 req/min plan. The
# short TTL re-tries missing histories; stale serves are capped + disclosed.
CACHE_TTL_HOURS = 24
# Not the family's 1h: the short TTL re-runs the WHOLE 16-request sweep (there
# is no per-item resume), and not every cause self-heals — a malformed row or a
# MAX_HISTORY_ROWS_HARD breach for one company is deterministic and lands in
# companies_failed on every retry. At 1h that pins a module budgeted for 16
# requests/day at 384, a 24x amplification of the exact quota the 24h TTL was
# chosen to protect. base/4 keeps a transient gap healing the same day while
# bounding a permanent one at 4 sweeps; the ETF module stays at its own 1h
# because base/6 of a 6h TTL is already that.
INCOMPLETE_CACHE_TTL_HOURS = 6
MAX_STALE_DAYS = 14

# Consecutive transport-level failures before the remaining histories are
# skipped into ``companies_failed`` unattempted — family breaker.
MAX_CONSECUTIVE_NETWORK_FAILURES = 3

# BTC and USD figures arrive as digit strings ("840447", "-108600000").
# Comma grouping was not observed here, but the same provider's macro feed
# does emit it, so a switch to "840,447" is a formatting change to expect —
# and refusing it would fail every company at once and take the vendor down
# with it. Both shapes parse; anything else still does not, so a malformed
# value cannot slip through as something else. Digit counts stay bounded
# (live values top out at 9 integer digits): an unbounded digit string would
# float() to inf past ~309 digits, poisoning sums with a value the cache
# validator then rejects forever (a silent perpetual-refetch loop).
_AMOUNT_RE = re.compile(r"^-?(?:\d{1,3}(?:,\d{3}){1,4}|\d{1,15})(?:\.\d{1,8})?$")

# The report renders costs in US$m, unit-consistent with the ETF module.
_USD_PER_MILLION = 1e6


def _parse_amount(x: object) -> float | None:
    """A finite number from the API's numeric-string (or bare-number) fields.

    Returns None for anything else — including bools, NaN/Infinity, and
    strings with grouping or units — so the caller decides whether a field
    is required (raise) or optional (absent).
    """
    if _is_finite_number(x):
        return float(x)
    if isinstance(x, str) and _AMOUNT_RE.match(x):
        return float(x.replace(",", ""))
    return None


def _clean_name(x: object) -> str:
    """A renderable company name, or "" to fall back to the ticker.

    Rendered verbatim in report text, so it must be printable ASCII and
    bounded; anything else is dropped rather than escaped (the ticker is
    always shown anyway).
    """
    if (
        isinstance(x, str)
        and 0 < len(x) <= MAX_COMPANY_NAME_CHARS
        and x == x.strip()
        and all(32 <= ord(c) <= 126 for c in x)
    ):
        return x
    return ""


def _parse_company_list(data: list) -> tuple[list[tuple[str, str]], int]:
    """Validate the listing into ``((ticker, name) pairs, unusable count)``.

    Mirrors the ETF listing parser: an entry without a plausible ticker is
    dropped with a warning and counted (the report discloses the shrunken
    universe); duplicates keep the first entry; order is preserved — it IS
    the holdings ranking (live-verified), which the MAX_COMPANIES cut relies
    on. An unusable name does not drop the entry: the ticker still names the
    company.
    """
    companies: list[tuple[str, str]] = []
    seen: set[str] = set()
    unusable = 0
    for raw in data:
        ticker = raw.get("ticker") if isinstance(raw, dict) else None
        if not _is_valid_ticker(ticker):
            unusable += 1
            logger.warning(
                "SoSoValue treasuries list entry %.120r has no usable ticker; skipping it",
                raw,
            )
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        companies.append((ticker, _clean_name(raw.get("name"))))
    return companies, unusable


def _parse_purchase_rows(data: list, ticker: str) -> list[dict]:
    """Validate one company's history into ascending normalized rows.

    ``btc_holding`` is required on every row (every live row carries it, and
    it is what the holdings section reads). ``btc_acq``/``acq_cost`` are
    OPTIONAL — live-verified missing on holdings-only disclosures (MARA), so
    absence maps to None; but a *present* field that fails to parse raises,
    because a filed figure this module cannot read safely is a contract
    break, not an absence. Raises land the company in ``companies_failed``
    (non-fatal, disclosed). An empty ``data`` raises too, as a defensive
    backstop — ``_fetch_one_company`` pre-checks emptiness and routes it into
    ``companies_empty`` before this parser runs. Duplicate dates are kept —
    two same-day filings are two disclosures, and dropping one would hide a
    real event; rows sort ascending by date and keep the provider's own
    sequence within one date, so ``rows[-1]`` is the last row the provider
    listed for the latest date. That it is also the LATEST disclosure rests on
    the within-date direction being oldest-first, which this endpoint's capture
    cannot confirm (no company has a duplicate date) and which is inferred from
    the sibling history endpoint — see the module docstring and the ordering
    comment below, and keep all three at the same confidence.
    """
    if not data:
        raise SoSoValueError(f"SoSoValue returned no treasury history rows for {ticker}")
    if len(data) > MAX_HISTORY_ROWS_HARD:
        raise SoSoValueError(
            f"SoSoValue served {len(data)} treasury history rows for {ticker} "
            f"(> {MAX_HISTORY_ROWS_HARD}, against a requested limit of {HISTORY_LIMIT}); "
            f"the API contract may have changed"
        )
    rows = []
    for raw in data:
        if not isinstance(raw, dict) or not _is_iso_date(raw.get("date")):
            raise SoSoValueError(
                f"Malformed {ticker} treasury row {_sanitize(repr(raw), limit=200)}"
            )
        holding = _parse_amount(raw.get("btc_holding"))
        # >= 0, not merely finite: btc_holding is a stock quantity, and the
        # sign _AMOUNT_RE allows exists for btc_acq (disposals are negative).
        # A negative holding would flow into the combined total and make the
        # concentration share exceed 100% while the top-holders line rendered
        # a negative BTC balance — figures no reader could interpret.
        if holding is None or holding < 0:
            raise SoSoValueError(
                f"{ticker} treasury row for {raw['date']} has no readable non-negative "
                f"btc_holding: {_sanitize(repr(raw.get('btc_holding')), limit=60)}"
            )
        row = {"date": raw["date"], "btc_holding": holding}
        for field in ("btc_acq", "acq_cost"):
            if field in raw and raw[field] is not None:
                value = _parse_amount(raw[field])
                if value is None:
                    raise SoSoValueError(
                        f"{ticker} treasury row for {raw['date']} has an unreadable "
                        f"{field}: {_sanitize(repr(raw[field]), limit=60)}"
                    )
                row[field] = value
            else:
                row[field] = None
        rows.append(row)
    # Stable ascending sort with NO pre-reversal, so a same-date pair keeps the
    # provider's own sequence and ``rows[-1]`` is the latest disclosure — which
    # is what every "latest disclosure" consumer reads: the combined-holdings
    # total, each company's as-of date, the concentration share, the
    # listing-order check, and the derived-delta baseline.
    #
    # The API serves DATES newest-first; two rows sharing one date are taken to
    # arrive OLDEST-first. No treasuries fixture carries a duplicate date, so the
    # evidence is the sibling endpoint of the same API family:
    # tests/fixtures/sosovalue_macro_history_nfp.json holds 2025-12-16 twice,
    # actual=-105 then actual=64, and the ``previous`` chain
    # (119 -> -105 -> 64 -> 50) fixes -105 as the EARLIER print.
    # sosovalue_macro._parse_event_rows correspondingly does not reverse.
    # An earlier revision of this parser did, on the assumption that
    # "newest-first" also held within a date; that made ``rows[-1]`` the
    # SUPERSEDED filing — exactly the inversion the reversal was meant to stop.
    rows.sort(key=lambda r: r["date"])
    return rows


class _TreasurySnapshot(NamedTuple):
    """What ``_load_snapshot`` resolved.

    ``companies`` maps ticker -> {"name": str, "rows": [normalized rows]} for
    each fetched history; ``companies_failed`` the selected tickers whose
    fetch failed (retried on the short TTL) and ``companies_empty`` those the
    provider answered with no rows — listed but not yet filing, which no
    retry can heal, so they do not shorten the TTL. The three together are
    the MAX_COMPANIES-capped selection, in listing order. ``rate_limited``
    records that this client's own per-minute quota refused a request
    mid-sweep and drained the rest — the failure bucket then holds tickers
    nothing upstream went wrong with, and the report has to say so rather
    than let a gap this client opened read as the provider going quiet.
    ``breaker_skipped`` is its sibling for the consecutive-transport-failure
    breaker: there the upstream trouble is real, but it was only ever
    observed on the companies actually requested, so the bucket must not read
    as that many proven failures. The two are mutually exclusive — either
    break ends the sweep.
    ``companies_total`` counts the full usable listing (the
    "top N of M" disclosure denominator) and ``companies_unusable`` the
    dropped listing entries. ``order_unverified`` records that the fetched
    holdings contradicted the listing's largest-first ordering, so the
    report must not claim a top-N ranking.
    """

    companies: dict[str, dict]
    companies_total: int
    companies_failed: list[str]
    companies_empty: list[str]
    companies_unusable: int
    order_unverified: bool
    rate_limited: bool
    breaker_skipped: bool
    fetched_at: str
    stale: bool


def _cache_path() -> str:
    """Path of the single rolling snapshot (the data is BTC-wide, no asset key)."""
    return os.path.join(_cache_dir(), "sosovalue_treasuries.json")


def _valid_rows(rows: object) -> bool:
    # Row-count bound mirrored read-side, like every other parse-boundary bound
    # in this family: a snapshot written before it existed must cost one
    # refetch rather than be re-walked in full on every read forever.
    if not (isinstance(rows, list) and rows and len(rows) <= MAX_HISTORY_ROWS_HARD):
        return False
    if not all(
        isinstance(r, dict)
        and _is_iso_date(r.get("date"))
        and _is_finite_number(r.get("btc_holding"))
        # Non-negative, mirroring the parse boundary exactly. The cache is the
        # lower trust tier of the two, so an invariant the parser enforces must
        # not be reachable by hand-editing the snapshot or by a file an older
        # code version wrote: a negative holding puts a negative BTC balance in
        # the top-holders line and pulls the combined total below the largest
        # holder's own, which the near-100 band then reports as a "100%"
        # concentration share — every figure disagreeing with the next, inside
        # a report carrying no staleness or degradation caveat at all.
        and r["btc_holding"] >= 0
        # The keys must be PRESENT (the parser always writes both): a
        # missing key is not the legal null, and admitting it would let the
        # renderer's row["btc_acq"] subscript raise a raw KeyError outside
        # the vendor taxonomy instead of this validator rejecting the file.
        and "btc_acq" in r
        and (r["btc_acq"] is None or _is_finite_number(r["btc_acq"]))
        and "acq_cost" in r
        and (r["acq_cost"] is None or _is_finite_number(r["acq_cost"]))
        for r in rows
    ):
        return False
    dates = [r["date"] for r in rows]
    # Non-descending, not strictly ascending: duplicate dates are legal
    # (two same-day disclosures) and preserved by the parser.
    return all(a <= b for a, b in zip(dates, dates[1:], strict=False))


_ORDER_DESCENDING = "descending"
_ORDER_TOO_FEW = "too_few"
_ORDER_CONTRADICTED = "contradicted"


def _holdings_order(companies: dict) -> str:
    """How the fetched holdings relate to the provider's listing order.

    One definition for the three sites that have to agree: ``_fetch_all`` sets
    the stored flag from it, ``_read_cache`` refuses a cache whose flag is more
    confident than its own rows support, and the renderer picks which of the
    two unverified causes to name. That agreement is load-bearing rather than
    incidental — the renderer's plain "unverified" wording is right ONLY
    because ``_read_cache`` deliberately accepts a stored ``True`` sitting over
    holdings that do descend — so the predicate lives here instead of in three
    copies coupled by prose.

    ``companies`` is keyed in the provider's listing order (the fetch loop
    inserts in that order and JSON round-trips it), and the comparison is on
    each company's LATEST row, deliberately unfiltered by ``curr_date``: the
    claim under test is about the listing the provider served, not about the
    window a report renders.

    Distinguishing ``too_few`` matters because it is routine here, not exotic:
    a 429 on the second company drains the rest, and three consecutive
    transport errors trip the breaker. ``any()`` over an empty pair sequence is
    False, so collapsing it into "descending" would ship the confident wording
    having compared nothing.
    """
    holdings = [c["rows"][-1]["btc_holding"] for c in companies.values()]
    if len(holdings) < 2:
        return _ORDER_TOO_FEW
    if any(later > earlier for earlier, later in zip(holdings, holdings[1:], strict=False)):
        return _ORDER_CONTRADICTED
    return _ORDER_DESCENDING


def _read_cache(path: str) -> dict | None:
    """Return a fully-validated cached payload, or None if untrusted.

    Family discipline: every rejection logged with its reason, a rejected
    cache costs one re-fetch and never bad data. The bookkeeping invariants
    ``_fetch_all`` always writes are re-checked: the selection (fetched +
    failed) is non-empty, within the cap, disjoint, and no larger than the
    disclosed universe — violations would render "-3 of 57" style coverage
    lines or a holdings section the caveats say cannot exist.
    """

    def _reject(reason: str) -> None:
        logger.warning("Ignoring SoSoValue treasuries cache %s: %s", path, reason)
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
    companies = payload.get("companies")
    if not (
        isinstance(companies, dict)
        and companies
        and all(
            _is_valid_ticker(t)
            and isinstance(c, dict)
            # The same predicate as the live parse side (_clean_name is
            # idempotent: valid names pass through, anything else maps to
            # ""): these names render verbatim into LLM-visible report text,
            # so the cache read must not admit what the parser would not.
            and isinstance(c.get("name"), str)
            and c["name"] == _clean_name(c["name"])
            and _valid_rows(c.get("rows"))
            for t, c in companies.items()
        )
    ):
        # Non-empty by the vendor-success decision: _fetch_all raises rather
        # than writing a payload with zero fetched histories.
        return _reject("'companies' is missing, empty, or contains a malformed entry")
    buckets = {}
    for key in ("companies_failed", "companies_empty"):
        tickers = payload.get(key)
        if not isinstance(tickers, list) or not all(_is_valid_ticker(t) for t in tickers):
            return _reject(f"'{key}' is missing or not a list of plausible tickers")
        buckets[key] = set(tickers)
        if len(buckets[key]) != len(tickers) or buckets[key] & companies.keys():
            return _reject(f"'{key}' repeats a ticker or overlaps 'companies'")
    if buckets["companies_failed"] & buckets["companies_empty"]:
        return _reject("'companies_failed' overlaps 'companies_empty'")
    if not isinstance(payload.get("rate_limited"), bool):
        return _reject("'rate_limited' is missing or not a boolean")
    # The drain always adds the company the 429 landed on, so a set flag over
    # an empty failure bucket is a shape the parser cannot write. Served, it
    # would blame this client's quota for a sweep that lost nothing — the
    # mirror of every other parse-boundary invariant checked here.
    if payload["rate_limited"] and not buckets["companies_failed"]:
        return _reject("'rate_limited' is set but no company landed in 'companies_failed'")
    if not isinstance(payload.get("breaker_skipped"), bool):
        return _reject("'breaker_skipped' is missing or not a boolean")
    # Same mirror, and one more: the two flags mark the two ways the sweep can
    # end early, and either one BREAKS the loop, so a file carrying both is a
    # shape no sweep produced. Served, the report would print both corrections
    # and blame one gap on two incompatible causes.
    if payload["breaker_skipped"] and not buckets["companies_failed"]:
        return _reject("'breaker_skipped' is set but no company landed in 'companies_failed'")
    if payload["rate_limited"] and payload["breaker_skipped"]:
        return _reject("'rate_limited' and 'breaker_skipped' cannot both be set")
    selected = len(companies) + sum(len(b) for b in buckets.values())
    if selected > MAX_COMPANIES:
        # Also covers a cache written under a larger historical cap.
        return _reject(f"selection ({selected}) exceeds MAX_COMPANIES ({MAX_COMPANIES})")
    total = payload.get("companies_total")
    if not isinstance(total, int) or isinstance(total, bool) or total < selected:
        return _reject("'companies_total' is missing or smaller than the selection")
    unusable = payload.get("companies_unusable")
    if not isinstance(unusable, int) or isinstance(unusable, bool) or unusable < 0:
        return _reject("'companies_unusable' is missing or not a non-negative integer")
    if not isinstance(payload.get("order_unverified"), bool):
        return _reject("'order_unverified' is missing or not a boolean")
    # The last parse-side invariant left without a read-side mirror, and only
    # the confident direction is worth rejecting: a file claiming the listing
    # IS ordered while its own stored holdings ascend would put "provider lists
    # largest holders first" on top of data that contradicts it, the exact
    # unearned claim the flag exists to prevent. The opposite (stored True over
    # holdings that do descend) only costs wording confidence, so it is
    # accepted rather than made to cost a full 16-request refetch — and the
    # renderer's third wording depends on that acceptance. Feeding _fetch_all's
    # own output back through here is a no-op: it set the flag from this same
    # predicate over this same order.
    order = _holdings_order(companies)
    if not payload["order_unverified"] and order != _ORDER_DESCENDING:
        # Named per arm: the too-few arm compared nothing at all, so calling it
        # "not descending" would report a contradiction that was never observed
        # — the very distinction _holdings_order was extracted to preserve.
        return _reject(
            "'order_unverified' is False but only one company is stored, so the "
            "listing order was never verified"
            if order == _ORDER_TOO_FEW
            else "'order_unverified' is False but the stored holdings are not descending"
        )
    if not isinstance(payload.get("fetched_at"), str) or not payload["fetched_at"]:
        return _reject("'fetched_at' is missing or not a non-empty string")
    return payload


def _fetch_one_company(ticker: str, name: str) -> dict | str | None:
    """Fetch and parse one company's history.

    Returns the company entry, the string ``"empty"`` when the provider
    answers with no rows, or ``None`` on a non-fatal failure worth retrying.
    An empty history is a listed company that has not filed yet, not a
    failure: counting it as one would keep ``companies_failed`` permanently
    non-empty and pin the cache to the INCOMPLETE_CACHE_TTL_HOURS refresh
    forever, re-running the whole 16-request sweep on that cadence against a
    shared quota with nothing to heal.
    Mirrors the macro module's ``events_unknown`` handling.

    Otherwise the family per-item handler: a rejected key and a 429 both
    propagate (config breakage must reach the router; a 429 makes the rest
    of the sweep pointless, so the caller drains it), a structural break
    logs at ERROR with a traceback, a transient stays a warning, and a
    transport failure is re-raised after logging so the caller's breaker can
    count the streak.
    """
    try:
        data = _request(
            f"/btc-treasuries/{quote(ticker, safe='')}/purchase-history",
            {"limit": HISTORY_LIMIT},
        )
        if not data:
            logger.warning(
                "SoSoValue treasuries %s has an empty purchase history — the "
                "company is listed but has filed nothing this endpoint serves; "
                "disclosing it rather than retrying it",
                ticker,
            )
            return "empty"
        rows = _parse_purchase_rows(data, ticker)
        return {"name": name, "rows": rows}
    except (requests.RequestException, SoSoValueError) as e:
        if isinstance(e, SoSoValueError):
            logger.error(
                "SoSoValue treasuries %s history failed structurally (coverage "
                "disclosed as incomplete) — the client likely needs a fix: %s",
                ticker,
                e,
                exc_info=True,
            )
        else:
            logger.warning(
                "SoSoValue treasuries %s history failed (coverage will be "
                "disclosed as incomplete): %s",
                ticker,
                e,
            )
        if isinstance(e, requests.RequestException):
            raise
        return None


def _fetch_all() -> dict:
    """One full refresh: company listing, then the top-MAX_COMPANIES histories.

    Returns a cache payload (without ``fetched_at``). Raises on a listing
    failure, on a rejected key from ANY request, and — the Q3 decision — when
    every selected history failed: with no aggregate endpoint the histories
    ARE the signal, and bare company names would be served as if they were
    one. Below that threshold, failures degrade into a disclosed-incomplete
    coverage with the family's consecutive-network-failure breaker.
    """
    listing, unusable = _parse_company_list(_request("/btc-treasuries", {}))
    if not listing:
        raise SoSoValueError(
            "SoSoValue treasuries listing returned no usable companies "
            f"({unusable} unusable entries)"
        )

    selected = listing[:MAX_COMPANIES]
    companies: dict[str, dict] = {}
    companies_failed: list[str] = []
    companies_empty: list[str] = []
    rate_limited: SoSoValueRateLimitError | None = None
    # The transport counterpart of ``rate_limited``, kept for the same reason:
    # every RequestException is absorbed per company, so without it an
    # all-failed sweep can only raise a bare SoSoValueError.
    last_network: requests.RequestException | None = None
    # Set when a company fails for a reason no retry can heal —
    # _fetch_one_company returns None only after swallowing a SoSoValueError (a
    # parse/contract break) — so the transport classification below cannot
    # claim a pure outage while a real structural break is in the same sweep.
    structural_failure = False
    consecutive_network = 0
    # The breaker's counterpart to ``rate_limited``: it too leaves tickers in
    # companies_failed that were never requested, and the report must not let
    # the proven failures stand in for the skipped ones. Set only when
    # something was actually skipped — the breaker also trips on the last
    # company, where nothing went unattempted and there is nothing to disclose.
    breaker_skipped = False
    # Only the requests this sweep actually made. The all-failed message below
    # said "every attempt failed" over the whole selection, which turns
    # MAX_CONSECUTIVE_NETWORK_FAILURES observed failures into fifteen claimed.
    attempted = 0
    remaining = iter(selected)
    for ticker, name in remaining:
        attempted += 1
        try:
            company = _fetch_one_company(ticker, name)
        except SoSoValueRateLimitError as e:
            rate_limited = e
            # The 20 req/min limit is per-key and per-minute: this 429 proves
            # every further request in this sweep would 429 too, so drain the
            # rest into companies_failed (short-TTL retry) instead of burning
            # a quota call per remaining company.
            skipped = [ticker, *(t for t, _ in remaining)]
            companies_failed.extend(skipped)
            logger.warning(
                "SoSoValue treasuries: rate limit hit on %s (%s); that request "
                "and the %d histories not yet attempted all go to "
                "companies_failed (disclosed as incomplete, retried on the "
                "short TTL)",
                ticker,
                e,
                len(skipped) - 1,
            )
            break
        except requests.RequestException as e:
            last_network = e
            companies_failed.append(ticker)
            consecutive_network += 1
            if consecutive_network >= MAX_CONSECUTIVE_NETWORK_FAILURES:
                skipped = [t for t, _ in remaining]
                if skipped:
                    companies_failed.extend(skipped)
                    breaker_skipped = True
                    logger.warning(
                        "SoSoValue treasuries: %d consecutive network failures; "
                        "skipping the remaining %d company histories "
                        "(disclosed as incomplete, retried on the short TTL)",
                        consecutive_network,
                        len(skipped),
                    )
                break
            continue
        consecutive_network = 0
        if company == "empty":
            companies_empty.append(ticker)
        elif company is None:
            structural_failure = True
            companies_failed.append(ticker)
        else:
            companies[ticker] = company

    if not companies:
        # Keep the taxonomy honest when a 429 drained the whole sweep: the
        # router and _load_snapshot classify by type, and a quota trip must
        # not masquerade as structural breakage (ERROR + traceback logs).
        if rate_limited is not None:
            raise SoSoValueRateLimitError(
                f"SoSoValue treasuries: rate limited before any company "
                f"history could be fetched: {rate_limited}"
            ) from rate_limited
        # Say which way it went: "failed" would misdescribe a sweep where every
        # selected company simply has no served history, sending a reader after
        # a transport problem that never happened.
        # The transport sibling of the rate-limit rule directly above. Every
        # RequestException is absorbed per company, so a total outage would
        # otherwise surface here as a bare SoSoValueError — which _load_snapshot
        # classifies as structural breakage and logs at ERROR with a traceback
        # and "the client likely needs a fix", for an outage no code change can
        # heal. Raising the transport class routes it to the warning branch,
        # where the ETF module's network failures already land. Only when the
        # sweep died PURELY of transport: an empty served history or a swallowed
        # parse break means the cause is not the network, and the generic error
        # below stays the honest answer.
        if last_network is not None and not structural_failure and not companies_empty:
            raise requests.RequestException(
                f"SoSoValue treasuries returned no usable history for any of the "
                f"{len(selected)} selected companies; every request this sweep made "
                f"({attempted} of {len(selected)}) failed at the transport layer "
                f"(last: {last_network})"
            ) from last_network
        raise SoSoValueError(
            f"SoSoValue treasuries returned no usable history for any of the "
            f"{len(selected)} selected companies ({len(companies_failed)} failed, "
            f"{len(companies_empty)} empty); a listing without holdings figures "
            f"is no signal"
        )
    # Verify the ordering the selection and the report's "largest holders"
    # claim both rest on: the fetched companies' latest holdings must be
    # non-increasing in listing order. Every fetched history carries
    # btc_holding, so the assumption is cheaply checkable — and a violation
    # must downgrade the claim, not ship a ranking the data contradicts. A
    # false trip (per-company as-of dates can skew adjacent, similar-sized
    # holders) only costs wording confidence, never a wrong figure.
    # Fewer than two fetched histories means the check never RAN: any() over an
    # empty pair sequence is False, which would ship the confident "largest
    # holders first" wording having compared nothing. That state is routine
    # here, not exotic — a 429 on the second company drains the rest into
    # companies_failed, and three consecutive transport errors trip the
    # breaker — so it has to read as unverified rather than as verified.
    order = _holdings_order(companies)
    order_unverified = order != _ORDER_DESCENDING
    if order_unverified:
        # Say which of the two it was: below two fetched histories nothing was
        # compared, so claiming the listing is misordered would be the same
        # unearned assertion the report no longer makes.
        logger.warning(
            "SoSoValue treasuries listing ordering %s for this snapshot; the "
            "report will present the selection as unranked",
            "could not be checked (fewer than two histories fetched)"
            if order == _ORDER_TOO_FEW
            else "is not by holdings",
        )
    return {
        "companies": companies,
        "companies_total": len(listing),
        "companies_failed": companies_failed,
        "companies_empty": companies_empty,
        "companies_unusable": unusable,
        "order_unverified": order_unverified,
        # Persisted, not just logged: the drain fills ``companies_failed`` with
        # tickers this client never asked for, and by the time the report is
        # rendered the local that knew why is long gone.
        "rate_limited": rate_limited is not None,
        "breaker_skipped": breaker_skipped,
    }


def _snapshot_from(payload: dict, fetched_at: str, stale: bool) -> _TreasurySnapshot:
    return _TreasurySnapshot(
        companies=payload["companies"],
        companies_total=payload["companies_total"],
        companies_failed=payload["companies_failed"],
        companies_empty=payload["companies_empty"],
        companies_unusable=payload["companies_unusable"],
        order_unverified=payload["order_unverified"],
        rate_limited=payload["rate_limited"],
        breaker_skipped=payload["breaker_skipped"],
        fetched_at=fetched_at,
        stale=stale,
    )


def _load_snapshot() -> _TreasurySnapshot:
    """Return the treasuries snapshot, via the family cache/stale discipline.

    Key first; TTL-fresh cache served as-is (a *failed* history earns the
    short TTL — an empty one cannot be healed by retrying, so it does not);
    otherwise fetch + overwrite; on failure fall back to the
    cached snapshot up to MAX_STALE_DAYS; failures never written;
    ``SoSoValueNotConfiguredError`` never absorbed.
    """
    get_api_key()

    path = _cache_path()
    cached = _read_cache(path)
    if cached:
        age_h = _cache_age_hours(cached["fetched_at"])
        # Every degradation bucket is named here and every exclusion argued
        # here, following the ETF module's TTL site and the macro twin — the
        # enumeration is what stops a new bucket being wired into the report
        # and forgotten by the refresh. Shortened only for companies_failed
        # (429 / network / breaker, transient by definition). NOT shortened:
        #   companies_empty — listed but not yet filing; no retry heals it.
        #   companies_unusable — a listing entry whose ticker fails validation
        #     is deterministic, same payload drops the same entry every sweep
        #     (the ETF module settled this for its funds_unusable twin).
        #   order_unverified — a fact about the listing versus filed holdings,
        #     not lost data; and whenever its TOO_FEW arm is caused by
        #     something retriable, companies_failed is already non-empty.
        # The 6h value is itself deliberate (see the constant): do not drag it
        # back to the family's 1h.
        ttl = INCOMPLETE_CACHE_TTL_HOURS if cached["companies_failed"] else CACHE_TTL_HOURS
        if age_h is not None and 0 <= age_h < ttl:
            return _snapshot_from(cached, cached["fetched_at"], stale=False)

    try:
        payload = _fetch_all()
    except SoSoValueNotConfiguredError:
        raise
    except (requests.RequestException, VendorError) as e:
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
                    f"SoSoValue treasuries fetch failed and the newest cache "
                    f"{stale_desc} (> {MAX_STALE_DAYS}-day cap): {_sanitize(e)}"
                ) from e
            age_str = _humanize_age(fetched_at)
            if isinstance(e, SoSoValueError):
                logger.error(
                    "SoSoValue treasuries refresh failed structurally (%s); serving "
                    "stale cache (%s old) — the client likely needs a fix",
                    e,
                    age_str,
                    exc_info=True,
                )
            else:
                logger.warning(
                    "SoSoValue treasuries refresh failed (%s); using stale cache (%s old)",
                    e,
                    age_str,
                )
            return _snapshot_from(cached, fetched_at, stale=True)
        # Not capped, only flattened: most of this string is the module's own
        # diagnostic, and a foreign requests.RequestException can carry a
        # server-influenced URL into the same LLM-visible line.
        raise wrap_cls(
            f"SoSoValue treasuries unavailable and no usable cache exists: {_sanitize(e)}"
        ) from e

    fetched_at = _iso_now()
    payload["fetched_at"] = fetched_at
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        logger.warning(
            "Could not write SoSoValue treasuries cache %s: %s — the fetch "
            "throttle stays disabled until a write succeeds, so further calls "
            "will each re-fetch",
            path,
            e,
        )
    return _snapshot_from(payload, fetched_at, stale=False)


def _classify_asset(asset: str) -> tuple[str | None, bool]:
    """Classify a caller symbol: ``(asset_key, is_proxy)``.

    Mirrors the ETF module's classifier with BTC as the only native asset:
    the treasuries product tracks BTC holdings, so every other recognized
    crypto risk asset — ETH included — is served the BTC data as a labelled
    market-wide demand proxy, and a stablecoin or unrecognized symbol gets a
    no-signal note.
    """
    base = normalize_symbol((asset or "").replace("/", "-")).split("-")[0]
    if base in SUPPORTED_ASSETS:
        return base, False
    if base in CRYPTO_BASES:
        return "BTC", True
    return None, False


class _Activity(NamedTuple):
    """One rendered row of the activity table.

    A record rather than a bare tuple because ``derived`` is consulted in four
    places (the row's own label, the cost/implied suppression, the mix note's
    count, and the legend it justifies) and the aggregates read two more
    fields. Positional access to a 6-tuple couples every one of those to the
    field order: inserting a field ahead of ``since`` would silently turn the
    mix-note count into a read of ``implied`` — non-None on exactly the filed
    rows and None on every derived one — inverting the very sentence whose job
    is to disclose that the aggregate mixes filed and derived figures.
    """

    date: str
    ticker: str
    delta: float
    cost: float | None
    implied: float | None
    # The previous disclosure's date on a holdings-derived row, else None: the
    # delta then spans everything since that date rather than one filing.
    since: str | None

    @property
    def derived(self) -> bool:
        return self.since is not None


def _fmt_signed_btc(value: float) -> str:
    """A signed BTC change with thousands grouping, whole-coin granularity.

    A non-zero value that would round to zero keeps two decimals instead: a
    -0.4 BTC disposal rendered "+0" would carry the wrong sign AND contradict
    the reducers tally computed from the unrounded deltas. ``+ 0.0``
    normalizes a negative zero on the whole-coin path.
    """
    rounded = round(value)
    if rounded == 0 and value != 0:
        return f"{value:+.2f}"
    return f"{rounded + 0.0:+,.0f}"


def _fmt_signed_usd_m(value: float) -> str:
    """A signed filed cost in US$m, tenth-of-a-million granularity.

    The Cost column's twin of ``_fmt_signed_btc`` and for the same reason: a
    filing under $50k rounds to "+0.0" here while the Implied US$/BTC cell on
    the same row still prints a price computed from the unrounded figure, and
    the legend says Implied is blank on a cost of zero — two cells the reader
    is told cannot both be true. Drop a granularity step instead. ``+ 0.0``
    normalizes a negative zero on the coarse path, which a sub-tick disposal
    would otherwise render as "-0.0"; the fine path needs no such guard for
    the values it is reached with, but shares the residual below: a filing
    under $500 renders "+0.000"/"-0.000". At this universe (top-15 corporate
    holders) that is not a reachable disclosure, and an unbounded precision
    escape would make the column unreadable.
    """
    millions = value / _USD_PER_MILLION
    rounded = round(millions, 1)
    if rounded == 0 and millions != 0:
        return f"{millions:+,.3f}"
    return f"{rounded + 0.0:+,.1f}"


def _fmt_btc(value: float) -> str:
    return f"{value:,.0f}"


def get_btc_treasury_data(
    asset: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """Fetch corporate BTC treasury holdings/activity as a markdown report.

    Args:
        asset: "BTC" natively; another recognized crypto risk asset (ETH,
            SOL, ...) is served the BTC data as a market-wide demand proxy;
            a stablecoin or unrecognized symbol gets a no-signal message.
        curr_date: End of the window (yyyy-mm-dd); disclosures dated after it
            are dropped so a past date never leaks future filings.
        look_back_days: Trailing window for the activity section; ``None``
            uses DEFAULT_LOOKBACK_DAYS (90 — disclosures are sparse).

    Returns:
        A markdown report: combined and top-5 holdings (each company as of
        its latest visible disclosure), a windowed activity table of
        disclosed holdings changes (buys positive, disposals negative, with
        an implied US$/BTC where cost was filed), and coverage caveats.

    Raises:
        SoSoValueError: if ``curr_date`` is not a yyyy-mm-dd date, if
            ``look_back_days`` is not an integer, or if
            ``asset`` is truthy but not a string (a caller's malformed argument
            is reported as this vendor's error class rather than left to escape
            as a raw ``ValueError``/``AttributeError``, which the router would
            render into the model-visible sentinel — with the argument echoed
            verbatim in the ``curr_date`` case); or if the live fetch fails —
            including on a pure network error — and no cache is usable, because
            none exists or the newest is past ``MAX_STALE_DAYS``. An
            unrecognized but well-formed asset is NOT an error: it returns a
            no-signal message.
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
    # Guarded like curr_date and asset below, and for the same reason: a
    # non-int reaches ``<=`` first, where the TypeError escapes the vendor
    # taxonomy into the router's bare except and is rendered into the
    # model-visible sentinel. bool is rejected explicitly even though it passes
    # isinstance(x, int) — True would silently mean a one-day window rather
    # than the default. Mirrors the macro twin.
    if look_back_days is not None and (
        isinstance(look_back_days, bool) or not isinstance(look_back_days, int)
    ):
        raise SoSoValueError(
            f"look_back_days {_sanitize(repr(look_back_days), limit=120)} is not an integer"
        )
    if look_back_days is None or look_back_days <= 0:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    # Normalise curr_date BEFORE any lexical date comparison (family rule).
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
        # otherwise have contributed. Mirrors the macro twin.
        detail = _sanitize(curr_date, limit=200)
        raise SoSoValueError(
            f"curr_date {detail!r} ({type(curr_date).__name__}) is not a yyyy-mm-dd date"
        ) from e
    curr_date = curr_dt.strftime("%Y-%m-%d")

    # The other caller-supplied argument, guarded the same way. A truthy
    # non-string asset reaches ``normalize_symbol((asset or "").replace(...))``
    # and escapes as AttributeError — again a caller's bug reported as a vendor
    # outage. ``bytes`` has to be caught HERE rather than duck-typed away:
    # ``bytes.replace`` exists, so it survives a hasattr check and only fails
    # deeper in. isinstance rather than ``type(...) is str`` so a str subclass,
    # which every operation below handles, is still accepted.
    if asset and not isinstance(asset, str):
        raise SoSoValueError(f"asset must be a symbol string, got {type(asset).__name__}")
    # Flattened BEFORE classification, not after, so exactly one string is both
    # decided on and rendered — sanitising afterwards makes the classified and
    # the rendered strings disagree (deribit's comment spells out that failure).
    # ``asset`` is an LLM-written tool argument echoed into a markdown ``##``
    # heading, an emphasis caveat and the no-signal sentence, so every copy is a
    # chance to forge structure: an asset of
    # "ETH-USD | ## Combined holdings: 9,999,999 BTC" classifies on its base
    # "ETH", takes the proxy branch, and lands that heading inside the report.
    asset = _sanitize(asset, limit=200)
    asset_key, market_proxy = _classify_asset(asset)
    if asset_key is None:
        return (
            f"There is no corporate BTC-treasury signal for '{asset}': it is not a "
            f"recognized crypto risk asset for which BTC treasury flows serve as a "
            f"market-wide demand proxy (e.g. a stablecoin or an unrecognized symbol). "
            f"Do not substitute BTC figures."
        )

    snapshot = _load_snapshot()

    # Visible rows per company; a company's latest disclosure is its last
    # visible row (one source of truth — a parallel latest-row dict could
    # silently disagree after a later filter).
    visible_by_company: dict[str, list[dict]] = {}
    for ticker, company in snapshot.companies.items():
        visible = [r for r in company["rows"] if r["date"] <= curr_date]
        if visible:
            visible_by_company[ticker] = visible

    if market_proxy:
        header_lines = [
            f"## BTC Corporate Treasuries (market-wide demand proxy for '{asset}', SoSoValue)",
            f"_Corporate treasuries hold BTC, not '{asset}'; showing BTC treasury "
            f"holdings and flows as a market-wide crypto demand proxy, not an "
            f"'{asset}'-specific signal._",
        ]
    else:
        header_lines = ["## BTC Corporate Treasuries (SoSoValue)"]

    if snapshot.stale:
        age_str = _humanize_age(snapshot.fetched_at)
        header_lines.append(
            f"_STALE by {age_str}: live refresh failed (network error, rate limit, or "
            f"an API contract break); showing the last cached snapshot (fetched "
            f"{snapshot.fetched_at}). Treat with caution._"
        )
    # NOT gated on staleness. The window below is labelled by curr_date, but no
    # snapshot can carry a disclosure filed after it was fetched, so its most
    # recent stretch is empty by construction rather than quiet. Treasury flow
    # is lumpy and announcement-driven: "no disclosed holdings changes" is
    # exactly the sentence a reader turns into "no corporate accumulation", and
    # that misreading does not depend on the snapshot being stale — at a 24h
    # TTL any serve whose fetch fell on an earlier day has the same blind tail,
    # which is an ordinary serve rather than an edge case. The guard is the
    # fact itself (blind > 0), so on the production default
    # curr_date == fetched_at[:10] the sentence stays silent. Counterpart of
    # the macro module's sentence.
    fetched_day = snapshot.fetched_at[:10]
    blind = _days_unobserved(snapshot.fetched_at, curr_dt)
    if blind is not None and blind > 0:
        # Clamped to the window: look_back_days is a caller-supplied tool
        # argument, so an age larger than it would claim a tail longer than
        # the window it describes ("the most recent 10 days" of a 5-day
        # window). When the age swallows the window the true statement is
        # the stronger one, not a truncated version of the weaker one.
        # Strict >, not >=: the window is inclusive at both ends, so it
        # spans look_back_days + 1 days while the unobserved stretch
        # (fetched_day, curr_date] is exactly blind. At equality
        # fetched_day IS window_start, whose own filings are observable —
        # the whole-window claim needs the fetch to precede the window.
        unseen = min(blind, look_back_days)
        extent = (
            "the whole of it is"
            if blind > look_back_days
            else f"the most recent {unseen} {_plural_days(unseen)} of it "
            f"{_plural(unseen, 'is', 'are')}"
        )
        header_lines.append(
            f"_The window below is bounded by the fetch date: this snapshot cannot carry "
            f"a disclosure filed after {fetched_day}, so {extent} unobserved rather than "
            f"quiet — read a flat net change as coverage ending early, not as no "
            f"accumulation._"
        )

    selected = (
        len(snapshot.companies) + len(snapshot.companies_failed) + len(snapshot.companies_empty)
    )
    if snapshot.companies_failed:
        n = len(snapshot.companies_failed)
        # "could not be fetched" on its own reads as the provider withholding
        # these histories, and a reader who downgrades the whole feed on that
        # basis is acting on a gap this client opened. Appended rather than
        # substituted: the bucket also collects transport failures and
        # swallowed parse breaks from before the 429, so the quota explains
        # part of the list, never provably all of it. Mirrors the macro twin.
        # Two ways the sweep ends early, two different corrections: the quota is
        # a gap this client OPENED, while the breaker leaves real upstream
        # trouble that was only ever OBSERVED on the companies actually asked
        # for. Mutually exclusive by construction (either arm breaks the loop)
        # and read-side too, so the branch order carries no precedence.
        quota = ""
        if snapshot.rate_limited:
            quota = (
                " This client's own per-minute request quota ran out partway through the "
                "sweep: the request it stopped on was refused for that reason and every "
                "company behind it was never attempted, so that much of the gap is this "
                "client's rather than the provider going quiet, and it is retried on the "
                "short TTL."
            )
        elif snapshot.breaker_skipped:
            quota = (
                f" This client stopped the sweep after "
                f"{MAX_CONSECUTIVE_NETWORK_FAILURES} consecutive transport failures, so "
                f"the companies behind that point were never requested: the failures are "
                f"established only for the ones actually attempted, and the rest are "
                f"retried on the short TTL."
            )
        header_lines.append(
            f"_Coverage incomplete ({n} of {selected} selected companies): histories "
            f"for {', '.join(sorted(snapshot.companies_failed))} could not be fetched, "
            f"so the figures below exclude them.{quota}_"
        )
    if snapshot.companies_empty:
        n = len(snapshot.companies_empty)
        header_lines.append(
            f"_{n} selected {_plural(n, 'company', 'companies')} "
            f"({', '.join(sorted(snapshot.companies_empty))}) "
            f"{_plural(n, 'is', 'are')} listed by the provider but "
            f"{_plural(n, 'has', 'have')} no served purchase history at all, so "
            f"{_plural(n, 'it holds', 'they hold')} no figures below._"
        )
    if snapshot.companies_unusable:
        n = snapshot.companies_unusable
        header_lines.append(
            f"_{n} listing {_plural(n, 'entry', 'entries')} had no usable ticker and "
            f"{_plural(n, 'was', 'were')} skipped; a dropped entry may itself be a "
            f"large holder, so the top-{selected} cut may not be the true top "
            f"{selected}._"
        )
    # A fetched company whose every disclosure postdates curr_date drops out of
    # the whole report — combined holdings, the tracked count and the
    # concentration denominator all shrink with it. That is a coverage gap like
    # any other and must be disclosed, not applied silently.
    no_disclosure = sorted(set(snapshot.companies) - set(visible_by_company))
    if no_disclosure:
        n = len(no_disclosure)
        header_lines.append(
            f"_{n} fetched {_plural(n, 'company', 'companies')} "
            f"({', '.join(no_disclosure)}) {_plural(n, 'has', 'have')} no disclosure "
            f"dated on or before {curr_date}, so {_plural(n, 'it is', 'they are')} "
            f"excluded from every figure below, including the combined total, the "
            f"tracked-company count and the concentration share._"
        )

    header_lines.append(
        "_Dates are disclosure dates and may lag the underlying transactions; some "
        "companies disclose only monthly or quarterly snapshots, so each holding is "
        "as of that company's own latest filing, not a common date._"
    )
    header_lines.append(
        "_Treasury flow is announcement-driven and lumpy — read it as a medium-term "
        "demand-side signal, not a timing signal._"
    )
    # Which of the two states the flag records, said apart the way the
    # fetch-time log already says them apart. Below two fetched histories
    # nothing was compared, so calling the listing misordered would be the same
    # unearned assertion the flag exists to avoid; an actual contradiction is
    # the stronger fact — the provider's own ordering disagrees with the
    # figures it served — and reached the reader as the same hedge. Recomputed
    # rather than inferred from len(): a cache may legitimately carry True over
    # holdings that do descend, and that third state must claim neither.
    if not snapshot.order_unverified:
        ordering = "provider lists largest holders first"
    else:
        # Recomputed rather than inferred from the flag alone: the flag records
        # only THAT the claim is unearned, and the two causes read very
        # differently to an analyst. "only 1 history" is exact — _fetch_all
        # raises rather than returning zero companies and _read_cache rejects
        # an empty dict, so the too-few case is always exactly one.
        order = _holdings_order(snapshot.companies)
        if order == _ORDER_TOO_FEW:
            ordering = (
                "provider ordering unchecked — only 1 history fetched, so nothing was "
                "compared; these may not be the true largest holders"
            )
        elif order == _ORDER_CONTRADICTED:
            ordering = (
                "the fetched holdings contradict the provider's listing order, so this "
                "selection may not be the true largest holders"
            )
        else:
            # A cache may legitimately carry True over holdings that do
            # descend (see _read_cache), so this third state claims neither.
            ordering = (
                "provider ordering unverified for this snapshot, so these may not be the "
                "true largest holders"
            )
    # The rows are lookahead-filtered to curr_date but the SELECTION is not:
    # the listing is fetched now and ranked by present holdings. On a
    # historical curr_date that is a hindsight universe — a company that was a
    # large holder then and has since fallen out of the listing's head is
    # absent from every figure here, and the no_disclosure caveat cannot name
    # it because it was never fetched. Same class of disclosure as the macro
    # module's "current figures, not point-in-time snapshots".
    header_lines.append(
        f"_The company universe is the provider's listing order at the time this "
        f"snapshot was fetched. Individual disclosures are filtered to {curr_date}, but "
        f"the top-{selected} cut is not — so where {curr_date} sits earlier than that "
        f"fetch, a company that ranked large then and has since dropped out of the "
        f"listing's head is missing from every figure below with nothing above naming "
        f"it._"
    )
    # "listed companies" only while the two numbers come from the same
    # universe. companies_total is len(listing) AFTER _parse_company_list drops
    # entries with no valid ticker, so with companies_unusable > 0 the
    # denominator is the provider's count minus this client's drops — "top 15
    # of 47 listed companies" would state a provider figure that is short by
    # exactly those drops. The caveat above discloses that entries were skipped
    # and that the top-N cut may be wrong, but never that the denominator
    # itself shrank.
    # The shortfall rides inside the ordering parenthetical rather than opening
    # a second one: two bracketed clauses back to back on the same line read as
    # a formatting slip, and this line already carries three fields.
    listing_scope = (
        f"{snapshot.companies_total} listed "
        f"{_plural(snapshot.companies_total, 'company', 'companies')}"
    )
    unreadable = ""
    if snapshot.companies_unusable:
        n_bad = snapshot.companies_unusable
        listing_scope = (
            f"{snapshot.companies_total} readable listing "
            f"{_plural(snapshot.companies_total, 'entry', 'entries')}"
        )
        unreadable = f"; {n_bad} further {_plural(n_bad, 'entry', 'entries')} could not be read"
    header_lines.append(
        f"- Source: SoSoValue OpenAPI (BTC treasuries) | Snapshot fetched "
        f"{snapshot.fetched_at} | Coverage: top {selected} of "
        f"{listing_scope} ({ordering}{unreadable}) | Window ending "
        f"{curr_date}"
    )
    header = "\n\n".join(header_lines) + "\n"

    if not visible_by_company:
        # Do not assert a single cause: served-history depth is one reason this
        # can be empty, but so are the coverage gaps disclosed above, or a
        # curr_date earlier than every disclosure this snapshot holds.
        return (
            header + f"\nNo treasury disclosures on or before {curr_date} in this "
            f"snapshot. That can be the provider's served history not reaching back "
            f"this far (up to {HISTORY_LIMIT} rows per company), or the coverage gaps "
            f"disclosed above. Report this as no treasury data for the date; do not "
            f"fabricate values."
        )

    # ---- holdings -----------------------------------------------------------
    combined = sum(v[-1]["btc_holding"] for v in visible_by_company.values())
    holders = sorted(
        ((t, v[-1]) for t, v in visible_by_company.items()),
        key=lambda kv: kv[1]["btc_holding"],
        reverse=True,
    )

    def _label(ticker: str) -> str:
        # The company name is free vendor text rendered verbatim into an
        # LLM-visible line, so it is flattened here — _clean_name bounds the
        # length and charset at the parse boundary but leaves markdown intact,
        # and "*"/"`"/"|" inside a name can forge emphasis or a table cell.
        name = _sanitize(snapshot.companies[ticker]["name"])
        return f"{ticker} ({name})" if name else ticker

    top_str = "; ".join(
        f"{_label(t)} {_fmt_btc(r['btc_holding'])} BTC (as of {r['date']})"
        for t, r in holders[:TOP_HOLDERS]
    )
    n_tracked = len(visible_by_company)
    # Per-company as-of dates can differ by months (some filers are
    # quarterly); the span makes that staleness mix visible on the headline
    # figure itself, not only in the top-5 as-of dates (user decision).
    #
    # One ordering serves both the span sentence and the per-company list: two
    # independent sorts of the same data could disagree about which filing is
    # the oldest, and the two lines sit next to each other in the report.
    by_as_of = sorted((v[-1]["date"], t) for t, v in visible_by_company.items())
    # One test feeding two sentences: the concentration line's basis clause
    # below must not assert a mix of as-of dates while this line prints a
    # single one. They sit three rows apart in the report.
    single_as_of = by_as_of[0][0] == by_as_of[-1][0]
    as_of_note = (
        f"as of {by_as_of[0][0]}"
        if single_as_of
        else f"as-of dates span {by_as_of[0][0]} → {by_as_of[-1][0]}"
    )
    # Every contributor's as-of date, not just the top five (user decision):
    # the combined total and the concentration share weight all of them
    # equally, so a company that stopped filing a year ago carries its stale
    # holdings at full weight into a figure the report presents as current —
    # and outside the top five it had no visible date at all. Oldest first,
    # because those are the ones worth discounting.
    as_of_line = "; ".join(f"{t} {d}" for d, t in by_as_of)
    holdings_block = (
        f"\n**Combined holdings:** {_fmt_btc(combined)} BTC across {n_tracked} tracked "
        f"{_plural(n_tracked, 'company', 'companies')}, each as of its latest "
        f"disclosure on or before {curr_date} ({as_of_note})\n"
        f"**Top holders:** {top_str}\n"
        f"**As-of date of every company in that total (oldest first):** {as_of_line}\n"
        f"_No filing-age cut is applied: a company that has not disclosed for months "
        f"still contributes its last known holding at full weight above, so read the "
        f"oldest dates in that list as the staleness carried by the combined figure._\n"
    )
    if combined > 0:
        top_ticker, top_row = holders[0]
        share = top_row["btc_holding"] / combined * 100
        # "100" may be printed only for a share that IS the whole. Any
        # ROUNDING format breaks that on its own boundary — .0f fails from
        # 99.5 and .1f fails again from 99.95 — so the near-100 band is
        # truncated toward zero instead, which can never round up into the
        # claim that one company is the entire multi-company total.
        if share >= 100:
            share_str = "100%"
        elif round(share) >= 100:
            share_str = f"{math.floor(share * 10) / 10:.1f}%"
        else:
            share_str = f"{share:.0f}%"
        # Only claim dominance where the arithmetic supports it. Printed
        # unconditionally, this clause told the reader to weight one filer's
        # disclosures above everything else even at a 14% share — reachable
        # whenever the mega-holder lands in companies_failed / companies_empty
        # or has no disclosure on or before curr_date.
        dominance = (
            " and, at more than half the total, the combined figure moves mostly with "
            "what this one company does"
            if share > 50
            else ""
        )
        # With one contributor — routine after a mid-sweep 429, or on a
        # historical curr_date that leaves the rest in no_disclosure — every
        # holding shares one as-of date, and the mixed-dates clause would
        # contradict the combined-holdings line three rows above.
        basis = (
            f"and it divides holdings that are all as of {by_as_of[0][0]}"
            if single_as_of
            else "and not a single-date measure (it divides holdings carrying mixed as-of dates)"
        )
        holdings_block += (
            f"**Concentration:** largest holder {top_ticker} = {share_str} of the "
            f"{n_tracked}-company combined total above — not of the whole market, "
            f"{basis}{dominance}\n"
        )

    # ---- activity -----------------------------------------------------------
    window_start = (curr_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    events: list[_Activity] = []
    underivable = 0
    for ticker, visible in visible_by_company.items():
        for i, row in enumerate(visible):
            if row["date"] < window_start:
                continue
            if row["btc_acq"] is not None:
                delta = row["btc_acq"]
                since = None
            elif i > 0:
                # A holdings-only disclosure (live-verified: MARA): the change
                # is implied by the move from the previous disclosure, and it
                # spans everything SINCE that disclosure — the tag carries the
                # start date so a multi-month drift is not read as a one-day
                # event (user decision).
                delta = row["btc_holding"] - visible[i - 1]["btc_holding"]
                since = visible[i - 1]["date"]
            else:
                # First served row and no filed quantity: no baseline to
                # derive a change from — disclosed below, not guessed.
                underivable += 1
                continue
            # A filed cost belongs to a filed quantity. On a holdings-derived
            # row the BTC change spans every transaction since the previous
            # disclosure while acq_cost is one filing's own figure, so the two
            # cells sit side by side with independent signs — a disposal row
            # carrying a positive cost, which reads as a purchase of that size.
            # Drop the cost on those rows (user decision) rather than print a
            # number the legend cannot describe truthfully.
            cost = row["acq_cost"] if since is None else None
            # An implied price only makes sense for a filed quantity, and a
            # zero cost is not a price: a self-mined coin filed at 0 would
            # render an implied US$/BTC of exactly 0, indistinguishable in the
            # column from a computed one.
            implied = abs(cost / delta) if cost and delta != 0 else None
            events.append(_Activity(row["date"], ticker, delta, cost, implied, since))
    events.sort(key=lambda e: (e.date, e.ticker))

    # History-depth honesty, gated on the provider's per-company row cap.
    # ``len(rows) >= HISTORY_LIMIT`` is what makes the claim true, matching the
    # macro twin: only a history the per-request cap actually truncated can be
    # hiding earlier activity. A company that simply began disclosing inside
    # the window — a recent adopter, the common shape among smaller filers —
    # has nothing older to show, and saying otherwise invents a gap and tells
    # the reader to discount a net-change figure that is in fact complete.
    # Measured on the full served history rather than the curr_date-filtered
    # view: the cap drops the OLDEST rows, independently of that filter.
    shallow = sorted(
        t
        for t, visible in visible_by_company.items()
        if len(snapshot.companies[t]["rows"]) >= HISTORY_LIMIT and visible[0]["date"] > window_start
    )

    if events:
        shown = events[-MAX_ROWS:]
        note = (
            f"\n_(showing the most recent {MAX_ROWS} of {len(events)} disclosures in the window)_\n"
            if len(events) > MAX_ROWS
            else ""
        )
        lines = [
            "\n_BTC change is positive for an acquisition, negative for a disposal; "
            "Cost is the amount the company filed for that same event in millions of "
            "US dollars and carries the matching sign (negative = proceeds received on "
            "a disposal, not a cost); Implied US$/BTC is that filing's own cost divided "
            "by its own quantity as an absolute value — an average price for that "
            "event, not a market price. Both are blank on a row derived from a holdings "
            "change: there the BTC figure spans every transaction since the previous "
            "disclosure, so no single filed cost belongs to it. Cost is blank on a FILED "
            "row too whenever the company reported a quantity without a cost — common on "
            "monthly snapshot disclosures — so a blank Cost does not by itself mark a row "
            "as derived; only the 'from holdings change since' label does. Implied US$/BTC "
            "is blank in that case as well, and on a cost of zero or a quantity of zero — "
            "there is nothing to divide in any of those._",
            "\n| Date | Company | BTC change | Cost (US$m) | Implied US$/BTC |",
            "| --- | --- | --- | --- | --- |",
        ]
        for e in shown:
            delta_cell = _fmt_signed_btc(e.delta) + (
                f" (from holdings change since {e.since})" if e.derived else ""
            )
            cost_cell = _fmt_signed_usd_m(e.cost) if e.cost is not None else "—"
            implied_cell = f"{e.implied:,.0f}" if e.implied is not None else "—"
            lines.append(f"| {e.date} | {e.ticker} | {delta_cell} | {cost_cell} | {implied_cell} |")
        net = sum(e.delta for e in events)
        by_company: dict[str, float] = {}
        for e in events:
            by_company[e.ticker] = by_company.get(e.ticker, 0.0) + e.delta
        adders = sum(1 for v in by_company.values() if v > 0)
        reducers = sum(1 for v in by_company.values() if v < 0)
        # The rows are heterogeneous — filed quantities and holdings-derived
        # deltas — and only the rows say so. The net, the adding/reducing
        # counts and the window label all inherit that mix, so state it where
        # the aggregate is read (user decision).
        derived = sum(1 for e in events if e.derived)
        mix_note = (
            f" Of these, {derived} {_plural(derived, 'row is', 'rows are')} derived "
            f"from a holdings change rather than a filed quantity, and each spans "
            f"everything since that company's previous disclosure — which can start "
            f"before this {look_back_days}-day window, so the net and the "
            f"adding/reducing counts can cover a longer period than the label."
            if derived
            else ""
        )
        activity_block = (
            f"\n**{look_back_days}d disclosed net change:** {_fmt_signed_btc(net)} BTC "
            f"({adders} {_plural(adders, 'company', 'companies')} adding, {reducers} "
            f"reducing, of {n_tracked} tracked).{mix_note}\n" + "\n".join(lines) + "\n" + note
        )
    else:
        latest_any = max(v[-1]["date"] for v in visible_by_company.values())
        activity_block = (
            f"\n**{look_back_days}d disclosed net change:** no disclosed holdings "
            f"changes in the window ending {curr_date} (latest disclosure across "
            f"tracked companies: {latest_any})\n"
        )
    if underivable:
        activity_block += (
            f"\n_{underivable} holdings-only "
            f"{_plural(underivable, 'disclosure', 'disclosures')} in the window "
            f"{_plural(underivable, 'is', 'are')} not in the table: as the first "
            f"served row of {_plural(underivable, 'its company', 'their companies')}, "
            f"no prior disclosure exists to derive the change from._\n"
        )
    if shallow:
        activity_block += (
            f"\n_The served history for {', '.join(shallow)} runs to the provider's "
            f"per-company cap (at least {HISTORY_LIMIT} rows) and still starts inside "
            f"the window, so earlier activity exists that this snapshot cannot show; "
            f"the window totals can understate it._\n"
        )
    if not events:
        # Every bucket that contributed nothing, not just the failures: a
        # company the provider lists with no served history at all, and one
        # whose disclosures all postdate curr_date, leave the same hole.
        gap = _coverage_gap_note(
            set(snapshot.companies_failed) | set(snapshot.companies_empty) | set(no_disclosure),
            "contributed nothing",
            "no company disclosed a change",
        )
        if gap:
            activity_block += f"\n_{gap}_\n"

    return header + holdings_block + activity_block
