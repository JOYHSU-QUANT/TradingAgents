"""StockTwits public symbol-stream fetcher.

StockTwits exposes a per-symbol message stream at
``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` that requires no
API key, no OAuth, and no registration. Each message includes a
user-labeled sentiment field (``Bullish``/``Bearish``/null), the message
body, timestamp, and posting user.

The function is deliberately self-contained: short timeout, graceful
degradation on any HTTP or parse failure, and a string return type so
the calling agent gets a uniform interface regardless of whether the
network call succeeded.
"""

from __future__ import annotations

import http.client
import json
import logging
from urllib.request import Request, urlopen

from .utils import data_lag_note, live_snapshot_note

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# Maximum age (calendar days) of the newest rendered message relative to the
# analysis date before the block carries a data-lag disclosure. Active symbols
# see posts daily; when even the newest message is a week old the stream is
# effectively stalled and the summary percentages describe stale chatter (#30).
MAX_MESSAGE_LAG_DAYS = 7


def _symbols_match(requested: str, echoed: str) -> bool:
    """Case-insensitive symbol match tolerating StockTwits' crypto ``.X``
    suffix (requesting BTC may legitimately echo BTC.X). Only that one known
    vendor convention is tolerated — BRK.A vs BRK.B is still a mismatch."""
    r, e = requested.upper(), echoed.upper()
    return r == e or e == f"{r}.X" or r == f"{e}.X"


def fetch_stocktwits_messages(
    ticker: str,
    limit: int = 30,
    timeout: float = 10.0,
    curr_date: str | None = None,
) -> str:
    """Fetch recent StockTwits messages for ``ticker`` and return them as a
    formatted plaintext block ready for prompt injection.

    ``curr_date`` (yyyy-mm-dd) is the date being analysed. The stream is
    live-only (no historical query), so when curr_date sits behind the wall
    clock (a backtest) the block leads with a live-snapshot disclosure —
    today's chatter must not read as that date's sentiment. Otherwise, when
    the newest rendered message trails curr_date by more than
    MAX_MESSAGE_LAG_DAYS, it leads with a data-lag disclosure so a stalled
    stream cannot read as current sentiment (#30). ``None`` skips both checks
    (legacy callers).

    Returns a placeholder string when the endpoint is unreachable, the
    symbol has no messages, or the response shape is unexpected — the
    caller never has to special-case None or exceptions.
    """
    url = _API.format(ticker=ticker.upper())
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return f"<stocktwits unavailable: {type(exc).__name__}>"

    if not isinstance(data, dict):
        data = {}

    # Identity echo: when the response names the symbol it is streaming, a
    # mismatch means the vendor answered for a different instrument — degrade
    # and disclose rather than render another symbol's sentiment (#36). A
    # malformed (non-dict) symbol envelope just skips the check: this function
    # must degrade, never raise.
    symbol_env = data.get("symbol")
    echoed = symbol_env.get("symbol") if isinstance(symbol_env, dict) else None
    if isinstance(echoed, str) and not _symbols_match(ticker, echoed):
        logger.warning(
            "StockTwits symbol mismatch: requested %s, response is for %s",
            ticker.upper(),
            echoed.upper(),
        )
        return (
            f"<stocktwits unavailable: symbol mismatch "
            f"(requested {ticker.upper()}, response is for {echoed.upper()})>"
        )

    # Same degrade-don't-raise contract for the message list itself: a truthy
    # non-list `messages` (or non-dict entries) is a malformed response, not
    # an excuse to crash the sentiment analyst.
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        logger.warning("StockTwits returned a non-list messages field for %s", ticker.upper())
        return f"<stocktwits unavailable: unexpected response shape for ${ticker.upper()}>"
    dict_messages = [m for m in messages if isinstance(m, dict)]
    if len(dict_messages) != len(messages):
        # Same operator-visibility as every other malformed-shape branch.
        logger.warning(
            "StockTwits returned %d non-dict message entrie(s) for %s — skipped",
            len(messages) - len(dict_messages),
            ticker.upper(),
        )
    messages = dict_messages
    if not messages:
        return f"<no StockTwits messages found for ${ticker.upper()}>"

    # Newest rendered-message date, for the freshness note below. Computed
    # over the same slice the loop renders, but outside it so the render loop
    # stays pure formatting. ISO date prefixes compare lexicographically, so
    # max() needs no parsing; data_lag_note degrades to no note if the prefix
    # isn't a real date.
    newest_date = max(
        (
            c[:10]
            for m in messages[:limit]
            if isinstance(c := m.get("created_at"), str) and len(c) >= 10
        ),
        default=None,
    )

    lines = []
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        # Missing fields get explicit unavailability markers, not values that
        # render like a real timestamp or a user named "?". Every nested
        # access is isinstance-guarded: a truthy non-dict still means the
        # field is unusable, never that we may call .get() on it.
        created = m.get("created_at") or "[time unknown]"
        user_env = m.get("user")
        username = user_env.get("username") if isinstance(user_env, dict) else None
        user = f"@{username}" if username else "[unknown user]"
        entities = m.get("entities")
        sentiment_obj = entities.get("sentiment") if isinstance(entities, dict) else None
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        raw_body = m.get("body")
        body = raw_body.replace("\n", " ").strip() if isinstance(raw_body, str) else ""
        if len(body) > 280:
            body = body[:280] + "…"

        if sentiment == "Bullish":
            bullish += 1
            tag = "Bullish"
        elif sentiment == "Bearish":
            bearish += 1
            tag = "Bearish"
        else:
            unlabeled += 1
            tag = "no-label"
        lines.append(f"[{created} · {user} · {tag}] {body}")

    total = bullish + bearish + unlabeled
    bull_pct = round(100 * bullish / total) if total else 0
    bear_pct = round(100 * bearish / total) if total else 0
    summary = (
        f"Bullish: {bullish} ({bull_pct}%) · "
        f"Bearish: {bearish} ({bear_pct}%) · "
        f"Unlabeled: {unlabeled} · "
        f"Total: {total} most-recent messages"
    )
    note = ""
    if curr_date:
        # Backtest first: the stream is live-only, so an analysis date behind
        # the wall clock means these are *today's* messages — disclose that,
        # and skip the lag check (a historical date cannot meaningfully trail
        # a live stream). Otherwise flag a stalled stream via the newest
        # rendered message's age.
        note = live_snapshot_note(curr_date, "these StockTwits messages are")
        if not note and newest_date:
            note = data_lag_note(newest_date, curr_date, MAX_MESSAGE_LAG_DAYS, "StockTwits message")
        if note:
            note += "\n\n"
    return note + summary + "\n\n" + "\n".join(lines)
