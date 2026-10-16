"""A guardrail rule, and the side it held at every close of a store's history.

The trend guardrail (decided 2026-10-02) bounds what the trader may hold by
the SIDE of one fixed rule: no position against it, and none while it is
flat. This module is the research half of that: which rule (a spec file
under ``guardrails/``), what to call it, and its side at each bar. It scores
nothing and files nothing.

A guardrail rule is NOT a promoted trial and does not pass through the
ledger. The promote gate reads one validation window and exists to ration a
search; a guardrail rule is admitted on other terms, decided the same day:
written down before any result was seen, measured on long history, and never
tuned afterwards. The last of those is the one a test can hold, so
``tests/test_guardrail.py`` pins the committed spec's hash, and editing the
rule is a change to two files rather than a drift in one.

Two consumers. The replay package's offline report asks what the guardrail
WOULD have done to decisions the paper trader already took
(:func:`build_timeline`). The ``guardrail-signal`` command writes the rule's
newest side as a handoff document (:func:`build_guardrail_signal`), which
the trading daemon's risk gate reads — the same seam as the research signal,
and the one thing in this package that reaches a gate (wired 2026-10-16,
off by default on the daemon's side).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .costs import CostModel
from .dsl import Side, StrategySpec, describe_spec, load_spec, spec_hash
from .store import ResearchStore, canonical_coin
from .upstream import (
    MAX_SIGNAL_AGE_INTERVALS,
    GuardrailSide,
    GuardrailSignal,
    from_epoch_ms,
    gap_label,
    max_signal_age_ms,
)
from .vocabulary import SpecError

__all__ = [
    "DEFAULT_RULE",
    "RULE_INTERVAL",
    "GuardrailError",
    "GuardrailRule",
    "RuleReading",
    "RuleTimeline",
    "build_guardrail_signal",
    "build_timeline",
    "describe_guardrail_signal",
    "load_rule",
    "require_fresh",
]

# The rule the guardrail was decided on (2026-10-02): enter on a close beyond
# the 120-bar channel, leave on a close beyond the 55-bar one, both sides. On
# 4h bars that is a twenty-day breakout with a nine-day exit.
DEFAULT_RULE: Final = Path(__file__).resolve().parent / "guardrails" / "btc-20d-breakout.json"

# The cadence every guardrail rule is replayed on. A spec counts its windows
# in bars and carries no interval of its own, so the cadence is part of what
# the rule IS: the same file on daily bars would be a 120-day channel, a
# different rule under the same name. Kept here, beside the rule, so every
# reader of a timeline gets the same one.
RULE_INTERVAL: Final = "4h"

# How many leading characters of the spec hash a rule's name carries: enough
# to tell two versions of a file apart in a report, short enough to read.
_HASH_CHARS: Final = 8


class GuardrailError(ValueError):
    """The rule cannot be read, or this store cannot say which side it held.

    A ``ValueError``, like :class:`~.signal.SignalError`: a sentence written
    for an operator, which the replay CLI catches by name, prints and exits 1
    on.
    """


@dataclass(frozen=True)
class GuardrailRule:
    """One guardrail rule: its spec, and the name a report prints for it.

    ``rule_id`` is ``<file stem>@<spec hash prefix>``, so a report made under
    an edited rule cannot be mistaken for one made under the committed rule.
    """

    rule_id: str
    spec: StrategySpec


def load_rule(path: str | Path = DEFAULT_RULE) -> GuardrailRule:
    """The rule in the spec file at ``path``, or a :class:`GuardrailError` saying why not."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GuardrailError(f"could not read the guardrail rule {str(path)!r}: {exc}") from exc
    try:
        spec = load_spec(text)
    except SpecError as exc:
        raise GuardrailError(f"{str(path)!r} is not a rule this package can replay: {exc}") from exc
    return GuardrailRule(rule_id=f"{target.stem}@{spec_hash(spec)[:_HASH_CHARS]}", spec=spec)


@dataclass(frozen=True)
class RuleReading:
    """The side the rule held at one instant (``None`` is flat), and the close it was decided at."""

    side: Side | None
    close_time: int


