"""The past papers: put each recorded question to a variant, and gate its answer (replay plan PR 2).

One question, one completion. The paper trader's cycle runs the whole
upstream graph (analysts, debates, trader, portfolio manager); the simple
replay skips all of it and asks the variant's model once (plan §3-3):

- the SYSTEM message is the variant's system prompt;
- the HUMAN message is the recorded payload's ``context_text`` and
  ``format_instructions``, assembled by the engine's own
  ``inject_perp_context`` under the same heading, without the instrument
  identity line the engine puts above it (the payload does not keep it).
  The portfolio manager saw that block mid-prompt, before its rating
  scale, the plans and the debate; here it is the whole message, so the
  format block is the last thing the model reads. A variant's
  ``extra_context`` goes after the market context and before the format
  block.

The answer then goes through the same two functions the recorded one did:
``parse_target_decision`` (with the completion's own truncation verdict)
and ``risk_gate.evaluate`` under the run's genesis ``risk:`` and
``decision:`` blocks, from the account state the input row recorded. No
position is carried from one question to the next: each question is asked
from the position the paper trader actually held (plan §3-4).

Two differences from the daemon are known and accepted. The daemon gates
at a fresh mark read moments after the answer, and this replay gates at
the mark the input row recorded, so a target sitting on the deadband's
edge can land on the other side of it. And the gate's position input is
rebuilt from the row's recorded size, margin and leverage rather than from
the books, which the store no longer holds as they were.

The scores are comparable between variants, never with the paper trader's
own (plan §3-3): one completion is not the graph it replaces.

What the model is does not matter here: :data:`Model` is any callable from
``(system, human)`` to a :class:`Completion`. The engine's client lives in
:mod:`.model`, and the tests hand in a fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Final

from .paper_store import InputFacts
from .replay_store import ReplayStore, StoredAnswer
from .score import Question, bar_open_ms, segment_of
from .upstream import (
    DECIMAL_CONTEXT,
    CurrentPositionState,
    DecisionConfig,
    RiskAction,
    RiskConfig,
    RiskGateResult,
    SegmentName,
    Split,
    TargetSide,
    evaluate,
    inject_perp_context,
    parse_target_decision,
    payload_digest,
)
from .variant import Variant

__all__ = [
    "BACKOFF_SECONDS",
    "Completion",
    "Model",
    "Paper",
    "Prepared",
    "ReplayError",
    "ReplayReport",
    "ask_all",
    "human_message",
    "inside",
    "judge",
    "pending",
    "position_state",
    "prepare",
    "select",
]

# Seconds to wait before the second and the third try of a failed model call
# that is retried (see :func:`ask_all` for which are): a call is tried once,
# and once more after each pause.
BACKOFF_SECONDS: Final = (5.0, 20.0)

# HTTP statuses that say the key or the model is wrong: every question will
# fail the same way, so the replay stops at the first, untried again.
_STOP_STATUSES: Final = frozenset({401, 403, 404})
# Client errors that are still worth another try: a timeout, a conflict, a
# rate limit. Every other 4xx is the question's own (its context too long, a
# content filter) and is recorded as unanswered instead (decided 2026-09-24).
_RETRY_CLIENT_STATUSES: Final = frozenset({408, 409, 429})


class ReplayError(Exception):
    """A replay that cannot go on; the sentence names the question and why."""


@dataclass(frozen=True)
class Completion:
    """What one model call returned.

    ``text`` is the response content as the engine normalises it; a
    non-string is kept as it came, and the parse seam files it as
    ``invalid_output``, as it does for the daemon. ``truncated`` is the
    provider's own verdict that the completion hit its token cap: the one
    fact that turns a missing JSON block into ``truncated_output``.
    ``usage_reported`` is false when the usage collector recorded no
    completion for the call (its callback never fired, or the metadata
    could not be read); ``truncated`` is then ``False`` by default, the
    daemon's own reading of a call it has no record of, and the replay
    counts such answers so the gap is visible.
    """

    text: object
    truncated: bool = False
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_reported: bool = True


# ``(system, human) -> Completion``. A call that raises is a failed call, and is retried.
Model = Callable[[str, str], Completion]


@dataclass(frozen=True)
class Paper:
    """One question the replay will ask: the scorecard's record, its segment, its input row."""

    question: Question
    segment: SegmentName
    facts: InputFacts


@dataclass(frozen=True)
class Prepared:
    """A question ready to ask: the two texts and the gate's inputs, all checked."""

    paper: Paper
    context_text: str
    format_instructions: str
    account_equity: Decimal
    current: CurrentPositionState

    @property
    def input_id(self) -> str:
        return self.paper.question.input_id


