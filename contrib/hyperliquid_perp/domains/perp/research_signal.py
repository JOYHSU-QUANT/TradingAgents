"""Read the research radar's handoff document — or refuse it, by name.

The consumer half of plan §7 / PR C1. ``contrib/autoresearch`` fits and
scores rules offline on its own history store and writes one small JSON
document (:class:`.schema.ResearchSignal` owns both halves of that contract);
this module is the only thing in the trading package that reads it, and it
does so with the standard library alone. Nothing here imports the research
package, and nothing here knows its SQLite schema — see
:class:`.schema.ResearchSignal` for why the seam is a document.

**Fail-closed, whole-section.** Every way of not having a trustworthy signal
— no file, an unreadable one, malformed JSON, a version this build does not
read, an unknown field, another coin's document, a bar too old, a bar from
ahead of this run's own — answers ``None`` and logs ONE named WARNING. The
context then carries no signal and
:func:`.prompt_context.context_shape` carries no ``autoresearch`` token, so a
cycle that fell back files under the no-signal shape and shows up in the
paper review as its own bucket rather than hiding inside the shape that has
the section. There is deliberately no partial reading and no ``n/a`` row: a
header with nothing under it reads as a measurement that came back empty
rather than one that was never taken.

**Not a gate.** Like the volume profile, this is an analyst INPUT. It feeds
no risk gate, no sizing and no order path; its worth to decision quality is
unmeasured, which is what run 6 is for.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .schema import ResearchSignal, interval_to_ms

logger = logging.getLogger(__name__)

__all__ = ["MAX_SIGNAL_AGE_INTERVALS", "load_research_signal"]

_MS_PER_HOUR = 3_600_000

# How many of the DOCUMENT's own bars old the signal may be before it is
# refused.
#
# NOT "because the producer may be one bar behind": the bound is a strict
# ``>``, so one bar behind is accepted at a bound of one, and a comment
# claiming otherwise was refuted by a probe in review. The reason for two is
# tolerance for a SKIPPED producer run — one missed cron firing (or one that
# overran its interval) still leaves the section standing — and for a research
# interval finer than this run's candle interval, where several research bars
# close between two of this run's.
#
# It is also what an operator schedules against: at the project's 4h research
# bars the producer must run at least every 8 hours or the section disappears.
# That is why the number is stated in SETUP and printed by the producer, and
# why the producer BORROWS it from here rather than restating it.
MAX_SIGNAL_AGE_INTERVALS = 2

# What ``_read_document`` returns when it has already logged the refusal. A
# private sentinel rather than ``None``, because ``None`` is also what
# ``json.loads`` returns for a document holding ``null`` — and that document
# has a named refusal waiting for it in ``ResearchSignal.from_document`` ("a
# research signal document is a JSON object, got NoneType"). Sharing the two
# meanings suppressed it: a four-byte file made the section vanish with no log
# line at all, which is the one outcome this module's whole design forbids.
_UNREAD = object()


def load_research_signal(
    path: str, *, coin: str, as_of_ms: int, candle_interval_ms: int
) -> ResearchSignal | None:
    """The signal at ``path``, or ``None`` with one WARNING saying why not.

    ``coin`` and ``as_of_ms`` are this cycle's own — the coin being traded and
    the close of the newest candle the context was built from — so the
    document is judged against the market this prompt describes rather than
    against the host's clock. Same discipline as the freshness guard, for the
    same reason: measured against a host clock, a window a slow host cut looks
    current. The one caller only reaches here with candles in hand, so
    ``as_of_ms`` is always a venue bar close and never a wall-clock reading.

    ``candle_interval_ms`` is THIS run's candle interval, and it bounds the
    future side. A closed bar cannot be newer than "now", and "now" is less
    than one of this run's intervals past ``as_of_ms`` — otherwise another of
    this run's candles would have closed and ``as_of_ms`` would be that one.
    So a document stamped at or past ``as_of_ms + candle_interval_ms`` is not
    a closed bar of the same market: it is a clock or a store that disagrees
    with the venue, and printing it would date this prompt's own section into
    the future. The stale side is bounded by the DOCUMENT's interval instead,
    because "too old" is a statement about the producer's cadence, not about
    this run's.
    """
    document = _read_document(path)
    if document is _UNREAD:
        return None
    try:
        signal = ResearchSignal.from_document(document)
    except ValueError as exc:
        logger.warning(
            "research signal at %s was refused, so the prompt omits the section: %s", path, exc
        )
        return None

    if signal.coin != coin:
        logger.warning(
            "research signal at %s is for %s and this run trades %s, so the prompt omits the "
            "section",
            path,
            signal.coin,
            coin,
        )
        return None

    age_ms = as_of_ms - signal.as_of_ms
    max_age_ms = MAX_SIGNAL_AGE_INTERVALS * interval_to_ms(signal.interval)
    if age_ms > max_age_ms:
        logger.warning(
            "research signal at %s was decided %.1fh before this context's own bar, past the "
            "%d x %s bound, so the prompt omits the section — re-run the research radar's "
            "`signal` command",
            path,
            age_ms / _MS_PER_HOUR,
            MAX_SIGNAL_AGE_INTERVALS,
            signal.interval,
        )
        return None
    if -age_ms >= candle_interval_ms:
        logger.warning(
            "research signal at %s is stamped %.1fh AFTER this context's own bar, which no "
            "closed bar of the same market can be, so the prompt omits the section — check the "
            "clock on the host that wrote it",
            path,
            -age_ms / _MS_PER_HOUR,
        )
        return None
    return signal


def _read_document(path: str) -> object:
    """The decoded JSON at ``path``, or :data:`_UNREAD` with one WARNING.

    Every message prints the path RESOLVED — ``~`` expanded and made absolute
    — rather than as it was configured. A relative path is the case that needs
    it: the producer's cron and the daemon's unit can be started from
    different working directories, and two processes then disagree about one
    string with nothing in either message able to say so.

    The three ``except`` clauses are wider than the obvious ones on purpose,
    because this runs inside a decision cycle and anything that escapes fails
    the CYCLE rather than the section — a pre-LLM failure that repeats until a
    human intervenes, with an open position left to its stops. Each was
    checked against what the library actually raises rather than against what
    it is usually described as raising.
    """
    try:
        # Inside the try: ``expanduser`` raises ``RuntimeError`` — not
        # ``OSError`` — when there is no home directory to expand against (a
        # Windows service account with no ``USERPROFILE``, or ``~someuser``
        # for a user not in passwd), and SETUP invites ``~`` paths.
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "research signal path %r could not be resolved, so the prompt omits the section: %s",
            path,
            exc,
        )
        return _UNREAD
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        # Missing is the ordinary case on the day the switch is turned on and
        # the producer has not run yet. Unreadable, a directory, and a path
        # the OS refuses outright (an embedded NUL) share this sentence
        # because the answer is the same: the section is omitted, and the
        # operator is told where it looked.
        logger.warning(
            "research signal document %s could not be read, so the prompt omits the section: %s",
            resolved,
            exc,
        )
        return _UNREAD
    except UnicodeDecodeError as exc:
        # Raised by the decoder, and it is a ``ValueError``, not an
        # ``OSError`` — so it needs its own clause or it escapes this reader
        # entirely and fails the cycle rather than the section.
        logger.warning(
            "research signal document %s is not UTF-8 text, so the prompt omits the section: %s",
            resolved,
            exc,
        )
        return _UNREAD
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        # ``ValueError``, not ``json.JSONDecodeError``: the scanner also
        # raises a BARE ``ValueError`` for an integer literal past
        # ``sys.get_int_max_str_digits()`` (4300 digits), and ``RecursionError``
        # for nesting past the interpreter's limit. Neither is a
        # ``JSONDecodeError``, and both are reachable by pointing the switch
        # at the wrong JSON file — a store dump, an export — which is an
        # operator mistake, not a defect, and must cost the section rather
        # than the cycle. (``schema.epoch_ms_out_of_range`` already defends
        # the digit limit at the DTO; this is the same bound at the parse.)
        logger.warning(
            "research signal document %s could not be decoded as JSON, so the prompt omits the "
            "section: %s",
            resolved,
            exc,
        )
        return _UNREAD