@dataclass(frozen=True)
class RuleTimeline:
    """One rule's side after every bar of one coin's stored history.

    ``sides[i]`` is the side held once the decision at ``close_times[i]`` has
    been applied, and ``unevaluable[i]`` says the rule could not evaluate
    that decision, so the side there is one it carried in rather than one it
    took. ``first_decided`` is the index of the first bar it could evaluate:
    before it the features are still warming up, and a side there is the
    replay's starting state.
    """

    rule: GuardrailRule
    coin: str
    interval: str
    close_times: tuple[int, ...]
    sides: tuple[Side | None, ...]
    unevaluable: tuple[bool, ...]
    first_decided: int

    @property
    def carried(self) -> int:
        """How many bars from the first decided one on the rule could not evaluate."""
        return sum(self.unevaluable[self.first_decided :])

    @property
    def first_taken_ms(self) -> int | None:
        """The close at which the rule first takes a side in this history, if it ever does.

        The replay starts flat at the store's first bar, whatever the rule
        held before it. A flat reading before this close may therefore be a
        position opened before the store begins; from it on the replay holds
        what the rule holds.
        """
        return next(
            (
                close_time
                for close_time, side in zip(
                    self.close_times[self.first_decided :],
                    self.sides[self.first_decided :],
                    strict=True,
                )
                if side is not None
            ),
            None,
        )

    def reading_at(self, at_ms: int) -> RuleReading | None:
        """The rule's side at ``at_ms``, or ``None`` where this timeline cannot say.

        Read off the newest bar that had closed by then. Two different
        ``None`` here: the RESULT is ``None`` when there is no reading, while
        a reading whose ``side`` is ``None`` is a rule that was flat.

        No reading in three cases. Before the first decided bar. Where that
        newest bar is more than ``MAX_SIGNAL_AGE_INTERVALS`` intervals old,
        because the store ends before the instant asked about. And where the
        rule could not evaluate that bar, because the side there is one it
        took earlier and could not have left. The last two are the live path's
        own refusals, read off each bar in turn: its reader drops a document
        older than that bound, and :func:`~.signal.build_signal` writes none
        when the newest bar is one the rule could not evaluate.
        """
        index = bisect_right(self.close_times, at_ms) - 1
        if index < self.first_decided or self.unevaluable[index]:
            return None
        close_time = self.close_times[index]
        if at_ms - close_time > max_signal_age_ms(self.interval):
            return None
        return RuleReading(self.sides[index], close_time)

    def describe(self) -> list[str]:
        """The rule and the history its sides were read from, as lines to print."""
        decided = self.sides[self.first_decided :]
        counts = Counter("flat" if side is None else side.value for side in decided)
        shares = ", ".join(
            f"{name} {counts[name] / len(decided):.0%}" for name in ("long", "flat", "short")
        )
        lines = [f"guardrail rule: {self.rule.rule_id}"]
        lines.extend(f"  {line}" for line in describe_spec(self.rule.spec))
        lines.append(
            f"rule history: {len(decided)} {self.coin} {self.interval} bars decided, closing "
            f"{from_epoch_ms(self.close_times[self.first_decided]):%Y-%m-%d %H:%M} to "
            f"{from_epoch_ms(self.close_times[-1]):%Y-%m-%d %H:%M} UTC ({shares})"
        )
        if self.carried:
            lines.append(
                f"  on {self.carried} of those bars the rule could not be evaluated and kept the "
                "side it was on; an instant read off one of them has no reading"
            )
        taken = self.first_taken_ms
        lines.append(
            "  the replay starts flat at the store's first bar; the rule "
            + (
                "never takes a side in this history"
                if taken is None
                else f"first takes a side at the bar closing {from_epoch_ms(taken):%Y-%m-%d %H:%M}"
            )
        )
        return lines


