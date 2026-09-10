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
import math
from datetime import datetime, timezone

import requests

from .utils import (
    MAX_UNTRUSTED_CHARS,
    date_refusal,
    json_body_or_outage,
    live_snapshot_note,
    normalize_iso_date,
    quote_argument,
    raise_for_http_status,
    sanitize_untrusted,
)

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


# What a market line says where the vendor sent no figure. Named rather than
# defaulted: the two slots carry meaning the model acts on (depth, and when the
# question settles), so a stand-in number would be read as the answer.
VOLUME_UNAVAILABLE = "volume unavailable"
DATE_UNAVAILABLE = "(date unavailable)"


def _traded_volume(market: dict) -> float | None:
    """A market's traded volume as a number, or ``None`` if there is not one.

    One reading for the two places volume is used — the RANKING and the
    rendered line — because they used to disagree about what a usable number
    is. The ranking's ``or 0`` accepted whatever the vendor sent and then let
    ``sort`` compare it: a string volume beside any second market raised
    ``TypeError`` out of the report path, which no single-market test can see.

    Refused, and each for the reason the line would otherwise state something
    the data does not support: ``bool``, because it is an ``int`` and would
    rank and render as 1; a non-finite float, because ``$nan volume`` is as
    invented a depth reading as the ``$0`` this replaced; a negative, because
    no market has traded a negative amount; and an integer too large to be a
    double, because ``json.loads`` keeps arbitrary precision and ``float`` on
    a 400-digit integer raises ``OverflowError`` — out of the RANKING, which
    touches every candidate, including ones no line would ever have shown.
    """
    value = market.get("volumeNum")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _rendered_text(value: object) -> str:
    """A Gamma text field as the report will show it, or ``""`` if unshowable.

    Two ways a field with nothing to say used to reach the report anyway, both
    because ``sanitize_untrusted`` goes through ``str``:

    * a MISSING field (``None``) came back as the literal ``"None"`` — a market
      rendered ``- **None**``, which no reader can tell from a real market
      whose question text happens to be the string ``"None"``;
    * a field of pure markdown (``"###"``) is a real, non-empty string that
      FLATTENS to nothing, rendering an empty label.

    Returning ``""`` for both lets the caller drop and disclose the market with
    one check, and — because this is also the spelling the caller renders —
    the value judged and the value shown are the same value (#233).

    Scalars only, and that boundary is load-bearing in BOTH directions. A JSON
    number or bool arriving in one of these fields is a value with something to
    show and used to render, so refusing it would drop a real market and
    disclose it as a MISSING question — a different claim from the one the data
    supports. A list or an object has nothing to show, and admitting it would
    put a Python repr in the report as a label: an outcome rendered ``**[]**``
    beside a real probability.
    """
    if not isinstance(value, (str, int, float)):
        return ""
    return sanitize_untrusted(value, limit=MAX_UNTRUSTED_CHARS)


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
        limit: Max markets to return (ranked by traded volume); ``None``, zero
            or a negative (a hallucinated argument) uses
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
    # A None, zero, or nonsensical negative limit (a hallucinated tool argument)
    # falls back to the default rather than producing a degenerate report, the
    # same coercion farside and fear_greed give their windows. Zero is not
    # merely degenerate here: the walk below breaks before judging anything, so
    # the report would reach the all-dropped branch and say every market was
    # malformed when none had been looked at.
    if limit is None or limit <= 0:
        limit = DEFAULT_LIMIT

    # Refused for the fundamentals getters' reason (#89) — their curr_date is
    # likewise a disclosure input, not a bound — but before the request rather
    # than after it: nothing Gamma answers outranks the date here (#139).
    if (
        refusal := date_refusal(
            curr_date, what="prediction-market probabilities", kind="disclosure", omitted_ok=True
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
        # The log line below keeps the whole reason; the prose gets it
        # flattened and capped, because a 4xx carries the request URL and
        # with it the model's own ``topic`` — verbatim, that is a fragment the
        # model authored flowing back into text it reads (#201). The topic
        # named in the closing sentence is that same fragment, so it gets the
        # argument echo too, as do the other two places this getter quotes it
        # back: the report heading and the no-match sentence (#231).
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable "
            f"(network error: {sanitize_untrusted(e, limit=MAX_UNTRUSTED_CHARS)}). "
            f"Proceed without prediction-market signal for {quote_argument(topic)}."
        )

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    # Ranked on the same reading the line renders, so a market cannot be
    # ordered by a figure the report then declines to show.
    candidates.sort(key=lambda m: _traded_volume(m) or 0.0, reverse=True)

    header = (
        f"## Polymarket prediction markets: {quote_argument(topic)}\n"
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
            f"No open prediction markets matched {quote_argument(topic)}. Polymarket coverage "
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
        # A malformed market — a missing question or outcome label, mismatched
        # outcome/price lists, an unparsable or out-of-range probability — is
        # dropped and disclosed below, never rendered with a fabricated label
        # or an impossible probability.
        #
        # The question and the first outcome label are judged on what they will
        # RENDER as, which is what ``_rendered_text`` returns and what the line
        # below shows — so the value judged and the value shown cannot differ.
        # The comment above has claimed this completeness since before #232
        # while the guard checked neither field (#233).
        question = _rendered_text(m.get("question"))
        if not question:
            omitted += 1
            continue
        if not outcomes or not prices or len(outcomes) != len(prices):
            omitted += 1
            continue
        label = _rendered_text(outcomes[0])
        if not label:
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
        # Gamma's question text and outcome labels are written by whoever
        # created the market, and the router serves a successful report
        # verbatim (it caps only its sentinel slots), so an unflattened
        # question was a second forgery site three lines below the heading
        # PR #232 fixed: a question carrying its own "## " line renders a
        # heading impersonating another tool inside this report (#201
        # review). Both are flattened at the guard above, which judges what
        # this line will actually show. The date is a vendor field too — and
        # it is flattened BEFORE the slice, not after: slicing first spends
        # the ten characters on the junk and the flatten then deletes the
        # evidence the junk was there, so "\n2030-12-31" rendered as
        # "2030-12-3" — a date 28 days early with nothing left in the line to
        # say it was cut.
        # Volume and resolution date are named, not judged: neither absence
        # makes the line unreadable, so the market keeps its probability
        # signal. What they may NOT do is answer with a plausible figure the
        # vendor never sent. ``or 0`` rendered a missing volume as "$0 volume"
        # — and this report's own header tells the model that higher volume
        # means a deeper, more reliable market, so "$0" asserted the market was
        # the least trustworthy one on the page. A missing endDate rendered
        # "resolves " with nothing after it. Both are fabrications of the same
        # kind as the bolded ``None`` this guard already refuses (#233).
        volume = _traded_volume(m)
        volume_str = f"${volume:,.0f} volume" if volume is not None else VOLUME_UNAVAILABLE
        # The slice is not a parse: flattening turns "2#030-12-31" into
        # "2 030-12-3" and "2030-12-3*1" into "2030-12-3", so a value with
        # noise in it rendered a PLAUSIBLE WRONG date — 12/3 for 12/31 —
        # with nothing in the line to say it had been cut. Only a value the
        # shared normaliser recognises as a date is shown as one.
        end_date = normalize_iso_date(sanitize_untrusted(m.get("endDate") or "")[:10])
        end_date = end_date or DATE_UNAVAILABLE
        wk = m.get("oneWeekPriceChange")
        wk_str = f", 1-week {wk * 100:+.1f}pp" if isinstance(wk, (int, float)) and wk else ""
        lines.append(
            f"- **{question}** — {label} {prob:.0%} "
            f"({volume_str}, resolves {end_date}{wk_str})"
        )

    if not lines:
        # Every candidate was dropped. The header above has already promised
        # "market-implied probabilities", so an empty body would leave the
        # analyst a heading with nothing under it to reason from — the same
        # bare-header failure ``get_fundamentals`` refuses. Say what happened;
        # the omitted clause below gives the reason.
        # The reason is left to the omitted clause below rather than spelled
        # again here: that one names WHICH malformations were seen, so a second
        # sentence saying "malformed vendor data" would be the weaker of two
        # copies a reader has to reconcile.
        report = (
            header + f"No prediction markets for {quote_argument(topic)} could be rendered.\n"
        )
    else:
        report = header + "\n".join(lines) + "\n"
    if omitted:
        report += (
            f"\n{omitted} market(s) omitted (malformed vendor data: "
            f"no renderable question or outcome label, outcome/price mismatch, "
            f"unparsable price, or out-of-range probability).\n"
        )
    return report
