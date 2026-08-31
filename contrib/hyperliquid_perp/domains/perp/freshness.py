"""Market-context freshness guard — is this context the CURRENT market?

Extracted from ``engine_bridge`` (issue #94): the exchange-clock rewrite
(PR #91) grew the guard from ~50 lines to ~200 inside a module whose job is
composing exchange reads into engine inputs. This module owns the verdict —
:func:`freshness_refusal` and the pieces it is built from — and
:func:`~.context_guards.context_refusal` calls it as the last of the four
pre-LLM guards.

SDK-free and persistence-free on purpose: it imports only :mod:`.schema` (for
the context type and :func:`~.schema.interval_to_ms`) and ``common``. A
domain module that reached into the exchange adapter or the store would
invert the package's layering; staying below both is also what lets
:class:`ContextRefusal` validate its class against
``common.constants.ERROR_TYPES`` without importing the persistence package
that admits the same set at the write boundary. The layering is pinned as an
import-closure test (``tests/common/test_layering.py``), not left to prose;
it is a property of the guard family, not of the ``--context-only`` command,
which fetches live candles through the SDK regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ...common.constants import CYCLE_INTERVAL, ERROR_TYPES, STALE_MARKET_DATA_ERROR
from ...common.enum_guard import check_enum
from ...common.instants import whole_hours_label
from .schema import PerpMarketContext, interval_to_ms

__all__ = [
    "STALE_CONTEXT_ERROR",
    "ContextRefusal",
    "freshness_refusal",
]

logger = logging.getLogger(__name__)


def _format_duration_ms(ms: int) -> str:
    """``ms`` as ``"45m 0s"`` / ``"14h 12m 30s"`` / ``"153d 4h"``, for refusal text.

    Seconds are carried because the refusal message prints an age and the limit
    it exceeded side by side: a minute-resolution format renders every age in
    the first MINUTE past the limit as the limit itself, and the message then
    reads "X is past the X limit". Seconds narrow that window to the first
    second rather than removing it — sub-second precision would cost more
    legibility than the residue is worth.

    The day form starts at two days, not one, purely for readability: an outage
    of thirty-odd hours reads better as ``"30h 0m 0s"`` than as ``"1d 6h"``,
    and the collision the seconds exist for cannot happen in either band (the
    widest limit, one 1d bar plus one decision cycle, is under the two-day
    band, and the future tolerance is a minute).
    """
    seconds, minutes = ms // 1000 % 60, ms // 60_000 % 60
    hours, days = ms // 3_600_000, ms // 86_400_000
    if ms < 3_600_000:
        return f"{minutes}m {seconds}s"
    if days < 2:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{days}d {hours % 24}h"


def _utc_stamp(moment: datetime) -> str:
    """``moment`` as a UTC ISO stamp; the tz is normalized, never assumed."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# How old the newest candle may be, counted in candle intervals.
# ``get_candles`` drops the still-forming bar, so a healthy feed's newest CLOSED
# candle is under one interval old, and each bar the exchange fails to publish
# adds another interval: three tolerates two consecutive missing bars and
# refuses at the third — for bars the clamp below leaves alone; a bar wider
# than the ceiling gets one bar plus one cycle instead (``_candle_age_limit``,
# issue #92). Not a config knob — a correctness bound on "is this still the
# current market?", not a tuning parameter.
_MAX_CANDLE_AGE_INTERVALS = 3
# ...but the interval is operator-configurable (1m through 1d) while the decision
# cycle is fixed, so the bar width alone would make this guard mean wildly
# different things: 3 x 1d would let 18 cycles trade through a three-day outage,
# 3 x 1m would refuse a cycle over three minutes of feed jitter. Clamp both ends
# in terms of the DECISION cadence the guard actually protects: at most three
# cycles of stale data (except where that would sit under a single healthy bar
# — then one bar plus one cycle, 28h for 1d), and never so tight that ordinary
# jitter refuses a cycle.
# The ceiling states three DECISION cycles, so it is DERIVED from the cadence
# it names, ``common.constants.CYCLE_INTERVAL``, and a changed cycle length
# moves this bound — and the operator-facing "3 x the 4h decision cycle" text —
# with it. (Until issue #122 the cadence lived on ``paper.scheduler``, whose
# import would have pulled the whole paper engine into this keyless module for
# one timedelta, so the value was written out here and drift-locked instead.)
# The label refuses a cadence that is not whole hours at import, like the
# reconciler's lookback label — see ``whole_hours_label``.
_DECISION_CYCLE_MS = int(CYCLE_INTERVAL.total_seconds() * 1000)
_CYCLE_LABEL = whole_hours_label(CYCLE_INTERVAL, what="common.constants.CYCLE_INTERVAL")
_MAX_CANDLE_AGE_CEILING_MS = _MAX_CANDLE_AGE_INTERVALS * _DECISION_CYCLE_MS
_MAX_CANDLE_AGE_FLOOR_MS = 30 * 60_000


