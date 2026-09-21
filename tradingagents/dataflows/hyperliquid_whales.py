"""Hyperliquid whale-positioning vendor (public stats leaderboard + info API).

Aggregates the open perpetual positions of the largest Hyperliquid mainnet
accounts into one coin's long/short picture: how many of the sampled accounts
sit on each side, the notional behind each side, the long/short ratio, the
notional-weighted leverage, the three largest positions, and the change in all
of that since a snapshot about a day earlier. Surfaced to the news analyst as a
crypto-only *positioning* signal alongside ETF flows, Fear & Greed and the
treasury feed.

Two keyless endpoints, both public:

* ``GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`` — every
  ranked account with its ``ethAddress`` and ``accountValue``. Undocumented
  (a stats endpoint behind the web UI, not part of the published API), so the
  URL and the parse are isolated here and any failure degrades the optional
  ``whale_positioning`` category to the router sentinel rather than raising
  into a cycle. It is also LARGE — 38 MB / ~46k rows measured 2026-09-21 — so
  the body is read under a hard byte cap and only the top-N addresses are
  kept.
* ``POST https://api.hyperliquid.xyz/info`` with
  ``{"type": "clearinghouseState", "user": "0x..."}`` — the documented
  endpoint, one request per sampled address.

What this signal is, and is not. The leaderboard ranks by ITS OWN
``accountValue``, which is not the perp account equity the info endpoint
reports: of the top 20 addresses measured on 2026-09-21, eleven held no perp
position at all (several reported a perp account value of zero), and the ones
that did were dominated by large, systematically short books — market makers
and vaults hedging exposure held elsewhere rather than accounts expressing a
directional view. So the aggregate is *venue-level positioning of the biggest
accounts*, not a crowd-sentiment gauge, and the report says so, prints how many
of the sampled accounts actually held the coin, and never presents an absent
coin as an absence of open interest. ``TOP_N`` is the one knob that changes the
sample's character; it is deliberately small enough to finish inside the
fan-out budget below.

Live-only. Neither endpoint has a historical form, so a past ``curr_date``
cannot be served as that date's state: the report is labelled with the UTC
instant it was fetched, and a ``curr_date`` meaningfully behind the wall clock
additionally carries the shared live-snapshot disclosure (decision 6 of the
data-source plan: label live-only data, never dress it as history).

Caching. One rolling file holds the newest snapshot, a bounded history of
earlier snapshots' per-coin aggregates, and the trimmed leaderboard. A call
within ``SNAPSHOT_TTL_MINUTES`` of the newest snapshot reuses it, which is what
keeps the N+1 fan-out off every tool call; the leaderboard carries its own,
much longer TTL because the largest accounts change slowly and re-downloading
38 MB hourly buys nothing. A refresh failure falls back to the newest snapshot
(marked STALE) up to ``MAX_STALE_HOURS`` and no further — positioning presented
as live must not be half a day old. A failed fetch is never written to cache.

The history is what makes the 24-hour change computable at all, and it is why
this module keeps more than the two snapshots the plan sketched: with an hourly
TTL the second-newest snapshot is an hour old, never a day, so a two-slot cache
can never hold a comparison point in the 20-30 hour band. The entries are
per-coin aggregates only (a few hundred bytes each), pruned to
``HISTORY_KEEP_HOURS``.

As with every cache in this package, the TTLs are enforced only by that file
existing: a non-persistent ``data_cache_dir`` (tmpfs, a container with no
volume, CI) turns every call into a full refresh, and leaves the 24-hour change
permanently unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import NamedTuple

import requests

from .errors import VendorError, VendorRateLimitError, VendorUnavailableError
from .sosovalue_common import (
    _cache_dir,
    _cache_rejecter,
    _read_cache_preamble,
    _stale_caveat,
    fmt_signed_usd_m,
    fmt_usd_m,
)
from .symbol_utils import CRYPTO_BASES, classify_crypto_asset
from .utils import (
    date_refusal,
    echo_argument,
    failure_account,
    is_unreached,
    json_body_or_outage,
    json_bytes_or_outage,
    live_snapshot_note,
    quote_argument,
    raise_for_http_status,
)

logger = logging.getLogger(__name__)

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

# How many leaderboard addresses are sampled, ranked by the leaderboard's own
# account value. The sample's character, not just its size: see the module
# docstring for what that ranking does and does not select for.
#
# Twenty finishes inside POSITION_FETCH_BUDGET_S at the measured per-request
# latency (0.45s mean, 0.83s worst, 2026-09-21), which with the throttle below
# is ~0.65s per address: ~13s for the sweep, ~19s with a leaderboard refresh.
# Raising it is not the free win it looks like. Measured the same day, against
# the same ranking: the top 20 held 5 BTC positions, the top 50 held 8 and the
# top 100 held 12 — and the accounts between rank 20 and 100 were almost all
# short and mostly small, so a larger sample deepens the short skew described
# in the module docstring rather than balancing it. N=50 also no longer fits
# this budget serialized (~32s), so it would cost a concurrency change too.
TOP_N = 20

# Minimum spacing between two clearinghouseState requests. The endpoint
# publishes no rate limit for this weight class; 200ms caps this module at
# 5 req/s, which is polite for a fan-out that runs at most once an hour.
MIN_REQUEST_INTERVAL_S = 0.2

# Wall-clock ceiling on the whole per-address fan-out. A network that hangs
# instead of failing fast would otherwise turn one refresh into TOP_N
# sequential timeouts inside a single analyst tool call. On exhaustion the
# snapshot is built from whatever answered and the report discloses "k of N".
POSITION_FETCH_BUDGET_S = 30.0

# Per-request timeouts. The leaderboard's is generous because the body is tens
# of megabytes (5.8s measured); the info endpoint's is short so one stuck
# address cannot eat the whole budget above.
LEADERBOARD_TIMEOUT = 60
POSITION_TIMEOUT = 10

# Hard cap on the leaderboard body. The endpoint is undocumented, so its size
# is not a contract; reading it unbounded into memory is how a changed endpoint
# becomes an OOM instead of a degraded category. 38 MB measured 2026-09-21, so
# this is roughly 3x headroom.
MAX_LEADERBOARD_BYTES = 128 * 1024 * 1024

# Chunk size for the capped streaming read above.
_LEADERBOARD_CHUNK_BYTES = 1024 * 1024

# How long the newest snapshot may be reused before the fan-out runs again.
# Positions move continuously, but the analyst cycle is hours long and each
# refresh costs TOP_N requests plus (at most) the leaderboard download.
SNAPSHOT_TTL_MINUTES = 60

# How long the trimmed leaderboard may be reused. The top-N-by-account-value
# cohort turns over slowly, and a stable cohort is what makes the 24-hour
# change a comparison of the same accounts rather than of two samples.
LEADERBOARD_TTL_HOURS = 12

# How stale a leaderboard may be when its own refresh fails and positions can
# still be fetched. Longer than the TTL: an out-of-date cohort still yields a
# real positioning read (and the report dates the cohort), where no cohort at
# all yields nothing.
LEADERBOARD_MAX_STALE_HOURS = 72

# How stale the newest snapshot may be served when a refresh fails. Short, and
# much shorter than the ETF-flow vendors' day-scale caps: this report is
# labelled a live snapshot, and positioning from half a day ago presented under
# that heading would be a false claim rather than a lagging one.
MAX_STALE_HOURS = 6

# The band an earlier snapshot must fall in to serve as the "24h ago"
# comparison point, and the target inside it. Wider than a day on both sides
# because the refresh cadence is set by whenever an analyst cycle asks, not by
# a scheduler: a strict 24h match would almost never exist.
DELTA_MIN_HOURS = 20.0
DELTA_TARGET_HOURS = 24.0
DELTA_MAX_HOURS = 30.0

# How long a history entry is kept. Must stay >= DELTA_MAX_HOURS or the
# comparison point would be pruned before it becomes eligible; the assertion
# below is the early word on that.
HISTORY_KEEP_HOURS = 32.0

# Ceiling on history entries regardless of age, so a caller that refreshes far
# faster than the TTL cannot grow the file without bound.
MAX_HISTORY_ENTRIES = 64

# Tuning either constant past the other would silently make the 24-hour change
# unreachable — no exception, no log line, just an "n/a" the reader reads as a
# cold start forever. This fails at import instead, before a cycle runs; it is
# stripped under ``python -O``, so the suite pins the same relation.
assert HISTORY_KEEP_HOURS >= DELTA_MAX_HOURS, (
    "history is pruned before an entry can reach the comparison band: "
    f"HISTORY_KEEP_HOURS={HISTORY_KEEP_HOURS} < DELTA_MAX_HOURS={DELTA_MAX_HOURS}"
)

# Positions listed individually in the report.
TOP_POSITIONS = 3

# Cache payload version. A payload written by an older build is ignored rather
# than migrated: it costs one refresh, where a half-understood old shape costs
# a wrong report.
CACHE_SCHEMA = 1

# An EVM address as both endpoints spell it. The leaderboard's addresses are
# vendor-authored text that lands in the report AND in an outbound request
# body, so a row whose address is not exactly this shape is dropped rather than
# sanitized into something that looks like an address.
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# A Hyperliquid perp symbol. Wider than the crypto bases this module can be
# asked about (the venue lists ``kPEPE``, ``@107`` and similar), because every
# coin in a sampled account's book passes through the parser on its way to the
# history aggregate, not just the requested one. Anything outside this shape is
# a contract change, not a coin.
_COIN_RE = re.compile(r"^[A-Za-z0-9@_\-]{1,20}$")



class HyperliquidWhalesError(VendorError):
    """Hyperliquid's public endpoints returned an unusable payload, or refused us.

    A ``VendorError`` (the shared taxonomy in ``errors.py``) so the routing
    layer reacts by behaviour rather than by vendor, and the optional
    ``whale_positioning`` category degrades to a sentinel instead of aborting
    the run. Every failure mode — network error, non-2xx, undecodable body,
    wrong payload shape, an oversized leaderboard, a fan-out where nothing
    answered — is funnelled through this one type; the ones that mean the
    venue was DOWN come as the subclass below.
    """


class HyperliquidWhalesUnavailableError(HyperliquidWhalesError, VendorUnavailableError):
    """Hyperliquid was down: unreachable, a 5xx, or a body that is not JSON.

    The router logs this lane without a traceback and counts the vendor as
    down (#172). A ``HyperliquidWhalesError`` too, so every caller written
    against the module type keeps working unchanged.
    """


class HyperliquidWhalesRateLimitError(HyperliquidWhalesError, VendorRateLimitError):
    """Hyperliquid refused a request with HTTP 429.

    A type of its own because the three reactions differ and only the type
    carries which one applies. The shared ``raise_for_http_status`` types a 5xx
    and leaves every other status to ``requests``, and ``is_unreached`` excludes
    ``requests.HTTPError`` — so without this a throttle would be filed as this
    module's STRUCTURAL error and get the ending reserved for a broken parser:
    an ERROR with a traceback saying the endpoint likely changed, and a router
    verdict of "the client needs a fix" for something no code change heals.

    The distinction earns its keep three times over: the router arms its
    per-vendor throttle latch on this type, so the cycle's other tool calls stop
    re-discovering the same refusal; the sweep below drains on it instead of
    spending the remaining requests learning the same thing (the info endpoint's
    budget is per-IP, so a refusal is a fact about this client, not about one
    address); and the stale-cache lane logs it as the vendor answering rather
    than as breakage. The SoSoValue, Deribit, Alpha Vantage and yfinance
    boundaries all draw the same line.
    """


class WhalePosition(NamedTuple):
    """One sampled account's open position in one coin.

    ``notional`` is the venue's ``positionValue``, which is unsigned; the side
    lives in the sign of ``szi`` alone, so the two are never read apart.
    ``entry_px`` and ``leverage`` are optional because a contract change that
    drops either must cost a column, not the report.
    """

    address: str
    coin: str
    szi: float
    notional: float
    entry_px: float | None
    leverage: float | None

    @property
    def is_long(self) -> bool:
        return self.szi > 0


class CoinAggregate(NamedTuple):
    """The sampled accounts' combined position in one coin.

    ``levered_notional`` is the notional that actually carried a readable
    leverage figure, i.e. the weight behind ``avg_leverage``; without it a
    weighted average over half the book would read as one over all of it.
    """

    coin: str
    long_count: int
    short_count: int
    long_notional: float
    short_notional: float
    avg_leverage: float | None
    levered_notional: float
    top: tuple[WhalePosition, ...]

    @property
    def holders(self) -> int:
        return self.long_count + self.short_count

    @property
    def total_notional(self) -> float:
        return self.long_notional + self.short_notional

    @property
    def ratio(self) -> float | None:
        """This snapshot's long/short ratio, by the one rule (``_ratio_of``)."""
        return _ratio_of(self.long_notional, self.short_notional)


class _Snapshot(NamedTuple):
    """What one call has to render from, whether it fetched or read the cache."""

    current: dict
    history: list[dict]
    leaderboard: dict
    stale: bool


def _ratio_of(long_notional: float, short_notional: float) -> float | None:
    """Long notional over short notional, or None when there is no short side.

    None rather than infinity: "all long" is a statement the report makes in
    words, and a rendered ``inf`` would read as a figure.

    One function because the SAME report renders this twice from two different
    shapes — the current snapshot's typed ``CoinAggregate`` and the baseline's
    plain per-coin totals out of the history — and the 24h line prints them
    side by side as ``A -> B``. Two hand-written copies of the rule would
    disagree exactly at the boundary case (a coin whose short side is zero),
    which is the one place the arrow would read as a move that never happened.
    """
    if short_notional <= 0:
        return None
    return long_notional / short_notional


def _sleep(seconds: float) -> None:
    """The fan-out's throttle, as one patchable seam (tests must not sleep)."""
    time.sleep(seconds)


def _utc_now() -> datetime:
    """The single UTC clock source (tests patch this one function)."""
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    """Current UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` for a snapshot's fetched_at."""
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(fetched_at: object) -> datetime | None:
    """A ``fetched_at`` stamp as a UTC datetime, or None if it cannot be read."""
    if not isinstance(fetched_at, str) or not fetched_at:
        return None
    try:
        return datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_hours(fetched_at: object) -> float | None:
    """Hours since ``fetched_at``, or None if the stamp cannot be parsed.

    None is left to the caller to treat as "not fresh", forcing a refresh; a
    NEGATIVE result (a future-dated stamp from clock skew or a tampered file)
    is returned as it is, so each caller refuses it explicitly rather than
    having this helper decide what a future stamp means.
    """
    stamp = _parse_stamp(fetched_at)
    if stamp is None:
        return None
    return (_utc_now() - stamp).total_seconds() / 3600.0


def _hours_between(older: object, newer: object) -> float | None:
    """Hours from ``older`` to ``newer``, or None if either stamp is unreadable.

    The 24-hour comparison is measured between the two SNAPSHOTS, not against
    the wall clock, so a stale serve compares the snapshot it is showing with
    one a day before THAT — and the figure does not drift while the cache sits.
    """
    a, b = _parse_stamp(older), _parse_stamp(newer)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _humanize_hours(hours: float) -> str:
    """A readable span for the STALE caveat and the cohort's reuse log.

    Day-granular beyond a day, where those two reach (a 72-hour cohort reads
    better as "3.0 days"). The 24-hour change does NOT use this: its span is
    bounded to the 20-30 hour band by construction, where day granularity
    would round 25 and 30 hours to figures a reader cannot tell apart.
    """
    if hours < 24:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _stale_age(fetched_at: str) -> str:
    """This module's age formatter for the shared STALE template.

    Supplied rather than defaulted because the template's default reads the
    SoSoValue family's clock, while every freshness decision here reads
    ``_utc_now`` above - the one the tests patch. An unreadable or future-dated
    stamp is not normally reachable (``_load_snapshot`` degrades rather than
    serving such a snapshot), but a negative age rendered as "-12.0 hours"
    would be worse than admitting the age is unknown.
    """
    hours = _age_hours(fetched_at)
    if hours is None or hours < 0:
        return "an unknown age"
    return _humanize_hours(hours)


def _cohort_digest(addresses: list[str]) -> str:
    """A short, order-independent fingerprint of the sampled address set.

    Persisted with each snapshot so the 24-hour change can say whether it is
    comparing the same accounts. Without it a cohort turnover at the
    leaderboard TTL would show up as a position change nobody made.
    """
    joined = "\n".join(sorted(a.lower() for a in addresses))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def _abbrev(address: str) -> str:
    """``0x1234…abcd`` — the address as the report shows it.

    Only ever applied to a string that matched ``_ADDRESS_RE``, so the ellipsis
    cannot be hiding characters that would have mattered.
    """
    return f"{address[:6]}…{address[-4:]}"


def _is_address(value: object) -> bool:
    """Whether ``value`` is an EVM address exactly as both endpoints spell it."""
    return isinstance(value, str) and _ADDRESS_RE.fullmatch(value) is not None


def _is_coin(value: object) -> bool:
    """Whether ``value`` is a venue perp symbol this module will carry."""
    return isinstance(value, str) and _COIN_RE.fullmatch(value) is not None


def _count_ok(value: object, *, minimum: int = 0) -> bool:
    """Whether ``value`` is a plain integer count of at least ``minimum``.

    ``bool`` is excluded because it is an ``int`` subclass, so a JSON ``true``
    would otherwise pass as a count of 1. One spelling, because the three
    validators below each check counts and a copy that quietly dropped the
    ``bool`` clause would still look right.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _amount_ok(value: object) -> bool:
    """Whether ``value`` is a finite, non-negative notional."""
    amount = _finite_float(value)
    return amount is not None and amount >= 0


def _finite_float(value: object) -> float | None:
    """``value`` as a finite float, or None. The venue sends numbers as strings.

    bool is rejected along with the rest: it is an ``int`` subclass, so a JSON
    ``true`` would otherwise become a position size of 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _failure_class(e: Exception) -> type[HyperliquidWhalesError]:
    """Which of this module's types a failed request becomes.

    One authoring, because both request boundaries ask it and the answer has
    to be the same: the vendor was DOWN when it could not be reached, or when
    the shared status helper already judged it so (it raises the BARE shared
    type for a 5xx, which is why that type is caught at all); anything else it
    answered with is this module's structural error.
    """
    down = isinstance(e, VendorUnavailableError) or is_unreached(e)
    return HyperliquidWhalesUnavailableError if down else HyperliquidWhalesError


def _raise_for_rate_limit(response, vendor: str) -> None:
    """Raise the rate-limit type for a 429, before any other status reading.

    Ahead of ``raise_for_http_status`` because that helper types only a 5xx and
    hands everything else to ``requests.raise_for_status()``, whose
    ``HTTPError`` this module's boundaries would then file as structural. The
    message carries the status only, never the body: it travels into a sentinel
    the model reads.
    """
    if response.status_code == 429:
        # Worded distinctly, as the Deribit / Alpha Vantage / SoSoValue
        # boundaries word theirs: "answered HTTP N" belongs to
        # ``utils.generic_failure_words``, and a hand-spelled copy of a shared
        # phrase drifts the moment that one is reworded.
        raise HyperliquidWhalesRateLimitError(f"{vendor} is rate limiting this client (HTTP 429)")


def _request_leaderboard() -> dict:
    """GET the leaderboard body under a hard byte cap, or raise.

    Streamed and accumulated rather than read through ``response.json()``: the
    endpoint is undocumented and tens of megabytes, so its size is not a
    contract this module can rely on. A 429 is the vendor throttling this
    client and takes the rate-limit type above. Otherwise the family's split:
    an unreachable host,
    a 5xx and a body that does not decode at all (a CDN or WAF interstitial
    served with a 2xx) are the vendor being DOWN and take the outage type,
    which the router logs without a traceback and counts as an outage; a body
    that decodes to the wrong SHAPE is what an endpoint change looks like and
    stays the module type. Both are this module's types, never the bare
    ``VendorUnavailableError`` the shared status helper raises: the cache lane
    above catches ``HyperliquidWhalesError``, so a leaked bare type would walk
    past the stale-snapshot fallback on its way to the router.
    """
    response = None
    try:
        response = requests.get(LEADERBOARD_URL, timeout=LEADERBOARD_TIMEOUT, stream=True)
        _raise_for_rate_limit(response, "Hyperliquid stats")
        raise_for_http_status(response, "Hyperliquid stats")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_LEADERBOARD_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > MAX_LEADERBOARD_BYTES:
                raise HyperliquidWhalesError(
                    f"Hyperliquid leaderboard exceeded the "
                    f"{MAX_LEADERBOARD_BYTES // (1024 * 1024)} MB cap; treating the endpoint "
                    f"as changed rather than reading it further"
                )
    except (requests.RequestException, VendorUnavailableError) as e:
        # The cause is quoted through ``failure_account``: a requests exception
        # contributes its status or class only, never its message, which
        # carries the request URL (#203).
        raise _failure_class(e)(
            f"Hyperliquid leaderboard request failed ({failure_account(e)})"
        ) from e
    finally:
        # A streamed response holds its connection until it is released, and
        # the cap above leaves the body half-read on purpose.
        if response is not None:
            response.close()

    try:
        # ``body`` is handed over as the bytearray the chunked read built:
        # ``json.loads`` takes one directly, and copying it to ``bytes`` first
        # would hold two full copies of a tens-of-megabytes body at once.
        payload = json_bytes_or_outage(body, "Hyperliquid stats", response.status_code)
    except VendorUnavailableError as e:
        # The outage verdict and its sentence are the shared decoder's, as for
        # every vendor whose data answer is JSON: a body that does not decode
        # is an error page served with a 2xx, so the vendor answered without
        # data. Only the module type is added here. (The shape check below is
        # the structural half.)
        raise HyperliquidWhalesUnavailableError(str(e)) from e
    if not isinstance(payload, dict):
        raise HyperliquidWhalesError(
            f"Hyperliquid leaderboard returned a JSON {type(payload).__name__}, "
            f"expected an object"
        )
    return payload


def _parse_leaderboard(payload: dict) -> list[dict]:
    """The top ``TOP_N`` usable rows as ``{"address", "account_value"}``, ranked.

    Defensive by row, strict about the whole: a row whose address is not an EVM
    address or whose account value is not a finite number is skipped (the
    endpoint is undocumented and carries display fields this module has no use
    for), but a payload that yields NO usable rows is a contract break and
    raises, so a silently-emptied leaderboard can never read as "no whales hold
    anything". Ties break on the address so the sampled cohort is a function of
    the data rather than of dict ordering.
    """
    rows = payload.get("leaderboardRows")
    if not isinstance(rows, list) or not rows:
        raise HyperliquidWhalesError("Hyperliquid leaderboard payload has no 'leaderboardRows' list")
    by_address: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = row.get("ethAddress")
        if not _is_address(address):
            continue
        value = _finite_float(row.get("accountValue"))
        if value is None:
            continue
        address = address.lower()
        # A duplicated address would otherwise occupy two of the N slots and
        # double-count its positions in the aggregate; keep the larger figure.
        by_address[address] = max(by_address.get(address, value), value)
    if not by_address:
        raise HyperliquidWhalesError(
            f"Hyperliquid leaderboard carried {len(rows)} rows but none had a usable "
            f"address and account value"
        )
    ranked = sorted(by_address.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"address": a, "account_value": v} for a, v in ranked[:TOP_N]]


