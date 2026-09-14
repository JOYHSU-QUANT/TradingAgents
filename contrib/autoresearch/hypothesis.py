"""The hypothesis loop (plan PR B1): ask a model for a rule, score it, show it what failed.

This is the only module that lets something outside the package decide what
gets measured, so almost all of it is about what the model is NOT handed and
what an answer cannot do.

**It never sees the holdout.** Every figure in the prompt comes from
:class:`~.ledger.SearchTrial`, the view with no holdout field on it, and the
loop reaches the ledger through :func:`~.research.measure` alone — never
:func:`~.research.promote`, which is the one function that reads those rows
(plan §12: B1 may use the search view only, and its tests assert it).
Promotion STATUS is withheld too, decided here: a search is asked to propose
rules, and "this one was promoted" is not a fact it needs to do that. It is
also not a fact ``SearchTrial`` carries, so withholding it costs nothing,
while showing it would mean widening the one type whose whole purpose is to
have nothing to leak.

**It is told no calendar dates.** Not one instant appears in the prompt: the
windows are described in BARS. This is the leak the holdout lock cannot close
on its own. A model has its own memory of what BTC did, so a prompt naming the
validation window's dates invites a rule fitted from outside knowledge rather
than from the search — and the multiple-comparison penalty, which charges for
rules TRIED, cannot price that at all. The windows are contiguous, so naming
validation's end would name the holdout's start as well. Bar counts say
everything a hypothesis needs (how much history it is judged over) and nothing
about which history it is.

**An answer is text until the parser says otherwise.** What comes back goes
through :func:`~.dsl.load_spec`, the same parser an operator's hand-written
spec goes through, so a model cannot widen the language by writing
confidently. The one thing stripped first is a markdown code fence, and that
line is drawn deliberately: a fence is a wrapper the chat format adds around
an answer that is otherwise exactly right, while prose around JSON is an
answer that did not follow the instruction, and hunting for an object inside
prose is where an extractor starts guessing which of two objects was meant.

**Every answer spends budget.** ``--max-trials`` bounds ANSWERS, not filed
trials: a refusal costs a round and so does a rule the experiment already
holds (plan §3.11 and §10.7). A budget that only counted accepted rules would
not terminate — a model repeating one malformed answer would loop forever —
and it is the looking that the penalty exists to charge for. The exception is
a seam failure (:class:`~.ports.HypothesistError`), which is not an answer at
all: it spends nothing and stops the run, so a bad key cannot quietly burn a
run's trials and report a search that never happened.

**What was tried is written down.** Every answer becomes a
:class:`~.ledger.Proposal` row, refusals included, so the next run starts
knowing what already failed instead of paying to rediscover it. Refusals are
not trials and never raise the promote threshold — only measured rules do.

Imports the feature stack through :mod:`.research` (pandas, stockstats), so
the CLI imports this module inside the one command that uses it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from .constants import DEFAULT_MAX_TRIALS
from .dsl import SpecError, describe_language, load_spec, spec_hash, spec_to_document
from .ledger import (
    Answer,
    Experiment,
    Ledger,
    LedgerError,
    Proposal,
    ProposalOutcome,
    SearchTrial,
)
from .ports import Hypothesist, HypothesistError
from .research import Measurement, measure
from .upstream import interval_to_ms
from .vocabulary import describe_vocabulary

__all__ = [
    "DEFAULT_MAX_TRIALS",
    "MAX_RESPONSE_CHARS",
    "ChatHypothesist",
    "Round",
    "SearchReport",
    "build_system_prompt",
    "build_user_prompt",
    "search",
    "strip_fence",
]

# How long an answer may be before it is refused unread. A spec is a few
# hundred characters; this is room for a very verbose one and a fence. The
# bound exists because the answer is STORED — the evidence for a refusal is
# the text itself — and an essay, or a model that streams until it is cut off,
# would otherwise be written into the ledger in full.
MAX_RESPONSE_CHARS: Final = 8000

# How much of the past goes into a prompt. Both are caps on PROMPT SIZE rather
# than on what the ledger holds: an experiment with four hundred trials still
# has to produce a prompt a model can read.
_TRIALS_SHOWN: Final = 12
_REFUSALS_SHOWN: Final = 6


@dataclass(frozen=True)
class Round:
    """One answer and what became of it.

    ``measurement`` is ``None`` exactly when the answer was refused: there was
    no rule, so nothing was scored. A duplicate HAS a measurement — the one
    :func:`~.research.measure` answers with, carrying the earlier trial and no
    fresh result.
    """

    number: int
    proposal: Proposal
    measurement: Measurement | None

    def __post_init__(self) -> None:
        # The docstring above states a correlation between two fields, so it is
        # checked rather than merely described - the rule every sibling value in
        # this package follows (``Trial``, ``Proposal``). No production path
        # builds a contradictory Round; a hand-built one could.
        refused = self.proposal.outcome is ProposalOutcome.REFUSED
        if refused != (self.measurement is None):
            raise ValueError(
                f"a round carries a measurement if and only if its answer was not refused; got "
                f"outcome {self.proposal.outcome.value} with measurement "
                f"{'present' if self.measurement is not None else 'absent'}"
            )

    @property
    def outcome(self) -> ProposalOutcome:
        return self.proposal.outcome

    def describe(self) -> str:
        return f"round {self.number}: {self.proposal.describe()}"


@dataclass(frozen=True)
class SearchReport:
    """What one invocation of the loop did."""

    experiment_id: str
    max_trials: int
    rounds: tuple[Round, ...]
    # Why the run ended early, or ``None`` if it spent its whole budget. A seam
    # failure used to propagate out of ``search``, which threw away the summary
    # of every round that HAD filed - the rows survived, but the operator was
    # shown nothing about what the dead run bought (decided 2026-09-14).
    stopped: str | None = None

    @property
    def spent(self) -> int:
        """Rounds used. Equal to the number of answers, whatever became of them."""
        return len(self.rounds)

    def counts(self) -> dict[ProposalOutcome, int]:
        counts = dict.fromkeys(ProposalOutcome, 0)
        for round_ in self.rounds:
            counts[round_.outcome] += 1
        return counts

    def describe(self) -> list[str]:
        counts = self.counts()
        lines = [
            f"{self.spent} of {self.max_trials} round(s) spent on {self.experiment_id}: "
            + ", ".join(f"{count} {outcome.value}" for outcome, count in counts.items())
        ]
        if self.stopped is not None:
            lines.append(f"the run stopped early: {self.stopped}")
        if counts[ProposalOutcome.MEASURED] == 0:
            # Said out loud, because the run "succeeded" and filed nothing: a
            # summary that only counted rounds would let a model answering with
            # prose every time read as a search that explored and found little.
            lines.append(
                "no new rule was filed this run — every answer was refused or already tried; "
                "`report --experiment` shows the refusals the next run will be shown"
            )
        return lines


# -- the answer -------------------------------------------------------------


def strip_fence(text: str) -> str:
    """Drop a markdown code fence WRAPPING the whole answer; leave everything else alone.

    Only a fence that opens the answer and closes it is removed, along with its
    info string (```` ```json ````). Anything else — prose before the fence,
    two fenced blocks, a fence that never closes — is passed through untouched
    and refused by the parser, because at that point there is a choice about
    which text was meant and this is not the layer to be guessing it.
    """
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```") or len(stripped) < 6:
        return stripped
    body = stripped[3:-3]
    head, newline, rest = body.partition("\n")
    if not newline:
        # A single-line ```{...}``` has no info string to drop.
        return body.strip()
    if head.strip() and not head.strip().isalnum():
        # Not an info string — the answer itself began on the fence line, so
        # dropping that line would drop part of the rule.
        return body.strip()
    return rest.strip()


# -- the prompt -------------------------------------------------------------


def build_system_prompt() -> str:
    """The half that is the same every round: the language and the vocabulary.

    Generated from the parser's own tables
    (:func:`~.vocabulary.describe_vocabulary`, :func:`~.dsl.describe_language`)
    rather than written out here, so a feature added to the vocabulary reaches
    the model the day it is added and a model is never asked for a name the
    parser would refuse.
    """
    return "\n".join(
        [
            "You propose timing rules for a single crypto perpetual future, one at a time.",
            "Each rule is scored by a deterministic backtest on history you cannot see.",
            "",
            "FEATURES you may refer to:",
            *describe_vocabulary(),
            "",
            "THE LANGUAGE a rule is written in:",
            *describe_language(),
        ]
    )


def build_user_prompt(ledger: Ledger, experiment: Experiment) -> str:
    """The half that changes: the conditions, what has been tried, and what was refused.

    Carries no calendar instant — see the module docstring for why the windows
    are given in bars.
    """
    step = interval_to_ms(experiment.split.interval)
    train, validation, _holdout = experiment.split.ordered
    tried = ledger.rules_tried(experiment.coin)
    lines = [
        f"Market: {experiment.coin} perpetual, {experiment.split.interval} bars.",
        f"A rule is scored on {(train.end_ms - train.start_ms) // step} training bars and "
        f"{(validation.end_ms - validation.start_ms) // step} validation bars. A third window "
        f"is withheld and is never shown to you.",
        experiment.costs.describe(),
        f"indicator window: {experiment.indicator_lookback} bars",
        experiment.penalty.describe(tried) if tried else "no rule has been tried on this coin yet",
    ]
    trials = _ranked(ledger.search_trials(experiment.experiment_id))
    if trials:
        lines += [
            "",
            f"RULES ALREADY SCORED in this experiment ({len(trials)} of them; the "
            f"{min(len(trials), _TRIALS_SHOWN)} best by validation sharpe shown, and proposing "
            f"one of them again spends a round and scores nothing new):",
        ]
        for trial in trials[:_TRIALS_SHOWN]:
            lines += [json.dumps(spec_to_document(trial.spec), sort_keys=True), _result_line(trial)]
        # Every OTHER rule, without figures. The budget charges a round for
        # proposing a rule already tried, and showing only the best twelve would
        # charge the model for an omission of the prompt's rather than a mistake
        # of its own (decided 2026-09-14). Figures stay capped; identity does not.
        rest = trials[_TRIALS_SHOWN:]
        if rest:
            lines.append(f"Also already scored, figures not shown ({len(rest)}):")
            lines += [json.dumps(spec_to_document(t.spec), sort_keys=True) for t in rest]
    refusals = ledger.refusals(experiment.experiment_id, _REFUSALS_SHOWN)
    if refusals:
        lines += ["", "ANSWERS THAT WERE REFUSED, most recent last — do not repeat these:"]
        lines += [f"- {refusal.refusal}" for refusal in refusals]
    lines += ["", "Propose ONE new rule as a single JSON object, and nothing else."]
    return "\n".join(lines)


def _ranked(trials: Sequence[SearchTrial]) -> list[SearchTrial]:
    """Best validation net sharpe first, ruined runs last whatever their ratios.

    The same ordering ``report`` prints in, and for the same reason: no ratio
    of a ruined run is read, so one must not be able to head a list a model
    reads as "what is working".
    """
    return sorted(trials, key=lambda t: (t.validation.ruined, -t.validation.net.sharpe, t.trial_id))


def _result_line(trial: SearchTrial) -> str:
    """One trial's validation figures — no window named, so no instant."""
    validation = trial.validation
    line = (
        f"  -> validation sharpe {validation.net.sharpe:.2f}, net "
        f"{validation.net.total_return:+.2%} over {validation.trades} trades, "
        f"exposure {validation.exposure:.0%}, turnover {validation.turnover:.1f}x"
    )
    if validation.ruined:
        line += " — RUINED, the account emptied"
    elif validation.trades == 0:
        # Named, because it is the failure a threshold alone does not explain:
        # a rule that never fired scores 0 and reads as merely weak.
        line += " — never fired"
    return line


# -- the loop ---------------------------------------------------------------


def search(
    ledger: Ledger,
    experiment: Experiment,
    hypothesist: Hypothesist,
    *,
    model: str,
    max_trials: int = DEFAULT_MAX_TRIALS,
    on_round: Callable[[Round], None] | None = None,
) -> SearchReport:
    """Spend up to ``max_trials`` answers on ``experiment``, filing every one.

    The prompt is rebuilt each round, so an answer is shown the trial the
    previous one became and the sentence that refused it. ``on_round`` is
    called as each round completes, so a long run reports as it goes rather
    than only at the end.

    A :class:`~.ports.HypothesistError` ends the run without spending a round
    for itself and without becoming a refusal - it is not an answer (see the
    module docstring) - but the report is still RETURNED, naming where it
    stopped, so the rounds that did file are reported rather than thrown away
    with the exception. The caller decides the exit code. An interrupt is
    treated the same way, for the same reason.

    ``EvaluationError`` from the measurement still propagates. It is
    a fact about the STORE — a hole in the history, a window it cannot cover —
    and not about the rule (plan §12): filing it as the rule's refusal would
    teach the model to avoid a legal hypothesis because the data was missing.
    """
    if isinstance(max_trials, bool) or not isinstance(max_trials, int) or max_trials < 1:
        raise ValueError(
            f"--max-trials is a whole number of answers, at least 1, got {max_trials!r}"
        )
    # Refused before the first token is spent: a value that is not the stored
    # row could not file anything it measured, and the seam is the costly part.
    ledger.require_stored(experiment)
    system = build_system_prompt()
    rounds: list[Round] = []
    stopped: str | None = None
    for number in range(1, max_trials + 1):
        try:
            said = hypothesist.propose(system, build_user_prompt(ledger, experiment))
        except HypothesistError as exc:
            stopped = str(exc)
            break
        except KeyboardInterrupt:
            stopped = "interrupted"
            break
        completed = _resolve(ledger, experiment, number, said, model)
        rounds.append(completed)
        if on_round is not None:
            on_round(completed)
    return SearchReport(
        experiment_id=experiment.experiment_id,
        max_trials=max_trials,
        rounds=tuple(rounds),
        stopped=stopped,
    )


def _resolve(
    ledger: Ledger, experiment: Experiment, number: int, answer: object, model: str
) -> Round:
    """Turn one answer into a filed proposal, and a trial if it was a new rule."""
    if not isinstance(answer, str):
        # The port says text; a fake or a client returning something else is a
        # defect on this side, not a refusal to teach a model about.
        raise TypeError(f"a hypothesist answers with text, got {type(answer).__name__}")
    if len(answer) > MAX_RESPONSE_CHARS:
        return _refused(
            ledger,
            experiment,
            number,
            answer[:MAX_RESPONSE_CHARS],
            model,
            f"the answer is {len(answer)} characters; a rule is one JSON object and has to be "
            f"under {MAX_RESPONSE_CHARS}",
        )
    try:
        spec = load_spec(strip_fence(answer))
    except SpecError as exc:
        return _refused(ledger, experiment, number, answer, model, str(exc))
    # A rule already in the experiment writes no trial, so its proposal has
    # nothing to be atomic with and is filed on its own. A NEW rule's answer
    # rides INTO the measurement, so the trial and the row saying where it came
    # from are written in one transaction.
    already = ledger.trial_by_hash(experiment.experiment_id, spec_hash(spec)) is not None
    said = None if already else Answer(response=answer, model=model)
    measurement = measure(ledger, experiment, spec, answer=said)
    if measurement.duplicate:
        proposal = ledger.record_proposal(
            experiment,
            outcome=ProposalOutcome.DUPLICATE,
            response=answer,
            model=model,
            spec_hash=spec_hash(spec),
            trial_id=measurement.trial.trial_id,
        )
    elif measurement.proposal is None:  # pragma: no cover - record_trial had the answer
        raise LedgerError("the trial was filed without the answer it came from")
    else:
        proposal = measurement.proposal
    return Round(number=number, proposal=proposal, measurement=measurement)


def _refused(
    ledger: Ledger, experiment: Experiment, number: int, response: str, model: str, refusal: str
) -> Round:
    proposal = ledger.record_proposal(
        experiment,
        outcome=ProposalOutcome.REFUSED,
        response=response,
        model=model,
        refusal=refusal,
    )
    return Round(number=number, proposal=proposal, measurement=None)


# -- the model behind the seam ----------------------------------------------


@dataclass(frozen=True)
class ChatHypothesist:
    """A :class:`~.ports.Hypothesist` over one of the repo's own chat clients.

    Built through :func:`~.upstream.build_chat_model`, so the provider list,
    the key handling and the per-provider quirks are the ones the rest of the
    repo already has rather than a second set living here.
    """

    model: object
    label: str = "the model"

    @classmethod
    def build(
        cls, provider: str, model: str, base_url: str | None = None, **kwargs
    ) -> ChatHypothesist:
        from .upstream import build_chat_model

        return cls(
            model=build_chat_model(provider, model, base_url, **kwargs),
            label=f"{provider}/{model}",
        )

    def propose(self, system: str, user: str) -> str:
        try:
            reply = self.model.invoke([("system", system), ("human", user)])
        except Exception as exc:  # noqa: BLE001 - see below
            # Deliberately the whole family, and this is the one place in the
            # package that catches it. There is no common base class to name:
            # behind this call sit half a dozen provider SDKs over httpx, and a
            # timeout, a 401, a truncated stream and a rate limit arrive as
            # unrelated types. Every one of them means the same thing here —
            # no answer came back — and letting one escape would end the run
            # with a third-party traceback that reads as a defect in this
            # package. ``BaseException`` is NOT caught, so an interrupt still
            # stops the run.
            raise HypothesistError(f"{self.label} did not answer: {exc}") from exc
        content = getattr(reply, "content", None)
        if not isinstance(content, str):
            # A provider handing back typed blocks rather than text: named as a
            # seam failure rather than fed to the parser, which would refuse it
            # as "not valid JSON" and charge the model for the client's shape.
            raise HypothesistError(f"{self.label} answered with {type(content).__name__}, not text")
        return content