def _candle_age_limit(interval_ms: int, interval: str) -> tuple[int, str]:
    """``(limit_ms, how it was derived)`` for this candle interval.

    The derivation travels with the number so a refusal message never states a
    bound whose origin the operator cannot see — the clamp is invisible in the
    number alone, and "12h" reads very differently as "3 x 4h" than as "3 x 1d,
    capped".

    Invariant, whatever the clamp does: the limit is never below ONE interval
    (issue #92). The newest CLOSED bar of a healthy feed is anywhere from zero
    to one interval old — a 1d bar closes at 00:00 UTC and ``get_candles``
    drops the forming one, so by 12:00 the newest closed bar is twelve hours
    old with nothing wrong anywhere — and a limit under one interval refused
    every cycle in the second half of every day, which the issue-#50
    escalation then reported as a stalled feed (exit 4). Where the clamp
    lands under one bar the limit becomes one bar plus one decision cycle:
    the bar due at the boundary has a whole cycle to appear before the feed
    is called stopped. A cycle, not the seconds it actually takes (the bar
    reads closed the moment the exchange's clock passes its close — see the
    measurement on ``_CANDLE_LEAD_TOLERANCE_MS``), because the guard is
    stated in decision cycles throughout and "24h plus a few seconds" would
    refuse the first cycle after any hiccup in that publishing at all. For 1d
    that is 28h: a daily feed that misses a bar is refused once the newest
    closed one is over 28h old — the first or second cycle after the missed
    boundary (consecutive cycles are over 4h apart, so at most one can land
    inside the grace window). Only the ceiling
    can land under a bar (the floor is three bars wide by construction), and
    of the configurable intervals only 1d makes it do so; the cap stays for
    an interval over one cycle and up to three, which none of them is today
    (4h is exactly one cycle and takes the uncapped ``3 x 4h``).
    """
    base = _MAX_CANDLE_AGE_INTERVALS * interval_ms
    if base > _MAX_CANDLE_AGE_CEILING_MS:
        limit, basis = (
            _MAX_CANDLE_AGE_CEILING_MS,
            (
                f"{_MAX_CANDLE_AGE_INTERVALS} x {interval} capped at "
                f"{_MAX_CANDLE_AGE_INTERVALS} x the {_CYCLE_LABEL} decision cycle"
            ),
        )
    elif base < _MAX_CANDLE_AGE_FLOOR_MS:
        # The floor's label is DERIVED, like the ceiling's above. Written out as
        # "30m" it would keep telling the operator 30m after the constant moved
        # — the one branch of this function whose derivation did not travel with
        # its number, which is the property the docstring claims for all of them.
        # Whole minutes by floor division, which is honest only while the
        # constant is whole minutes; a test asserts that rather than leaving the
        # assumption to be discovered by a truncated label.
        limit, basis = (
            _MAX_CANDLE_AGE_FLOOR_MS,
            (
                f"{_MAX_CANDLE_AGE_INTERVALS} x {interval} raised to the "
                f"{_MAX_CANDLE_AGE_FLOOR_MS // 60_000}m floor"
            ),
        )
    else:
        limit, basis = base, f"{_MAX_CANDLE_AGE_INTERVALS} x {interval}"
    if limit < interval_ms:
        # Reachable only from the ceiling, so the interval is over three
        # cycles here and the raise stays under ``base`` (interval + cycle <
        # 3 x interval whenever interval > cycle / 2); a test pins both ends.
        return interval_ms + _DECISION_CYCLE_MS, (
            f"one {interval} bar plus one {_CYCLE_LABEL} decision cycle — the "
            f"{_MAX_CANDLE_AGE_INTERVALS} x {_CYCLE_LABEL} cap would sit "
            "below a single healthy bar"
        )
    return limit, basis