def _request_state(address: str) -> dict:
    """POST clearinghouseState for one address, or raise.

    Same split as the leaderboard boundary: a 429 is the rate-limit type,
    unreachable / a 5xx / an undecodable body is the outage type, and a decoded
    body of the wrong shape is a contract break. The first stops the sweep (the
    endpoint's budget is per-IP, so the remaining addresses would each buy the
    same refusal); the other two are absorbed per address — one account that
    cannot be read costs that account, not the report.
    """
    try:
        response = requests.post(
            INFO_URL,
            json={"type": "clearinghouseState", "user": address},
            timeout=POSITION_TIMEOUT,
        )
        _raise_for_rate_limit(response, "Hyperliquid info")
        raise_for_http_status(response, "Hyperliquid info")
        payload = json_body_or_outage(response, "Hyperliquid info")
    except (requests.RequestException, VendorUnavailableError) as e:
        raise _failure_class(e)(
            f"Hyperliquid clearinghouseState request failed ({failure_account(e)})"
        ) from e
    if not isinstance(payload, dict):
        raise HyperliquidWhalesError(
            f"Hyperliquid clearinghouseState returned a JSON {type(payload).__name__}, "
            f"expected an object"
        )
    return payload


def _parse_state(address: str, state: dict) -> tuple[list[dict], int]:
    """One account's positions as plain records, plus a count of unreadable ones.

    Pure. Returns records rather than ``WhalePosition``s because these are what
    the cache file persists; the typed form is rebuilt on the way out
    (``_positions_from_records``), so the render path reads one shape whether
    the data came from the wire or from disk.

    A flat entry (``szi == 0``) is not a position and is dropped silently. An
    entry this module cannot read — no position object, an unrecognizable coin,
    an unusable size or notional — is COUNTED, because an aggregate silently
    short of a leg is worse than one that says a leg was dropped. An
    ``assetPositions`` that is not a list at all counts as one unreadable
    entry: that is the contract breaking, not an account holding nothing.
    """
    entries = state.get("assetPositions")
    if not isinstance(entries, list):
        return [], 1
    records: list[dict] = []
    malformed = 0
    for entry in entries:
        position = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(position, dict):
            malformed += 1
            continue
        coin = position.get("coin")
        szi = _finite_float(position.get("szi"))
        notional = _finite_float(position.get("positionValue"))
        if not _is_coin(coin):
            malformed += 1
            continue
        if szi is None or notional is None or notional < 0:
            malformed += 1
            continue
        if szi == 0:
            continue
        leverage = position.get("leverage")
        records.append(
            {
                "address": address,
                "coin": coin,
                "szi": szi,
                "notional": notional,
                "entry_px": _finite_float(position.get("entryPx")),
                "leverage": (
                    _finite_float(leverage.get("value")) if isinstance(leverage, dict) else None
                ),
            }
        )
    return records, malformed


