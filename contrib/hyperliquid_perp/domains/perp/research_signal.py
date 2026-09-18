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
# refused. Two rather than one: the producer is an out-of-band command, so the
# newest bar it saw is at best the one this run is looking at and at worst the
# one before it, and a bound of one would refuse a perfectly current document
# whenever the producer happened to run a few minutes before a bar closed. Two
# is also what an operator schedules against — at the project's 4h research
# bars the producer must run at least every 8 hours or the section disappears
# — which is why the number is stated in SETUP rather than left to be inferred
# from a WARNING.
MAX_SIGNAL_AGE_INTERVALS = 2


def load_research_signal(
    path: str, *, coin: str, as_of_ms: int, candle_interval_ms: int
) -> ResearchSignal | None:
    """The signal at ``path``, or ``None`` with one WARNING saying why not.

    ``coin`` and ``as_of_ms`` are this cycle's own — the coin being traded and
    the close of the newest candle the context was built from — so the
    document is judged against the market this prompt describes rather than
    against the host's clock. Same discipline as the freshness guard, for the
    same reason: measured against a host clock, a window a slow host cut looks
    current.

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
    if document is None:
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


def _read_document(path: str) -> object | None:
    """The decoded JSON at ``path``, or ``None`` with one WARNING.

    ``~`` is expanded, and the messages print the path as it was LOOKED FOR
    rather than as it was configured: an operator who wrote ``~/signal.json``
    and an operator whose relative path resolved against a working directory
    they did not expect both need to see where the daemon actually went.
    """
    resolved = Path(path).expanduser()
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        # Missing is the ordinary case on the day the switch is turned on and
        # the producer has not run yet. Unreadable, a directory, and a bad
        # encoding share this sentence because the answer is the same: the
        # section is omitted, and the operator is told where it looked.
        logger.warning(
            "research signal document %s could not be read, so the prompt omits the section: %s",
            resolved,
            exc,
        )
        return None
    except ValueError as exc:
        # A file that is not UTF-8 at all. Separate from OSError because
        # ``read_text`` raises this one from the decoder, and it is not an
        # ``OSError``, so it would otherwise escape this reader entirely and
        # fail the cycle rather than the section.
        logger.warning(
            "research signal document %s is not UTF-8 text, so the prompt omits the section: %s",
            resolved,
            exc,
        )
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # A half-written file is the expected shape of this failure, and it
        # should be unreachable: the producer writes a temporary file and
        # renames it into place, which is atomic on both platforms this runs
        # on. Reaching here means that rule was broken somewhere, which is
        # worth more to an operator than a bare parse error.
        logger.warning(
            "research signal document %s is not valid JSON, so the prompt omits the section: %s",
            resolved,
            exc,
        )
        return None
