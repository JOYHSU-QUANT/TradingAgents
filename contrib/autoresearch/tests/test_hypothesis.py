"""The hypothesis loop: what the model is shown, what an answer may do, what a round costs.

The store is the same synthetic history ``test_research`` builds — 400 4h bars
on a rising random walk, a daily backdrop, hourly funding — reached through
that module's own builders rather than a second copy of them. One synthetic
market, not two: a fixture that drifted from the one the evaluator tests use
would make a result here incomparable with a result there, which is the same
argument this package makes for borrowing the live path's analytics instead of
re-implementing them. (Plan §12 records moving these builders into ``conftest``
as a deferred refactor; importing them is the version of that which changes no
existing test.)

What is under test is almost entirely about limits rather than about output:
that the prompt carries no calendar instant and no holdout figure, that the
loop reaches the ledger through the search view alone, and that every answer —
refused, duplicate or measured — costs exactly one round of the budget.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from contrib.autoresearch import hypothesis as loop
from contrib.autoresearch.cli import main
from contrib.autoresearch.dsl import SpecError, parse_spec, spec_to_document
from contrib.autoresearch.hypothesis import (
    MAX_RESPONSE_CHARS,
    ChatHypothesist,
    Round,
    build_system_prompt,
    build_user_prompt,
    search as _search,
    strip_fence,
)
from contrib.autoresearch.ledger import (
    Answer,
    Ledger,
    Proposal,
    ProposalOutcome,
    SearchTrial,
    TrialStatus,
)
from contrib.autoresearch.ports import Hypothesist, HypothesistError
from contrib.autoresearch.research import promote
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.vocabulary import FeatureKind, describe_vocabulary

from .test_research import _fill, _open

_SIZING = {"mode": "fixed_margin_fraction", "fraction": 0.5}
_MODEL = "test/fake"


def search(ledger, experiment, hypothesist, **kwargs):
    """``hypothesis.search`` with this suite's model label filled in.

    ``model`` is required on the real function - which model proposed a rule is
    part of what the trial means, so there is no default - and every test here
    would otherwise repeat the same label. Wrapped here rather than defaulted in
    the package, so the production call site still has to say who it asked.
    """
    kwargs.setdefault("model", _MODEL)
    return _search(ledger, experiment, hypothesist, **kwargs)

# An instant in either shape this package prints one in: the ISO form the
# ledger stamps rows with, and the "%Y-%m-%d %H:%M" a Segment renders as.
_A_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Epoch milliseconds — the other way a window edge could reach the prompt.
_EPOCH_MS = re.compile(r"\b\d{13}\b")


def _rule(threshold: float, *, family: str = "breakout") -> str:
    """A legal spec as the text a model would send. Distinct thresholds are distinct rules."""
    return json.dumps(
        {
            "family": family,
            "entry": {"long": [{"left": "close", "op": ">", "right": threshold}]},
            "sizing": _SIZING,
        }
    )


@dataclass
class ScriptedModel:
    """A hypothesist that answers from a list and records what it was asked.

    Written to the PORT's contract — text in, text out — and never to the chat
    client's internals, so nothing here can accidentally test that the loop
    knows what a langchain message looks like.
    """

    answers: list[str]
    asked: list[tuple[str, str]] = field(default_factory=list)

    def propose(self, system: str, user: str) -> str:
        self.asked.append((system, user))
        if not self.answers:
            raise AssertionError("the loop asked for more answers than the test scripted")
        return self.answers.pop(0)


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    path = tmp_path_factory.mktemp("loop-history") / "autoresearch.sqlite"
    with ResearchStore(path) as store:
        _fill(store)
        _open(Ledger(store))
    return path


@pytest.fixture
def opened(history, tmp_path):
    """A private copy of the store, since every test here writes to the ledger."""
    copy = tmp_path / "autoresearch.sqlite"
    shutil.copy(history, copy)
    with ResearchStore(copy) as store:
        yield store


@pytest.fixture
def ledger(opened):
    return Ledger(opened)


@pytest.fixture
def experiment(ledger):
    return ledger.experiment("btc-4h")


# -- the answer -------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('  ```JSON\n{"a": 1}\n```  ', '{"a": 1}'),
        ('```{"a": 1}```', '{"a": 1}'),
    ],
)
def test_a_fence_wrapping_the_whole_answer_is_a_wrapper_and_is_dropped(answer, expected):
    assert strip_fence(answer) == expected


@pytest.mark.parametrize(
    "answer",
    [
        'Here is my rule:\n```json\n{"a": 1}\n```',  # prose before the fence
        '```json\n{"a": 1}',  # never closed
        '```\n{"a": 1}\n```\nand another\n```\n{"b": 2}\n```',  # two blocks
    ],
)
def test_anything_but_a_wrapping_fence_is_left_for_the_parser_to_refuse(answer):
    """The line is drawn at guessing: past this shape there is a CHOICE of text."""
    with pytest.raises(SpecError):
        loop.load_spec(strip_fence(answer))


# -- the prompt -------------------------------------------------------------


def test_the_system_prompt_carries_the_vocabulary_generated_from_the_parsers_table():
    """Carried verbatim, so a feature added to the table reaches the model that same day.

    The listing shows a parameterised kind as ONE row - ret_N plus the
    periods N may take - rather than as its expanded spellings, which is what
    the vocab command prints and what plan §3.11 designates for this
    prompt. Asserting the 38 expanded names would be asserting a different
    listing, and a longer one carrying no more information.
    """
    prompt = build_system_prompt()
    for line in describe_vocabulary():
        assert line in prompt
    for kind in FeatureKind:
        assert kind.value in prompt


def test_the_system_prompt_states_the_refusals_a_model_would_otherwise_discover():
    prompt = build_system_prompt()
    for said in ("stop-loss", "negative offset", "'features' list", "vol_target"):
        assert said in prompt, f"the language description never mentions {said!r}"


def test_the_prompt_names_no_calendar_instant_and_no_epoch_stamp(ledger, experiment):
    """The leak the holdout lock cannot close: a model has its own memory of the market.

    The three windows are contiguous, so naming validation's end would name the
    holdout's start. Bar counts say how much history a rule is judged over
    without saying WHICH history, which is the whole of what a hypothesis needs.
    """
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    text = build_system_prompt() + "\n" + build_user_prompt(ledger, experiment)
    assert _A_DATE.search(text) is None, f"a date reached the prompt: {_A_DATE.search(text)}"
    assert _EPOCH_MS.search(text) is None


def test_the_prompt_gives_the_windows_in_bars(ledger, experiment):
    step = 4 * 60 * 60_000
    train, validation, _holdout = experiment.split.ordered
    prompt = build_user_prompt(ledger, experiment)
    assert f"{(train.end_ms - train.start_ms) // step} training bars" in prompt
    assert f"{(validation.end_ms - validation.start_ms) // step} validation bars" in prompt


def test_a_scored_rule_comes_back_in_the_next_prompt_with_its_validation_figures(
    ledger, experiment
):
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    trial = ledger.trials("btc-4h")[0]
    prompt = build_user_prompt(ledger, experiment)
    assert "RULES ALREADY SCORED" in prompt
    assert f"validation sharpe {trial.validation.net.sharpe:.2f}" in prompt


def test_a_refusal_comes_back_in_the_next_prompt_so_the_round_is_not_respent(ledger, experiment):
    search(ledger, experiment, ScriptedModel(["not json at all"]), max_trials=1)
    prompt = build_user_prompt(ledger, experiment)
    assert "ANSWERS THAT WERE REFUSED" in prompt
    assert "spec is not valid JSON" in prompt


def test_the_prompt_of_a_promoted_trial_still_shows_no_holdout_figure(ledger, experiment):
    """The strongest form of the lock: promote one, then look at what the model is handed."""
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    trial = ledger.trials("btc-4h")[0]
    promoted = promote(ledger, experiment, trial.trial_id)
    assert promoted.status is TrialStatus.PROMOTED and promoted.holdout is not None

    prompt = build_user_prompt(ledger, experiment)
    assert "holdout" not in prompt.lower()
    assert "promoted" not in prompt.lower()
    assert f"{promoted.holdout.net.total_return:+.2%}" not in prompt


# -- what a round costs -----------------------------------------------------


def test_one_round_of_a_scripted_model_files_one_trial_and_one_proposal(ledger, experiment):
    report = search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)

    assert report.spent == 1
    assert report.counts()[ProposalOutcome.MEASURED] == 1
    trials = ledger.trials("btc-4h")
    proposals = ledger.proposals("btc-4h")
    assert len(trials) == 1 and len(proposals) == 1
    assert proposals[0].outcome is ProposalOutcome.MEASURED
    assert proposals[0].trial_id == trials[0].trial_id
    assert proposals[0].spec_hash == trials[0].spec_hash
    # The trial holds the PARSED rule; the proposal holds what was actually sent.
    assert proposals[0].response == _rule(0)
    assert trials[0].status is TrialStatus.MEASURED


def test_a_refused_answer_spends_a_round_and_files_no_trial(ledger, experiment):
    """Plan §3.11: the budget bounds ANSWERS. A loop counting only rules would not end."""
    report = search(ledger, experiment, ScriptedModel(["not json", "{}", "[]"]), max_trials=3)

    assert report.spent == 3
    assert report.counts()[ProposalOutcome.REFUSED] == 3
    assert ledger.trials("btc-4h") == []
    assert ledger.rules_tried("BTC") == 0
    filed = ledger.proposals("btc-4h")
    assert [p.outcome for p in filed] == [ProposalOutcome.REFUSED] * 3
    assert all(p.refusal and p.trial_id is None and p.spec_hash is None for p in filed)


def test_a_rule_already_tried_spends_a_round_and_is_not_a_second_trial(ledger, experiment):
    """Plan §10.7: the same rule again is the same numbers, so it is not another look."""
    report = search(ledger, experiment, ScriptedModel([_rule(0), _rule(0)]), max_trials=2)

    assert report.spent == 2
    counts = report.counts()
    assert counts[ProposalOutcome.MEASURED] == 1 and counts[ProposalOutcome.DUPLICATE] == 1
    assert len(ledger.trials("btc-4h")) == 1
    assert ledger.rules_tried("BTC") == 1
    duplicate = ledger.proposals("btc-4h")[1]
    assert duplicate.outcome is ProposalOutcome.DUPLICATE
    assert duplicate.trial_id == ledger.trials("btc-4h")[0].trial_id


def test_the_budget_is_the_whole_run_whatever_the_answers_were(ledger, experiment):
    model = ScriptedModel([_rule(0), "not json", _rule(0), _rule(1)])
    report = search(ledger, experiment, model, max_trials=4)

    assert report.spent == 4 == len(ledger.proposals("btc-4h"))
    assert report.counts() == {
        ProposalOutcome.MEASURED: 2,
        ProposalOutcome.DUPLICATE: 1,
        ProposalOutcome.REFUSED: 1,
    }
    assert len(ledger.trials("btc-4h")) == 2


def test_an_over_long_answer_is_refused_unread_and_stored_clipped(ledger, experiment):
    report = search(
        ledger, experiment, ScriptedModel(["x" * (MAX_RESPONSE_CHARS + 50)]), max_trials=1
    )

    assert report.counts()[ProposalOutcome.REFUSED] == 1
    stored = ledger.proposals("btc-4h")[0]
    assert len(stored.response) == MAX_RESPONSE_CHARS
    assert "characters" in stored.refusal


@pytest.mark.parametrize("budget", [0, -1, 2.5, True])
def test_a_budget_that_is_not_a_count_of_answers_is_refused(ledger, experiment, budget):
    with pytest.raises(ValueError, match="max-trials"):
        search(ledger, experiment, ScriptedModel([]), max_trials=budget)


def test_a_seam_failure_stops_the_run_and_spends_nothing_for_itself(ledger, experiment):
    """An outage is not an answer: it must not burn budget, and must not lose the run.

    It used to propagate, which threw away the summary of every round that had
    already filed - the rows survived, the operator saw nothing.
    """

    class Broken:
        def __init__(self):
            self.calls = 0

        def propose(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return _rule(0)
            raise HypothesistError("the model did not answer: connection reset")

    model = Broken()
    report = search(ledger, experiment, model, max_trials=5)

    assert model.calls == 2
    assert report.spent == 1
    assert len(ledger.proposals("btc-4h")) == 1
    assert report.stopped == "the model did not answer: connection reset"
    assert "the run stopped early: the model did not answer" in "\n".join(report.describe())


def test_an_interrupt_keeps_the_rounds_that_already_filed(ledger, experiment):
    class Impatient:
        asked = ()

        def __init__(self):
            self.calls = 0

        def propose(self, system, user):
            self.calls += 1
            if self.calls == 1:
                return _rule(0)
            raise KeyboardInterrupt

    report = search(ledger, experiment, Impatient(), max_trials=4)
    assert report.spent == 1 and report.stopped == "interrupted"
    assert len(ledger.trials("btc-4h")) == 1


def test_the_rounds_are_reported_as_they_land(ledger, experiment):
    seen = []
    search(
        ledger,
        experiment,
        ScriptedModel([_rule(0), "not json"]),
        max_trials=2,
        on_round=seen.append,
    )
    assert [completed.number for completed in seen] == [1, 2]
    assert [completed.outcome for completed in seen] == [
        ProposalOutcome.MEASURED,
        ProposalOutcome.REFUSED,
    ]


def test_a_trial_and_the_answer_it_came_from_are_written_in_one_transaction(
    ledger, experiment, monkeypatch
):
    """The finding this fix closes: a trial must not outlive the record of its answer.

    The two were written in separate transactions, so a failure between them
    left a trial counting toward the coin's rule count with nothing saying where
    it came from. Forced here by failing the proposal insert while the trial row
    is already in the open transaction.
    """
    import contrib.autoresearch.ledger as ledger_module

    real = ledger_module.Ledger._write_proposal

    def explode(self, conn, **kwargs):
        if kwargs["outcome"] is ProposalOutcome.MEASURED:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, conn, **kwargs)

    monkeypatch.setattr(ledger_module.Ledger, "_write_proposal", explode)
    with pytest.raises(sqlite3.OperationalError):
        search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)

    # Neither row survives, so no trial raises the bar with nothing explaining it.
    assert ledger.trials("btc-4h") == []
    assert ledger.proposals("btc-4h") == []
    assert ledger.rules_tried("BTC") == 0


def test_every_answer_records_which_model_gave_it(ledger, experiment):
    search(ledger, experiment, ScriptedModel([_rule(0), "not json", _rule(0)]), max_trials=3)
    filed = ledger.proposals("btc-4h")
    assert [p.outcome for p in filed] == [
        ProposalOutcome.MEASURED,
        ProposalOutcome.REFUSED,
        ProposalOutcome.DUPLICATE,
    ]
    assert {p.model for p in filed} == {_MODEL}


def test_the_answer_behind_a_trial_is_findable_from_the_trial(ledger, experiment):
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    trial = ledger.trials("btc-4h")[0]
    found = ledger.proposal_for_trial("btc-4h", trial.trial_id)
    assert found is not None
    assert found.model == _MODEL and found.response == _rule(0)


def test_a_trial_filed_by_hand_has_no_answer_behind_it(ledger, experiment):
    """``evaluate`` files an operator's spec, which no model proposed."""
    from contrib.autoresearch.research import measure

    measure(ledger, experiment, parse_spec(json.loads(_rule(0))))
    trial = ledger.trials("btc-4h")[0]
    assert ledger.proposal_for_trial("btc-4h", trial.trial_id) is None


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_an_answer_must_name_the_model_that_gave_it(bad):
    with pytest.raises(ValueError, match="model"):
        Answer(response="{}", model=bad)