@dataclass(frozen=True)
class ContextRefusal:
    """Why a context must not be traded on: its §6.2 class and its sentence.

    The class is what the DURABLE record keys on
    (``decision_attempts.error_type``, which the acceptance validators query);
    the sentence is what an operator reads. They are split because the four
    guards fail for very different reasons and only the freshness ones recur
    until a human acts. The acceptance validators gate on trailing
    CONSECUTIVENESS, not on the class; what the class earns is the specific
    operator wording and the reported stale-feed subset (issue #50).

    A frozen dataclass rather than a ``NamedTuple`` so the class can be
    VALIDATED: ``NamedTuple`` never calls ``__post_init__``. The write boundary
    checks the same set, but only when the daemon records the failed cycle —
    a class spelled wrong here would pass both one-shot entry points (they
    print the sentence and exit) and surface as a ``ValueError`` out of the
    repository, on the first refused cycle, in the daemon alone.
    """

    error_type: str
    message: str

    def __post_init__(self) -> None:
        check_enum(self.error_type, ERROR_TYPES, name="ContextRefusal.error_type")


# The §6.2 class for the freshness guard's verdicts, which do NOT recover on
# their own: a feed that stopped advancing, or an age that cannot be
# established, blocks every cycle until a human fixes the feed or the host
# clock. Imported, never spelled out here — the acceptance validators query on
# this exact word. (The three "cannot be reasoned over" guards write
# ``context_guards.UNUSABLE_CONTEXT_ERROR`` instead, beside the guards that
# use it; :class:`ContextRefusal` binds both words to the registry.)
STALE_CONTEXT_ERROR = STALE_MARKET_DATA_ERROR