def inside(
    questions: Iterable[Question], *, split: Split, step_ms: int
) -> tuple[list[Question], int]:
    """``(questions inside the split's span, how many were decided after its end)``.

    A pinned split does not grow with the run: the questions a run gained
    after it was pinned belong to no segment and are not part of the exam.
    A question BEFORE the split's start cannot happen on a run that only
    grows, so it is refused rather than dropped.
    """
    kept: list[Question] = []
    after = 0
    for question in questions:
        opened = bar_open_ms(question.at_ms, step_ms)
        if opened >= split.holdout.end_ms:
            after += 1
        elif opened < split.train.start_ms:
            raise ReplayError(
                f"{question.input_id}: decided before the split pinned for this run begins; "
                "the store's run and the pinned one are not the same run"
            )
        else:
            kept.append(question)
    return kept, after


def select(
    questions: Iterable[Question],
    inputs: Mapping[str, InputFacts],
    *,
    split: Split,
    step_ms: int,
    segments: Collection[SegmentName],
) -> list[Paper]:
    """The questions in ``segments``, in time order. Nothing is read for the others.

    The holdout lock is the caller's choice of ``segments``: a question in a
    segment not asked for is dropped here, before its payload is opened.
    """
    chosen = []
    for question in sorted(questions, key=lambda q: q.at_ms):
        segment = segment_of(split, question.at_ms, step_ms)
        if segment in segments:
            assert segment is not None  # a split was given, so every question has one
            chosen.append(Paper(question, segment, inputs[question.input_id]))
    return chosen


def position_state(facts: InputFacts, risk: RiskConfig) -> tuple[Decimal, CurrentPositionState]:
    """The gate's account inputs, rebuilt from the row the daemon wrote before it asked.

    The daemon gated a paper position at the CONFIGURED leverage (paper
    books carry no other), so a row whose leverage disagrees with the run's
    genesis ``risk.leverage`` is refused: the gate would size at one and the
    row describe the other. The margin is the row's own, not re-derived: the
    audit row wrote it from the imputation the gate uses
    (``CurrentPositionState.from_signed_size``).
    """
    me = facts.input_id
    if facts.account_equity is None:
        raise ReplayError(f"{me}: account_equity is NULL; the gate cannot be run without it")
    if facts.leverage != risk.leverage:
        raise ReplayError(
            f"{me}: configured_leverage {facts.leverage} is not the genesis risk.leverage "
            f"{risk.leverage}; the gate would size this question at a leverage it was not "
            "asked at"
        )
    if facts.side is TargetSide.FLAT:
        if facts.size not in (None, Decimal(0)):
            raise ReplayError(f"{me}: a flat position with size {facts.size}")
        return facts.account_equity, CurrentPositionState.flat()
    if facts.size is None:
        raise ReplayError(f"{me}: a {facts.side.value} position with no recorded size")
    if facts.margin_pct is None:
        # The gate would take an unknown margin as "skip the deadband": a
        # different gate from the one the recorded answer met. The daemon
        # records none only when the account had no equity, a row the
        # scorecard refuses too.
        raise ReplayError(f"{me}: a {facts.side.value} position with no recorded margin")
    with localcontext(DECIMAL_CONTEXT):
        signed = facts.size * facts.mark
    try:
        current = CurrentPositionState(
            side=facts.side,
            signed_notional=signed,
            margin_pct=facts.margin_pct,
            leverage=facts.leverage,
        )
    except ValueError as exc:
        raise ReplayError(
            f"{me}: the recorded position cannot be put back through the gate ({exc})"
        ) from exc
    return facts.account_equity, current


