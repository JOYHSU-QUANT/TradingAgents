"""The asking walk: one simulated account per repeat, the lower graph asked at every question from its own book (replay plan PR 3b).

The backtest of plan PR 3a walks answers known in advance. A graph variant
has none: its answer at a question depends on the position the account
holds there, which depends on its earlier answers. So the walk goes one
question at a time, in time order, per repeat (:class:`~.account.Walker`):
settle the funding, read the book, render its position section into the
recorded context, run the lower half of the graph, gate the decision under
the run's own gate from that book, apply it to the account, store the
answer, and move on. The holdout is never asked here (plan §3-9: it is a
look, spent elsewhere); :class:`Exam` refuses a holdout question, so the
walk covers the train and validation segments and nothing else.

Every answer is an ordinary ``answers`` row of the replay store, so the
scorecard, the comparison and ``backtest --variant`` read a graph variant's
answers as they read any variant's; beside each one a ``graph_books`` row
keeps the book the question was asked from and what the question cost. A
walk stopped by ``--limit`` or an interruption resumes from the store: the
stored answers are applied to a fresh account in order, each one's book
checked against the row that recorded it (the mark, the position, the
entry, the wallet, the equity, the last fill and the guardrail reading,
all exact: the research store, the tick, the run's terms or the rule
changed otherwise, and the resumed walk would not be the stored one), and
the asking continues from the first question not yet asked. A stored
answer after an unasked question is refused by name: a walk is a prefix.
The account is advanced BEFORE the answer is stored, so a stored answer is
one the account could take; an answer the account refuses (an order the
tick rounds to nothing) is paid for, not stored, and the walk stops there
by name.

The call discipline is the replay's (:func:`~.replay.attempt`): a 401, 403
or 404 stops the walk by name; another client error (bar 408, 409 and 429)
records the question as refused, the account holds its book through it,
and the walk goes on; anything else is retried after each pause, then
stops by name. A question's cost is every graph run it took, the failed
ones included, and is written on its book row. An account whose equity
reaches zero is ruined and takes no further question in that repeat. A
question whose cycle wrote no reports sidecar (the engine failed that
cycle) is never asked: the account holds its book through it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from functools import partial
from pathlib import Path

from .account import AccountError, Decision, Series, Step, Terms, Walker
from .gate import answer_target, approved_target, question_decision
from .lower_graph import (
    LowerGraph,
    LowerGraphError,
    Reports,
    load_reports,
    position_section,
    replace_position_section,
)
from .paper_store import REPORTS_SUFFIX, InputFacts
from .replay import Prepared, QuestionRefused, ReplayError, attempt
from .replay_store import ReplayStore, StoredAnswer, StoredBook
from .score import Answer, Question
from .upstream import (
    CurrentPositionState,
    DecisionConfig,
    GuardrailSide,
    RiskAction,
    RiskConfig,
    RuleTimeline,
    SegmentName,
    evaluate,
    from_epoch_ms,
    parse_target_decision,
    sidecar_path,
)
from .variant import GUARDRAIL_OFF, GUARDRAIL_RECORDED, GUARDRAIL_RULE, Variant

__all__ = [
    "Exam",
    "GuardrailReader",
    "WalkReport",
    "ask_walks",
    "guardrail_reader",
    "load_all_reports",
    "pending_walks",
    "preflight",
]

# ``(question, its input row) -> what the gate reads from the trend guardrail``.
GuardrailReader = Callable[[Question, InputFacts], GuardrailSide | None]


def guardrail_reader(mode: str, timeline: RuleTimeline | None = None) -> GuardrailReader:
    """The guardrail reading for a variant's ``guardrail`` setting (:mod:`.variant`).

    ``recorded`` reads the side the input row recorded (``None`` with the
    guardrail off), ``off`` reads nothing, and ``rule`` reads ``timeline``
    at each question the way the live gate reads the signal document: a
    side is a side, a flat rule is ``flat``, and no reading (before the
    rule's history, a stale or unevaluable bar) is ``unknown``.
    """
    if mode == GUARDRAIL_RECORDED:
        return lambda question, facts: facts.guardrail_side
    if mode == GUARDRAIL_OFF:
        return lambda question, facts: None
    if mode == GUARDRAIL_RULE:
        if timeline is None:
            raise ValueError("the rule guardrail needs the rule's timeline")

        def rule(question: Question, facts: InputFacts) -> GuardrailSide | None:
            reading = timeline.reading_at(question.at_ms)
            if reading is None:
                return GuardrailSide.UNKNOWN
            return GuardrailSide.FLAT if reading.side is None else GuardrailSide(reading.side.value)

        return rule
    raise ValueError(f"no guardrail mode named {mode!r}")


@dataclass(frozen=True)
class Exam:
    """One run's asking material: its questions ready to ask, its reports, its account terms.

    ``reports`` holds the questions that can be asked, by id; a question of
    ``prepared`` absent from it has no reports sidecar and is walked as a
    hold, never asked. The questions are in time order, each id once, none
    of them in the holdout (the lock the module docstring states), and
    every report names one of them.
    """

    run_id: str
    terms: Terms
    series: Series
    risk: RiskConfig
    decision: DecisionConfig
    prepared: Sequence[Prepared]
    reports: Mapping[str, Reports]

    def __post_init__(self) -> None:
        ids = [item.input_id for item in self.prepared]
        if len(set(ids)) != len(ids):
            raise ReplayError(f"run {self.run_id!r}: a question is prepared twice")
        for earlier, later in zip(self.prepared, self.prepared[1:], strict=False):
            if later.paper.question.at_ms <= earlier.paper.question.at_ms:
                raise ReplayError(
                    f"run {self.run_id!r}: {later.input_id} does not follow {earlier.input_id} in "
                    "time; the walk asks in time order"
                )
        holdout = [
            item.input_id for item in self.prepared if item.paper.segment is SegmentName.HOLDOUT
        ]
        if holdout:
            raise ReplayError(
                f"run {self.run_id!r}: {holdout[0]} is a holdout question; the walk never asks the "
                "holdout (plan section 3-9)"
            )
        strangers = set(self.reports) - set(ids)
        if strangers:
            raise ReplayError(
                f"run {self.run_id!r}: reports for {sorted(strangers)[0]!r}, which is not a prepared "
                "question"
            )

    @property
    def coin(self) -> str:
        return self.terms.coin

    @property
    def askable(self) -> int:
        return sum(1 for item in self.prepared if item.input_id in self.reports)


def load_all_reports(prepared: Sequence[Prepared], payload_root: Path) -> dict[str, Reports]:
    """Every question's reports sidecar that exists, read and checked before any call is paid for.

    A cycle whose engine run failed wrote no sidecar (``decision_reports``):
    that question is left out, not refused; the walk holds the account's
    book through it, as through a refused question. A sidecar that exists
    but is not the daemon's shape is refused by name.
    """
    reports: dict[str, Reports] = {}
    for item in prepared:
        name = item.paper.facts.payload_name
        assert name is not None  # ``prepare`` refused a row naming no payload
        payload = payload_root / name
        if sidecar_path(payload, REPORTS_SUFFIX).is_file():
            reports[item.input_id] = load_reports(payload, input_id=item.input_id)
    return reports


def preflight(exam: Exam) -> None:
    """Refuse now what the walk would refuse at a question, before anything is paid for or pinned.

    Every askable question must carry a position section the account's can
    replace, a candle end for that section's as-of, and a funding rate in
    force in the research store. The walk reads the same things again on
    the way; checking them here is what lets a dry run promise what the
    live run will do.
    """
    for item in exam.prepared:
        if item.input_id in exam.reports:
            replace_position_section(
                item.context_text, item.context_shape, (), input_id=item.input_id
            )
            _as_of_ms(item)
            _rate(exam, item)


def _as_of_ms(item: Prepared) -> int:
    """The instant the recorded context was built as of: the row's candle end, as the daemon's.

    The daemon renders the position section (and dates the trade) against
    the newest closed candle, the ``As of:`` line the context prints, not
    against the moment the cycle ran.
    """
    candle_end = item.paper.facts.candle_end_ms
    if candle_end is None:
        raise ReplayError(
            f"{item.input_id}: the input row records no candle_end, so the as-of the position "
            "section is rendered against is unknown; the question cannot be asked"
        )
    return candle_end


def _rate(exam: Exam, item: Prepared) -> Decimal:
    rate = exam.series.rate_at(item.paper.question.at_ms)
    if rate is None:
        raise ReplayError(
            f"{item.input_id}: the research store has no funding settlement at or before "
            f"{from_epoch_ms(item.paper.question.at_ms):%Y-%m-%d %H:%M} UTC, so the holding cost "
            "the position section quotes cannot be priced; fetch the store so that its funding "
            "covers the run"
        )
    return rate


@dataclass(frozen=True)
class WalkReport:
    """What one ``backtest --ask`` invocation did on one run.

    The graph token figures are what the invocation PAID for on this run:
    every graph run, the ones that failed or were refused included;
    ``lost_*`` say how much of that went to those. ``completions`` counts
    the completions of the runs that produced an answer.
    """

    asked: int
    already_stored: int
    refused: int
    fail_closed: int
    input_tokens: int
    output_tokens: int
    graph_input_tokens: int
    graph_output_tokens: int
    completions: int
    unreported: int
    ruined: int
    stopped_at_limit: bool
    funding_missing: int = 0
    lost_input_tokens: int = 0
    lost_output_tokens: int = 0

    def describe(self) -> list[str]:
        lines = [
            f"asked: {self.asked} new answer(s); already stored, skipped: {self.already_stored}",
            f"fail-closed among the new answers: {self.fail_closed}",
            f"tokens reported: portfolio manager {self.input_tokens} in, {self.output_tokens} out; "
            f"whole graph {self.graph_input_tokens} in, {self.graph_output_tokens} out over "
            f"{self.completions} completion(s)",
        ]
        if self.lost_input_tokens or self.lost_output_tokens:
            lines.append(
                f"of which on graph runs that failed or were refused: {self.lost_input_tokens} in, "
                f"{self.lost_output_tokens} out (paid for; counted above and on the question's book)"
            )
        if self.refused:
            lines.append(
                f"questions the provider refused for their own sake: {self.refused} (recorded as "
                "unanswered; the account held its book through them)"
            )
        if self.unreported:
            lines.append(
                f"answers whose portfolio manager call the usage collector recorded nothing for: "
                f"{self.unreported} (truncation unknown, read as not truncated, as the daemon "
                "reads it)"
            )
        if self.funding_missing:
            lines.append(
                f"funding settlements missing from the research store while a position was held: "
                f"{self.funding_missing} (NOT taken as zero; the books the questions were asked "
                "from lack them)"
            )
        if self.ruined:
            lines.append(
                f"repeat(s) ruined on the way: {self.ruined} (equity reached zero; no further "
                "question asked in that repeat)"
            )
        if self.stopped_at_limit:
            lines.append("stopped at --limit; the same command continues from here")
        return lines


@dataclass
class _Tally:
    """The walk's running counts, closed into a :class:`WalkReport`."""

    already_stored: int
    asked: int = 0
    refused: int = 0
    fail_closed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    graph_input_tokens: int = 0
    graph_output_tokens: int = 0
    completions: int = 0
    unreported: int = 0
    ruined: int = 0
    funding_missing: int = 0
    lost_input_tokens: int = 0
    lost_output_tokens: int = 0

    @property
    def done(self) -> int:
        return self.already_stored + self.asked + self.refused

    def report(self, stopped: bool) -> WalkReport:
        return WalkReport(
            asked=self.asked,
            already_stored=self.already_stored,
            refused=self.refused,
            fail_closed=self.fail_closed,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            graph_input_tokens=self.graph_input_tokens,
            graph_output_tokens=self.graph_output_tokens,
            completions=self.completions,
            unreported=self.unreported,
            ruined=self.ruined,
            stopped_at_limit=stopped,
            funding_missing=self.funding_missing,
            lost_input_tokens=self.lost_input_tokens,
            lost_output_tokens=self.lost_output_tokens,
        )


@dataclass
class _State:
    """One repeat's account mid-walk, the index of its first question not yet asked, the stored count."""

    walker: Walker
    start: int
    stored: int


def _hold(item: Prepared) -> Decision:
    """The question taken with no ask: the account keeps what it holds."""
    return question_decision(item.paper.question, item.paper.facts, item.paper.segment, None)


def _book(walker: Walker, facts: InputFacts, side: GuardrailSide | None) -> StoredBook:
    """The book a question is asked from, as the store keeps it (after :meth:`Walker.settle`)."""
    return StoredBook(
        mark=facts.mark,
        size=walker.position.size,
        entry=walker.position.entry_price,
        wallet=walker.wallet,
        equity=walker.equity(facts.mark),
        last_fill_ms=walker.last_fill_ms,
        guardrail_side=side,
    )


def _rebuild(
    exam: Exam,
    answers: Mapping[str, Answer],
    refused: Collection[str],
    books: Mapping[str, StoredBook],
    guardrail: GuardrailReader,
    repeat: int,
) -> _State:
    """Apply one repeat's stored answers to a fresh account, checking each one's book."""
    walker = Walker(exam.terms, exam.series)
    first_unasked: str | None = None
    start = len(exam.prepared)
    stored = 0
    for index, item in enumerate(exam.prepared):
        qid = item.input_id
        asked = qid in answers or qid in refused
        me = f"{qid} repeat {repeat}"
        if qid not in exam.reports:
            if asked:
                raise ReplayError(
                    f"{me}: stored for a question whose cycle wrote no .reports.json; the walk "
                    "could not have asked it, so these answers were not written by this walk"
                )
            if first_unasked is None and not walker.ruined:
                # Up to the first question still to ask, the walk is replayed
                # here, holds included; from there the asking loop walks it.
                walker.advance(_hold(item))
            continue
        if not asked:
            if first_unasked is None:
                first_unasked, start = qid, index
            continue
        stored += 1
        if first_unasked is not None:
            raise ReplayError(
                f"{me}: stored after {first_unasked}, which the walk never asked; a walk asks its "
                "questions in order, so these answers were not written by this walk"
            )
        book = books.get(qid)
        if book is None:
            raise ReplayError(
                f"{me}: an answer with no book recorded beside it; it was not asked by backtest "
                "--ask (a replayed simple answer under the same name?)"
            )
        if walker.ruined:
            raise ReplayError(
                f"{me}: stored after the account was ruined; the stored answers do not rebuild "
                "the walk that wrote them"
            )
        facts = item.paper.facts
        walker.settle(item.paper.question.at_ms)
        found = _book(walker, facts, guardrail(item.paper.question, facts))
        if found.identity != book.identity:
            raise ReplayError(
                f"{me}: the book it was asked from ({book.describe()}) is not the one this command "
                f"rebuilds ({found.describe()}); the research store, the tick, the run's terms or "
                "the guardrail rule changed since it was asked, so the walk cannot resume under "
                "them; restore them or walk the variant under a new name"
            )
        walker.advance(
            question_decision(
                item.paper.question, facts, item.paper.segment, answer_target(answers.get(qid))
            )
        )
    return _State(walker, start, stored)


def _states(
    exam: Exam, sha: str, store: ReplayStore, repeats: int, guardrail: GuardrailReader
) -> dict[int, _State]:
    """Every repeat's account rebuilt from the store, read once."""
    answers = store.answers(sha, exam.run_id)
    failed = store.failed(sha, exam.run_id)
    books = store.books(sha, exam.run_id)
    return {
        repeat: _rebuild(
            exam,
            {a.input_id: a for a in answers.get(repeat, ())},
            failed.get(repeat, ()),
            books.get(repeat, {}),
            guardrail,
            repeat,
        )
        for repeat in range(repeats)
    }


def pending_walks(
    exam: Exam,
    variant: Variant,
    store: ReplayStore | None,
    repeats: int,
    timeline: RuleTimeline | None = None,
) -> tuple[int, int]:
    """``(stored, to ask)`` over the exam's askable questions and ``repeats``: what a dry run reports.

    Counted by rebuilding every repeat's account from the store under the
    variant's own guardrail reading, as the walk does, so a stored answer
    the walk would refuse to resume from is refused here too, by name.
    """
    if store is None:
        return 0, exam.askable * repeats
    reader = guardrail_reader(variant.guardrail, timeline)
    states = _states(exam, variant.sha, store, repeats, reader)
    stored = sum(state.stored for state in states.values())
    # A repeat already ruined asks nothing more, whatever is left of its
    # questions; so does one the next question finds ruined (that ruin is
    # a hold the walk never stores, so only settling to it shows it).
    todo = sum(
        0 if _ruined_ahead(exam, state) else exam.askable - state.stored
        for state in states.values()
    )
    return stored, todo


def _ruined_ahead(exam: Exam, state: _State) -> bool:
    if state.walker.ruined:
        return True
    if state.start >= len(exam.prepared):
        return False
    item = exam.prepared[state.start]
    state.walker.settle(item.paper.question.at_ms)
    return state.walker.equity(item.paper.facts.mark) <= 0


def ask_walks(
    exam: Exam,
    *,
    graph: LowerGraph,
    store: ReplayStore,
    repeats: int,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    timeline: RuleTimeline | None = None,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> WalkReport:
    """Walk ``repeats`` accounts through the exam's questions, asking the graph at each.

    The variant is the graph's own (``graph.variant``): its sha keys every
    row written, and its ``guardrail`` setting (with ``timeline`` for the
    rule) is what the gate reads, so the three cannot disagree. Question by
    question, repeats inside (the replay's asking order), so a ``limit`` of
    twelve on three repeats asks the first four questions of each. Each
    answer is applied to its account, then stored with its book in one
    transaction. ``limit`` caps the NEW answers and refusals stored.
    """
    variant = graph.variant
    sha, run_id = variant.sha, exam.run_id
    guardrail = guardrail_reader(variant.guardrail, timeline)
    states = _states(exam, sha, store, repeats, guardrail)
    tally = _Tally(already_stored=sum(state.stored for state in states.values()))
    total = exam.askable * repeats

    def took(step: Step, label: str | None = None) -> None:
        tally.funding_missing += step.funding_missing
        if step.equity <= 0:
            # A hold that found the account ruined: counted and said, as an
            # asked question's ruin is.
            tally.ruined += 1
            if progress is not None and label is not None:
                progress(f"{label}: equity {step.equity} at the mark; the account is ruined")

    def lost() -> tuple[int, int]:
        return graph.lost_input_tokens, graph.lost_output_tokens

    if progress is not None:
        for repeat, state in states.items():
            if state.walker.ruined:
                progress(
                    f"repeat {repeat}: already ruined in an earlier invocation; nothing more is "
                    "asked of it"
                )
    for index, item in enumerate(exam.prepared):
        question, facts = item.paper.question, item.paper.facts
        qid = item.input_id
        if qid not in exam.reports:
            # No sidecar: nothing to ask; every account still mid-walk holds
            # its book through the question (``_rebuild`` did so before ``start``).
            for repeat, state in states.items():
                if index >= state.start and not state.walker.ruined:
                    took(state.walker.advance(_hold(item)), f"{qid} repeat {repeat} (no reports)")
            continue
        for repeat in range(repeats):
            state = states[repeat]
            walker = state.walker
            if index < state.start or walker.ruined:
                continue
            if limit is not None and tally.asked + tally.refused >= limit:
                return tally.report(True)
            label = f"[{tally.done + 1}/{total}] {qid} repeat {repeat}"
            walker.settle(question.at_ms)
            equity = walker.equity(facts.mark)
            if equity <= 0:
                took(walker.advance(_hold(item)), label)
                continue
            as_of = from_epoch_ms(_as_of_ms(item))
            lines = position_section(
                size=walker.position.size,
                entry=walker.position.entry_price,
                wallet=walker.wallet,
                last_fill_ms=walker.last_fill_ms,
                mark=facts.mark,
                funding_rate=_rate(exam, item),
                coin=exam.coin,
                as_of=as_of,
                risk=exam.risk,
                decision=exam.decision,
                costs=exam.terms.costs,
            )
            context = replace_position_section(
                item.context_text, item.context_shape, lines, input_id=qid
            )
            book = _book(walker, facts, guardrail(question, facts))
            lost_before = lost()
            try:
                completion = attempt(
                    partial(
                        graph.ask,
                        coin=exam.coin,
                        as_of=as_of,
                        context_text=context,
                        format_instructions=item.format_instructions,
                        reports=exam.reports[qid],
                    ),
                    input_id=qid,
                    repeat=repeat,
                    sleep=sleep,
                    asked=tally.asked,
                    unretried=(LowerGraphError,),
                )
            except QuestionRefused as exc:
                lost_in, lost_out = (a - b for a, b in zip(lost(), lost_before, strict=True))
                took(walker.advance(_hold(item)))
                store.record_graph_failure(
                    sha,
                    run_id=run_id,
                    input_id=qid,
                    repeat=repeat,
                    error=str(exc),
                    now=now(),
                    book=replace(book, graph_input_tokens=lost_in, graph_output_tokens=lost_out),
                )
                tally.refused += 1
                tally.graph_input_tokens += lost_in
                tally.graph_output_tokens += lost_out
                tally.lost_input_tokens += lost_in
                tally.lost_output_tokens += lost_out
                if progress is not None:
                    progress(f"{label}: refused ({exc}); recorded as unanswered, the book held")
                continue
            lost_in, lost_out = (a - b for a, b in zip(lost(), lost_before, strict=True))
            stored = False
            try:
                parsed = parse_target_decision(
                    completion.text, exam.decision, truncated=completion.truncated
                )
                gate = evaluate(
                    parsed,
                    account_equity=equity,
                    current=CurrentPositionState.from_signed_size(
                        walker.position.size,
                        mark=facts.mark,
                        equity=equity,
                        leverage=facts.leverage,
                    ),
                    risk=exam.risk,
                    decision_cfg=exam.decision,
                    guardrail=book.guardrail_side,
                )
                # The account first: an answer it cannot take is not stored, so
                # what the store holds always rebuilds (module docstring).
                try:
                    step = walker.advance(
                        question_decision(
                            question,
                            facts,
                            item.paper.segment,
                            approved_target(gate.target_side, gate.approved_target_margin_pct),
                        )
                    )
                except AccountError as exc:
                    raise ReplayError(
                        f"{label}: the account cannot take the answer it was given ({exc}); the "
                        f"answer ({completion.graph_input_tokens} in / "
                        f"{completion.graph_output_tokens} out graph tokens) was paid for and not "
                        "stored, so the next run asks it again; change the terms (--tick) before that"
                    ) from exc
                took(step)
                store.write_graph_answer(
                    sha,
                    StoredAnswer(
                        run_id=run_id,
                        input_id=qid,
                        repeat=repeat,
                        asked_at=now(),
                        segment=item.paper.segment.value,
                        raw_response=parsed.raw_response,
                        truncated=completion.truncated,
                        invalid_reason=parsed.invalid_reason,
                        gate=gate,
                        model_reported=completion.model,
                        input_tokens=completion.input_tokens,
                        output_tokens=completion.output_tokens,
                    ),
                    replace(
                        book,
                        graph_input_tokens=completion.graph_input_tokens + lost_in,
                        graph_output_tokens=completion.graph_output_tokens + lost_out,
                    ),
                )
                stored = True
            finally:
                if not stored:
                    # A run that produced an answer nobody kept: its cost goes
                    # where the stopping line reads it, since no book will.
                    graph.lost_input_tokens += completion.graph_input_tokens
                    graph.lost_output_tokens += completion.graph_output_tokens
            tally.asked += 1
            tally.fail_closed += gate.risk_action is RiskAction.INVALID_FAIL_CLOSED
            tally.input_tokens += completion.input_tokens or 0
            tally.output_tokens += completion.output_tokens or 0
            tally.graph_input_tokens += completion.graph_input_tokens + lost_in
            tally.graph_output_tokens += completion.graph_output_tokens + lost_out
            tally.lost_input_tokens += lost_in
            tally.lost_output_tokens += lost_out
            tally.completions += completion.completions
            tally.unreported += not completion.usage_reported
            if progress is not None:
                shown = "" if gate.target_side is None else f" {gate.target_side.value}"
                progress(
                    f"{label}: {gate.decision_mode.value}{shown} -> {gate.risk_action.value} "
                    f"(book size {walker.position.size:.8f}, equity {equity:.2f}; graph "
                    f"{completion.graph_input_tokens} in / {completion.graph_output_tokens} out)"
                )
    return tally.report(False)