def test_a_proposal_must_name_its_model_too():
    with pytest.raises(ValueError, match="model"):
        Proposal(
            proposal_id=1,
            experiment_id="e",
            outcome=ProposalOutcome.REFUSED,
            response="r",
            model="",
            spec_hash=None,
            trial_id=None,
            refusal="why",
            created_at="t",
        )


def test_every_rule_already_tried_appears_in_the_prompt_even_past_the_detailed_cap(
    ledger, experiment
):
    """Otherwise the budget charges the model for an omission of the prompt's."""
    from contrib.autoresearch.hypothesis import _TRIALS_SHOWN

    wanted = _TRIALS_SHOWN + 3
    search(ledger, experiment, ScriptedModel([_rule(i) for i in range(wanted)]), max_trials=wanted)
    assert len(ledger.trials("btc-4h")) == wanted

    prompt = build_user_prompt(ledger, experiment)
    assert "figures not shown (3)" in prompt
    for trial in ledger.trials("btc-4h"):
        assert json.dumps(spec_to_document(trial.spec), sort_keys=True) in prompt
    # The cap is on FIGURES, not on identity.
    assert prompt.count("-> validation sharpe") == _TRIALS_SHOWN


def test_a_round_cannot_disagree_with_the_answer_it_carries(ledger, experiment):
    """The invariant the docstring states, checked the way every sibling value checks its own."""
    measured = search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1).rounds[0]
    refused = search(ledger, experiment, ScriptedModel(["not json"]), max_trials=1).rounds[0]

    with pytest.raises(ValueError, match="if and only if"):
        Round(number=1, proposal=refused.proposal, measurement=measured.measurement)
    with pytest.raises(ValueError, match="if and only if"):
        Round(number=1, proposal=measured.proposal, measurement=None)