def _read_payload(root: Path, facts: InputFacts) -> tuple[str, str]:
    """``(context_text, format_instructions)`` of the question's payload, checked against its row."""
    me = facts.input_id
    if facts.payload_name is None:
        raise ReplayError(f"{me}: the input row names no payload")
    path = root / facts.payload_name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReplayError(f"{me}: payload {str(path)!r} cannot be read ({exc})") from exc
    digest = payload_digest(raw)
    if facts.payload_hash is not None and digest != facts.payload_hash:
        raise ReplayError(
            f"{me}: payload {str(path)!r} is not the one the input row recorded "
            f"(file {digest}, row {facts.payload_hash})"
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ReplayError(f"{me}: payload {str(path)!r} is not JSON ({exc})") from exc
    texts = []
    for key in ("context_text", "format_instructions"):
        value = document.get(key) if isinstance(document, dict) else None
        if not isinstance(value, str) or not value:
            raise ReplayError(f"{me}: payload {str(path)!r} has no {key} text")
        texts.append(value)
    return texts[0], texts[1]


def prepare(papers: Sequence[Paper], *, payload_root: Path, risk: RiskConfig) -> list[Prepared]:
    """Read and check every question's payload and account state before any call is paid for.

    A payload that is missing, altered since its row was written (the
    digest the row recorded does not match), or not the daemon's shape
    refuses the replay by name, and so does a row the gate cannot be put
    back through. A row that recorded no digest is read unchecked.
    """
    prepared = []
    for paper in papers:
        context_text, format_instructions = _read_payload(payload_root, paper.facts)
        equity, current = position_state(paper.facts, risk)
        prepared.append(Prepared(paper, context_text, format_instructions, equity, current))
    return prepared


def human_message(prepared: Prepared, extra_context: str | None) -> str:
    """The human message: the context the engine was given, headed as the engine headed it."""
    context = prepared.context_text
    if extra_context is not None:
        context = f"{context}\n\n{extra_context}"
    return inject_perp_context("", context, prepared.format_instructions).lstrip("\n")


def judge(
    completion: Completion, prepared: Prepared, *, risk: RiskConfig, decision: DecisionConfig
) -> tuple[str | None, str, RiskGateResult]:
    """``(invalid_reason, raw_response, gate result)``: the parse seam, then the run's gate."""
    parsed = parse_target_decision(completion.text, decision, truncated=completion.truncated)
    gate = evaluate(
        parsed,
        account_equity=prepared.account_equity,
        current=prepared.current,
        risk=risk,
        decision_cfg=decision,
    )
    return parsed.invalid_reason, parsed.raw_response, gate


@dataclass(frozen=True)
class ReplayReport:
    """What one ``replay`` invocation did."""

    asked: int
    already_stored: int
    fail_closed: int
    input_tokens: int
    output_tokens: int
    unreported: int
    refused: int
    stopped_at_limit: bool

    def describe(self) -> list[str]:
        lines = [
            f"asked: {self.asked} new answer(s); already stored, skipped: {self.already_stored}",
            f"fail-closed among the new answers: {self.fail_closed}",
            f"tokens reported: {self.input_tokens} in, {self.output_tokens} out",
        ]
        if self.refused:
            lines.append(
                f"questions the provider refused for their own sake: {self.refused} (recorded as "
                "unanswered; not asked again unless --retry-failed)"
            )
        if self.unreported:
            lines.append(
                f"answers whose call the usage collector recorded nothing for: {self.unreported} "
                "(truncation unknown, read as not truncated, as the daemon reads it)"
            )
        if self.stopped_at_limit:
            lines.append("stopped at --limit; the same command continues from here")
        return lines


def pending(
    prepared: Sequence[Prepared], *, answered: Collection[tuple[str, int]], repeats: int
) -> list[tuple[Prepared, int]]:
    """``(question, repeat)`` still to ask, in asking order: question by question, repeats inside.

    The one plan both the replay and its dry run count from, so what a dry
    run says will be asked is what is asked. ``answered`` is every pair not
    to ask again: the answers stored and the failures recorded.
    """
    return [
        (item, repeat)
        for item in prepared
        for repeat in range(repeats)
        if (item.input_id, repeat) not in answered
    ]


def ask_all(
    prepared: Sequence[Prepared],
    *,
    variant: Variant,
    model: Model,
    store: ReplayStore,
    run_id: str,
    repeats: int,
    risk: RiskConfig,
    decision: DecisionConfig,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> ReplayReport:
    """Ask every prepared question ``repeats`` times, skipping the answers already stored.

    Each answer is written the moment it is judged, in its own transaction,
    so an interruption keeps every answer paid for, and the same command
    resumes where it stopped. What a failed call means depends on what the
    provider said (decided 2026-09-24):

    - 401, 403 or 404 (the key or the model is wrong): the replay stops at
      once, by name; every question would fail the same way.
    - any other 4xx but 408, 409 and 429 (the question's own: its context
      too long, a content filter): the question is recorded as a failure,
      not asked again unless the caller clears the failures, and the replay
      goes on. The scorecard counts it as unanswered.
    - anything else (no status, a timeout, a rate limit, a 5xx): tried again
      after each of :data:`BACKOFF_SECONDS`; after the last try the replay
      stops by name, and the same command resumes from there.

    ``limit`` caps the NEW answers and failures this invocation stores; a
    call tried again counts once.
    """
    sha = variant.sha
    done = set(store.answered(sha, run_id))
    for repeat, input_ids in store.failed(sha, run_id).items():
        done.update((input_id, repeat) for input_id in input_ids)
    todo = pending(prepared, answered=done, repeats=repeats)
    total = len(prepared) * repeats
    skipped = total - len(todo)
    asked = refused = fail_closed = tokens_in = tokens_out = unreported = 0

    def report(stopped: bool) -> ReplayReport:
        return ReplayReport(
            asked=asked,
            already_stored=skipped,
            fail_closed=fail_closed,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            unreported=unreported,
            refused=refused,
            stopped_at_limit=stopped,
        )

    humans: dict[str, str] = {}
    for item, repeat in todo:
        if limit is not None and asked + refused >= limit:
            return report(True)
        human = humans.setdefault(item.input_id, human_message(item, variant.extra_context))
        try:
            completion = _call(
                model,
                variant.system_prompt,
                human,
                item=item,
                repeat=repeat,
                sleep=sleep,
                asked=asked,
            )
        except _QuestionRefused as exc:
            store.record_failure(
                sha, run_id=run_id, input_id=item.input_id, repeat=repeat, error=str(exc), now=now()
            )
            refused += 1
            if progress is not None:
                progress(
                    f"[{skipped + asked + refused}/{total}] {item.input_id} repeat {repeat}: "
                    f"refused ({exc}); recorded as unanswered"
                )
            continue
        invalid_reason, raw, gate = judge(completion, item, risk=risk, decision=decision)
        store.write_answer(
            sha,
            StoredAnswer(
                run_id=run_id,
                input_id=item.input_id,
                repeat=repeat,
                asked_at=now(),
                segment=item.paper.segment.value,
                raw_response=raw,
                truncated=completion.truncated,
                invalid_reason=invalid_reason,
                gate=gate,
                model_reported=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            ),
        )
        asked += 1
        fail_closed += gate.risk_action is RiskAction.INVALID_FAIL_CLOSED
        tokens_in += completion.input_tokens or 0
        tokens_out += completion.output_tokens or 0
        unreported += not completion.usage_reported
        if progress is not None:
            side = "" if gate.target_side is None else f" {gate.target_side.value}"
            progress(
                f"[{skipped + asked + refused}/{total}] {item.input_id} repeat {repeat}: "
                f"{gate.decision_mode.value}{side} -> {gate.risk_action.value}"
            )
    return report(False)


class _QuestionRefused(Exception):
    """The provider refused this question for its own sake; the text says how."""


def _status(exc: BaseException) -> int | None:
    """The HTTP status a provider SDK's exception carries, on itself or its response."""
    for holder in (exc, getattr(exc, "response", None)):
        for attribute in ("status_code", "status", "http_status"):
            value = getattr(holder, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _call(
    model: Model,
    system: str,
    human: str,
    *,
    item: Prepared,
    repeat: int,
    sleep: Callable[[float], None],
    asked: int,
) -> Completion:
    """One model call, sorted by what a failure says (see :func:`ask_all`).

    Raises :class:`ReplayError` to stop the replay, or
    :class:`_QuestionRefused` for a question the caller records and passes.
    """
    for attempt, pause in enumerate((*BACKOFF_SECONDS, None)):
        try:
            completion = model(system, human)
        except Exception as exc:  # noqa: BLE001 - any provider failure is a failed call
            status = _status(exc)
            if status in _STOP_STATUSES:
                raise ReplayError(
                    f"{item.input_id} repeat {repeat}: the provider refused the call with "
                    f"{status} ({type(exc).__name__}: {exc}); that is the key or the model, not "
                    f"the question, so the replay stops here; {asked} new answer(s) were stored "
                    "before it"
                ) from exc
            if status is not None and 400 <= status < 500 and status not in _RETRY_CLIENT_STATUSES:
                raise _QuestionRefused(f"{status} {type(exc).__name__}: {exc}") from exc
            if pause is None:
                raise ReplayError(
                    f"{item.input_id} repeat {repeat}: the model call failed {attempt + 1} time(s), "
                    f"last with {type(exc).__name__}: {exc}; {asked} new answer(s) were stored "
                    "before it, and the same command resumes from here"
                ) from exc
            sleep(pause)
            continue
        if not isinstance(completion, Completion):
            raise ReplayError(f"the model returned a {type(completion).__name__}, not a Completion")
        return completion
    raise AssertionError("unreachable: the last pause is None, and a failure there raises")