def freshness_refusal(
    ctx: PerpMarketContext, coin: str, *, now: datetime | None = None
) -> ContextRefusal | None:
    """Why ``ctx`` describes a market other than the current one, or ``None``.

    ``ctx.as_of`` is the newest candle's close (context_builder) and nothing
    upstream compares it to a clock: a feed that stalled — or a snapshot
    replayed from an earlier run — yields a context whose indicators all
    compute cleanly and whose regime reads healthy, so the three "cannot be
    reasoned over" guards in ``context_guards.context_refusal`` pass it. It
    merely describes the past. That same ``as_of`` becomes the engine's
    ``trade_date`` (cli), so a stale feed also silently moves the analysts'
    whole research window to an earlier day.

    Vacuous with zero candles by construction (context_builder falls back to a
    wall-clock ``as_of``, age zero) — the warm-up guard owns that case, which
    is one reason it runs first.

    The age is measured against the EXCHANGE's clock when the context carries
    one (``ctx.exchange_time``, read by ``_build_context`` in the same fetch as
    the candles): the candle window is cut by the host clock, so a host that
    runs behind truncates it by the same amount and the newest bar looks
    ordinary against that same host clock — the guard was blind to a host
    three days slow (issue #51). Measured against the exchange's clock the age
    is the real one, and the existing limit refuses it — the stale side needs
    no second threshold. The FUTURE side does have its own
    (``_CANDLE_LEAD_TOLERANCE_MS``): a closed bar never leads the exchange's
    clock, so a lead past a minute is the still-forming bar a host running
    ahead admitted, whatever the stale limit is (issue #93).

    ``now`` is the HOST clock, and it has exactly ONE use: the fallback
    measuring clock for a context with no ``exchange_time`` (fixtures,
    replays; that fallback keeps the blind spot and says so in its message).
    It is NOT what the skew is measured from — that comes from
    ``ctx.host_time_at_exchange_read``, the host reading ``_build_context``
    took adjacent to the exchange one, because ``now`` on the daemon path is
    a reading from BEFORE the fetch and subtracting it would report the
    fetch's elapsed time as clock drift. The daemon passes the reading its
    own clock gave at the start of THIS attempt; the one-shot callers let it
    default to :func:`datetime.now`. Every production caller builds ``ctx``
    from a live REST read (``_build_context`` is ``build_market_context``'s
    only production caller), so the fallback is a fixture path — the
    parameter exists to make the clock injectable, not to model a second
    time base.
    """
    host_now = now if now is not None else datetime.now(tz=timezone.utc)
    try:
        interval_ms = interval_to_ms(ctx.candle_interval)
    except ValueError as exc:
        # Unreachable via _build_context (get_candles resolves the same interval
        # before a single candle exists, so an unusable one raises there first).
        # A context that gets here anyway carries an interval nothing can
        # measure — refuse rather than skip the age check.
        return ContextRefusal(
            STALE_CONTEXT_ERROR,
            f"cannot establish the age of {coin}'s market data — {exc}. Refusing "
            "to run the engine on a context whose freshness cannot be checked.",
        )
    limit_ms, limit_basis = _candle_age_limit(interval_ms, ctx.candle_interval)
    exchange_now = ctx.exchange_time
    if exchange_now is None:
        return _host_clock_freshness_refusal(ctx, coin, host_now, limit_ms, limit_basis)
    age_ms = int((exchange_now - ctx.as_of).total_seconds() * 1000)
    # Skew is measured between the two readings ``_build_context`` took
    # ADJACENTLY, never against ``now``: the daemon's ``now`` is its own clock
    # reading from before the fetch, so subtracting it would report the fetch's
    # elapsed time as clock error. A context that carries an exchange clock but
    # no paired host reading (hand-built) simply has no skew to report.
    host_at_read = ctx.host_time_at_exchange_read
    skew_ms = (
        None if host_at_read is None else int((host_at_read - exchange_now).total_seconds() * 1000)
    )
    skew_note = _host_clock_skew_note(skew_ms, host_at_read)
    if skew_ms is not None and abs(skew_ms) >= _CLOCK_SKEW_WARN_MS:
        # Log-only, never a gate: the age check below is what decides, and it
        # is measured entirely between exchange-side values. This is the
        # operator's early notice — a 2h-slow host still PASSES with 4h bars
        # (the data is 6h old, inside the 12h limit) but is one outage away
        # from not passing, and "fix NTP" is cheaper before that. Built from
        # the same ``skew_note`` the refusals carry so the two cannot drift.
        #
        # It does NOT say the decision is unaffected, because for a host
        # running AHEAD it can be: ``get_candles`` cuts its window at this
        # host's clock and keeps ``close_time <= end``, so a lead admits the
        # still-forming bar, whose partial OHLCV understates ATR and skews
        # RSI/EMA. The lead check below refuses that bar once it is more than
        # ``_CANDLE_LEAD_TOLERANCE_MS`` past the exchange's clock; a lead
        # inside the tolerance can still admit it in the last seconds of a
        # bar, which is what this notice is for.
        logger.warning(
            "%s Fix time sync (NTP): the candle window this host asks for is "
            "offset from the exchange's clock (%s) by the same amount, and a "
            "host running AHEAD can pull in a bar the exchange has not closed",
            skew_note,
            _utc_stamp(exchange_now),
        )
    # A candle closing AFTER the exchange's clock by more than the tolerance —
    # the still-forming bar a host running AHEAD pulls through get_candles'
    # ``close_time <= end`` filter (issue #93). Its own bound, not the stale
    # limit: a CLOSED bar never closes after the exchange's clock (the clock is
    # read after the candles, and the exchange publishes a bar as closed at
    # its boundary), so the only legitimate lead is the error between two
    # exchange-side stamps, and the stale limit (12h for 4h bars) let every
    # host lead under one interval through unseen.
    if age_ms < -_CANDLE_LEAD_TOLERANCE_MS:
        # The lead cannot exceed the host's own lead when the bar came from a
        # live fetch with a steady host clock (the window was cut at
        # ``host_at_read`` minus the fetch gap, and the exchange's clock only
        # advanced since). A paired reading that does NOT show the host ahead
        # by at least this much therefore leaves two shapes: the host clock
        # STEPPED back between the candle read and the clock read (an NTP
        # step, suspend/resume — the fallback path's docstring lists the same
        # events), or the context was replayed or hand-built. Name both; the
        # first is a production event and must not be waved off as replay.
        if skew_ms is not None and skew_ms >= -age_ms:
            cause = (
                f"{skew_note} The candle window this host asked for ends that far "
                "in the exchange's future, so it kept a bar the exchange has not "
                "closed — its OHLCV is partial (understated ATR, skewed RSI/EMA). "
                "Fix time sync (NTP) before trusting any cycle. Refusing to run "
                "the engine on a candle the exchange has not closed."
            )
        elif skew_ms is None:
            cause = (
                f"{skew_note} Either this host's clock ran ahead when the candle "
                "window was cut, admitting a bar the exchange has not closed, or "
                "this context did not come from a live market fetch. Refusing to "
                "run the engine on a context whose age cannot be established."
            )
        else:
            cause = (
                f"{skew_note} No host lead that size was recorded beside the "
                "exchange's clock: either this host's clock stepped back between "
                "the candle read and the clock read (an NTP step, suspend/resume "
                "— check the system log), or this context did not come from a "
                "live market fetch. Refusing to run the engine on a context whose "
                "age cannot be established."
            )
        return ContextRefusal(
            STALE_CONTEXT_ERROR,
            f"the newest {coin} candle closes at {_utc_stamp(ctx.as_of)}, which is "
            f"{_format_duration_ms(-age_ms)} AFTER the exchange's clock "
            f"({_utc_stamp(exchange_now)}) — more than the "
            f"{_format_duration_ms(_CANDLE_LEAD_TOLERANCE_MS)} a closed candle can "
            f"lead it by. {cause}",
        )
    if age_ms > limit_ms:
        # Name the cause only as far as the two clocks license. A host behind
        # by S shifts the candle window back by S, so S is how much of this age
        # the clock can account for: past the whole limit it is sufficient on
        # its own, below the warn floor it is irrelevant, and in between it is
        # a contributor the operator must check ALONGSIDE the feed. Claiming
        # either cause outright in that middle band would send half the
        # investigations down the wrong path — the same coin-flip the
        # host-clock-only fallback below still has to live with.
        if skew_ms is None:
            cause = f"{skew_note} The cause cannot be narrowed further from here."
        elif skew_ms < -limit_ms:
            cause = (
                f"{skew_note} An offset that large by itself puts the newest "
                "candle this host can ask for past the limit — fix time sync "
                "(NTP) before trusting any cycle."
            )
        elif skew_ms <= -_CLOCK_SKEW_WARN_MS:
            cause = (
                f"{skew_note} That shifts the candle window back by the same "
                f"amount, so it accounts for {_format_duration_ms(-skew_ms)} of "
                "this age but not all of it — check time sync (NTP) AND the "
                "exchange's candle feed."
            )
        else:
            cause = (
                f"{skew_note} The market data feed itself stopped advancing: the "
                "exchange published no newer closed candle."
            )
        return ContextRefusal(
            STALE_CONTEXT_ERROR,
            f"the newest {coin} candle closed at {_utc_stamp(ctx.as_of)}, "
            f"{_format_duration_ms(age_ms)} before the exchange's clock "
            f"({_utc_stamp(exchange_now)}) — past the {_format_duration_ms(limit_ms)} "
            f"freshness limit ({limit_basis}). {cause} This context describes a "
            "market other than the current one. Refusing to run the engine on "
            "market data this old.",
        )
    return None