def test_a_search_trial_family_must_agree_with_its_spec(ledger, experiment):
    """The guard ``Trial`` has, on the value a search is actually handed."""
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    view = ledger.search_trials("btc-4h")[0]
    assert isinstance(view, SearchTrial)
    with pytest.raises(ValueError, match="family"):
        dataclasses.replace(view, family="mean_reversion")


# -- the loop's reach -------------------------------------------------------


def test_the_loop_module_never_imports_promote():
    """Plan §12: B1 may use the search view only. Read off the import graph, not the prose.

    A source scan for the word would light up on this module's own docstring,
    which discusses ``promote`` at length; the parsed imports cannot be fooled
    either way.
    """
    tree = ast.parse(Path(loop.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "promote" not in imported
    assert "evaluate_split" not in imported, "the loop measures through research.measure"


def test_the_loop_does_not_promote_even_when_a_rule_would_pass(ledger, experiment, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("the hypothesis loop must never promote a trial")

    monkeypatch.setattr("contrib.autoresearch.research.promote", explode)
    search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    assert all(trial.status is TrialStatus.MEASURED for trial in ledger.trials("btc-4h"))


def test_the_loop_is_handed_the_search_view_of_a_trial(ledger, experiment):
    """``Measurement.trial`` is a SearchTrial, which has no holdout field to leak."""
    report = search(ledger, experiment, ScriptedModel([_rule(0)]), max_trials=1)
    trial = report.rounds[0].measurement.trial
    assert not hasattr(trial, "holdout")
    assert not hasattr(trial, "status")


def test_an_experiment_the_store_does_not_hold_is_refused_before_a_model_is_asked(ledger):
    """The seam is the costly part, so the cheap refusal runs first."""
    invented = dataclasses.replace(ledger.experiment("btc-4h"), experiment_id="never-written")
    model = ScriptedModel([])
    with pytest.raises(Exception, match="never-written"):
        search(ledger, invented, model, max_trials=3)
    assert model.asked == []


# -- the seam ---------------------------------------------------------------


def test_a_scripted_model_satisfies_the_port():
    assert isinstance(ScriptedModel([]), Hypothesist)


def test_the_chat_adapter_turns_any_client_failure_into_a_named_seam_failure():
    class Angry:
        def invoke(self, _messages):
            raise TimeoutError("read timed out")

    with pytest.raises(HypothesistError, match="did not answer"):
        ChatHypothesist(model=Angry(), label="prov/mod").propose("s", "u")


def test_the_chat_adapter_refuses_a_reply_that_is_not_text():
    class Blocks:
        def invoke(self, _messages):
            return type("Reply", (), {"content": [{"type": "text", "text": "hi"}]})()

    with pytest.raises(HypothesistError, match="not text"):
        ChatHypothesist(model=Blocks(), label="prov/mod").propose("s", "u")


def test_the_chat_adapter_hands_back_the_content_it_was_given():
    class Fine:
        def invoke(self, messages):
            assert [role for role, _text in messages] == ["system", "human"]
            return type("Reply", (), {"content": _rule(0)})()

    answered = ChatHypothesist(model=Fine()).propose("s", "u")
    assert answered == _rule(0)
    assert parse_spec(json.loads(answered)) is not None


# -- the command ------------------------------------------------------------


def _serve(monkeypatch, model):
    """Make ``research`` reach a scripted model instead of a provider SDK."""
    monkeypatch.setattr(ChatHypothesist, "build", classmethod(lambda _cls, *a, **k: model))
    return model


def test_the_research_command_files_what_the_model_answered(opened, monkeypatch, capsys):
    path = opened.path
    opened.close()
    _serve(monkeypatch, ScriptedModel([_rule(0), "not json"]))

    code = main(
        ["research", "--experiment", "btc-4h", "--max-trials", "2", "--provider", "p",
         "--model", "m", "--db", str(path)]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "round 1: measured as trial #1" in out
    assert "round 2: refused:" in out
    assert "2 of 2 round(s) spent" in out
    with ResearchStore(path) as reopened:
        assert {p.model for p in Ledger(reopened).proposals("btc-4h")} == {"p/m"}


def test_the_research_command_refuses_to_pick_a_model_for_you(opened, capsys):
    path = opened.path
    opened.close()
    assert main(["research", "--experiment", "btc-4h", "--db", str(path)]) == 1
    assert "--provider and --model" in capsys.readouterr().err


def test_a_dry_run_prints_the_prompt_and_asks_no_model(opened, monkeypatch, capsys):
    path = opened.path
    opened.close()

    def explode(*_args, **_kwargs):
        raise AssertionError("a dry run must not build a chat model")

    monkeypatch.setattr(ChatHypothesist, "build", classmethod(explode))
    assert main(["research", "--experiment", "btc-4h", "--dry-run", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "FEATURES you may refer to" in out
    assert "no model was asked" in out
    with ResearchStore(path) as reopened:
        assert Ledger(reopened).proposals("btc-4h") == []


def test_a_seam_failure_leaves_the_command_on_the_named_exit_one_lane(opened, monkeypatch, capsys):
    path = opened.path
    opened.close()

    class Broken:
        def propose(self, system, user):
            raise HypothesistError("prov/mod did not answer: 401 unauthorized")

    _serve(monkeypatch, Broken())
    code = main(
        ["research", "--experiment", "btc-4h", "--provider", "p", "--model", "m", "--db", str(path)]
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "401 unauthorized" in captured.err
    # The partial report is still printed: a failed run says what it bought.
    assert "0 of 10 round(s) spent" in captured.out


def test_an_interrupted_run_keeps_the_interrupt_exit_code(opened, monkeypatch, capsys):
    """Ctrl-C means the same thing wherever it lands in the command.

    ``search`` catches the interrupt so the filed rounds can still be reported,
    and that must not turn a cancellation into a failure: ``main``'s own handler
    answers 130 when the interrupt lands anywhere else in the same command, so
    this path answers 130 too.
    """
    path = opened.path
    opened.close()

    class Impatient:
        def propose(self, system, user):
            raise KeyboardInterrupt

    _serve(monkeypatch, Impatient())
    code = main(
        ["research", "--experiment", "btc-4h", "--provider", "p", "--model", "m", "--db", str(path)]
    )
    assert code == 130
    captured = capsys.readouterr()
    assert "interrupted" in captured.err
    assert "0 of 10 round(s) spent" in captured.out


def test_report_says_what_a_model_answered_including_what_never_became_a_trial(
    opened, monkeypatch, capsys
):
    path = opened.path
    opened.close()
    _serve(monkeypatch, ScriptedModel([_rule(0), "not json", "not json either"]))
    main(
        ["research", "--experiment", "btc-4h", "--max-trials", "3", "--provider", "p",
         "--model", "m", "--db", str(path)]
    )
    capsys.readouterr()

    assert main(["report", "--experiment", "btc-4h", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "answers from a hypothesis loop: 1 measured, 0 duplicate, 2 refused" in out
    assert "refused: spec is not valid JSON" in out
