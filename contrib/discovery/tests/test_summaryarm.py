"""The summary arm's block and its hypothesist (tools plan §3.7, T6): what a model is handed, the line it is read behind, and how it is asked.

Three groups. The WORDING: the paragraph and the list of features are
pinned beside their version, and a field is described in the look-ups'
server's own line. The BLOCK: one line for every feature of the vocabulary
in its order, each the ``feature_stats`` answer as the server writes it —
and nothing after the training window reaches it, held as the look-ups are
held, by rewriting everything later. The HYPOTHESIST: the block goes after
the system prompt, the user prompt is handed on untouched, the adapter's
answer and its failure are the arm's, and a block that is not this build's
cannot be put in front of a model.

The view is the suite's hand-written history under an opened experiment
(``opened_arm``), read by the function the bench reads it with for this
arm. That a line is the text the look-ups' server answers with, over MCP,
is held in ``test_tools_server.py``, which has the client.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import MarketRegime
from contrib.autoresearch.vocabulary import feature_names
from contrib.discovery import summaryarm
from contrib.discovery.bench import _train_view
from contrib.discovery.manifest import WorldKind
from contrib.discovery.research_tools import TrainView, call
from contrib.discovery.summaryarm import (
    AFTER_SYSTEM_PROMPT,
    STATS_TOOL,
    SUMMARY_VERSION,
    SummaryHypothesist,
    figures,
    preamble,
    summary,
    wording,
    wording_digest,
)
from contrib.discovery.tools_server import stats_fields
from contrib.discovery.upstream import ChatHypothesist

from .conftest import COIN, STEP_MS, ScriptedHypothesist, arm_store
from .test_bench import _ScriptedModel
from .test_research_tools import _rewrite

# The digest of what this module decides of what the arm's model reads, at
# SUMMARY_VERSION 1. An edit that moves it — to the paragraph, to a
# sentence of the look-ups' server it borrows, to the vocabulary, to how a
# line is written out — is another wording: raise ``SUMMARY_VERSION`` with
# it, and pin the new digest.
WORDING_DIGEST = "1d398e8ddb8bfe0ef68ca45acb7c2aa8217840e94fddbfcb6ca712c07259d672"

# A ``feature_stats`` answer as the look-ups' server describes one: the
# keys it lists, and its sentences about them.
FIELDS = stats_fields()


@pytest.fixture(scope="module")
def view(opened_arm) -> TrainView:
    return _train_view(*opened_arm)


@pytest.fixture(scope="module")
def block(view) -> str:
    return summary(view)


def _lines(block: str) -> list[str]:
    """The figure lines of a block: what follows the paragraph."""
    opening = preamble() + "\n"
    assert block.startswith(opening)
    return block[len(opening) :].split("\n")


def _retokened(line: str, key: str, token: str) -> str:
    """A figure line with ``token`` written where ``key``'s value is spelled: what ``json.dumps`` would not write."""
    spelled = f'"{key}": ' + json.dumps(json.loads(line)[key])
    assert line.count(spelled) == 1
    return line.replace(spelled, f'"{key}": {token}')


# -- the wording ---------------------------------------------------------------------------------


def test_the_wording_is_pinned_beside_its_version():
    assert SUMMARY_VERSION == 1
    assert wording() == {
        "summary_version": SUMMARY_VERSION,
        "preamble": preamble(),
        "features": list(feature_names()),
        "tool": STATS_TOOL,
        # How an answer is written out as a line, on an answer of no world:
        # the look-ups' server's own spelling of a tool result.
        "sample_line": '{"feature": "a_feature", "bars": 2, "bars_with_value": 1, "min": 0.5}',
        "after_system_prompt": "\n\n",
    }
    assert AFTER_SYSTEM_PROMPT == "\n\n"
    assert wording_digest() == WORDING_DIGEST


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.setattr(summaryarm, "_SAME", "- The figures change every round."),
        lambda m: m.setattr(summaryarm, "feature_names", lambda: feature_names()[:-1]),
        lambda m: m.setattr(summaryarm, "SUMMARY_VERSION", 2),
        lambda m: m.setattr(
            summaryarm, "stats_fields", lambda: FIELDS._replace(rounding="Figures are exact.")
        ),
        lambda m: m.setattr(
            summaryarm, "_line", lambda answer: json.dumps(answer, separators=(",", ":"))
        ),
        lambda m: m.setattr(summaryarm, "AFTER_SYSTEM_PROMPT", "\n"),
    ],
    ids=[
        "a sentence",
        "the features listed",
        "the version",
        "the server's lines",
        "how a line is written",
        "what stands before the block",
    ],
)
def test_the_digest_moves_with_everything_the_version_names(monkeypatch, change):
    change(monkeypatch)
    assert wording_digest() != WORDING_DIGEST