def _positions_from_records(records: list[dict], coin: str) -> list[WhalePosition]:
    """The typed positions in ``coin``, from the cache's plain records.

    Matched case-insensitively: the coins this module can be ASKED about are
    upper-case bases, while the venue's own spelling is its business.
    """
    wanted = coin.upper()
    return [
        WhalePosition(
            address=r["address"],
            coin=r["coin"],
            szi=r["szi"],
            notional=r["notional"],
            entry_px=r["entry_px"],
            leverage=r["leverage"],
        )
        for r in records
        if r["coin"].upper() == wanted
    ]


def aggregate_positions(positions: list[WhalePosition], coin: str) -> CoinAggregate:
    """Combine one coin's sampled positions into the report's figures. Pure.

    The long/short split reads the SIGN of ``szi`` and the WEIGHT of
    ``notional``, which the venue reports unsigned — so a short's notional adds
    to the short side at full size rather than subtracting from the long one.
    ``avg_leverage`` is weighted by the notional that carried a leverage figure
    and is None when none did; an empty sample yields zeros and None
    throughout, which the renderer states in words rather than as "0.0x".
    """
    longs = [p for p in positions if p.szi > 0]
    shorts = [p for p in positions if p.szi < 0]
    levered = [p for p in positions if p.leverage is not None and p.notional > 0]
    levered_notional = sum(p.notional for p in levered)
    return CoinAggregate(
        coin=coin,
        long_count=len(longs),
        short_count=len(shorts),
        long_notional=sum(p.notional for p in longs),
        short_notional=sum(p.notional for p in shorts),
        avg_leverage=(
            sum(p.notional * p.leverage for p in levered) / levered_notional
            if levered_notional > 0
            else None
        ),
        levered_notional=levered_notional,
        top=tuple(sorted(positions, key=lambda p: (-p.notional, p.address))[:TOP_POSITIONS]),
    )