def build_timeline(store: ResearchStore, rule: GuardrailRule, *, coin: str) -> RuleTimeline:
    """``rule``'s side after every :data:`RULE_INTERVAL` bar ``store`` holds for ``coin``.

    The replay starts at the store's first bar, for the reason
    :func:`~.evaluator.replay_sides` gives: a side is path dependent, so a
    later start cannot see a position opened before it. The history is
    scanned first, as a measurement's is, because a missing bar does not
    merely shorten a replay: it can change which side the rule is on.
    """
    # Imported here, like the signal's own: the feature stack costs half a
    # second of pandas, and a caller that only loads a rule should not pay it.
    from .evaluator import EvaluationError, load_bundle, replay_sides
    from .features import FeatureError, FeatureFrame
    from .research import require_clean_history

    interval = RULE_INTERVAL
    try:
        bundle = load_bundle(store, coin=coin, interval=interval)
        require_clean_history(bundle, interval)
        # The cost model sizes a firing rule's notional and nothing else, and
        # a replay tracks sides only, so the defaults are as good as any.
        replayed = replay_sides(
            rule.spec, FeatureFrame(bundle), CostModel(), since_ms=bundle.bars[0].open_time
        )
    except (EvaluationError, FeatureError) as exc:
        raise GuardrailError(
            f"this research store cannot say which side {rule.rule_id} held on {coin}: {exc}"
        ) from exc
    first = next((i for i, missed in enumerate(replayed.unevaluable) if not missed), None)
    if first is None:
        raise GuardrailError(
            f"{rule.rule_id} could not be evaluated on any of the {len(replayed.sides)} "
            f"{coin} {interval} bars in this research store; its features need more history "
            "than the store holds"
        )
    return RuleTimeline(
        rule=rule,
        coin=canonical_coin(coin),
        interval=interval,
        close_times=replayed.close_times,
        sides=replayed.sides,
        unevaluable=replayed.unevaluable,
        first_decided=first,
    )


def build_guardrail_signal(
    store: ResearchStore, rule: GuardrailRule, *, coin: str
) -> tuple[GuardrailSignal, RuleTimeline]:
    """The rule's side at the store's newest bar, as the handoff document the gate reads.

    ``UNKNOWN`` when the rule could not evaluate that bar (the side there is
    one it carried in, not one it took — the same bar :meth:`RuleTimeline.reading_at`
    answers ``None`` for); ``FLAT`` when it holds nothing. The timeline is
    returned beside the document so the command can print where the side
    came from without replaying twice.
    """
    timeline = build_timeline(store, rule, coin=coin)
    last = len(timeline.close_times) - 1
    if timeline.unevaluable[last]:
        side = GuardrailSide.UNKNOWN
    else:
        held = timeline.sides[last]
        side = GuardrailSide.FLAT if held is None else GuardrailSide(held.value)
    signal = GuardrailSignal(
        coin=timeline.coin,
        interval=timeline.interval,
        as_of_ms=timeline.close_times[last],
        rule_id=rule.rule_id,
        side=side,
    )
    return signal, timeline


def describe_guardrail_signal(signal: GuardrailSignal, timeline: RuleTimeline) -> list[str]:
    """The timeline's own lines, then the document's side and the cadence the reader enforces."""
    bound_h = max_signal_age_ms(signal.interval) / 3_600_000
    return [
        *timeline.describe(),
        f"guardrail signal: {signal.side.value} as of the bar closing "
        f"{from_epoch_ms(signal.as_of_ms):%Y-%m-%d %H:%M} UTC ({signal.rule_id})",
        f"the gate drops this document once its bar is more than {MAX_SIGNAL_AGE_INTERVALS} x "
        f"{signal.interval} old ({bound_h:g}h) and then opens nothing new: the bar is the "
        "store's newest, so `fetch` and then this command must both run more often than that",
    ]


def require_fresh(signal: GuardrailSignal, *, now_ms: int) -> None:
    """Refuse a side the gate would drop on arrival; ``GuardrailError`` naming the age.

    ``as_of_ms`` is the STORE's newest bar, not this command's clock: a cron
    that runs this command on time over a store nobody fetches would rewrite
    the same stale bar every time and read as healthy, while the gate had
    long since dropped the document. So the command refuses to write at all
    once the newest bar is past the reader's bound, and the previous document
    stays where it is (stale, which the gate already answers with UNKNOWN).
    """
    age_ms = now_ms - signal.as_of_ms
    bound_ms = max_signal_age_ms(signal.interval)
    if age_ms > bound_ms:
        # Two figures, the way the readers' refusals print them: the age, and
        # how far past the bound it is (which can be small).
        raise GuardrailError(
            f"the research store's newest {signal.coin} {signal.interval} bar closed "
            f"{gap_label(age_ms)} ago, {gap_label(age_ms - bound_ms)} past the "
            f"{MAX_SIGNAL_AGE_INTERVALS} x {signal.interval} bound the gate enforces — run "
            "`fetch` first; the document was not written"
        )
