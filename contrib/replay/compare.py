"""A variant's replayed answers, scored and compared (replay plan §3-10, §3-11, §6).

What ``score --replay-db`` reports, as a pure function of records: the CLI
opens the stores and prints, and everything between is here.

- **One card per repeat.** Each repeat's answers go through the one
  :func:`~.score.score_run`, restricted (``only``) to the questions that
  repeat answered, so a question nobody asked does not read as unanswered.
- **The cutoff (plan §6).** Questions decided on or before the model's
  cutoff day are left out unless the caller includes them. With a second
  variant to compare against, the LATER of the two cutoffs decides: a
  question the other model may have seen the answer to cannot count for
  either side of a paired comparison.
- **The spread across repeats (§3-10)**: the median and the range of the
  model's and the executed hit rate, and of the executed P&L as a mean per
  decision (repeats can have answered different numbers of questions).
- **The paired comparison (§3-11)**: each repeat's model reading against
  the paper trader's own answers, or against the same repeat of another
  variant, on the questions both have a reading for; McNemar's exact p.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from .score import HORIZONS, Answer, Question, Scorecard, Summary, csv_table, paired_hits
from .variant import Variant

__all__ = ["Card", "Comparison", "Table", "compare"]

# One CSV: its header and its rows.
Table = tuple[list[str], list[list[object]]]

# Scores ``answers`` over the questions ``only`` names, against the whole run.
Card = Callable[[Sequence[Answer], Collection[str]], Scorecard]


@dataclass(frozen=True)
class Comparison:
    """The report: ``preamble`` (what is scored, under which cutoff), then ``body``, and the CSV."""

    preamble: list[str]
    body: list[str]
    table: Table


def compare(
    *,
    questions: Sequence[Question],
    card: Card,
    variant: Variant,
    answers: Mapping[int, Sequence[Answer]],
    paper_answers: Sequence[Answer],
    against: Variant | None = None,
    against_answers: Mapping[int, Sequence[Answer]] | None = None,
    include_pre_cutoff: bool = False,
) -> Comparison:
    """Score ``variant``'s repeats and compare them with the paper trader, or with ``against``."""
    compared = [variant] if against is None else [variant, against]
    known = [v for v in compared if v.cutoff_ms is not None]
    owner = max(known, key=lambda v: v.cutoff_ms or 0) if known else None
    cutoff = None if owner is None else owner.cutoff_ms
    post = {q.input_id for q in questions if cutoff is None or q.at_ms >= cutoff}
    eligible = {q.input_id for q in questions} if include_pre_cutoff else post

    preamble = [variant.describe()]
    if against is not None:
        preamble.append(f"against {against.describe()}")
    if owner is None:
        preamble.append("model_cutoff unknown: the questions are not split at the model's cutoff")
    else:
        preamble.append(
            f"model_cutoff {owner.model_cutoff} ({owner.name}): {len(questions) - len(post)} of "
            "the run's questions are decided on or before it; "
            + (
                "they are scored too (--include-pre-cutoff)"
                if include_pre_cutoff
                else "none of them is scored"
            )
        )
        preamble.extend(
            f"model_cutoff unknown for {v.name}: questions it may have seen are not left out"
            for v in compared
            if v.cutoff_ms is None
        )

    def answered(given: Sequence[Answer]) -> Scorecard:
        return card(given, {a.input_id for a in given} & eligible)

    cards = {repeat: answered(given) for repeat, given in sorted(answers.items())}
    summaries = {repeat: scored.summary() for repeat, scored in cards.items()}
    body: list[str] = []
    header: list[str] = []
    rows: list[list[object]] = []
    for repeat, scored in cards.items():
        body.append(f"== repeat {repeat}: {len(scored.rows)} question(s) scored ==")
        body.extend(summaries[repeat].describe(scored))
        columns, lines = csv_table(scored)
        header = ["repeat", *columns]
        rows.extend([repeat, *line] for line in lines)
    body.extend(_spread(cards, summaries))
    if against is None:
        paper = card(paper_answers, eligible)
        body.extend(_paired(cards, lambda _repeat: paper, variant.name, "paper"))
    else:
        assert against_answers is not None
        theirs = {repeat: answered(given) for repeat, given in against_answers.items()}
        body.extend(_paired(cards, theirs.get, variant.name, against.name))
    return Comparison(preamble, body, (header, rows))


def _spread(cards: Mapping[int, Scorecard], summaries: Mapping[int, Summary]) -> list[str]:
    """Per horizon, the median and the range across repeats; a repeat with no figure is left out."""
    lines = [f"-- across {len(cards)} repeat(s) --"]
    any_card = next(iter(cards.values()))
    for index, bars in enumerate(HORIZONS):
        horizons = [summary.horizons[index] for summary in summaries.values()]
        parts = []
        for name, values, form in (
            ("model hit", [h.ai.rate for h in horizons], "{:.1%}"),
            ("executed hit", [h.executed.rate for h in horizons], "{:.1%}"),
            (
                "executed pnl mean",
                [h.executed_pnl.mean if h.executed_pnl.n else None for h in horizons],
                "{:+.3%}",
            ),
        ):
            known = [value for value in values if value is not None]
            if not known:
                parts.append(f"{name} n/a")
                continue
            parts.append(
                f"{name} median {form.format(statistics.median(known))} "
                f"(range {form.format(min(known))} to {form.format(max(known))})"
            )
        lines.append(f"  {any_card.horizon_label(bars)}: " + "; ".join(parts))
    return lines


def _paired(
    cards: Mapping[int, Scorecard],
    other: Callable[[int], Scorecard | None],
    name: str,
    other_name: str,
) -> list[str]:
    """Each repeat's model hits paired with ``other``'s, on the questions both have a reading for."""
    lines = [f"-- paired with {other_name} (model reading; McNemar exact p) --"]
    for repeat, scored in cards.items():
        against = other(repeat)
        if against is None:
            lines.append(f"  repeat {repeat}: {other_name} has no repeat {repeat}")
            continue
        for bars in HORIZONS:
            mine = {row.question.input_id: row.outcomes[bars].ai_hit for row in scored.rows}
            theirs = {row.question.input_id: row.outcomes[bars].ai_hit for row in against.rows}
            common = sorted(set(mine) & set(theirs))
            paired = paired_hits([mine[i] for i in common], [theirs[i] for i in common])
            lines.append(
                f"  repeat {repeat}, {scored.horizon_label(bars)}: n {paired.n}, "
                f"{name} only {paired.a_only}, {other_name} only {paired.b_only}, "
                f"p {paired.p_value:.3f}"
            )
    return lines