def test_the_paragraph_describes_every_field_of_a_line_in_the_server_s_own_sentences(block):
    said = preamble().split("\n")
    for line in (*FIELDS.numeric, FIELDS.regime_intro, *FIELDS.regime, FIELDS.rounding):
        assert line in said
    # Every key a line of the block carries is a field the paragraph
    # describes, and it describes no other.
    carried = {key for line in _lines(block) for key in json.loads(line)}
    assert carried == set(FIELDS.numeric_keys) | set(FIELDS.regime_keys)
    # (A field's line opens "- name:" with a name that is one word; the
    # paragraph's two other dashed lines open with a sentence.)
    opened = [line[2:].split(":")[0] for line in said if line.startswith("- ")]
    assert {name for name in opened if " " not in name} == carried
    # "the feature asked about" is the tool's sentence, and nobody asked
    # for a line of a summary: this arm has its own for that one field.
    assert "- feature: the feature the line describes." in said
    assert not any("asked about" in line for line in said)


# -- the block -----------------------------------------------------------------------------------


def test_the_block_is_one_line_for_every_feature_of_the_vocabulary_in_its_order(view, block):
    names = list(feature_names())
    lines = _lines(block)
    assert len(lines) == len(names) == len(set(names))
    for name, line in zip(names, lines, strict=True):
        answer = call(view, STATS_TOOL, {"feature": name})
        assert answer["feature"] == name
        # The look-up's answer, written as the look-ups' server writes it.
        assert line == json.dumps(answer, allow_nan=False)
        # Its keys, in order, are the ones the server's description lists —
        # which is what a hand-built block's lines are held to.
        described = FIELDS.regime_keys if name == "regime" else FIELDS.numeric_keys
        assert tuple(json.loads(line)) == described
    assert figures(view) == tuple(lines)
    assert summary(view) == block == "\n".join([preamble(), *figures(view)])
    # Every line is of the training window: its bars are the window's.
    first, stop = view.window
    assert {json.loads(line)["bars"] for line in lines} == {stop - first}


def test_nothing_after_the_training_window_reaches_the_block(
    opened_arm, view, block, history, tmp_path
):
    _path, experiment = opened_arm
    bundle = view.frame.bundle
    last = bundle.bars[-1]
    # Every bar, daily bar and settlement after the last training bar's
    # close, replaced: the block is the same text.
    rewritten = tmp_path / "rewritten.sqlite"
    with arm_store(history, rewritten) as store:
        assert _rewrite(store, since_ms=last.close_time + 1) > 0
    assert summary(_train_view(rewritten, experiment)) == block
    # The controls, a series at a time: the same rewrite reaching just
    # inside training is seen, so the equality above is not a block that
    # never read that series. On one line of the block for each — a
    # feature that reads that series and needs no indicator walk.
    for series, since_ms, feature in (
        ("4h", last.close_time, "close"),
        ("1d", bundle.daily[-1].close_time, "close_1d"),
        ("funding", last.close_time - STEP_MS, "funding_rate"),
    ):
        control = tmp_path / f"control-{series}.sqlite"
        with arm_store(history, control) as store:
            assert _rewrite(store, since_ms=since_ms, series=(series,)) > 0
        asked = {"feature": feature}
        moved = call(_train_view(control, experiment), STATS_TOOL, asked)
        assert moved != call(view, STATS_TOOL, asked), series