def _coin_totals(records: list[dict]) -> dict[str, dict]:
    """Every coin's counts and notionals in one snapshot, for the history entry.

    All coins, not just the one a call asked about: the coin is a tool argument
    and the history has to answer for whichever coin the NEXT call names. Per
    coin this is four numbers, which is what keeps the history small enough to
    hold a day and a half of snapshots.
    """
    totals: dict[str, dict] = {}
    for r in records:
        bucket = totals.setdefault(
            r["coin"].upper(),
            {"long_count": 0, "short_count": 0, "long_notional": 0.0, "short_notional": 0.0},
        )
        if r["szi"] > 0:
            bucket["long_count"] += 1
            bucket["long_notional"] += r["notional"]
        else:
            bucket["short_count"] += 1
            bucket["short_notional"] += r["notional"]
    return totals


def _fetch_positions(addresses: list[dict]) -> dict:
    """Sweep the sampled addresses, absorbing per-address failures.

    Four things end the sweep short of the full list, and the snapshot records
    which in ``stopped``, because the coverage sentence states a REASON and a
    wrong reason is worse than none: an address that fails is skipped and
    counted; the wall-clock budget stops the sweep with the remainder
    unattempted (``"budget"``); a 429 drains it (``"rate_limit"``), the info
    endpoint's budget being per-IP, so the remaining addresses would each spend
    a request learning the same refusal; and a sweep where NOTHING answered
    raises — the rate-limit type when a throttle drained it (the router then
    stands the vendor off rather than reading a routine throttle as breakage),
    the outage type when every failure was transport, and the module type when
    any structural break was in the mix.
    """
    deadline = time.monotonic() + POSITION_FETCH_BUDGET_S
    records: list[dict] = []
    attempted = answered = malformed = 0
    transport_failures = structural_failures = 0
    last_error: Exception | None = None
    stopped = ""
    for index, row in enumerate(addresses):
        if index:
            if time.monotonic() >= deadline:
                stopped = "budget"
                logger.warning(
                    "Hyperliquid whale sweep: %.0fs budget spent after %d of %d addresses; "
                    "building the snapshot from what answered (disclosed as incomplete)",
                    POSITION_FETCH_BUDGET_S,
                    attempted,
                    len(addresses),
                )
                break
            _sleep(MIN_REQUEST_INTERVAL_S)
        attempted += 1
        try:
            state = _request_state(row["address"])
        except HyperliquidWhalesRateLimitError as e:
            # Per-IP, so the refusal is about this client rather than this
            # address: the rest of the sweep has nothing new to learn and would
            # only add requests to a host already turning us away.
            last_error = e
            stopped = "rate_limit"
            logger.warning(
                "Hyperliquid whale sweep: rate limited at %s (%s); the %d addresses not "
                "yet attempted are left unattempted (disclosed as incomplete)",
                _abbrev(row["address"]),
                e,
                len(addresses) - attempted,
            )
            break
        except HyperliquidWhalesError as e:
            last_error = e
            if isinstance(e, VendorUnavailableError):
                transport_failures += 1
            else:
                structural_failures += 1
            logger.warning(
                "Hyperliquid whale sweep: %s could not be read (%s); skipping that account",
                _abbrev(row["address"]),
                e,
            )
            continue
        answered += 1
        parsed, bad = _parse_state(row["address"], state)
        records.extend(parsed)
        malformed += bad
    if answered == 0:
        if isinstance(last_error, HyperliquidWhalesRateLimitError):
            # A throttle that drained the sweep must not masquerade as
            # breakage: the router stands the vendor off on this type instead
            # of logging an ERROR with a traceback for a routine refusal. Read
            # off ``last_error`` rather than a second variable kept in step
            # with it - the sweep breaks on the throttle, so it IS the last one.
            raise HyperliquidWhalesRateLimitError(
                f"No Hyperliquid account state could be read: rate limited after "
                f"{attempted} of {len(addresses)} addresses ({failure_account(last_error)})"
            ) from last_error
        # "Purely transport" is the gate, as in the SoSoValue sweeps: a
        # structural break anywhere in the sweep means the honest verdict is
        # breakage, not an outage no code change can heal.
        failure_cls = (
            HyperliquidWhalesUnavailableError
            if transport_failures and not structural_failures
            else HyperliquidWhalesError
        )
        raise failure_cls(
            f"No Hyperliquid account state could be read: {attempted} of {len(addresses)} "
            f"addresses attempted, {transport_failures} transport failures, "
            f"{structural_failures} contract failures"
            + (f" (last: {failure_account(last_error)})" if last_error is not None else "")
        )
    return {
        "fetched_at": _iso_now(),
        "digest": _cohort_digest([row["address"] for row in addresses]),
        "sampled": len(addresses),
        "attempted": attempted,
        "answered": answered,
        "malformed": malformed,
        "stopped": stopped,
        "positions": records,
    }


