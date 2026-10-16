"""Read the trend guardrail's handoff document — or refuse it, by name.

The consumer half of the guardrail seam (design 2026-10-02; wired
2026-10-16). ``contrib/autoresearch`` replays one fixed rule over its own
history store and writes one small JSON document
(:class:`.schema.GuardrailSignal` owns both halves of that contract); this
module is the only thing in the trading package that reads it, with the
standard library alone, exactly as :mod:`.research_signal` reads the research
radar's document. Nothing here imports the research package.

**This one reaches a gate.** The research signal is an analyst input; the
guardrail's side is what :func:`.risk_gate.evaluate` lets a position stand
against, and it is the one document that can close one. So every way of not
having a trustworthy reading — no file, an unreadable one, malformed JSON, a
version this build does not read, another coin's document, a bar too old, a
bar from ahead of the clock — answers ``None`` with ONE named WARNING, and
the engine turns ``None`` into :attr:`.schema.GuardrailSide.UNKNOWN`, whose
answer at the gate was decided 2026-10-16: open nothing new, keep what is
held. A missing document therefore never flattens the account, and never
lets a new position through either.

Freshness is judged against the HOST clock at the gate rather than against
the context's bar: the gate runs when the scheduler says, and the document
is the producer's newest closed bar, so "too old" means the producer has not
run for more than ``MAX_SIGNAL_AGE_INTERVALS`` of ITS bars — the same bound
as the research signal's, for the same reason (one missed producer run must
not take the section, or here the guardrail, down), so it is borrowed rather
than restated.
"""

from __future__ import annotations

import logging
from datetime import datetime

from contrib.core.instants import epoch_ms, gap_label

from .research_signal import MAX_SIGNAL_AGE_INTERVALS, UNREAD, max_signal_age_ms, read_document
from .schema import GuardrailSide, GuardrailSignal

logger = logging.getLogger(__name__)

__all__ = ["guardrail_side_for", "load_guardrail_signal"]

_WHAT = "trend guardrail"
_CONSEQUENCE = "so the gate opens nothing new and keeps what is held"


def load_guardrail_signal(path: str, *, coin: str, now_ms: int) -> GuardrailSignal | None:
    """The guardrail's reading at ``path``, or ``None`` with one WARNING saying why not.

    ``coin`` is the one this run trades; ``now_ms`` is the gate's own instant
    (the engine's clock, in UTC epoch milliseconds). A document stamped after
    ``now_ms`` is a clock that disagrees with this host, not a closed bar.
    """
    resolved, document = read_document(path, what=_WHAT, consequence=_CONSEQUENCE)
    if document is UNREAD:
        return None
    try:
        signal = GuardrailSignal.from_document(document)
    except ValueError as exc:
        logger.warning("%s document at %s was refused, %s: %s", _WHAT, resolved, _CONSEQUENCE, exc)
        return None
    if signal.coin != coin.strip().upper():
        logger.warning(
            "%s document at %s is for %r and this run trades %r, %s",
            _WHAT,
            resolved,
            signal.coin,
            coin,
            _CONSEQUENCE,
        )
        return None
    age_ms = now_ms - signal.as_of_ms
    max_age_ms = max_signal_age_ms(signal.interval)
    if age_ms > max_age_ms:
        logger.warning(
            "%s document at %s was decided %s ago, %s past the %d x %s bound, %s — re-run "
            "the research package's `guardrail-signal` command",
            _WHAT,
            resolved,
            gap_label(age_ms),
            gap_label(age_ms - max_age_ms),
            MAX_SIGNAL_AGE_INTERVALS,
            signal.interval,
            _CONSEQUENCE,
        )
        return None
    if age_ms < 0:
        logger.warning(
            "%s document at %s is stamped %s AFTER this gate's own clock, which no closed bar "
            "can be, %s — check the clock on the host that wrote it",
            _WHAT,
            resolved,
            gap_label(-age_ms),
            _CONSEQUENCE,
        )
        return None
    return signal


def guardrail_side_for(path: str, *, coin: str, now: datetime) -> GuardrailSide | None:
    """What the gate is handed: ``None`` when ``path`` is empty (the guardrail is off), else a side.

    A document that cannot be read or trusted is ``UNKNOWN`` — the reader has
    already said why — so nothing new opens and what is held is kept. The one
    place the switch's two meanings (off, and on-but-unreadable) are told
    apart, used by the paper engine and the one-shot command alike.
    """
    if not path:
        return None
    signal = load_guardrail_signal(
        path, coin=coin, now_ms=epoch_ms(now, what="the guardrail gate's instant")
    )
    return GuardrailSide.UNKNOWN if signal is None else signal.side