# Host-vs-exchange clock skew at or past which the guard logs a warning (and
# names the host clock as the cause in a refusal). Log-only — the age check
# against the exchange's clock is the gate — so this is an operator nudge, not
# a correctness bound: one minute is far outside anything NTP leaves behind and
# far inside ``_MAX_CANDLE_AGE_FLOOR_MS``, the floor of the limit the skew would
# have to reach to matter. (Named, not written out as "30m": this is one of the
# sites that would keep quoting the old number if that constant moved.)
# Deliberately NOT the kill switch's 5s ``_MAX_CLOCK_SKEW_S``: that bound is
# about absolute scheduleCancel deadlines, where seconds change the protection
# window; here seconds change nothing, and importing live.kill_switch would
# drag the live package into the keyless --context-only path.
_CLOCK_SKEW_WARN_MS = 60_000

# How far past the EXCHANGE's clock the newest candle's close may sit before
# the guard calls it a bar the exchange has not closed (issue #93). Only the
# exchange-clock path uses it; the host-clock fallback keeps the stale limit
# as its future tolerance because there the daemon's clock reading precedes
# the fetch and a boundary closing in between legitimately lands ahead of it.
#
# On the exchange-clock path no such shape exists: ``_build_context`` reads
# the candles FIRST and the exchange's clock after, so a bar that closed
# during the fetch is simply not in the candle response, and a closed bar's
# close_time is at or before the exchange's clock by construction. What can
# legitimately remain is the disagreement between two exchange-side stamps —
# the l2Book ``time`` and the candle service's boundaries. Measured
# 2026-08-26 against the public API (BTC, 1m bars, 114 paired reads over
# 240s, sampled every 2s): l2Book time minus the newest kept bar's
# close_time was never negative and bottomed at 7.2s right after each
# boundary — the measuring host's own 6s lag behind the exchange (stable
# within ±0.4s over the run) plus the sampling period — and the bar the
# host kept was always one the exchange's clock had passed. That leaves no
# observed disagreement between the two exchange-side stamps beyond the 2s
# sampling resolution; a minute is well above that. The same run showed the
# forming bar present in every candleSnapshot response, reading as closed
# the moment the exchange's clock passed its close — no publish step. And
# the l2Book ``time`` is the SERVER's clock, not the book's last update:
# sampled the same day on the three thinnest perps by 24h volume (under
# $50k each) it advanced with every read exactly as BTC's did, so a coin
# that trades rarely does not hand the guard a stale stamp. What it lets
# through
# is bounded too: a host ahead by H admits the forming bar when the fetch
# lands within H of the bar's close, and that bar leads the exchange's clock
# by the time still left to its close — so a refusal means more than a
# minute of the bar was missing, and a pass means less. At the 1m interval
# that is every admitted bar, so there a host lead is never refused on this
# branch (the bar is missing under a minute of trades). The bound cannot
# sit below ``_CLOCK_SKEW_WARN_MS``: a live fetch's lead is at most the
# host's own lead (the window was cut at the host's clock before the
# exchange's was read), so a refusal here with the note saying the clocks
# "agree" would contradict itself — a test pins the ordering.
_CANDLE_LEAD_TOLERANCE_MS = 60_000


