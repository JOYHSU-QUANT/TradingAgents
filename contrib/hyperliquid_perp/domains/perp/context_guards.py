"""The pre-LLM context guards — may this context be traded on at all?

Four guards, one verdict (:func:`context_refusal`): warm-up, fully-dead
indicator set, missing/dead regime indicators, and the freshness verdict
:mod:`.freshness` owns. The first three say "this context cannot be reasoned
over" and are written out here; the fourth says "it is well-formed but out of
date" and is called last, for the reason its docstring gives.

Extracted from ``engine_bridge`` (issue #122): the guards are pure domain
logic — a context plus the operator's config — and lived in the module that
composes exchange reads into engine inputs, while the §6.2 class they write
(:data:`UNUSABLE_CONTEXT_ERROR`) lived in ``freshness``, which never used it.
Here the whole family sits below the SDK, the persistence package and both
engines: this module's load-time import closure is ``domains.perp`` and
``common`` only, and ``tests/common/test_layering.py`` pins that. (The
``--context-only`` path itself still loads the SDK — it fetches live candles
through ``engine_bridge._build_context`` — so this is a property of the
guards, not of that command.)

Patch-target note: the callers reach these names through THIS module —
``main`` by attribute access, the ``cli`` provider by a function-local
from-import — so a test stubbing :func:`warmup_threshold` patches
``context_guards`` and is seen by every path. ``engine_bridge`` keeps no
re-export: a second binding there would be exactly the second patch surface
its own docstring forbids.
"""

from __future__ import annotations

from datetime import datetime

from .freshness import ContextRefusal, freshness_refusal
from .indicator_vocab import (
    REGIME_INDICATORS,
    indicator_names,
    required_candles,
    supported_indicators,
)
from .schema import PerpMarketContext

__all__ = [
    "UNUSABLE_CONTEXT_ERROR",
    "context_refusal",
    "context_refusal_message",
    "warmup_threshold",
]

# The §6.2 class for the three guards that describe a context nothing can be
# reasoned over: a broken indicator engine or a too-young listing is an
# environmental failure like any other, and the run recovers on its own once
# the coin warms up or stockstats is fixed. The freshness guard's verdicts,
# which do NOT recover on their own, carry ``freshness.STALE_CONTEXT_ERROR``
# instead; :class:`~.freshness.ContextRefusal` refuses any word outside the
# ``ERROR_TYPES`` registry at construction, so both classes the family writes
# are bound to it.
UNUSABLE_CONTEXT_ERROR = "server_error"


def warmup_threshold(config: dict) -> int:
    """Candles the configured indicators need before they read as real signal."""
    return required_candles(indicator_names(config))


def context_refusal(
    ctx: PerpMarketContext, coin: str, config: dict, *, now: datetime | None = None
) -> ContextRefusal | None:
    """Why this context must not be traded on, or ``None`` if usable.

    Single source of truth for the four pre-LLM context guards — warm-up,
    fully-dead indicator set, missing/dead regime indicators
    (atr_14/ema_20/ema_50), stale feed, in that order: an
    under-warmed context legitimately has all-None indicators, so the dead-set
    diagnosis only means "the indicator engine broke" once the warm-up bar is
    cleared. Shared by the one-shot path (print + exit 1), the daemon
    provider (retry ladder -> api_failed cycle), and
    ``--context-only`` (render + warn) so the entry points can't drift apart —
    the daemon missing guards the one-shot had is exactly the drift this
    helper exists to prevent.

    The three "cannot be reasoned over" guards are written out here; the
    staleness verdict is :func:`~.freshness.freshness_refusal`'s, and ``now``
    — the HOST clock, whose only use is the fallback measuring clock for a
    context that carries no exchange clock — is passed straight through to
    it. Which clock the age is measured against, and what ``now`` is NOT used
    for, is documented there.
    """
    # Refuse to reason over under-warmed data: if fewer candles came back than
    # the configured indicators need, every indicator is None and the regime is
    # a guess — refuse before spending an LLM call on a hollow context.
    needed = warmup_threshold(config)
    if ctx.candle_count < needed:
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"only {ctx.candle_count} candles available for {coin}, but the "
            f"configured indicators need {needed}. Refusing to run the engine on "
            "under-warmed market data.",
        )
    # Past the warm-up gate every configured indicator has enough candles, so a
    # fully-None known-indicator set is not under-warm — it means the indicator
    # engine (stockstats) failed on every column (version drift, bad frame). That
    # set is indistinguishable from a warm-up dict downstream: the regime silently
    # defaults to RANGING. Refuse it before spending an LLM call on signals that
    # are all dead.
    known = set(supported_indicators())
    computed = [v for k, v in ctx.indicators.items() if k in known]
    if computed and all(v is None for v in computed):
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"every technical indicator failed to compute for {coin} despite "
            f"{ctx.candle_count} candles — the indicator engine (stockstats) is likely "
            "broken or incompatible. Refusing to run the engine on a fully-dead "
            "indicator set.",
        )
    # The regime trio (see REGIME_INDICATORS for why they are load-bearing):
    # refuse whether a name was dropped from the configured indicator set
    # entirely or computed to None (stockstats failing on that column past the
    # warm-up gate — dead columns slipping past the all-dead guard above);
    # either way, do not trade on a fabricated-calm regime.
    dead = [name for name in REGIME_INDICATORS if ctx.indicators.get(name) is None]
    if dead:
        verb = "is" if len(dead) == 1 else "are"
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"{', '.join(dead)} {verb} unavailable for {coin} (not in the "
            f"configured indicator set, or failed to compute despite "
            f"{ctx.candle_count} candles) — the regime would silently default "
            "to RANGING, hiding a volatile or trending market. Refusing to run "
            "the engine without usable regime indicators.",
        )
    # Freshness — last of the four on purpose: those three say "this context
    # cannot be reasoned over at all", this one says "it is well-formed but out
    # of date". Why a stale context passes the three above, which clock the
    # age is measured against, and what ``now`` is for: see the guard itself.
    return freshness_refusal(ctx, coin, now=now)


def context_refusal_message(
    ctx: PerpMarketContext, coin: str, config: dict, *, now: datetime | None = None
) -> str | None:
    """:func:`context_refusal`'s sentence alone, or ``None`` if usable.

    The view for the two callers that only report it: ``main.run_engine``
    prints it and exits 1, ``main.run_context_only`` (the ``--context-only``
    path) renders it as a warning and exits 4. Only the daemon provider, which
    writes the durable attempt row, needs the §6.2 class alongside it.
    """
    refusal = context_refusal(ctx, coin, config, now=now)
    return None if refusal is None else refusal.message