def _cache_path() -> str:
    """Path of the single rolling cache file. No caller-controlled component."""
    return os.path.join(_cache_dir(), "hyperliquid_whales.json")


def _valid_leaderboard(value: object) -> bool:
    """Whether a cached leaderboard block is a usable ranked cohort."""
    if not isinstance(value, dict) or not isinstance(value.get("addresses"), list):
        return False
    if not value["addresses"] or _parse_stamp(value.get("fetched_at")) is None:
        return False
    return all(
        isinstance(row, dict)
        and _is_address(row.get("address"))
        and _finite_float(row.get("account_value")) is not None
        for row in value["addresses"]
    )


def _valid_record(r: object) -> bool:
    """Whether one cached position record is fully readable.

    Stricter than the live parse, and deliberately so: the live parse tolerates
    a bad entry by counting it, because the alternative is losing a whole
    account's read over one leg. A cache file is the lower trust tier — a
    hand-edited or older-build file must not reach the renderer carrying a
    shape the live path would have rejected — and a rejected cache costs one
    refresh.
    """
    if not isinstance(r, dict):
        return False
    if not _is_address(r.get("address")) or not _is_coin(r.get("coin")):
        return False
    szi = _finite_float(r.get("szi"))
    if szi is None or szi == 0 or not _amount_ok(r.get("notional")):
        return False
    for key in ("entry_px", "leverage"):
        if r.get(key) is not None and _finite_float(r[key]) is None:
            return False
    return True


def _valid_snapshot(value: object) -> bool:
    """Whether a cached snapshot block can be rendered from."""
    if not isinstance(value, dict):
        return False
    if _parse_stamp(value.get("fetched_at")) is None or not isinstance(value.get("digest"), str):
        return False
    counts = ("sampled", "attempted", "answered", "malformed")
    if not all(_count_ok(value.get(key)) for key in counts):
        return False
    if value.get("stopped") not in ("", *_STOP_REASONS):
        # The coverage sentence indexes ``_STOP_REASONS`` with this value: an
        # unknown one would raise inside the renderer, and a wrong one would
        # blame the wrong cause for the accounts the sweep never reached.
        return False
    if value["answered"] < 1:
        # The live path cannot produce this: ``_fetch_positions`` raises rather
        # than returning a sweep nothing answered, and ``_parse_leaderboard``
        # raises rather than returning an empty cohort. A cache file is the
        # lower trust tier, so a shape the live parse would have refused must
        # not reach the renderer — it would print "no BTC position among the 0
        # sampled accounts that answered" as though that were a reading.
        return False
    if value["answered"] > value["attempted"] or value["attempted"] > value["sampled"]:
        # The coverage line is arithmetic over these three ("k of N answered,
        # m unattempted"), so a file where they disagree renders a report that
        # contradicts itself — or a negative count of unattempted accounts.
        return False
    return isinstance(value.get("positions"), list) and all(
        _valid_record(r) for r in value["positions"]
    )