def test_a_day_that_straddles_the_end_of_training_does_not_reach_the_block(
    opened_arm, history, tmp_path
):
    # The fixture's training window ends on a UTC midnight, where no daily
    # bar straddles the line. Ended one bar earlier one does: the day that
    # holds the last training bar closes four hours after it, and a
    # feature of the daily series must not have read it.
    path, experiment = opened_arm
    split = experiment.split
    cut = split.train.end_ms - STEP_MS
    earlier = replace(
        experiment,
        split=replace(
            split,
            train=replace(split.train, end_ms=cut),
            validation=replace(split.validation, start_ms=cut),
        ),
    )
    view = _train_view(path, earlier)
    last = view.frame.bundle.bars[-1]
    with ResearchStore(path) as store:
        straddling = [
            day
            for day in store.iter_candles(COIN, "1d")
            if day.open_time <= last.open_time and day.close_time > last.close_time
        ]
    assert len(straddling) == 1
    rewritten = tmp_path / "days.sqlite"
    with arm_store(history, rewritten) as store:
        assert _rewrite(store, since_ms=last.close_time + 1) > 0
    assert summary(_train_view(rewritten, earlier)) == summary(view)


def test_the_block_names_nothing_of_the_world_s_kind_or_of_what_is_planted(block):
    # The paragraph does say "validation": that those bars stay hidden. No
    # line of figures does, and nothing in the block names a kind of world.
    kinds = [kind.value for kind in WorldKind]
    for word in ("planted", "holdout", "conditional", *kinds):
        assert word not in block.lower(), word
    figures_text = "\n".join(_lines(block)).lower()
    for word in ("null", "cond", "validation"):
        assert word not in figures_text, word


def test_a_feature_the_history_cannot_answer_is_not_skipped(view, monkeypatch):
    # A block with a feature missing would be another condition under the
    # same version: the error is left to whoever opens the arm.
    real = summaryarm.call

    def unanswerable(view, tool, arguments):
        if arguments == {"feature": "rsi_14"}:
            raise EvaluationError("rsi_14 has no value at the first training bar")
        return real(view, tool, arguments)

    monkeypatch.setattr(summaryarm, "call", unanswerable)
    with pytest.raises(EvaluationError, match="rsi_14 has no value"):
        summary(view)


# -- the hypothesist -------------------------------------------------------------------------------


def test_the_block_goes_after_the_system_prompt_and_the_rest_is_the_adapter_s(view, block):
    client = _ScriptedModel(['{"a": 1}', "not a rule"])
    hypothesist = SummaryHypothesist.over(ChatHypothesist(model=client, label="openai/gpt"), view)
    assert hypothesist.block == block
    # The label the loop files answers under and the client a counter is
    # read off are the adapter's own.
    assert (hypothesist.label, hypothesist.model) == ("openai/gpt", client)
    assert hypothesist.propose("SYSTEM", "USER one") == '{"a": 1}'
    assert client.prompts[0] == [("system", f"SYSTEM\n\n{block}"), ("human", "USER one")]
    assert client.prompts[0][0][1] == f"SYSTEM{AFTER_SYSTEM_PROMPT}{block}"
    # Text comes back as it is, whatever the parser will make of it; and
    # the next round is handed the same block: nothing is kept or added.
    assert hypothesist.propose("SYSTEM", "USER two") == "not a rule"
    assert client.prompts[1] == [("system", f"SYSTEM\n\n{block}"), ("human", "USER two")]
    # A model that does not answer is the adapter's failed seam.
    with pytest.raises(HypothesistError, match="openai/gpt did not answer"):
        hypothesist.propose("SYSTEM", "USER three")
    assert len(client.prompts) == 3


