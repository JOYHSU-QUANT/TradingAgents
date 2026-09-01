"""Polymarket prediction-market vendor.

Surfaces live, market-implied probabilities for forward-looking events (Fed
decisions, recession, elections, geopolitics, crypto) to the news analyst, as a
complement to news (what happened) and FRED macro data (where things stand):
what the crowd actually prices to happen next.

Uses Polymarket's public Gamma API (https://gamma-api.polymarket.com) — no key,
no auth. Each market's ``outcomePrices`` are the implied probabilities of its
outcomes (a "Yes" at 0.76 means the market prices a 76% chance).
"""

import json
import logging
from datetime import datetime, timezone

import requests

from .utils import date_refusal, json_body_or_outage, live_snapshot_note, raise_for_http_status

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30

# Default number of markets to return, ranked by traded volume.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    """GET a Gamma endpoint and return its JSON body.

    A 5xx, or a 2xx whose body is not JSON, raises ``VendorUnavailableError``
    (the shared boundary helpers) rather than the ``requests.HTTPError`` /
    ``requests.JSONDecodeError`` it used to: Gamma answered without data,
    and that verdict belongs to the router — the caller's transport handler
    below is not it. Both used to land there (a ``requests`` JSON decode
    error is a ``RequestException`` too) and come back as a "network error"
    paragraph the router read as a successful answer (#142). A 4xx keeps
    raising as before.
    """
    response = requests.get(f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT)
    raise_for_http_status(response, "Polymarket")
    return json_body_or_outage(response, "Polymarket")


def _parse_json_list(value) -> list:
    """Gamma encodes ``outcomes``/``outcomePrices`` as JSON-string arrays."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """Keep only open markets that resolve in the future.

    ``closed`` is the reliable resolved flag (``active`` stays True even for
    settled markets), and a past ``endDate`` means the event already resolved —
    either way it is not a forward-looking signal.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def get_prediction_markets(
    topic: str, limit: int | None = None, curr_date: str | None = None
) -> str:
    """Return live prediction-market probabilities for an event topic.

    Args:
        topic: Event keyword(s), e.g. "Fed rate cut", "recession 2026",
            "US election", or a sector/company event.
        limit: Max markets to return (ranked by traded volume); ``None`` uses
            DEFAULT_LIMIT.
        curr_date: The date being analysed (yyyy-mm-dd). Prices are always
            fetched live; when curr_date sits behind the wall clock (beyond
            the shared live-snapshot bound) the report leads with a
            disclosure so today's odds are not read as that date's odds.
            ``None`` skips the check. A date that is supplied but does not
            parse is refused with the shared ``INVALID_CURR_DATE`` sentinel
            before any request. The open/forward-looking filter stays on the
            wall clock either way — the prices are today's regardless.

    Returns:
        A markdown report of the most-traded open markets matching the topic,
        each with its implied probability, traded volume, resolution date, and
        recent (1-week) move — or the sentinel.
    """
    if limit is None:
        limit = DEFAULT_LIMIT

    # Refused for the fundamentals getters' reason (#89) — their curr_date is
    # likewise a disclosure input, not a bound — but before the request rather
    # than after it: nothing Gamma answers outranks the date here (#139).
    if (
        refusal := date_refusal(
            curr_date, what="prediction-market probabilities", kind="point", omitted_ok=True
        )
    ) is not None:
        return refusal

    # Transport failures (a reset, a timeout, a 4xx) degrade here, in prose;
    # an outage verdict (a 5xx, a non-JSON body) is a ``VendorUnavailableError``
    # from ``_request`` and is deliberately NOT caught: it propagates to the
    # router, which degrades the optional category to its own sentinel (#142).
    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )
    if curr_date:
        snapshot_note = live_snapshot_note(curr_date, "prediction-market probabilities are")
        if snapshot_note:
            header += snapshot_note + "\n\n"

    if not candidates:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    omitted = 0
    # Walk the full volume-ranked candidate list, not just the first `limit`:
    # when a malformed market is dropped, the next-ranked clean market
    # backfills its slot so the caller still gets `limit` markets where
    # available.
    for m in candidates:
        if len(lines) >= limit:
            break
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        # A malformed market — mismatched outcome/price lists, an unparsable
        # or out-of-range probability — is dropped and disclosed below, never
        # rendered with a fabricated label or an impossible probability.
        if not outcomes or not prices or len(outcomes) != len(prices):
            omitted += 1
            continue
        try:
            prob = float(prices[0])
        except (TypeError, ValueError):
            omitted += 1
            continue
        if not 0.0 <= prob <= 1.0:
            omitted += 1
            continue
        label = outcomes[0]
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        wk_str = f", 1-week {wk * 100:+.1f}pp" if isinstance(wk, (int, float)) and wk else ""
        lines.append(
            f"- **{m.get('question')}** — {label} {prob:.0%} "
            f"(${volume:,.0f} volume, resolves {end_date}{wk_str})"
        )

    report = header + "\n".join(lines) + "\n"
    if omitted:
        report += (
            f"\n{omitted} market(s) omitted (malformed vendor data: "
            f"outcome/price mismatch, unparsable price, or out-of-range "
            f"probability).\n"
        )
    return report