def _valid_history(value: object) -> bool:
    """Whether a cached history is a usable list of per-coin aggregate entries."""
    if not isinstance(value, list):
        return False
    for entry in value:
        if not isinstance(entry, dict) or _parse_stamp(entry.get("fetched_at")) is None:
            return False
        if not isinstance(entry.get("digest"), str):
            return False
        if not all(_count_ok(entry.get(k), minimum=1) for k in ("answered", "sampled")):
            return False
        if entry["answered"] > entry["sampled"]:
            # The delta's coverage caveat is a comparison of these two; a file
            # where they disagree would either hide an incomplete baseline or
            # invent one.
            return False
        coins = entry.get("coins")
        if not isinstance(coins, dict):
            return False
        for coin, totals in coins.items():
            if not _is_coin(coin) or not isinstance(totals, dict):
                return False
            if not all(_count_ok(totals.get(k)) for k in ("long_count", "short_count")):
                return False
            if not all(_amount_ok(totals.get(k)) for k in ("long_notional", "short_notional")):
                return False
    return True


def _read_cache(path: str) -> dict | None:
    """A fully-validated cache payload, or None if it cannot be trusted.

    Every rejection is logged: a silently-disabled cache (a future key rename,
    say) should be diagnosable rather than showing up as an unexplained
    permanent miss and an hourly 38 MB download.
    """
    reject = _cache_rejecter(path, cache_name="Hyperliquid whale cache", log=logger)
    payload = _read_cache_preamble(path, reject=reject)
    if payload is None:
        return None
    if payload.get("schema") != CACHE_SCHEMA:
        return reject(f"'schema' is {payload.get('schema')!r}, expected {CACHE_SCHEMA!r}")
    if not _valid_leaderboard(payload.get("leaderboard")):
        return reject("'leaderboard' is missing or malformed")
    if not _valid_snapshot(payload.get("current")):
        return reject("'current' is missing or malformed")
    if not _valid_history(payload.get("history")):
        return reject("'history' is missing or malformed")
    return payload


def _prune_history(history: list[dict], now_iso: str) -> list[dict]:
    """The entries still worth keeping, oldest first.

    Age is measured against the NEW snapshot rather than the wall clock, so a
    refresh prunes by the same measure the comparison later reads.
    """
    kept = [
        entry
        for entry in history
        if (age := _hours_between(entry["fetched_at"], now_iso)) is not None
        and 0 <= age <= HISTORY_KEEP_HOURS
    ]
    kept.sort(key=lambda e: e["fetched_at"])
    return kept[-MAX_HISTORY_ENTRIES:]


def _history_entry(current: dict) -> dict:
    """One snapshot's per-coin aggregate, as the history stores it.

    ``answered`` and ``sampled`` ride along because the 24-hour change is a
    subtraction against these totals, and a baseline whose own sweep was short
    of the cohort makes part of that subtraction missing coverage rather than a
    position anyone moved. The current snapshot's coverage is always stated
    (``_coverage_line``); without these the baseline's would be the one thing
    the report quietly dropped.
    """
    return {
        "fetched_at": current["fetched_at"],
        "digest": current["digest"],
        "answered": current["answered"],
        "sampled": current["sampled"],
        "coins": _coin_totals(current["positions"]),
    }


def _load_leaderboard(cached: dict | None) -> dict:
    """The ranked cohort to sample.

    A cohort younger than ``LEADERBOARD_TTL_HOURS`` is reused outright. On a
    refresh failure the cached cohort is reused up to
    ``LEADERBOARD_MAX_STALE_HOURS`` — an out-of-date cohort still reads real
    positions, and the report dates it — and beyond that the failure is raised
    for the snapshot lane to absorb or degrade.
    """
    cached_block = cached.get("leaderboard") if cached else None
    # One reading of the stamp for both decisions below: at hour granularity a
    # network round trip between them changes nothing, and two readings are two
    # things to keep in step.
    age = _age_hours(cached_block["fetched_at"]) if cached_block is not None else None
    # 0 <= age guards a future-dated stamp against being perpetually fresh.
    if age is not None and 0 <= age < LEADERBOARD_TTL_HOURS:
        return cached_block
    try:
        addresses = _parse_leaderboard(_request_leaderboard())
    except HyperliquidWhalesError as e:
        # ``age`` is None exactly when there is no cached cohort, so this
        # covers "no cohort to fall back on" as well as one past the cap.
        if age is not None and 0 <= age <= LEADERBOARD_MAX_STALE_HOURS:
            logger.warning(
                "Hyperliquid leaderboard refresh failed (%s); reusing the cohort fetched %s ago",
                e,
                _humanize_hours(age),
            )
            return cached_block
        raise
    return {"fetched_at": _iso_now(), "addresses": addresses}


