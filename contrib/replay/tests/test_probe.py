"""The direction probe (plan PR 2.1): asked apart from the decision, parsed, stored and scored.

The fixture is :mod:`.papers`' run (ten questions one bar apart, train
slots 0-5, validation 6-7, holdout 8-9, marks 100 102 101 104 103 105 104
106 108 107). Every expected figure below is worked from those marks by
hand:

- 4h returns (slot -> the next): +2.000%, -0.980%, +2.970%, -0.962%,
  +1.942%, -0.952%, +1.923%; slot 7's next mark is in the holdout, so it
  has none. The flat band is their median absolute value, 1.923%, so the
  classes are up, flat, up, flat, up, flat (train) and up (slot 6: equal
  to the band is not under it).
- 24h returns: slot 0 -> 6 is +4.000%, slot 1 -> 7 is +3.922%; no other
  question has a mark six bars on before the holdout. The band is their
  median, 3.961%: slot 0 is up, slot 1 flat.
- The base rate is therefore up 50% / down 0% / flat 50% at both
  horizons (6 train questions at 4h, 2 at 24h).
- The fake model answers every probe with h4 up .6 / down .1 / flat .3 and
  h24 up .2 / down .2 / flat .6. At 4h an up costs (.6-1)² + .1² + .3² =
  0.26 and a flat .6² + .1² + (.3-1)² = 0.86; the base rate costs 0.5 on
  either.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contrib.replay import cli
from contrib.replay.paper_store import gate_config, load_decisions
from contrib.replay.probe import (
    INVALID_PROBE,
    REFUSED,
    Probe,
    ProbeAnswer,
    ProbeError,
    load_probe,
    parse_probe,
)
from contrib.replay.probe_score import (
    base_rates,
    brier,
    figures,
    fit_temperature,
    log_loss,
    outcome_class,
    reliability,
    tempered,
)
from contrib.replay.replay import Completion, prepare, probe_message, select
from contrib.replay.replay_store import SCHEMA_VERSION, ReplayStore, ReplayStoreError
from contrib.replay.score import build_split, score_run
from contrib.replay.upstream import CostModel, Database, FillRole, SegmentName, payload_dir
from contrib.replay.variant import load_variant

from .papers import (
    FORMAT,
    PAPERS,
    RUN_ID,
    TRAIN,
    VALIDATION,
    context_text,
    input_id,
    replay_argv,
    variant as make_variant,
    write_gate_store,
    write_variant,
)

STEP_MS = 4 * 3_600_000
H4 = {"up": 0.6, "down": 0.1, "flat": 0.3}
H24 = {"up": 0.2, "down": 0.2, "flat": 0.6}
SYSTEM = "You forecast. Say how likely each outcome is."
INSTRUCTIONS = "## Direction forecast\n\nReply with the JSON block."


def forecast_text(h4: dict = H4, h24: dict = H24) -> str:
    return f"My read:\n\n```json\n{json.dumps({'h4': h4, 'h24': h24})}\n```\n"


class Forecaster:
    """A fake model that answers every probe with ``text``, or ``text_for[slot]`` for a slot.

    ``second`` is what it says the second time it is asked the same question
    (repeat 1, since the repeats of a question are asked one after another).
    """

    def __init__(
        self,
        text: str | None = None,
        text_for: dict[int, str] | None = None,
        *,
        second: str | None = None,
        usage_reported: bool = True,
    ) -> None:
        self.text = forecast_text() if text is None else text
        self.text_for = text_for or {}
        self.second = second
        self.usage_reported = usage_reported
        self.calls: list[tuple[str, str]] = []
        self.asked: dict[int, int] = {}

    def __call__(self, system: str, human: str) -> Completion:
        self.calls.append((system, human))
        for paper in PAPERS:
            if context_text(paper.slot) in human:
                self.asked[paper.slot] = self.asked.get(paper.slot, 0) + 1
                text = self.text_for.get(paper.slot, self.text)
                if self.second is not None and self.asked[paper.slot] == 2:
                    text = self.second
                return Completion(
                    text=text,
                    model="forecaster-1",
                    input_tokens=50,
                    output_tokens=20,
                    usage_reported=self.usage_reported,
                )
        raise AssertionError(f"no fixture question in {human!r}")


def write_probe(
    directory: Path, name: str = "direction-t", *, instructions: str = INSTRUCTIONS
) -> Path:
    path = directory / f"{name}.yaml"
    body = "".join(f"  {line}\n" if line else "\n" for line in instructions.splitlines())
    path.write_text(
        f"name: {name}\nsystem: |\n  {SYSTEM}\ninstructions: |\n{body}", encoding="utf-8"
    )
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db")


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    return write_variant(tmp_path, "echo"), write_probe(tmp_path)


def _use(monkeypatch, model) -> None:
    monkeypatch.setattr(cli, "_build_model", lambda _variant: model)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)


def _probe(store: Path, files: tuple[Path, Path], *extra: str) -> list[str]:
    variant_file, probe_file = files
    return replay_argv(store, variant_file, "--probe", str(probe_file), "--repeats", "1", *extra)


def _score(store: Path, *extra: str) -> list[str]:
    replay_db = str(store.parent / "replay.sqlite")
    return [
        "score",
        "--db",
        str(store),
        "--run-id",
        RUN_ID,
        "--replay-db",
        replay_db,
        "--variant",
        "echo",
        *extra,
    ]


def _rows(store: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM probe_answers ORDER BY input_id, repeat").fetchall()
    finally:
        conn.close()


def _now() -> datetime:
    return datetime(2027, 2, 1, tzinfo=timezone.utc)


# -- the answer ---------------------------------------------------------------------


def test_a_forecast_is_read_from_the_fenced_block_and_normalised():
    text = forecast_text(
        {"up": 0.5, "down": 0.2, "flat": 0.31}, {"up": 0.3, "down": 0.3, "flat": 0.4}
    )
    # A fenced block is preferred over a bare object after it.
    reading = parse_probe('An example: {"h4": 1}\n' + text + '\n{"note": "none"}', truncated=False)
    assert reading.invalid_detail is None
    assert reading.forecast is not None
    assert reading.forecast["h4"] == pytest.approx(
        {"up": 0.5 / 1.01, "down": 0.2 / 1.01, "flat": 0.31 / 1.01}
    )
    assert reading.forecast["h24"] == {"up": 0.3, "down": 0.3, "flat": 0.4}


def test_other_top_level_keys_are_ignored():
    triple = {"up": 0.2, "down": 0.3, "flat": 0.5}
    text = json.dumps({"h4": triple, "h24": triple, "note": "why"})
    assert parse_probe(text, truncated=False).forecast == {"h4": triple, "h24": triple}


@pytest.mark.parametrize(
    ("h4", "detail"),
    [
        ({"up": 0.5, "down": 0.2, "flat": 0.33}, "h4 sums to 1.0300, more than 0.02 away from 1"),
        ({"up": 0.5, "down": 0.5}, "h4 must hold exactly up, down, flat, got down, up"),
        ({"up": 0.5, "down": 0.5, "flat": 0, "sideways": 0}, "h4 must hold exactly"),
        ({"up": True, "down": 0, "flat": 0}, "h4.up must be a number in [0, 1], got True"),
        ({"up": "0.5", "down": 0.2, "flat": 0.3}, "h4.up must be a number in [0, 1], got '0.5'"),
        ({"up": 1.2, "down": -0.1, "flat": -0.1}, "h4.up must be a number in [0, 1], got 1.2"),
        ({"up": 50, "down": 20, "flat": 30}, "h4.up must be a number in [0, 1], got 50"),
        ({"up": 0, "down": 0, "flat": 0}, "h4 sums to 0.0000"),
    ],
)
def test_an_answer_that_is_not_a_forecast_is_invalid(h4, detail):
    reading = parse_probe(forecast_text(h4), truncated=False)
    assert reading.forecast is None
    assert reading.invalid_detail is not None
    assert reading.invalid_detail.startswith(detail)


def test_a_sum_just_inside_the_tolerance_is_taken():
    h4 = {"up": 0.5, "down": 0.2, "flat": 0.32}
    assert parse_probe(forecast_text(h4), truncated=False).forecast is not None


def test_no_forecast_says_so_and_a_cut_off_completion_says_that_too():
    assert parse_probe("I think it goes up.", truncated=False).invalid_detail == (
        "no JSON object in the response"
    )
    assert parse_probe('{"h4": {"up": 0.', truncated=True).invalid_detail == (
        "no JSON object in the response (the completion was cut off at its token cap)"
    )
    assert parse_probe(["up"], truncated=False).invalid_detail == "the response is a list, not text"
    assert parse_probe('{"h24": {}}', truncated=False).invalid_detail == (
        "no h4 forecast (an object of up, down, flat)"
    )


# -- the probe file --------------------------------------------------------------------


def test_a_probe_is_its_two_texts(tmp_path):
    probe = load_probe(write_probe(tmp_path))
    assert (probe.name, probe.system, probe.instructions) == (
        "direction-t",
        SYSTEM + "\n",
        INSTRUCTIONS + "\n",
    )
    assert replace(probe, name="other").sha == probe.sha
    assert replace(probe, instructions="Say it.").sha != probe.sha
    assert replace(probe, system="Forecast.").sha != probe.sha


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("name: p\nsystem: s\ninstructions: i\nmodel: x\n", r"unknown probe key\(s\) \['model'\]"),
        ("name: p\nsystem: s\n", r"lacks \['instructions'\]"),
        ("name: p\nsystem: ''\ninstructions: i\n", "system must be a non-empty string"),
        ("- a list\n", "must hold a mapping"),
    ],
)
def test_a_probe_file_is_checked(tmp_path, text, match):
    path = tmp_path / "p.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProbeError, match=match):
        load_probe(path)


def test_the_shipped_probe_loads_and_the_shape_it_shows_parses():
    shipped = Path(__file__).resolve().parents[1] / "probes" / "direction-v1.yaml"
    probe = load_probe(shipped)
    assert probe.name == "direction-v1"
    assert parse_probe(probe.instructions, truncated=False).forecast is not None


# -- the message ------------------------------------------------------------------------


def test_the_probe_is_sent_the_context_and_its_instructions_last_and_no_format_block(
    store, files, monkeypatch
):
    model = Forecaster()
    _use(monkeypatch, model)
    assert cli.main(_probe(store, files, "--limit", "1")) == 0
    [(system, human)] = model.calls
    assert system == SYSTEM + "\n"
    assert human == f"## Perpetual market context\n{context_text(TRAIN[0])}\n\n{INSTRUCTIONS}\n"
    assert FORMAT not in human


def test_a_variants_extra_context_goes_between_the_context_and_the_instructions(store):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
        risk, _ = gate_config(db, RUN_ID)
    split = build_split(decisions.questions, interval="4h", step_ms=STEP_MS)
    chosen = select(
        decisions.questions,
        decisions.inputs,
        split=split,
        step_ms=STEP_MS,
        segments={SegmentName.TRAIN},
    )
    [first, *_] = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)
    assert probe_message(first, Probe("p", "s", "the probe"), "a lesson") == (
        f"## Perpetual market context\n{context_text(TRAIN[0])}\n\na lesson\n\nthe probe"
    )


# -- asking ------------------------------------------------------------------------------


def test_every_train_question_is_probed_once_and_stored(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "probed: 6 new answer(s); already stored, skipped: 0" in out
    assert "invalid_probe among the new answers: 0 (counted, not scored, not asked again)" in out
    assert "tokens reported: 300 in, 120 out" in out
    rows = _rows(store)
    assert [r["input_id"] for r in rows] == [input_id(slot) for slot in TRAIN]
    assert {r["segment"] for r in rows} == {"train"}
    assert rows[0]["forecast_json"] == (
        '{"h24":{"down":0.2,"flat":0.6,"up":0.2},"h4":{"down":0.1,"flat":0.3,"up":0.6}}'
    )
    assert (rows[0]["invalid_reason"], rows[0]["model_reported"]) == (None, "forecaster-1")
    # The decision answers are untouched: a probe is not a decision.
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM answers").fetchone() == (0,)
    finally:
        conn.close()


def test_an_invalid_answer_is_stored_counted_and_not_asked_again(store, files, monkeypatch, capsys):
    model = Forecaster(text_for={TRAIN[2]: "Up, probably."})
    _use(monkeypatch, model)
    assert cli.main(_probe(store, files)) == 0
    assert "invalid_probe among the new answers: 1 (counted, not scored, not asked again)" in (
        capsys.readouterr().out.splitlines()
    )
    invalid = [r for r in _rows(store) if r["invalid_reason"] == INVALID_PROBE]
    assert [(r["input_id"], r["invalid_detail"], r["raw_response"]) for r in invalid] == [
        (input_id(TRAIN[2]), "no JSON object in the response", "Up, probably.")
    ]
    calls = len(model.calls)
    assert cli.main(_probe(store, files)) == 0
    assert len(model.calls) == calls
    assert "probed: 0 new answer(s); already stored, skipped: 6" in capsys.readouterr().out
    # An invalid answer is an answer, not a refusal: --retry-failed does not ask it again.
    assert cli.main(_probe(store, files, "--dry-run", "--retry-failed")) == 0
    dry = capsys.readouterr().out.splitlines()
    assert (
        "dry run: 6 answer(s) already stored, 0 to ask; this command would ask for 0 of them" in dry
    )
    assert cli.main(_probe(store, files, "--retry-failed")) == 0
    assert len(model.calls) == calls


class _ProviderError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"provider says {status}")
        self.status_code = status


class _Refuses(Forecaster):
    """A forecaster whose provider answers 400 for one slot while ``refusing`` is set."""

    def __init__(self, slot: int) -> None:
        super().__init__()
        self.slot = slot
        self.refusing = True

    def __call__(self, system: str, human: str) -> Completion:
        if self.refusing and context_text(self.slot) in human:
            self.calls.append((system, human))
            raise _ProviderError(400)
        return super().__call__(system, human)


def test_a_refusal_is_recorded_and_asked_again_only_with_retry_failed(
    store, files, monkeypatch, capsys
):
    model = _Refuses(TRAIN[1])
    _use(monkeypatch, model)
    assert cli.main(_probe(store, files)) == 0
    assert (
        "questions the provider refused for their own sake: 1 (recorded; not asked again unless "
        "--retry-failed)"
    ) in capsys.readouterr().out.splitlines()
    refused = [r for r in _rows(store) if r["invalid_reason"] == REFUSED]
    assert [(r["input_id"], r["raw_response"]) for r in refused] == [(input_id(TRAIN[1]), None)]
    assert refused[0]["invalid_detail"] == "400 _ProviderError: provider says 400"
    calls = len(model.calls)
    assert cli.main(_probe(store, files)) == 0
    assert len(model.calls) == calls
    capsys.readouterr()
    assert cli.main(_probe(store, files, "--dry-run")) == 0
    dry = capsys.readouterr().out.splitlines()
    assert "refused earlier and not asked again: 1 (--retry-failed asks them again)" in dry
    model.refusing = False
    assert cli.main(_probe(store, files, "--retry-failed")) == 0
    assert "note: 1 refused question(s) will be asked again" in capsys.readouterr().err
    assert [r["invalid_reason"] for r in _rows(store)] == [None] * len(TRAIN)


def test_a_dry_run_counts_the_probe_answers_not_the_decisions(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--limit", "2")) == 0
    capsys.readouterr()
    assert cli.main(_probe(store, files, "--dry-run")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2].startswith("probe direction-t (")
    assert out[2].endswith("): asked instead of a decision, with the variant's model; no gate")
    assert (
        "dry run: 2 answer(s) already stored, 4 to ask; this command would ask for 4 of them"
    ) in out


def test_a_probe_is_refused_on_a_run_not_traded_on_4h_candles(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    monkeypatch.setattr(cli, "PROBE_STEP_MS", 24 * 3_600_000)
    assert cli.main(_probe(store, files)) == 1
    assert "the direction probe asks for 4h and 24h" in capsys.readouterr().err
    assert not (store.parent / "replay.sqlite").exists()


def test_one_probe_name_is_one_text(store, files, monkeypatch, capsys, tmp_path):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--limit", "1")) == 0
    changed = tmp_path / "changed"
    changed.mkdir()
    reworded = write_probe(changed, instructions="Reply with the JSON block, please.")
    assert cli.main(_probe(store, (files[0], reworded))) == 1
    assert "probe name 'direction-t' already stands for" in capsys.readouterr().err


# -- the store ------------------------------------------------------------------------------


def test_a_v2_store_is_read_as_it_is_and_upgraded_by_a_writer(tmp_path):
    path = tmp_path / "replay.sqlite"
    with ReplayStore(path, create=True) as store:
        store.register(make_variant(), now=_now())
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE probe_answers")
    conn.execute("DROP TABLE probes")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    # A reader leaves it as it is, and finds no probe answer in it.
    with ReplayStore(path) as store:
        assert store.variant("v") == make_variant()
        assert store.probe_answers(make_variant().sha, "run") == []
        assert store.probe_done(make_variant().sha, "sha256:x", "run") == (set(), set())
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
    finally:
        conn.close()
    # A command that may create a store brings it to v3.
    with ReplayStore(path, create=True) as store:
        assert store.variant("v") == make_variant()
        assert store.probe_answers(make_variant().sha, "run") == []
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,) == (3,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"probes", "probe_answers"} <= tables
    finally:
        conn.close()


def test_an_edited_probe_row_is_refused(store, files, monkeypatch):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--limit", "1")) == 0
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    conn.execute("UPDATE probes SET instructions = 'something else'")
    conn.commit()
    conn.close()
    with (
        ReplayStore(store.parent / "replay.sqlite") as replay,
        pytest.raises(ReplayStoreError, match="the row was changed after it was stored"),
    ):
        replay.probe_answers(load_variant(files[0]).sha, RUN_ID)


def test_a_stored_answer_has_exactly_one_of_a_forecast_and_a_reason():
    with pytest.raises(ProbeError, match="exactly one of forecast / invalid_reason"):
        ProbeAnswer("q", None, None)
    with pytest.raises(ProbeError, match="exactly one of forecast / invalid_reason"):
        ProbeAnswer("q", {"h4": H4, "h24": H24}, INVALID_PROBE)
    with pytest.raises(ProbeError, match="unknown invalid_reason"):
        ProbeAnswer("q", None, "shrug")


# -- the scores ------------------------------------------------------------------------------


def test_the_outcome_classes():
    assert outcome_class(0.02, 0.019231) == "up"
    assert outcome_class(-0.019231, 0.019231) == "down"  # equal to the band is not under it
    assert outcome_class(0.0099, 0.019231) == "flat"
    assert outcome_class(0.0, 0.0) == "flat"
    assert outcome_class(None, 0.01) is None
    assert outcome_class(0.02, None) is None


def test_brier_and_log_loss_by_hand():
    assert brier(H4, "up") == pytest.approx(0.16 + 0.01 + 0.09)
    assert brier(H4, "flat") == pytest.approx(0.36 + 0.01 + 0.49)
    assert brier({"up": 1.0, "down": 0.0, "flat": 0.0}, "down") == pytest.approx(2.0)
    assert log_loss(H4, "flat") == pytest.approx(-math.log(0.3))
    assert log_loss({"up": 1.0, "down": 0.0, "flat": 0.0}, "down") == pytest.approx(-math.log(1e-3))
    scored = figures([(H4, "up"), (H4, "flat")], {"up": 0.5, "down": 0.0, "flat": 0.5})
    assert (scored.n, scored.brier, scored.base_brier) == (
        2,
        pytest.approx(0.56),
        pytest.approx(0.5),
    )
    assert scored.skill == pytest.approx(1 - 0.56 / 0.5)
    assert figures([], None).skill is None


def test_the_fitted_temperature_minimises_the_log_loss_it_was_fitted_on():
    pairs = [(H4, "up"), (H4, "flat")] * 3
    fitted = fit_temperature(pairs)
    assert fitted is not None

    def loss(temperature: float) -> float:
        return math.fsum(log_loss(tempered(f, temperature), o) for f, o in pairs) / len(pairs)

    assert loss(fitted) < loss(1.0)
    assert loss(fitted) <= loss(fitted * 1.01)
    assert loss(fitted) <= loss(fitted / 1.01)
    assert fit_temperature([]) is None
    # A forecast that is always right is sharpened to the lowest temperature searched.
    always_right = [({"up": 0.5, "down": 0.25, "flat": 0.25}, "up")] * 3
    assert fit_temperature(always_right) == pytest.approx(0.05, rel=1e-3)
    uniform = {"up": 1 / 3, "down": 1 / 3, "flat": 1 / 3}
    assert tempered(uniform, 7.0) == pytest.approx(uniform)


def test_reliability_files_the_most_likely_class():
    buckets, ece = reliability([(H4, "up"), (H4, "flat"), (H24, "flat"), (H24, "down")])
    assert [(b.low, b.n, b.predicted, b.happened) for b in buckets] == [
        (0.6, 4, pytest.approx(0.6), 0.5)
    ]
    assert ece == pytest.approx(0.1)
    tie = {"up": 0.4, "down": 0.4, "flat": 0.2}
    [bucket], _ = reliability([(tie, "up")])
    assert bucket.happened == 1.0  # the first of equals, up, is the class filed
    assert reliability([]) == ([], None)


def _card(store: Path, *, mark_7: float | None = None):
    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID).questions
    if mark_7 is not None:
        seven = input_id(VALIDATION[1])
        questions = [replace(q, mark=mark_7) if q.input_id == seven else q for q in questions]
    split = build_split(questions, interval="4h", step_ms=STEP_MS)
    costs = CostModel(fill_role=FillRole.MAKER, leverage=1)
    return score_run(questions, [], step_ms=STEP_MS, costs=costs, split=split)


def test_the_base_rate_is_read_from_the_train_questions_only(store):
    card = _card(store)
    assert base_rates(card, 1) == ({"up": 0.5, "down": 0.0, "flat": 0.5}, 6)
    assert base_rates(card, 6) == ({"up": 0.5, "down": 0.0, "flat": 0.5}, 2)
    # Slot 7 at 102 turns slot 6's 4h move from +1.923% to -1.923%: a validation
    # label flips (up to down), the band (an absolute value) stays, and the base
    # rate, read from train only, stays what it was.
    flipped = _card(store, mark_7=102.0)
    assert flipped.flat_bands[1] == pytest.approx(card.flat_bands[1])
    assert base_rates(flipped, 1) == base_rates(card, 1)


def test_the_probe_section_of_score_by_hand(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    for segment in ("train", "validation"):
        assert cli.main(_probe(store, files, "--segment", segment)) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(out) if line.startswith("== direction probe "))
    section = out[start + 1 :]
    assert section[:4] == [
        "flat band (median |return| of the train and validation questions): 4h 1.923%, 24h 3.961%",
        "base rate, train, 4h: up 50.0% / down 0.0% / flat 50.0% (n 6)",
        "base rate, train, 24h: up 50.0% / down 0.0% / flat 50.0% (n 2)",
        "-- probe repeat 0 --",
    ]
    # Three ups at 0.26 and three flats at 0.86 average 0.56 against the base
    # rate's 0.5; log loss (3 x -ln .6 + 3 x -ln .3) / 6 = 0.857, base -ln .5.
    assert section[4] == (
        "  4h train: n 6 scored (0 invalid_probe, 0 refused, 0 without an outcome); Brier 0.560 "
        "vs base 0.500, skill -0.120; log loss 0.857 vs base 0.693"
    )
    # Slot 6 is up (0.26); slot 7 has no mark before the holdout.
    assert section[5] == (
        "  4h validation: n 1 scored (0 invalid_probe, 0 refused, 1 without an outcome); Brier "
        "0.260 vs base 0.500, skill +0.480; log loss 0.511 vs base 0.693"
    )
    # The temperature is fitted on the six train forecasts and applied to slot 6.
    fitted = fit_temperature([(H4, "up"), (H4, "flat")] * 3)
    assert fitted is not None
    scaled = tempered(H4, fitted)
    assert section[6] == (
        f"  4h validation, temperature {fitted:.2f} fitted on train: Brier "
        f"{brier(scaled, 'up'):.3f}, skill {1 - brier(scaled, 'up') / 0.5:+.3f}; log loss "
        f"{log_loss(scaled, 'up'):.3f}"
    )
    # Slot 0 up at (.2-1)² + .2² + .6² = 1.04, slot 1 flat at .2² + .2² + (.6-1)² = 0.24.
    assert section[7] == (
        "  24h train: n 2 scored (0 invalid_probe, 0 refused, 4 without an outcome); Brier 0.640 "
        "vs base 0.500, skill -0.280; log loss 1.060 vs base 0.693"
    )
    assert section[8] == (
        "  24h validation: n 0 scored (0 invalid_probe, 0 refused, 2 without an outcome); Brier "
        "n/a vs base n/a, skill n/a; log loss n/a vs base n/a"
    )
    assert section[9:] == [
        "-- probe across 1 repeat(s): Brier skill score, median (range) --",
        "  4h train: -0.120 (-0.120 to -0.120)",
        "  4h validation: +0.480 (+0.480 to +0.480)",
        "  24h train: -0.280 (-0.280 to -0.280)",
        "  24h validation: n/a (no repeat has a skill score)",
        "-- probe reliability, repeats pooled (the most likely class: its mean probability, "
        "and how often it happened) --",
        "  4h train: 0.6-0.7 n 6 predicted 60.0% happened 50.0%; ECE 0.100",
        "  4h validation: 0.6-0.7 n 1 predicted 60.0% happened 100.0%; ECE 0.400",
        "  24h train: 0.6-0.7 n 2 predicted 60.0% happened 50.0%; ECE 0.100",
        "  24h validation: no forecast scored; ECE n/a",
    ]


def test_an_invalid_answer_is_counted_not_scored(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster(text_for={TRAIN[1]: "no idea"}))
    assert cli.main(_probe(store, files)) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    # Slot 1 (a flat) is left out: ups 0, 2, 4 at 0.26 and flats 3, 5 at 0.86 make
    # (0.78 + 1.72) / 5 = 0.5, the base rate's own score.
    assert (
        "  4h train: n 5 scored (1 invalid_probe, 0 refused, 0 without an outcome); Brier 0.500 "
        "vs base 0.500, skill +0.000; log loss 0.788 vs base 0.693"
    ) in out


def test_a_variant_asked_only_the_probe_is_scored_and_cannot_be_compared(
    store, files, monkeypatch, capsys, tmp_path
):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files)) == 0
    capsys.readouterr()
    out_dir = tmp_path / "out"
    assert cli.main(_score(store, "--out", str(out_dir))) == 0
    captured = capsys.readouterr()
    assert not any(line.startswith("== repeat") for line in captured.out.splitlines())
    assert any(line.startswith("== direction probe ") for line in captured.out.splitlines())
    assert sorted(p.name for p in out_dir.iterdir()) == [f"{RUN_ID}-echo-summary.txt"]
    other = tmp_path / "other"
    other.mkdir()
    assert cli.main(_probe(store, (write_variant(other, "other"), files[1]))) == 0
    capsys.readouterr()
    assert cli.main(_score(store, "--against", "other")) == 1
    assert (
        "--against compares decisions, and variant 'echo' was asked only the direction probe"
    ) in capsys.readouterr().err


def test_the_holdout_probe_answers_are_scored_only_with_holdout(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files)) == 0
    assert cli.main(_probe(store, files, "--segment", "holdout", "--holdout")) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    assert not any(" holdout" in line for line in capsys.readouterr().out.splitlines())
    assert cli.main(_score(store, "--holdout")) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(line.startswith("  4h holdout: n ") for line in out)
    assert any(line.startswith("  4h holdout, temperature ") for line in out)


def test_the_cutoff_leaves_the_same_questions_out_of_the_probe(
    store, tmp_path, monkeypatch, capsys
):
    # A cutoff on the fixture's first day leaves out every question decided on
    # it (slots 0-4, 2027-01-15); train keeps slot 5 alone, a flat at 0.86.
    variant_file = write_variant(tmp_path, "echo", cutoff="2027-01-15")
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, (variant_file, write_probe(tmp_path)))) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        "  4h train: n 1 scored (0 invalid_probe, 0 refused, 0 without an outcome); Brier 0.860 "
        "vs base 0.500, skill -0.720; log loss 1.204 vs base 0.693"
    ) in out
    # The base rate is a fact about the prices: the cutoff does not shrink it.
    assert "base rate, train, 4h: up 50.0% / down 0.0% / flat 50.0% (n 6)" in out


def test_the_base_rate_reads_every_train_question_not_only_the_ones_asked(
    store, files, monkeypatch, capsys
):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--limit", "3")) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "base rate, train, 4h: up 50.0% / down 0.0% / flat 50.0% (n 6)" in out
    # Slots 0-2 were asked: up, flat, up at 0.26, 0.86, 0.26.
    assert (
        "  4h train: n 3 scored (0 invalid_probe, 0 refused, 0 without an outcome); Brier 0.460 "
        "vs base 0.500, skill +0.080; log loss 0.742 vs base 0.693"
    ) in out


# -- review round 1: the branches the first tests did not reach -----------------------


def test_each_repeat_is_scored_and_the_summary_gives_the_median_and_range(
    store, files, monkeypatch, capsys
):
    # Repeat 1 says h4 up .8 / down .1 / flat .1: an up costs .2² + .1² + .1² =
    # 0.06, a flat .8² + .1² + .9² = 1.46; train averages 0.76, skill -0.52.
    sharper = forecast_text({"up": 0.8, "down": 0.1, "flat": 0.1})
    _use(monkeypatch, Forecaster(second=sharper))
    assert cli.main(_probe(store, files, "--repeats", "2")) == 0
    assert len(_rows(store)) == 2 * len(TRAIN)
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "-- probe repeat 0 --" in out and "-- probe repeat 1 --" in out
    train = [line for line in out if line.startswith("  4h train: n 6 scored")]
    assert [line.split("; ")[1] for line in train] == [
        "Brier 0.560 vs base 0.500, skill -0.120",
        "Brier 0.760 vs base 0.500, skill -0.520",
    ]
    assert "  4h train: -0.320 (-0.520 to -0.120)" in out
    assert (
        "  4h train: 0.6-0.7 n 6 predicted 60.0% happened 50.0%; 0.8-0.9 n 6 predicted "
        "80.0% happened 50.0%; ECE 0.200" in out
    )


def test_with_no_train_forecast_the_temperature_is_not_fitted(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--segment", "validation")) == 0
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert "  4h validation, temperature-scaled: n/a (no train forecast to fit it on)" in out
    assert not any(line.startswith("  4h train: n ") for line in out)


def test_without_a_split_there_is_no_base_rate(store):
    from contrib.replay.probe_score import describe_probe

    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID).questions
    card = score_run(questions, [], step_ms=STEP_MS, costs=CostModel(), split=None)
    first = input_id(TRAIN[0])
    lines = describe_probe(
        card=card,
        probe=Probe("p", "s", "i"),
        answers={0: [ProbeAnswer(first, {"h4": H4, "h24": H24}, None)]},
        eligible={first},
    )
    assert "base rate, train, 4h: n/a (no train question has an outcome)" in lines
    assert (
        "  4h unsplit: n 1 scored (0 invalid_probe, 0 refused, 0 without an outcome); Brier "
        "0.260 vs base n/a, skill n/a; log loss 0.511 vs base n/a"
    ) in lines
    assert "  4h unsplit: n/a (no repeat has a skill score)" in lines


def test_answers_whose_usage_went_unreported_are_counted(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster(usage_reported=False))
    assert cli.main(_probe(store, files, "--limit", "2")) == 0
    assert (
        "answers whose call the usage collector recorded nothing for: 2 (truncation unknown, "
        "read as not truncated)"
    ) in capsys.readouterr().out.splitlines()


def test_the_limit_counts_a_refusal(store, files, monkeypatch, capsys):
    _use(monkeypatch, _Refuses(TRAIN[0]))
    assert cli.main(_probe(store, files, "--limit", "2")) == 0
    assert "stopped at --limit; the same command continues from here" in (
        capsys.readouterr().out.splitlines()
    )
    assert [(r["input_id"], r["invalid_reason"]) for r in _rows(store)] == [
        (input_id(TRAIN[0]), REFUSED),
        (input_id(TRAIN[1]), None),
    ]


def test_each_answer_is_reported_on_stderr(store, files, monkeypatch, capsys):
    _use(monkeypatch, Forecaster(text_for={TRAIN[1]: "no idea"}))
    assert cli.main(_probe(store, files, "--limit", "2")) == 0
    err = capsys.readouterr().err.splitlines()
    assert f"[1/6] {input_id(TRAIN[0])} repeat 0: h4 0.60/0.10/0.30 h24 0.20/0.20/0.60" in err
    assert f"[2/6] {input_id(TRAIN[1])} repeat 0: invalid_probe" in err


def test_one_probe_text_is_one_name(store, files, monkeypatch, capsys, tmp_path):
    _use(monkeypatch, Forecaster())
    assert cli.main(_probe(store, files, "--limit", "1")) == 0
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    same_text = write_probe(renamed, "direction-renamed")
    assert cli.main(_probe(store, (files[0], same_text))) == 1
    assert "is already stored as 'direction-t'; ask it under that name" in (capsys.readouterr().err)