def _host_clock_skew_note(skew_ms: int | None, host_at_read: datetime | None) -> str:
    """One sentence on how this host's clock sits against the exchange's."""
    if skew_ms is None or host_at_read is None:
        return "This context carries no paired host-clock reading, so the skew is unknown."
    if abs(skew_ms) < _CLOCK_SKEW_WARN_MS:
        return f"This host's clock ({_utc_stamp(host_at_read)}) agrees with the exchange's."
    direction = "ahead of" if skew_ms > 0 else "behind"
    return (
        f"This host's clock reads {_utc_stamp(host_at_read)}, "
        f"{_format_duration_ms(abs(skew_ms))} {direction} the exchange's."
    )


def _host_clock_freshness_refusal(
    ctx: PerpMarketContext, coin: str, moment: datetime, limit_ms: int, limit_basis: str
) -> ContextRefusal | None:
    """The freshness verdict measured against the HOST clock — the fallback.

    Only for a context with no ``exchange_time`` (``_build_context`` always
    sets one, so this is the fixture / replay path). It keeps the issue-#51
    blind spot the exchange-clock path above closes — there is no second clock
    here to close it with — and its messages say so rather than claiming a
    verdict this measurement cannot support.

    What it DOES catch is the clock JUMPING between the two readings that
    produced these timestamps — ``moment`` and ``get_candles``' own window end
    — from a host resuming from suspend, an NTP step, a container clock
    resyncing; and a ``ctx`` that never came from a live fetch. Do not name a
    direction: the two readings happen in opposite orders on the two paths
    (the daemon reads its clock first and fetches after; the one-shot callers
    let ``now`` default, AFTER the fetch), so the same branch means a forward
    jump on one and a backward jump on the other. What is common to both is
    that the readings disagree, which is all the refusal needs to say.

    Small negatives are legitimate on the daemon path and must NOT trip it:
    its clock reading precedes the market reads, so a boundary closing in
    between lands slightly ahead of it. (The one-shot callers read after the
    fetch and expect no negative at all.) Sharing the bound keeps that slack
    comfortably wide at every interval: the gap spans two REST calls, so at the
    default ``network_timeout_s`` of 30 the worst case it has to cover is 60s,
    against a floor (``_MAX_CANDLE_AGE_FLOOR_MS``) of 1800s — 30x that gap, or
    60x a single timeout. Sharing it beats inventing a
    second threshold to re-derive whenever that timeout changes. (config
    validates network_timeout_s as a number but sets no upper bound, so a
    deployment choosing minutes-long timeouts would need this revisited.)
    """
    age_ms = int((moment - ctx.as_of).total_seconds() * 1000)
    if age_ms < -limit_ms:
        return ContextRefusal(
            STALE_CONTEXT_ERROR,
            f"the newest {coin} candle closes at {_utc_stamp(ctx.as_of)}, which is "
            f"{_format_duration_ms(-age_ms)} AFTER the current time "
            f"({_utc_stamp(moment)}) — more than the {_format_duration_ms(limit_ms)} "
            f"tolerance ({limit_basis}). The candle window is taken from this same "
            "clock, so a gap this size means it jumped between the two readings "
            "(suspend/resume, an NTP step, a container clock resync), or this "
            "context did not come from a live market fetch. Either way the two "
            "timestamps cannot be compared. Refusing to run the engine on a "
            "context whose age cannot be established.",
        )
    if age_ms > limit_ms:
        return ContextRefusal(
            STALE_CONTEXT_ERROR,
            f"the newest {coin} candle closed at {_utc_stamp(ctx.as_of)}, "
            f"{_format_duration_ms(age_ms)} before now ({_utc_stamp(moment)}) — "
            f"past the {_format_duration_ms(limit_ms)} freshness limit "
            f"({limit_basis}). Either the market data feed stopped advancing or "
            "this host's clock is ahead — the two are indistinguishable from "
            "here (this context carries no exchange clock to tell them apart), "
            "and both mean this context describes a market other than the "
            "current one. Refusing to run the engine on market data this old.",
        )
    return None