def _load_snapshot() -> _Snapshot:
    """The newest usable snapshot, refreshing when the TTL has passed.

    The Farside pattern, with one addition: the history the 24-hour comparison
    reads is grown here, on the refresh path, so a snapshot served from cache
    and one just fetched carry the same comparison points.
    """
    path = _cache_path()
    cached = _read_cache(path)
    if cached:
        age = _age_hours(cached["current"]["fetched_at"])
        if age is not None and 0 <= age < SNAPSHOT_TTL_MINUTES / 60:
            return _Snapshot(cached["current"], cached["history"], cached["leaderboard"], False)

    try:
        leaderboard = _load_leaderboard(cached)
        current = _fetch_positions(leaderboard["addresses"])
    except HyperliquidWhalesError as e:
        # The typed failure keeps its type through the context-adding raises:
        # the router classifies by type, so an outage must not be re-read as
        # this module's breakage on the way out (#172).
        wrap_cls = type(e)
        if cached:
            fetched_at = cached["current"]["fetched_at"]
            age = _age_hours(fetched_at)
            # An unknown or future-dated age is treated as beyond the cap — the
            # case where an unbounded-age serve is most likely — rather than
            # served with a nonsense age caveat.
            if age is None or age < 0 or age > MAX_STALE_HOURS:
                stale_desc = (
                    "has an unparseable or future-dated fetch date"
                    if age is None or age < 0
                    else f"is {_humanize_hours(age)} stale"
                )
                raise wrap_cls(
                    f"Hyperliquid whale positioning refresh failed and the newest snapshot "
                    f"{stale_desc} (> {MAX_STALE_HOURS}-hour cap): "
                    f"{failure_account(e, limit=None)}"
                ) from e
            if isinstance(e, (VendorUnavailableError, VendorRateLimitError)):
                # The vendor being down, or turning us away: both are things it
                # DID, and neither is a bug to escalate. Only what is left —
                # this module's structural type — earns the ERROR below.
                logger.warning(
                    "Hyperliquid whale refresh failed (%s); serving the snapshot from %s ago",
                    e,
                    _humanize_hours(age),
                )
            else:
                # A contract break is a code fix, not a brownout, and must not
                # hide among network-blip warnings for the whole stale window.
                logger.error(
                    "Hyperliquid whale refresh failed structurally (%s); serving the "
                    "snapshot from %s ago — the parser or the endpoint likely changed",
                    e,
                    _humanize_hours(age),
                    exc_info=True,
                )
            return _Snapshot(cached["current"], cached["history"], cached["leaderboard"], True)
        raise wrap_cls(
            f"Hyperliquid whale positioning unavailable and no usable cache exists: "
            f"{failure_account(e, limit=None)}"
        ) from e

    history = _prune_history(
        (cached["history"] if cached else []) + [_history_entry(current)],
        current["fetched_at"],
    )
    payload = {
        "schema": CACHE_SCHEMA,
        "leaderboard": leaderboard,
        "current": current,
        "history": history,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:  # a cache-write failure must not fail the call
        logger.warning(
            "Could not write the Hyperliquid whale cache %s: %s — the fetch throttle stays "
            "disabled until a write succeeds, so further calls each re-run the %d-address "
            "sweep, and the 24-hour change stays unavailable",
            path,
            e,
            TOP_N,
        )
    return _Snapshot(current, history, leaderboard, False)


def _baseline_entry(history: list[dict], current: dict) -> tuple[dict | None, float | None]:
    """The history entry closest to ``DELTA_TARGET_HOURS`` before ``current``, if any.

    Only entries inside ``[DELTA_MIN_HOURS, DELTA_MAX_HOURS]`` are eligible, so
    the change reported as "24h" is always measured over roughly a day. On a
    first run — or after a gap in the analyst's cycles — there is no such
    entry, which the report states rather than silently comparing against
    whatever happens to be oldest.
    """
    best: dict | None = None
    best_distance: float | None = None
    best_age: float | None = None
    for entry in history:
        age = _hours_between(entry["fetched_at"], current["fetched_at"])
        if age is None or not DELTA_MIN_HOURS <= age <= DELTA_MAX_HOURS:
            continue
        distance = abs(age - DELTA_TARGET_HOURS)
        if best_distance is None or distance < best_distance:
            best, best_distance, best_age = entry, distance, age
    return best, best_age


def _fmt_ratio(ratio: float | None) -> str:
    """The long/short ratio, or the words for a book with no short side."""
    return "n/a (no short notional)" if ratio is None else f"{ratio:.2f}"


def _fmt_price(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.6g}"


def _fmt_leverage(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}x"


def _classify_asset(asset: str) -> str | None:
    """The venue coin a caller symbol maps to, or None for no signal.

    Read through the shared ``classify_crypto_asset`` so pair forms, slash
    forms and the look-alike rejections (``WETH``, ``BTCB``, stablecoins) match
    the other crypto vendors. The proxy lane that helper offers cannot fire
    here — this vendor's native set IS the recognized-risk-asset vocabulary,
    since Hyperliquid lists a perp for each of them — so the flag is discarded:
    there is no symbol for which another coin's positioning would be the honest
    answer.
    """
    coin, _is_proxy = classify_crypto_asset(asset, CRYPTO_BASES)
    return coin


def _delta_line(snapshot: _Snapshot, coin: str, aggregate: CoinAggregate) -> str:
    """The 24-hour change, or the reason there is none to report.

    Stated rather than left out when it cannot be computed: a missing line
    reads as "nothing changed", and the first run of a fresh deployment has no
    comparison point at all.

    Three things can qualify the figure, and each is said rather than assumed
    away: the sampled accounts may have changed between the two snapshots (a
    cohort turnover at the leaderboard TTL is not a position anyone moved), the
    BASELINE's own sweep may have been short of its cohort (the current
    snapshot's coverage is always printed, so dropping the older one's would
    make the subtraction look better-founded than it is), and the coin may
    simply have been absent from both.
    """
    current = snapshot.current
    baseline, age = _baseline_entry(snapshot.history, current)
    if baseline is None or age is None:
        return (
            f"**24h change:** n/a — no earlier snapshot in the "
            f"{DELTA_MIN_HOURS:.0f}-{DELTA_MAX_HOURS:.0f} hour window to compare with "
            f"(normal on a first run, or after a gap in the analyst's cycles)."
        )
    totals = baseline["coins"].get(coin.upper())
    if totals is None and aggregate.holders == 0:
        # Neither end held the coin. The "opened since" wording below would
        # read as an event over two figures that are both zero, printed
        # directly under the report's own "No {coin} position" line. Answered
        # before the caveats are built, since none of them qualify a figure
        # this branch prints.
        return (
            f"**24h change:** n/a — no {coin} position in either this snapshot or the one "
            f"{age:.1f} hours earlier ({baseline['fetched_at']}), so there is no change to "
            f"report."
        )
    caveats = ""
    if baseline["digest"] != current["digest"]:
        caveats += (
            " — NOTE: the sampled accounts changed between the two snapshots, so part of "
            "this is a different sample rather than a position change"
        )
    if baseline["answered"] < baseline["sampled"]:
        # The current snapshot's coverage is always stated; without this the
        # baseline's would be the one thing the report quietly dropped, and
        # coverage missing a day ago would read as a position change today.
        caveats += (
            f" — NOTE: that earlier snapshot only reached {baseline['answered']} of its "
            f"{baseline['sampled']} accounts, so part of this may be coverage it missed "
            f"rather than a position change"
        )
    if totals is None:
        return (
            f"**24h change** (vs {age:.1f} hours earlier, {baseline['fetched_at']}): "
            f"that snapshot held no {coin} position at all, so the whole of the current "
            f"US${fmt_usd_m(aggregate.long_notional)}m long / "
            f"US${fmt_usd_m(aggregate.short_notional)}m short was opened since"
            f"{caveats}"
        )
    prior_ratio = _ratio_of(totals["long_notional"], totals["short_notional"])
    return (
        f"**24h change** (vs {age:.1f} hours earlier, {baseline['fetched_at']}): "
        f"long {fmt_signed_usd_m(aggregate.long_notional - totals['long_notional'])}m, "
        f"short {fmt_signed_usd_m(aggregate.short_notional - totals['short_notional'])}m, "
        f"long/short {_fmt_ratio(prior_ratio)} -> {_fmt_ratio(aggregate.ratio)}"
        f"{caveats}"
    )


# Why a sweep stopped early, in the words the coverage sentence uses. A key per
# stored value, so a reason added to ``_fetch_positions`` without a sentence
# here fails at the render rather than printing a bare token — and the cache
# validator refuses any value this table has no words for.
_STOP_REASONS = {
    "budget": f"the {POSITION_FETCH_BUDGET_S:.0f}s fetch budget was spent",
    "rate_limit": "Hyperliquid rate limited this client, so the sweep stopped",
}


def _coverage_line(current: dict) -> str:
    """What the sweep did and did not reach, always stated.

    Unconditional: a reader cannot tell a complete sweep from a partial one by
    the figures, and "20 of 20" is the sentence that makes "17 of 20" legible
    when it appears.
    """
    parts = [
        f"_Coverage: {current['answered']} of {current['sampled']} sampled accounts answered"
    ]
    failed = current["attempted"] - current["answered"]
    unattempted = current["sampled"] - current["attempted"]
    if failed:
        parts.append(f"{failed} could not be read")
    if unattempted:
        # The reason is read off what the sweep RECORDED, never assumed: the
        # budget and a throttle leave an identical count behind, and naming the
        # wrong one sends the reader to look at the wrong thing.
        parts.append(f"{unattempted} were not attempted ({_STOP_REASONS[current['stopped']]})")
    if current["malformed"]:
        parts.append(
            f"{current['malformed']} position entries could not be parsed and are excluded"
        )
    return "; ".join(parts) + "._"


def get_whale_positions_data(asset: str, curr_date: str) -> str:
    """Fetch Hyperliquid whale positioning for a coin as a markdown report.

    Args:
        asset: The crypto asset to read positioning for — "BTC", or a pair form
            like "BTC-USD". Any recognized crypto risk asset is served its own
            coin; a stablecoin or an unrecognized symbol gets a no-signal
            message, since there is no other coin whose positioning would
            answer the question.
        curr_date: The analysis date (yyyy-mm-dd). Both endpoints are
            live-only, so this does not select a historical state: it is used to
            refuse an unusable date up front and to disclose how far the live
            figures sit from the date being analysed.

    Returns:
        A markdown report: the long/short split by account count and by
        notional, the long/short ratio, the notional-weighted leverage, the
        24-hour change where a comparison snapshot exists, the largest
        positions, and the coverage and sample caveats. An unusable
        ``curr_date`` answers the shared ``INVALID_CURR_DATE`` sentinel before
        any request, as the sibling crypto tools do (#119).
    """
    refusal = date_refusal(curr_date, what="whale positioning", kind="point")
    if refusal is not None:
        return refusal

    # Before the echo, not after: ``echo_argument`` goes through ``str``, so a
    # truthy non-string would be silently turned into a symbol and answered
    # about. That is the caller's bug and belongs in the vendor-failed lane, as
    # it does at the deribit, treasuries and farside boundaries (#233).
    if asset and not isinstance(asset, str):
        raise HyperliquidWhalesError(f"asset must be a symbol string, got {type(asset).__name__}")
    # Flattened BEFORE classification so exactly one string is both decided on
    # and rendered: the argument is LLM-written and lands in the no-signal
    # sentence, where a line break or a quote character would forge structure
    # (#233).
    asset = echo_argument(asset)
    quoted = quote_argument(asset)
    coin = _classify_asset(asset)
    if coin is None:
        # A "no signal" statement rather than an error, so the analyst reads
        # why nothing is being shown instead of a degraded-category sentinel.
        return (
            f"There is no whale-positioning signal for {quoted}: it is not a recognized "
            f"crypto risk asset with a Hyperliquid perpetual market (a stablecoin or an "
            f"unrecognized symbol). Do not substitute another coin's positioning."
        )

    snapshot = _load_snapshot()
    current = snapshot.current
    aggregate = aggregate_positions(_positions_from_records(current["positions"], coin), coin)

    lines = [
        f"## Hyperliquid Whale Positioning — {coin}",
        f"- Sample: the {current['sampled']} largest Hyperliquid mainnet accounts by the "
        f"public leaderboard's account value (cohort fetched "
        f"{snapshot.leaderboard['fetched_at']}) | live snapshot as of {current['fetched_at']}",
    ]
    if snapshot.stale:
        # The family's shared STALE template, with this vendor's own cause set
        # and closing warning - and its own age formatter, because the
        # displayed age must read the clock this module's TTL and stale cap
        # read (the tests patch ``_utc_now`` here, not the shared one).
        lines.append(
            _stale_caveat(
                current["fetched_at"],
                "Positions move continuously — treat every figure below as that instant's, "
                "not now's.",
                causes="network error, a rate limit, an endpoint change, or a parser break",
                humanize=_stale_age,
            )
        )
    # ``max_behind_days=0`` rather than the shared default of 2: this vendor
    # serves PRESENT state (open positions), so a report for any past date is
    # showing something that date could not have seen. At the default, a
    # curr_date one or two days back carried only the fetch-instant header and
    # no sentence at all - and that band is where a backtest most often sits.
    live_note = live_snapshot_note(
        curr_date, "Hyperliquid whale positioning shows", max_behind_days=0
    )
    if live_note:
        lines.append(live_note)

    lines.append("")
    if aggregate.holders == 0:
        lines.append(
            f"**No {coin} position** among the {current['answered']} sampled accounts that "
            f"answered. Read this as absence in THIS sample — the largest accounts by "
            f"leaderboard account value — not as an absence of open interest in {coin}, and "
            f"not as a directional signal."
        )
    else:
        long_share = (
            aggregate.long_notional / aggregate.total_notional * 100
            if aggregate.total_notional > 0
            else 0.0
        )
        lines.append(
            f"**Accounts holding {coin}:** {aggregate.holders} of {current['answered']} that "
            f"answered — {aggregate.long_count} long, {aggregate.short_count} short"
        )
        lines.append(
            f"**Notional:** long US${fmt_usd_m(aggregate.long_notional)}m vs short "
            f"US${fmt_usd_m(aggregate.short_notional)}m — long/short "
            f"{_fmt_ratio(aggregate.ratio)} (long share {long_share:.0f}%)"
        )
        if aggregate.avg_leverage is None:
            lines.append("**Notional-weighted leverage:** n/a (no sampled position reported one)")
        else:
            lines.append(
                f"**Notional-weighted leverage:** {aggregate.avg_leverage:.1f}x (over "
                f"US${fmt_usd_m(aggregate.levered_notional)}m of the "
                f"US${fmt_usd_m(aggregate.total_notional)}m that reported one)"
            )

    lines.append(_delta_line(snapshot, coin, aggregate))

    if aggregate.top:
        lines.append("")
        lines.append("| Account | Side | Notional (US$m) | Entry | Leverage |")
        lines.append("| --- | --- | --- | --- | --- |")
        for position in aggregate.top:
            lines.append(
                f"| {_abbrev(position.address)} | {'LONG' if position.is_long else 'SHORT'} "
                f"| {fmt_usd_m(position.notional)} | {_fmt_price(position.entry_px)} "
                f"| {_fmt_leverage(position.leverage)} |"
            )

    lines.append("")
    lines.append(_coverage_line(current))
    lines.append(
        "_Reading: these are the venue's largest accounts, many of which are market makers "
        "or vaults whose perp book hedges exposure held elsewhere. Treat the split as "
        "venue-level positioning of large accounts, not as crowd sentiment, and never as a "
        "standalone directional signal._"
    )
    return "\n".join(lines) + "\n"