def test_a_block_that_is_not_this_build_s_cannot_be_put_in_front_of_a_model(block):
    asked = ChatHypothesist(model=_ScriptedModel([]), label="openai/gpt")
    lines = _lines(block)
    count = len(feature_names())
    assert SummaryHypothesist(asked=asked, block=block).block == block
    for wrong, sentence in (
        ("\n".join(lines), "does not open with this build's paragraph"),
        (block.replace("TRAINING-BAR", "TRAINING"), "does not open with this build's paragraph"),
        (preamble(), "does not open with this build's paragraph"),
        (None, "does not open with this build's paragraph"),
        (
            "\n".join([preamble(), *lines[:-1]]),
            rf"lists {count - 1} feature\(s\) and not the vocabulary's {count}",
        ),
        (
            "\n".join([preamble(), lines[1], lines[0], *lines[2:]]),
            rf"lists {count} feature\(s\) and not the vocabulary's {count}, each once and in "
            rf"its order",
        ),
        ("\n".join([preamble(), *lines, lines[-1]]), rf"lists {count + 1} feature\(s\)"),
        (block + "\nand a word of advice", "a line of the block is not a feature's figures"),
        ("\n".join([preamble(), '{"bars": 1}', *lines[1:]]), "is not a feature's figures"),
        ("\n".join([preamble(), "[1]", *lines[1:]]), "is not a feature's figures"),
        # The right feature under a key of its own making, or beside one
        # more: a line carries a look-up's fields and no others.
        (
            "\n".join(
                [preamble(), json.dumps({"feature": "close", "hint": "go long"}), *lines[1:]]
            ),
            "is not a feature's figures",
        ),
        (
            "\n".join([preamble(), json.dumps({**json.loads(lines[0]), "hint": 1}), *lines[1:]]),
            "is not a feature's figures",
        ),
        # A real answer's keys in another order: a line is the look-up's
        # answer as it is written, not a bag of its fields.
        (
            "\n".join(
                [preamble(), json.dumps(dict(reversed(json.loads(lines[0]).items()))), *lines[1:]]
            ),
            "is not a feature's figures",
        ),
        # A token JSON has no number for, where a figure goes.
        (
            "\n".join([preamble(), _retokened(lines[0], "min", "NaN"), *lines[1:]]),
            "is not a feature's figures",
        ),
    ):
        with pytest.raises(ValueError, match=sentence):
            SummaryHypothesist(asked=asked, block=wrong)
    # The shape is the FEATURE's: the regime's answer has its own, and a
    # line in the other's shape says nothing the look-up would of that
    # feature. And under a key goes a value of its kind — words where a
    # figure goes, a count that is no count, a number too large to be one.
    names = list(feature_names())
    regime = names.index("regime")
    assert regime != 0 and FIELDS.regime_keys != FIELDS.numeric_keys

    def with_line(index: int, answer: dict) -> str:
        changed = [*lines]
        changed[index] = json.dumps(answer)
        return "\n".join([preamble(), *changed])

    close = json.loads(lines[0])
    labelled = json.loads(lines[regime])
    for index, answer in (
        (0, {**labelled, "feature": names[0]}),
        (regime, {**close, "feature": "regime"}),
        (0, {**close, "min": "go long"}),
        (0, {**close, "bars": "many"}),
        (0, {**close, "bars_with_value": -1}),
        (0, {**close, "bars": True}),
        (regime, {**labelled, "counts": "mostly trending"}),
        (regime, {**labelled, "counts": {**labelled["counts"], "trending": "often"}}),
        # The labels are the vocabulary's, each once and in its order: no
        # word of a caller's own where a label goes, and none left out.
        (regime, {**labelled, "counts": {**labelled["counts"], "buy everything now": 1}}),
        (regime, {**labelled, "counts": {}}),
        (regime, {**labelled, "counts": dict(reversed(labelled["counts"].items()))}),
        # A whole number too long to be a float is refused like any other
        # line, and not by an error of another kind.
        (0, {**close, "bars": -(10**400)}),
    ):
        with pytest.raises(
            ValueError, match=rf"line for {names[index]} is not that feature's figures"
        ):
            SummaryHypothesist(asked=asked, block=with_line(index, answer))
    too_large = "\n".join([preamble(), _retokened(lines[0], "max", "1e999"), *lines[1:]])
    with pytest.raises(ValueError, match=rf"line for {names[0]} is not that feature's figures"):
        SummaryHypothesist(asked=asked, block=too_large)
    # A figure that is a whole number of any length IS a finite number:
    # judged as one, where asking a float's question of it would raise.
    long_whole = "\n".join([preamble(), _retokened(lines[0], "max", "1" + "0" * 400), *lines[1:]])
    assert SummaryHypothesist(asked=asked, block=long_whole).block == long_whole
    assert labelled["counts"].keys() == {regime.value for regime in MarketRegime}
    # A line is written with no such token in the first place.
    with pytest.raises(ValueError):
        summaryarm._line({"feature": "close", "min": float("nan")})
    # And what it asks is the research package's adapter, nothing that
    # merely answers ``propose``.
    with pytest.raises(TypeError, match="asks a ChatHypothesist, got ScriptedHypothesist"):
        SummaryHypothesist(asked=ScriptedHypothesist([]), block=block)
