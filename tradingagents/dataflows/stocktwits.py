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

from .utils import (
    MAX_UNTRUSTED_CHARS,
    data_lag_note,
    echo_argument,
    live_snapshot_note,
    normalize_iso_date,
    quote_argument,
    sanitize_untrusted,
)

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"

# Maximum age (calendar days) of the newest rendered message relative to the
# analysis date before the block carries a data-lag disclosure. Active symbols
# see posts daily; when even the newest message is a week old the stream is
# effectively stalled and the summary percentages describe stale chatter (#30).
MAX_MESSAGE_LAG_DAYS = 7

# Rendered length of one message body. This module's own number rather than the
# shared MAX_UNTRUSTED_CHARS: here the body IS the content the block exists to
# carry, where the shared cap bounds a field quoted inside a sentence of ours.
MAX_BODY_CHARS = 280

# Rendered length of one posting time. A timestamp's own size rather than the
# shared cap: the date check below reads ten characters, so at the shared 200 a
# stamp whose date is real could still carry ~190 characters of an author's
# prose into a slot every reader takes for a timestamp (#233).
MAX_STAMP_CHARS = 32

# What renders where a message carries no usable posting time: absent, or a
# stamp whose date this module cannot read (see _rendered_stamp).
TIME_UNKNOWN = "[time unknown]"


def _rendered_stamp(raw: object) -> tuple[str, str | None]:
    """The stamp as it renders, and the ISO day the freshness note may use.

    One function for both because the value JUDGED has to be the value SHOWN
    (#233): the note below decides "is this stream stalled" from the stamp's
    first ten characters, so a stamp whose prefix is not a date must not render
    as though it were one either. Flattening "2026-09-1#0T.." to "2026-09-1 0"
    would leave a plausible-looking date beside a message the freshness check
    had silently skipped. When the prefix does parse, those ten characters are
    digits and hyphens, which flattening cannot move, so the day the note names
    stays a prefix of the stamp shown beside every message.
    """
    day = normalize_iso_date(raw[:10]) if isinstance(raw, str) and len(raw) >= 10 else None
    if day is None:
        return TIME_UNKNOWN, None
    return sanitize_untrusted(raw, limit=MAX_STAMP_CHARS), day


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
        # BOTH spellings inside repr's own quotes, the vendor's included. The
        # whole content of this sentence is that the two DIFFER, so a guard
        # that let a hostile spelling come back reading like the clean one
        # would turn it into a self-contradiction the model reads as OUR bug:
        # flattening alone renders "AAPL#" as "AAPL", and "##" as nothing at
        # all (#233). This is the one place a vendor's value takes the argument
        # guard, and it takes it for that guard's own reason — it is being
        # CONTRASTED with an argument, character for character.
        return (
            f"<stocktwits unavailable: symbol mismatch "
            f"(requested {quote_argument(ticker.upper())}, response is for "
            f"{quote_argument(echoed.upper())})>"
        )

    # Same degrade-don't-raise contract for the message list itself: a truthy
    # non-list `messages` (or non-dict entries) is a malformed response, not
    # an excuse to crash the sentiment analyst.
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        logger.warning("StockTwits returned a non-list messages field for %s", ticker.upper())
        return (
            f"<stocktwits unavailable: unexpected response shape for "
            f"${echo_argument(ticker.upper())}>"
        )
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
        return f"<no StockTwits messages found for ${echo_argument(ticker.upper())}>"

    lines = []
    # The days the rendered stamps yielded, for the freshness note below. Taken
    # from the render loop rather than from a second pass over the same slice,
    # so the day that decides the note is one a message actually shows (#233).
    days: list[str] = []
    # Stamps that were present and could not be read. Counted for the operator
    # line below: the freshness note used to reach data_lag_note, which logged
    # an unparseable date itself, and without that line a vendor-side format
    # change would turn every future disclosure off invisibly.
    unreadable_stamps = 0
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        # Missing fields get explicit unavailability markers, not values that
        # render like a real timestamp or a user named "?". Every nested
        # access is isinstance-guarded: a truthy non-dict still means the
        # field is unusable, never that we may call .get() on it. Every field
        # here was written by whoever posted the message, so each is flattened
        # on the way into a block the sentiment analyst reads as part of its
        # own SYSTEM prompt: an unflattened body opens a "## " heading in it.
        created, day = _rendered_stamp(m.get("created_at"))
        if day is not None:
            days.append(day)
        elif m.get("created_at") is not None:
            unreadable_stamps += 1
        user_env = m.get("user")
        raw_user = user_env.get("username") if isinstance(user_env, dict) else None
        # A handle with nothing left after flattening ("##") is as unusable as
        # an absent one and takes the same marker, rather than rendering "@".
        username = sanitize_untrusted(raw_user, limit=MAX_UNTRUSTED_CHARS) if raw_user else ""
        user = f"@{username}" if username else "[unknown user]"
        entities = m.get("entities")
        sentiment_obj = entities.get("sentiment") if isinstance(entities, dict) else None
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        raw_body = m.get("body")
        body = (
            sanitize_untrusted(raw_body, limit=MAX_BODY_CHARS)
            if isinstance(raw_body, str)
            else ""
        )

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

    if unreadable_stamps:
        logger.warning(
            "StockTwits sent %d message(s) for %s whose posting time could not be read; "
            "rendered as %s and left out of the freshness check",
            unreadable_stamps,
            ticker.upper(),
            TIME_UNKNOWN,
        )
    newest_date = max(days, default=None)
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
        # rendered message's age. No date_refusal gate here, unlike the
        # routed getters (#139): curr_date is the graph's own end_date, not a
        # model argument, so a bad one is a programming error the helper logs
        # — and the get_news call beside this one in the sentiment analyst
        # already answers INVALID_END_DATE for it (see live_snapshot_note's
        # docstring).
        note = live_snapshot_note(curr_date, "these StockTwits messages are")
        if not note and newest_date:
            note = data_lag_note(newest_date, curr_date, MAX_MESSAGE_LAG_DAYS, "StockTwits message")
        if note:
            note += "\n\n"
    return note + summary + "\n\n" + "\n".join(lines)
