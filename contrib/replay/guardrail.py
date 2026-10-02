"""What the trend guardrail would have done to a paper run's recorded decisions.

The guardrail (decided 2026-10-02, not built into the trader) lets the book
hold only the side one fixed rule holds: no position against the rule, and
none while the rule is flat. This module reads that policy against decisions
the paper trader already took — the guardrail's shadow mode — so what it
would block can be seen before any of it reaches the trading path.

Per question, three facts:

- the rule's side at the decision instant, from the research package's
  timeline (:class:`~contrib.autoresearch.guardrail.RuleTimeline`);
- the side the paper book HELD once the decision had been applied: the
  approved target when an order was created, and otherwise the position it
  already had — a maintain, a rejection, a fail-closed round, a target
  inside the deadband, and a round with no answer at all leave the book
  where it was;
- the verdict: :data:`PASS` when that side is flat or the rule's own,
  :data:`BLOCK` when an order's target is one the guardrail refuses (the
  book would go flat instead), :data:`CLOSE` when no order was created and
  the position already held is one it refuses, and :data:`UNKNOWN` where
  the timeline cannot say which side the rule held.

Each question is read against the book the paper run actually had. The
guardrail was not there, so a position it would have closed is still held at
the next question and is counted again: the counts are QUESTIONS on which
the book was outside the guardrail, not trades.

Nothing here is scored. No later price is read, so the split's holdout lock
has nothing to guard and every finished question of the run is read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .compare import Table
from .score import Answer, Question
from .upstream import RuleTimeline, TargetSide, from_epoch_ms

__all__ = [
    "BLOCK",
    "CLOSE",
    "PASS",
    "RULE_INTERVAL",
    "UNKNOWN",
    "VERDICTS",
    "GuardrailRow",
    "csv_table",
    "describe",
    "held_after",
    "shadow",
    "verdict_of",
]

# The cadence the guardrail rule is written in: its channels are counted in
# bars, so on another interval the same file would be a different rule.
RULE_INTERVAL: Final = "4h"

PASS: Final = "pass"
BLOCK: Final = "block_to_flat"
CLOSE: Final = "close_position"
UNKNOWN: Final = "rule_unknown"
VERDICTS: Final = (PASS, BLOCK, CLOSE, UNKNOWN)

# How many refused questions the summary lists before pointing at the CSV.
_LISTED: Final = 20


def held_after(question: Question, answer: Answer | None) -> TargetSide:
    """The side the paper book held once this decision had been applied."""
    if answer is None or not answer.order_created:
        return question.current_side
    # An order is only ever created for a sized ``set_target`` (``Answer``'s
    # own invariants); a target of no margin is a flat book whatever its side.
    assert answer.target_side is not None
    return answer.target_side if answer.approved_margin_pct else TargetSide.FLAT


def verdict_of(rule_side: TargetSide | None, held: TargetSide, *, order_created: bool) -> str:
    """The guardrail's verdict on a book holding ``held`` while the rule holds ``rule_side``.

    ``rule_side`` is ``None`` where the rule's side is not known. A flat book
    passes under every rule side, an unknown one included: there is nothing
    for the guardrail to take away.
    """
    if held is TargetSide.FLAT:
        return PASS
    if rule_side is None:
        return UNKNOWN
    if held is rule_side:
        return PASS
    return BLOCK if order_created else CLOSE


@dataclass(frozen=True)
class GuardrailRow:
    """One question under the guardrail: the rule's side, the book's, and the verdict.

    ``rule_side`` and ``rule_decided_ms`` are ``None`` where the timeline
    could not say (the research store starts after the question, or ends
    before it).
    """

    question: Question
    answer: Answer | None
    rule_side: TargetSide | None
    rule_decided_ms: int | None
    held: TargetSide
    verdict: str


def shadow(
    questions: Sequence[Question], answers: Sequence[Answer], timeline: RuleTimeline
) -> list[GuardrailRow]:
    """Every question read under the guardrail, in the order given."""
    by_input = {answer.input_id: answer for answer in answers}
    rows = []
    for question in questions:
        answer = by_input.get(question.input_id)
        reading = timeline.reading_at(question.at_ms)
        rule_side: TargetSide | None = None
        if reading is not None:
            rule_side = TargetSide.FLAT if reading.side is None else TargetSide(reading.side.value)
        held = held_after(question, answer)
        rows.append(
            GuardrailRow(
                question=question,
                answer=answer,
                rule_side=rule_side,
                rule_decided_ms=None if reading is None else reading.close_time,
                held=held,
                verdict=verdict_of(
                    rule_side, held, order_created=answer is not None and answer.order_created
                ),
            )
        )
    return rows


def _what(row: GuardrailRow) -> str:
    """What the paper trader did at this question, in the summary's words."""
    answer = row.answer
    if answer is None:
        return f"no answer, holds {row.held.value}"
    if answer.order_created:
        assert answer.target_side is not None
        return f"set_target {answer.target_side.value} {answer.approved_margin_pct:g}%"
    return f"holds {row.held.value} ({answer.no_order_reason})"


def describe(rows: Sequence[GuardrailRow]) -> list[str]:
    """The run under the guardrail as lines to print: the counts, then each refused question."""
    verdicts = Counter(row.verdict for row in rows)
    sides = Counter("unknown" if row.rule_side is None else row.rule_side.value for row in rows)
    orders = [row for row in rows if row.answer is not None and row.answer.order_created]
    refused = [row for row in rows if row.verdict in (BLOCK, CLOSE)]
    lines = [
        f"questions: {len(rows)} ({sum(row.answer is not None for row in rows)} answered)",
        "rule side at the decisions: "
        + ", ".join(f"{name} {sides[name]}" for name in ("long", "flat", "short", "unknown")),
        "verdicts: " + ", ".join(f"{name} {verdicts[name]}" for name in VERDICTS),
        f"orders created: {len(orders)}, of which the guardrail refuses "
        f"{sum(row.verdict == BLOCK for row in orders)}",
    ]
    if not refused:
        lines.append("the guardrail would have changed nothing in this run")
        return lines
    lines.append(
        f"questions on which the book was outside the guardrail: {len(refused)} (read against "
        "the book the run had; a position it would have closed is counted at every question "
        "it was still held)"
    )
    for row in refused[:_LISTED]:
        assert row.rule_side is not None
        lines.append(
            f"  {from_epoch_ms(row.question.at_ms):%Y-%m-%d %H:%M} rule {row.rule_side.value}; "
            f"{_what(row)} -> {row.verdict}"
        )
    if len(refused) > _LISTED:
        lines.append(f"  ... and {len(refused) - _LISTED} more (the CSV lists every question)")
    return lines


def csv_table(rows: Sequence[GuardrailRow]) -> Table:
    """One row per question."""
    header = [
        "input_id",
        "at",
        "rule_side",
        "rule_decided_at",
        "current_side",
        "decision_mode",
        "target_side",
        "approved_margin_pct",
        "order_created",
        "held_side",
        "verdict",
    ]
    table: list[list[object]] = []
    for row in rows:
        q, a = row.question, row.answer
        table.append(
            [
                q.input_id,
                from_epoch_ms(q.at_ms).isoformat(),
                None if row.rule_side is None else row.rule_side.value,
                None
                if row.rule_decided_ms is None
                else from_epoch_ms(row.rule_decided_ms).isoformat(),
                q.current_side.value,
                None if a is None else a.decision_mode.value,
                None if a is None or a.target_side is None else a.target_side.value,
                None if a is None else a.approved_margin_pct,
                None if a is None else a.order_created,
                row.held.value,
                row.verdict,
            ]
        )
    return header, table
