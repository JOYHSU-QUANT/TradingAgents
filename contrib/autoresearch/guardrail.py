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

Nothing here reaches the trading path. The one consumer is the replay
package's offline report, which asks what the guardrail WOULD have done to
decisions the paper trader already took.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .costs import CostModel
from .dsl import Side, StrategySpec, describe_spec, load_spec, spec_hash
from .split import SplitError
from .store import ResearchStore, canonical_coin
from .upstream import MAX_SIGNAL_AGE_INTERVALS, from_epoch_ms, interval_to_ms
from .vocabulary import SpecError

__all__ = [
    "DEFAULT_RULE",
    "GuardrailError",
    "GuardrailRule",
    "RuleReading",
    "RuleTimeline",
    "build_timeline",
    "load_rule",
]

# The rule the guardrail was decided on (2026-10-02): enter on a close beyond
# the 120-bar channel, leave on a close beyond the 55-bar one, both sides. On
# 4h bars that is a twenty-day breakout with a nine-day exit.
DEFAULT_RULE: Final = Path(__file__).resolve().parent / "guardrails" / "btc-20d-breakout.json"

# How many leading characters of the spec hash a rule's name carries: enough
# to tell two versions of a file apart in a report, short enough to read.
_HASH_CHARS: Final = 8


class GuardrailError(ValueError):
    """The rule cannot be read, or this store cannot say which side it held.

    A ``ValueError``, like :class:`~.signal.SignalError`, so a CLI's named
    refusal lane prints the sentence and exits 1.
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
    been applied. ``first_decided`` is the index of the first bar whose
    decision the rule could evaluate: before it the features are still
    warming up, and a side there is the replay's starting state, not a
    reading. A bar after it that the rule could not evaluate keeps the side
    it carried in, as :func:`~.signal.build_signal` lets it; ``carried``
    counts those.
    """

    rule: GuardrailRule
    coin: str
    interval: str
    close_times: tuple[int, ...]
    sides: tuple[Side | None, ...]
    first_decided: int
    carried: int

    def reading_at(self, at_ms: int) -> RuleReading | None:
        """The rule's side at ``at_ms``, or ``None`` where this timeline cannot say.

        Read off the newest bar that had closed by then. ``None`` before the
        first decided bar, and where that newest bar is more than
        ``MAX_SIGNAL_AGE_INTERVALS`` intervals old: the store ends before the
        instant asked about, and the bound is the one the live reader of the
        research signal applies to its own document.
        """
        index = bisect_right(self.close_times, at_ms) - 1
        if index < self.first_decided:
            return None
        close_time = self.close_times[index]
        if at_ms - close_time > MAX_SIGNAL_AGE_INTERVALS * interval_to_ms(self.interval):
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
                f"  on {self.carried} of them the rule could not be evaluated and kept the side "
                "it was on"
            )
        return lines


def build_timeline(
    store: ResearchStore, rule: GuardrailRule, *, coin: str, interval: str
) -> RuleTimeline:
    """``rule``'s side after every ``interval`` bar ``store`` holds for ``coin``.

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

    try:
        bundle = load_bundle(store, coin=coin, interval=interval)
        require_clean_history(bundle, interval)
        # The cost model sizes a firing rule's notional and nothing else, and
        # a replay tracks sides only, so the defaults are as good as any.
        replayed = replay_sides(
            rule.spec, FeatureFrame(bundle), CostModel(), since_ms=bundle.bars[0].open_time
        )
    except (EvaluationError, FeatureError, SplitError) as exc:
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
        first_decided=first,
        carried=sum(replayed.unevaluable[first:]),
    )
