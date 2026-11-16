"""The look-ups' server: what it advertises, how a call ends, how it is started, and that it runs as a process (tools plan §4, T2 a–f).

Four groups. WHAT A CALL RETURNS: over an in-memory client the server
lists the three look-ups and answers each call with the pure function's
own ``dict``; a mistake comes back as the parser's sentence, marked as an
error; a history that cannot answer comes back as a protocol error that
carries none of the research package's sentence. WHAT IS ADVERTISED: the
enums are the research package's tables, each schema accepts what the
parser accepts on the suite's calls and refuses what it refuses on a list
of shape mistakes, each description shows a call that runs and names every
field of the answer, and the whole of it is pinned to the tools' version.
HOW IT IS STARTED: five parameters that survive the command line, a store
that has to exist, and a ``tools`` command that reads a manifest for
exactly those five and starts a child on a copy. AS A PROCESS: the real
stdio child, started both ways, answers as the view does and leaves its
store as it found it; the module that serves reaches no manifest.

The async client is driven from plain tests through ``anyio.run``, as a
synchronous hypothesist will have to drive it.
"""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import jsonschema
import pytest
from mcp import Client, MCPError, StdioServerParameters, types

from contrib.autoresearch.dsl import MAX_HOLD_BARS, Op, Side, parse_conditions, parse_spec
from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.features import FeatureError, FeatureFrame, SeriesBundle
from contrib.autoresearch.ledger import Ledger
from contrib.autoresearch.store import ResearchStore, StoreError
from contrib.autoresearch.upstream import MarketRegime
from contrib.autoresearch.vocabulary import (
    MAX_OFFSET_BARS,
    FeatureKind,
    FeatureRef,
    SeriesSource,
    feature_names,
    parse_feature_name,
)
from contrib.discovery import cli, tools_server
from contrib.discovery.bench import read_manifest, tools_experiment
from contrib.discovery.cli import main as discovery_main
from contrib.discovery.landing import ARTIFACT_FILENAME, artifact_digest, experiment_for, land
from contrib.discovery.manifest import SeedSet, WorldKind
from contrib.discovery.research_tools import TOOL_NAMES, ToolRefusal, TrainView, call
from contrib.discovery.tools_server import (
    IMPORT_ROOT,
    LOOKUP_FAILED,
    SERVER_NAME,
    STORE_CANNOT_ANSWER,
    TOOLS_VERSION,
    LaunchError,
    ServerParams,
    build_server,
    load_view,
    tool_specs,
    warm,
)
from contrib.discovery.worlds import generate

from .conftest import (
    COIN,
    LOOKBACK,
    PLANTED,
    STEP_MS,
    arm_store,
    bench_manifest,
    example_manifest,
    land_run,
    open_arm,
)
from .test_research_tools import CALLS, _hand_view

# The digest of everything a model is shown of the tools, at the version it
# is shown under. Changing a description, a schema or the vocabulary moves
# the digest: bump ``TOOLS_VERSION`` with it (after the wording is frozen,
# plan §5, that is a new set of runs) and pin both here.
PINNED = (1, "3d1a3e08db6ef0e1e52ff71ae82771115f2ae567200d9486e14691f02476669e")

_EXAMPLES = {spec.name: spec.example for spec in tool_specs()}

# Arguments that are wrong in SHAPE, which a schema can say: each is
# refused by its tool's input schema and by the look-up itself.
MISSHAPEN = (
    ("feature_stats", {"feature": "sma_9"}),
    ("feature_stats", {"feature": "close", "offset": 1}),
    ("feature_stats", {}),
    ("conditional_returns", {**_EXAMPLES["conditional_returns"], "horizon": 0}),
    ("conditional_returns", {**_EXAMPLES["conditional_returns"], "horizon": MAX_HOLD_BARS + 1}),
    ("conditional_returns", {**_EXAMPLES["conditional_returns"], "side": "up"}),
    ("conditional_returns", {**_EXAMPLES["conditional_returns"], "conditions": []}),
    (
        "conditional_returns",
        {
            **_EXAMPLES["conditional_returns"],
            "conditions": [{"left": "close", "op": "~", "right": 1}],
        },
    ),
    (
        "conditional_returns",
        {
            **_EXAMPLES["conditional_returns"],
            "conditions": [
                {
                    "left": "close",
                    "op": ">",
                    "right": {"feature": "close", "offset": MAX_OFFSET_BARS + 1},
                }
            ],
        },
    ),
    (
        "conditional_returns",
        {
            **_EXAMPLES["conditional_returns"],
            "conditions": [{"left": "rsi_14", "op": "<", "right": {"param": "cool_off"}}],
        },
    ),
    ("backtest_train", {"rule": "a rule in a string"}),
    ("backtest_train", {**_EXAMPLES["backtest_train"], "note": "read me"}),
)

# Mistakes a schema cannot say, which only the parser refuses: a comparison
# across units, a rule with a key the language does not have.
MISTAKEN = (
    (
        "conditional_returns",
        {
            **_EXAMPLES["conditional_returns"],
            "conditions": [{"left": "close", "op": ">", "right": "rsi_14"}],
        },
    ),
    ("backtest_train", {"rule": {**PLANTED, "stop_loss": 0.02}}),
    ("no_such_tool", {}),
)


def _ask(target, calls=(), *, mode="auto"):
    """One session with ``target``: who answered, what it lists, and each call's result or protocol error.

    ``mode`` is the handshake the client opens with: the SDK's current one,
    or ``"legacy"`` for the older ``initialize``, which a client is free to
    choose and under which the SDK treats a handler's exception differently.
    """

    async def session():
        with anyio.fail_after(120):
            async with Client(target, mode=mode) as client:
                listed = await client.list_tools()
                answers: list[types.CallToolResult | MCPError] = []
                for tool, arguments in calls:
                    try:
                        answers.append(await client.call_tool(tool, arguments))
                    except MCPError as exc:
                        answers.append(exc)
                return client.server_info, listed.tools, answers

    return anyio.run(session)


# A signal that holds at no bar: every figure computed from samples is null.
NEVER = (
    "conditional_returns",
    {
        "conditions": [{"left": "close", "op": ">", "right": 1e12}],
        "horizon": 6,
        "side": "long",
    },
)


def _refusal(view, tool, arguments) -> str:
    with pytest.raises(ToolRefusal) as refused:
        call(view, tool, arguments)
    return str(refused.value)


def _child(command: list[str]) -> StdioServerParameters:
    return StdioServerParameters(command=command[0], args=command[1:], cwd=str(IMPORT_ROOT))


@pytest.fixture(scope="module")
def params(history, tmp_path_factory) -> ServerParams:
    """One arm's store with its experiment opened, as the parameters a server is started on."""
    path = tmp_path_factory.mktemp("server") / "arm.sqlite"
    with arm_store(history, path) as store:
        experiment = open_arm(Ledger(store))
    return ServerParams.for_experiment(path, experiment)


@pytest.fixture(scope="module")
def view(params):
    return load_view(params)


@pytest.fixture(scope="module")
def listed(view):
    """The tools as a client is told of them, by name."""
    _info, tools, _answers = _ask(build_server(view))
    return {tool.name: tool for tool in tools}


@pytest.fixture(scope="module")
def landed_run(landed_world, tmp_path_factory):
    """The example world's run directory, as ``generate`` leaves one."""
    artifact, _landing = landed_world
    return land_run(tmp_path_factory.mktemp("tools-run"), artifact, bench_manifest())


# -- (a) what a call returns --------------------------------------------------------------------


def test_the_server_lists_the_three_look_ups_and_answers_as_the_pure_functions_do(view):
    info, tools, answers = _ask(build_server(view), CALLS)
    assert (info.name, info.version) == (SERVER_NAME, str(TOOLS_VERSION))
    assert tuple(tool.name for tool in tools) == TOOL_NAMES
    # Nothing a look-up does is a write, and a host is told so.
    assert all(tool.annotations.read_only_hint for tool in tools)
    assert len(answers) == len(CALLS)
    for (tool, arguments), answer in zip(CALLS, answers, strict=True):
        expected = call(view, tool, arguments)
        assert not answer.is_error, (tool, arguments)
        # The same ``dict`` twice: as structured content — which the client
        # held to the tool's output schema before handing it over — and as
        # the text a model reads.
        assert answer.structured_content == expected
        (block,) = answer.content
        assert json.loads(block.text) == expected


@pytest.mark.parametrize(("tool", "arguments"), MISSHAPEN + MISTAKEN)
def test_a_mistake_comes_back_as_the_look_up_s_own_sentence_marked_as_an_error(
    view, tool, arguments
):
    sentence = _refusal(view, tool, arguments)
    _info, _tools, (answer,) = _ask(build_server(view), [(tool, arguments)])
    assert answer.is_error and answer.structured_content is None
    assert [block.text for block in answer.content] == [sentence]


def test_no_arguments_at_all_is_refused_by_the_keys_that_are_missing(view):
    _info, _tools, (answer,) = _ask(build_server(view), [("conditional_returns", None)])
    assert answer.is_error
    assert answer.content[0].text == _refusal(view, "conditional_returns", {})
    assert "missing required key(s) ['conditions', 'horizon', 'side']" in answer.content[0].text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("error", [EvaluationError, FeatureError])
def test_a_history_that_cannot_answer_is_a_protocol_error_that_carries_no_sentence(
    view, monkeypatch, caplog, error, mode
):
    sentence = "the window that ends 2026-03-01T00:00:00+00:00 has no funding coverage"

    def unanswerable(_view, _tool, _arguments):
        raise error(sentence)

    monkeypatch.setattr(tools_server, "call", unanswerable)
    with caplog.at_level("ERROR", logger=tools_server.__name__):
        _info, _tools, (answer,) = _ask(
            build_server(view), [("feature_stats", {"feature": "close"})], mode=mode
        )
    # Not a tool result: a model is never handed it. And the wire carries a
    # fixed sentence — the instant is in the operator's log alone.
    assert isinstance(answer, MCPError)
    assert (answer.code, answer.message) == (types.INTERNAL_ERROR, STORE_CANNOT_ANSWER)
    assert "2026" not in str(answer.error)
    assert sentence in caplog.text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_a_defect_is_a_fixed_sentence_too_whichever_handshake_the_client_opened_with(
    view, monkeypatch, caplog, mode
):
    # Left to the SDK, an unexpected exception is answered with its own text
    # to a client that opened with the older handshake (mcp 2.3.0): so the
    # server answers it itself, and the text stays in the log.
    sentence = "a record refused its own figures at 2026-03-01T00:00:00+00:00"

    def broken(_view, _tool, _arguments):
        raise ValueError(sentence)

    monkeypatch.setattr(tools_server, "call", broken)
    with caplog.at_level("ERROR", logger=tools_server.__name__):
        _info, _tools, (answer,) = _ask(
            build_server(view), [("feature_stats", {"feature": "close"})], mode=mode
        )
    assert isinstance(answer, MCPError)
    assert (answer.code, answer.message) == (types.INTERNAL_ERROR, LOOKUP_FAILED)
    assert "2026" not in str(answer.error)
    # With its traceback, for whoever reads the server's stderr.
    assert sentence in caplog.text and "Traceback" in caplog.text


def test_a_feature_with_no_value_at_the_window_s_start_ends_as_a_protocol_error():
    # The look-ups' own refusal of such a feature is the evaluator's class
    # (plan §6 #18), so it takes the same way out: unpatched, end to end.
    unready = _hand_view([100.0 + index for index in range(30)], funding_hours=30 * 4)
    _info, _tools, (answer, ready) = _ask(
        build_server(unready),
        [("feature_stats", {"feature": "sma_20"}), ("feature_stats", {"feature": "close"})],
    )
    assert isinstance(answer, MCPError) and answer.message == STORE_CANNOT_ANSWER
    # The session outlives it: the next look-up is answered.
    assert not ready.is_error and ready.structured_content["bars"] == 30


# -- (e) what is advertised is read off the research package's tables ------------------------------


def test_the_advertised_enums_are_the_research_package_s_tables(listed):
    names = list(feature_names())
    assert listed["feature_stats"].input_schema["properties"]["feature"]["enum"] == names

    schema = listed["conditional_returns"].input_schema
    condition = schema["properties"]["conditions"]["items"]["properties"]
    # A feature is named in four places of a condition — either side, bare
    # or with an offset — and each is the vocabulary's list.
    by_name, with_offset = condition["left"]["anyOf"]
    assert condition["right"]["anyOf"][0] == condition["left"]
    assert by_name["enum"] == names and with_offset["properties"]["feature"]["enum"] == names
    assert condition["op"]["enum"] == [op.value for op in Op]
    regimes = [regime.value for regime in MarketRegime]
    assert {"type": "string", "enum": regimes} in condition["right"]["anyOf"]
    offset = with_offset["properties"]["offset"]
    assert (offset["minimum"], offset["maximum"]) == (0, MAX_OFFSET_BARS)
    horizon = schema["properties"]["horizon"]
    assert (horizon["minimum"], horizon["maximum"]) == (1, MAX_HOLD_BARS)
    assert schema["properties"]["side"]["enum"] == [side.value for side in Side]
    # A rule is advertised as an object and no more (decided 2026-11-16):
    # the parser is the one statement of the language.
    rule = listed["backtest_train"].input_schema["properties"]["rule"]
    assert set(rule) == {"type", "description"} and rule["type"] == "object"


def test_a_feature_added_to_the_vocabulary_is_in_the_next_listing(monkeypatch):
    grown = (*feature_names(), "made_up_9")
    monkeypatch.setattr(tools_server, "feature_names", lambda: grown)
    stats, returns, _backtest = tool_specs()
    assert stats.input_schema["properties"]["feature"]["enum"] == list(grown)
    left = returns.input_schema["properties"]["conditions"]["items"]["properties"]["left"]
    assert left["anyOf"][0]["enum"] == list(grown)


def test_no_schema_refers_to_a_definition_elsewhere(listed):
    # A provider's tool-calling layer may not resolve a ``$ref``, and one
    # that drops it shows its model a condition with no shape: every schema
    # says what it has to say where it says it.
    for tool in listed.values():
        written = json.dumps([tool.input_schema, tool.output_schema])
        assert "$ref" not in written and "$defs" not in written, tool.name


def test_each_schema_accepts_what_the_look_up_reads_and_refuses_what_it_refuses(view, listed):
    reads, answers = {}, {}
    for name, tool in listed.items():
        for schema in (tool.input_schema, tool.output_schema):
            jsonschema.Draft202012Validator.check_schema(schema)
        reads[name] = jsonschema.Draft202012Validator(tool.input_schema)
        answers[name] = jsonschema.Draft202012Validator(tool.output_schema)

    # Not narrower than the parser: every call the suite makes, and every
    # call a description shows, is one the input schema lets through — and
    # what comes back is what the output schema says, null figures included.
    for tool, arguments in (*CALLS, *_EXAMPLES.items(), NEVER):
        assert reads[tool].is_valid(arguments), (tool, arguments)
        answers[tool].validate(call(view, tool, arguments))
    empty = call(view, *NEVER)
    assert empty["samples"] == 0
    assert (empty["mean_return"], empty["std_return"], empty["t_stat"]) == (None, None, None)
    # Not wider where a schema can speak: a mistake of shape is refused by
    # both. (What only the parser can refuse is ``MISTAKEN``'s, above.)
    for tool, arguments in MISSHAPEN:
        assert not reads[tool].is_valid(arguments), (tool, arguments)
        _refusal(view, tool, arguments)


def test_a_spec_checks_itself_and_shares_nothing_with_the_next():
    stats = tool_specs()[0]
    with pytest.raises(ValueError, match="is not a look-up"):
        replace(stats, name="feature_statistics")
    with pytest.raises(ValueError, match="does not show its example"):
        replace(stats, example={"feature": "close"})
    # A caller that changes what it was handed changes no later listing:
    # the examples and the schemas are built afresh each time.
    stats.example["feature"] = "close"
    stats.input_schema["properties"]["feature"]["enum"].clear()
    returns = tool_specs()[1]
    returns.output_schema["properties"]["t_stat"]["type"].append("string")
    again = tool_specs()
    assert again[0].example == {"feature": "rsi_14"}
    assert again[0].input_schema["properties"]["feature"]["enum"] == list(feature_names())
    assert again[1].output_schema["properties"]["mean_return"]["type"] == ["number", "null"]
    assert again[1].output_schema["properties"]["t_stat"]["type"] == ["number", "null"]
    # And a field's schema can only be replaced under a name the table has.
    with pytest.raises(ValueError, match="no field named"):
        tools_server._object_schema(tools_server._BACKTEST_FIELDS, sdie={"type": "string"})


# -- (f) a description shows a call and names every field ------------------------------------------


def test_each_description_shows_a_call_that_runs_and_names_every_field_of_the_answer(view, listed):
    for spec in tool_specs():
        described = listed[spec.name].description
        assert described == spec.description
        assert f"Call: {json.dumps(spec.example)}" in described
        assert "Reads the training bars only" in described
        answers = [call(view, spec.name, spec.example)]
        if spec.name == "feature_stats":
            # The second shape: the one categorical feature.
            answers.append(call(view, "feature_stats", {"feature": "regime"}))
        # Under "Returns", where the answer's fields are listed — an
        # argument of the same name higher up does not stand in for one.
        returned = described[described.index("Returns") :]
        for answer in answers:
            for key in answer:
                assert f"- {key}: " in returned, (spec.name, key)


def test_the_sentences_the_plan_settled_are_in_the_descriptions(listed):
    returns = listed["conditional_returns"].description
    # The t-statistic, as decided 2026-11-16: what it is, that it is not
    # corrected, what to read beside it — and no threshold to judge it by.
    assert (
        "a one-sample t-statistic of mean_return against zero. It is not corrected for how "
        "many conditions you have looked at; read it together with samples and "
        "baseline_mean_return" in returns
    )
    assert "p-value" not in returns and "significant at" not in returns
    for needed in (
        '{"param": name} is refused',
        "with NO condition: what the window did whatever the signal said",
        "Not on the scale of backtest_train's net_return",
        "for two reasons",
        "exact negatives of each other",
        "The samples are not a rule's trades",
    ):
        assert needed in returns, needed
    assert "A rule tried here can be proposed as it stands" in listed["backtest_train"].description
    assert "For regime" in listed["feature_stats"].description
    # What a field IS, and not what to conclude from it (decided
    # 2026-11-16): advice here is read by the tool arm alone, and a result
    # could not then be told from the advice.
    shown = " ".join(tool.description for tool in listed.values())
    assert "is not scored and does not use up a proposal" in shown
    for advice in ("not a validation score", "Compare mean_return", "fail the next"):
        assert advice not in shown, advice


def test_the_examples_point_away_from_what_a_world_plants():
    # A world's rule enters on a funding feature, gates on the regime or on
    # realised volatility, and leaves on ``max_bars`` (discovery plan §4.1).
    # The research package's prompt shows no example rule, so an example
    # here is something only the tool arm reads: it is kept off all of
    # those, where it can cost that arm a recovery and cannot hand it one.
    stats, returns, backtest = (spec.example for spec in tool_specs())
    rule = parse_spec(backtest["rule"])
    read = [
        FeatureRef(*parse_feature_name(stats["feature"])),
        *(ref for condition in parse_conditions(returns["conditions"]) for ref in condition.refs),
        *rule.features,
    ]
    assert len(read) >= 3
    for ref in read:
        assert ref.source is SeriesSource.BARS, ref
        assert ref.kind not in (FeatureKind.REGIME, FeatureKind.REALIZED_VOL), ref
    assert rule.max_bars is None


def test_nothing_advertised_names_a_world_s_kind_or_an_instant(listed):
    shown = json.dumps(
        [
            [tool.name, tool.description, tool.input_schema, tool.output_schema]
            for tool in listed.values()
        ]
    ).lower()
    for word in ("planted", "holdout", COIN.lower(), *(kind.value for kind in WorldKind)):
        assert word not in shown, word
    assert re.search(r"\b20\d\d-\d\d", shown) is None


def test_what_a_model_is_shown_is_pinned_to_the_tools_version():
    shown = [[s.name, s.description, s.input_schema, s.output_schema] for s in tool_specs()]
    digest = hashlib.sha256(json.dumps(shown, sort_keys=True).encode("utf-8")).hexdigest()
    assert (TOOLS_VERSION, digest) == PINNED, (
        "the tools a model is shown changed: bump TOOLS_VERSION and pin the new digest"
    )


# -- how it is started ---------------------------------------------------------------------------


def test_a_server_is_started_on_five_things_that_survive_the_command_line(params):
    assert [field.name for field in fields(ServerParams)] == [
        "db",
        "coin",
        "split",
        "costs",
        "indicator_lookback",
    ]
    assert ServerParams.from_argv(params.argv()) == params
    # Each parameter is one ``--flag=value`` argument, so a value that
    # begins with a dash is still its flag's.
    dashed = replace(params, coin="-SYN")
    assert ServerParams.from_argv(dashed.argv()) == dashed
    # A path is made absolute: the child is started in another directory.
    relative = replace(params, db="arm.sqlite")
    assert relative.db.is_absolute() and relative.db == Path("arm.sqlite").resolve()
    assert params.command() == [
        sys.executable,
        "-m",
        "contrib.discovery.tools_server",
        *params.argv(),
    ]
    assert tools_server.__name__ == "contrib.discovery.tools_server"
    assert (IMPORT_ROOT / "contrib" / "discovery" / "tools_server.py").is_file()


def test_parameters_that_cannot_be_read_are_a_launch_refusal(params):
    argv = params.argv()

    def swapped(flag: str, value: str) -> list[str]:
        assert sum(arg.startswith(f"{flag}=") for arg in argv) == 1
        return [f"{flag}={value}" if arg.startswith(f"{flag}=") else arg for arg in argv]

    split = params.split.to_dict()
    for flag, value, sentence in (
        ("--split", "{", "--split: not JSON"),
        ("--split", "[]", "--split: expected a JSON object, got list"),
        ("--split", json.dumps({**split, "holdout": None}), "holdout: expected"),
        ("--split", json.dumps({k: v for k, v in split.items() if k != "train"}), "a split record"),
        ("--costs", json.dumps({"leverage": 1.0}), "a cost record has exactly the keys"),
        ("--costs", json.dumps({**params.costs.to_dict(), "leverage": -1}), "leverage"),
    ):
        with pytest.raises(LaunchError, match=re.escape(sentence)):
            ServerParams.from_argv(swapped(flag, value))
    # And built in code: the split and the costs are the experiment's records.
    with pytest.raises(LaunchError, match="the experiment's split and costs"):
        replace(params, split=params.split.train)
    with pytest.raises(LaunchError, match="a whole number of bars"):
        replace(params, indicator_lookback=True)
    with pytest.raises(LaunchError, match="a coin's name"):
        replace(params, coin="")
    with pytest.raises(LaunchError, match="the path of a research store"):
        replace(params, db=3)


def test_a_store_that_is_not_there_is_refused_and_not_created(params, tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(LaunchError, match="reads an existing research store and creates none"):
        load_view(replace(params, db=missing))
    # Opening a path makes a store there; the server must not have.
    assert not missing.exists()
    # Nor made one of an empty file, which opening would.
    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with pytest.raises(LaunchError, match="is not a file with a store in it"):
        load_view(replace(params, db=empty))
    assert empty.stat().st_size == 0
    # A file that is something else is the store's own refusal.
    other = tmp_path / "notes.txt"
    other.write_text("not a database, and longer than a header\n" * 8, encoding="utf-8")
    with pytest.raises(StoreError):
        load_view(replace(params, db=other))


def test_a_store_that_does_not_hold_the_window_is_a_launch_refusal(params, monkeypatch):
    # A window the store's bars do not run over: the view's own refusal,
    # under the store and the coin it was asked of.
    train = params.split.train
    early = replace(params.split, train=replace(train, start_ms=train.start_ms - 10_000 * STEP_MS))
    with pytest.raises(LaunchError, match="does not hold the training window the split names"):
        load_view(replace(params, split=early))
    # A coin the store holds nothing of is the research package's sentence.
    with pytest.raises(EvaluationError, match="holds no 4h bars for SYN-ELSEWHERE"):
        load_view(replace(params, coin="SYN-ELSEWHERE"))

    # Only the view's own refusal is told as the parameters' fault: any
    # other ``ValueError`` under the read is a defect and stays one.
    def defective(*_args, **_kwargs):
        raise ValueError("a defect under the read")

    monkeypatch.setattr(TrainView, "load", defective)
    with pytest.raises(ValueError, match="a defect under the read") as raised:
        load_view(params)
    assert not isinstance(raised.value, LaunchError)


def test_the_warm_up_passes_over_a_series_the_store_does_not_hold_and_nothing_else(
    params, view, monkeypatch, caplog
):
    # No daily bars at all: there is nothing to compute the daily features
    # from. The warm-up says which it passed over and goes on; how a call
    # ends stays the call's — that feature's look-up is a protocol error,
    # the rest answer.
    bundle = view.frame.bundle
    bare = replace(
        view,
        frame=FeatureFrame(
            SeriesBundle(bundle.bars, daily=[], funding=bundle.funding),
            indicator_lookback=LOOKBACK,
        ),
    )
    with caplog.at_level("WARNING", logger=tools_server.__name__):
        warm(bare)
    skipped = [record.getMessage() for record in caplog.records]
    assert skipped and all("holds no daily series" in line for line in skipped)
    assert {line.split()[2] for line in skipped} == {
        name for name in feature_names() if "_1d" in name
    }
    _info, _tools, (daily, close, backtest) = _ask(
        build_server(bare),
        [
            ("feature_stats", {"feature": "close_1d"}),
            ("feature_stats", {"feature": "close"}),
            ("backtest_train", _EXAMPLES["backtest_train"]),
        ],
    )
    assert isinstance(daily, MCPError) and daily.message == STORE_CANNOT_ANSWER
    assert not close.is_error and not backtest.is_error

    # A feature whose series IS held and still cannot be computed is no
    # missing series (the indicator engine returning nothing raises the
    # same class): it is not passed over, and stops the launch.
    real = FeatureFrame.series

    def engine_down(self, ref):
        if ref.name == "rsi_14":
            raise FeatureError("the indicator engine returned nothing past its warm-up")
        return real(self, ref)

    monkeypatch.setattr(FeatureFrame, "series", engine_down)
    with pytest.raises(FeatureError, match="engine returned nothing"):
        warm(load_view(params))


def test_the_frame_is_warmed_on_every_feature_of_the_vocabulary(params, monkeypatch):
    fresh = load_view(params)
    computed: dict[str, tuple] = {}
    real = FeatureFrame.series

    def recording(self, ref):
        values = real(self, ref)
        computed.setdefault(str(ref), values)
        return values

    monkeypatch.setattr(FeatureFrame, "series", recording)
    warm(fresh)
    monkeypatch.undo()
    assert set(computed) == set(feature_names())
    # And kept: a look-up that reads a feature afterwards is handed the
    # array the warm-up computed, not a second computation of it.
    for name in feature_names():
        assert fresh.frame.series(FeatureRef(*parse_feature_name(name))) is computed[name]


def test_main_warms_before_it_serves_and_names_its_refusals(params, monkeypatch, capsys, tmp_path):
    steps: list[str] = []
    monkeypatch.setattr(tools_server, "warm", lambda view: steps.append("warm"))
    monkeypatch.setattr(tools_server, "serve", lambda view: steps.append("serve"))
    assert tools_server.main(params.argv()) == 0
    assert steps == ["warm", "serve"]

    # Each family a launch can be refused in, through ``main``: this
    # module's own, the store's, and the research package's two.
    other = tmp_path / "notes.txt"
    other.write_text("not a database, and longer than a header\n" * 8, encoding="utf-8")
    for changed, sentence in (
        (replace(params, db=tmp_path / "missing.sqlite"), "is not a file"),
        (replace(params, db=other), "notes.txt"),
        (replace(params, coin="SYN-ELSEWHERE"), "holds no 4h bars"),
        (replace(params, indicator_lookback=1), "indicator"),
    ):
        capsys.readouterr()
        assert tools_server.main(changed.argv()) == 1, sentence
        said = capsys.readouterr()
        # stdout is the protocol's: a refusal is said on stderr alone.
        assert said.out == ""
        assert said.err.startswith("error: ") and sentence in said.err
    # No refusal got as far as serving.
    assert steps == ["warm", "serve"]


def test_main_stops_at_a_warm_up_that_fails_and_leaves_an_error_while_serving_as_raised(
    params, monkeypatch, capsys
):
    def engine_down(_view):
        raise FeatureError("the indicator engine returned nothing past its warm-up")

    served: list[str] = []
    monkeypatch.setattr(tools_server, "warm", engine_down)
    monkeypatch.setattr(tools_server, "serve", lambda view: served.append("serve"))
    assert tools_server.main(params.argv()) == 1
    assert "engine returned nothing" in capsys.readouterr().err and served == []

    # Once serving has begun an error is no refusal to start: it is not
    # dressed as one, and is raised as it is.
    def client_gone(_view):
        raise BrokenPipeError("the client stopped reading")

    monkeypatch.setattr(tools_server, "warm", lambda view: None)
    monkeypatch.setattr(tools_server, "serve", client_gone)
    with pytest.raises(BrokenPipeError):
        tools_server.main(params.argv())


def test_main_says_how_to_install_the_sdk_before_it_reads_anything(params, monkeypatch, capsys):
    def unread(_params):
        raise AssertionError("the store was read for a server that cannot be built")

    # ``None`` in ``sys.modules`` is how an import is made to fail: the SDK
    # is asked for by the names the server uses.
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setattr(tools_server, "load_view", unread)
    assert tools_server.main(params.argv()) == 1
    said = capsys.readouterr()
    assert 'pip install -e ".[discovery-tools]"' in said.err and said.out == ""


def test_main_ends_an_interrupt_before_serving_as_130(params, monkeypatch, capsys):
    def interrupted(_view):
        raise KeyboardInterrupt

    monkeypatch.setattr(tools_server, "warm", interrupted)
    assert tools_server.main(params.argv()) == 130
    said = capsys.readouterr()
    assert said.err == "interrupted\n" and said.out == ""


# -- (c) (d) the tools command --------------------------------------------------------------------


def test_the_tools_command_starts_a_child_on_five_things_and_a_copy(
    landed_run, monkeypatch, capsys
):
    artifact = landed_run / ARTIFACT_FILENAME
    before = artifact_digest(artifact)
    held = sorted(path.name for path in landed_run.iterdir())
    started: list[ServerParams] = []

    def child(server_params: ServerParams) -> int:
        # While the child would run: a copy of the artifact, outside the
        # run directory.
        assert artifact_digest(server_params.db) == before
        assert landed_run.resolve() not in server_params.db.parents
        started.append(server_params)
        return 0

    monkeypatch.setattr(cli, "_serve_child", child)
    assert discovery_main(["tools", "--run", str(landed_run)]) == 0
    said = capsys.readouterr()
    assert said.out == "" and f"starting a server for {COIN}'s training window" in said.err

    (server_params,) = started
    manifest = read_manifest(landed_run)
    # The experiment an arm of this world opens, and nothing else of the manifest.
    assert server_params == ServerParams.for_experiment(
        server_params.db, experiment_for(manifest, "tools")
    )
    # (d) Nothing on the command line says what the world is. The manifest
    # plants a rule; none of its words, nor the kind, nor the manifest's
    # own path or digest, is among the child's arguments.
    assert manifest.world.planted is not None
    line = " ".join(server_params.command()).lower()
    entry, gate = PLANTED["entry"]["long"][0], PLANTED["filters"][0]
    for word in (
        "planted",
        "manifest",
        PLANTED["family"],
        entry["left"],
        gate["left"],
        str(entry["right"]),
        str(gate["right"]),
        landed_run.name.lower(),
        *(kind.value for kind in WorldKind),
    ):
        assert word not in line, word
    # The control: those words are what the manifest holds.
    written = (landed_run / "manifest.json").read_text(encoding="utf-8")
    assert entry["left"] in written and str(gate["right"]) in written
    # And nothing is left: the copy is gone, the directory is as it was.
    assert not server_params.db.exists()
    assert sorted(path.name for path in landed_run.iterdir()) == held
    assert artifact_digest(artifact) == before


def test_the_tools_command_refuses_a_test_seed_and_starts_nothing(
    landed_world, tmp_path, monkeypatch, capsys
):
    artifact, _landing = landed_world
    run = land_run(tmp_path, artifact, bench_manifest(seed_set=SeedSet.TEST))

    def child(server_params: ServerParams) -> int:
        raise AssertionError("a server was started for a test seed")

    monkeypatch.setattr(cli, "_serve_child", child)
    assert discovery_main(["tools", "--run", str(run)]) == 1
    said = capsys.readouterr()
    assert "is a test seed" in said.err and "development seeds only" in said.err
    assert said.out == ""
    # A directory that is not a landed world is the bench's own refusal.
    assert discovery_main(["tools", "--run", str(tmp_path / "nowhere")]) == 1
    assert "error: " in capsys.readouterr().err
    # The seed set is the whole of the rule: a development world with
    # nothing planted is served like one with a rule in it, and under the
    # same conditions.
    null = land_run(tmp_path, artifact, bench_manifest(kind=WorldKind.NULL_V1))
    assert tools_experiment(null) == experiment_for(bench_manifest(), "tools")


def test_the_tools_command_asks_for_the_sdk_before_it_copies_and_ends_as_its_child_does(
    landed_run, monkeypatch, capsys
):
    def child(server_params: ServerParams) -> int:
        raise AssertionError("a copy was made and a child started without the SDK")

    with monkeypatch.context() as patched:
        patched.setitem(sys.modules, "mcp", None)
        patched.setattr(cli, "_serve_child", child)
        assert discovery_main(["tools", "--run", str(landed_run)]) == 1
        assert 'pip install -e ".[discovery-tools]"' in capsys.readouterr().err

    # The child's exit code is the command's; one a signal ended is said as
    # a shell says it, 128 plus the signal.
    ran: list[tuple[list[str], object]] = []
    for code, expected in ((0, 0), (1, 1), (130, 130), (-15, 143)):

        def run(command, *, cwd, check, code=code):
            ran.append((command, cwd))
            return SimpleNamespace(returncode=code)

        monkeypatch.setattr(cli.subprocess, "run", run)
        assert discovery_main(["tools", "--run", str(landed_run)]) == expected
    command, cwd = ran[-1]
    assert command[:3] == [sys.executable, "-m", "contrib.discovery.tools_server"]
    assert cwd == IMPORT_ROOT


# -- (b) as a process -----------------------------------------------------------------------------


def test_the_server_runs_as_a_child_over_stdio_and_answers_as_the_view_does(params, view):
    before = artifact_digest(params.db)
    calls = [*_EXAMPLES.items(), ("feature_stats", {"feature": "sma_9"})]
    info, tools, answers = _ask(_child(params.command()), calls)
    assert (info.name, info.version) == (SERVER_NAME, str(TOOLS_VERSION))
    assert tuple(tool.name for tool in tools) == TOOL_NAMES
    *answered, refused = answers
    for (tool, arguments), answer in zip(_EXAMPLES.items(), answered, strict=True):
        assert not answer.is_error
        assert answer.structured_content == call(view, tool, arguments)
    assert refused.is_error
    assert refused.content[0].text == _refusal(view, "feature_stats", {"feature": "sma_9"})
    # The child read the arm's store and wrote nothing to it.
    assert artifact_digest(params.db) == before


def test_the_tools_command_serves_a_landed_world_through_its_child(landed_run):
    artifact = landed_run / ARTIFACT_FILENAME
    before = artifact_digest(artifact)
    command = [sys.executable, "-m", "contrib.discovery", "tools", "--run", str(landed_run)]
    # Two look-ups that need no indicator walk on this side: the child has
    # warmed its own frame, and the comparison here should not pay for a second.
    asked = [
        ("feature_stats", {"feature": "close"}),
        (
            "conditional_returns",
            {
                "conditions": [{"left": "funding_zscore_14", "op": "<", "right": 0}],
                "horizon": 6,
                "side": "long",
            },
        ),
    ]
    _info, tools, answers = _ask(_child(command), asked)
    assert tuple(tool.name for tool in tools) == TOOL_NAMES
    # The artifact itself is read here, which the server never is pointed
    # at: this build wrote it, so opening it migrates nothing, and the
    # digest below holds that.
    here = load_view(
        ServerParams.for_experiment(artifact, experiment_for(read_manifest(landed_run), "tools"))
    )
    for (tool, arguments), answer in zip(asked, answers, strict=True):
        assert not answer.is_error
        assert answer.structured_content == call(here, tool, arguments)
    assert artifact_digest(artifact) == before


def _leaves(exc: BaseException) -> list[BaseException]:
    grouped = getattr(exc, "exceptions", None)
    return [exc] if grouped is None else [leaf for inner in grouped for leaf in _leaves(inner)]


def test_a_child_that_cannot_start_is_a_closed_connection_and_not_a_hang(params, tmp_path):
    # What a caller sees of a server that refused its parameters: the
    # refusal is on the child's stderr, and the client's side is a closed
    # connection, raised — which a hypothesist is to read as a failed seam.
    missing = replace(params, db=tmp_path / "missing.sqlite")
    # ``BaseException``: the client's task groups raise an exception group
    # on 3.11+, and the closed connection is a leaf of it.
    with pytest.raises(BaseException) as raised:
        _ask(_child(missing.command()))
    closed = [leaf for leaf in _leaves(raised.value) if isinstance(leaf, MCPError)]
    assert closed and closed[0].code == types.CONNECTION_CLOSED


def test_an_interrupt_stops_a_server_that_is_serving(params):
    # The SDK reads stdin on a worker thread, and a blocked read cannot be
    # interrupted: left to ``KeyboardInterrupt``, a serving process did not
    # stop until its stdin closed. Here the handshake is done, stdin is held
    # open, and the interrupt has to end the process by itself.
    # (On Windows a child cannot be sent Ctrl-C alone; Ctrl-Break goes
    # through the same handler.)
    windows = sys.platform == "win32"
    child = subprocess.Popen(
        params.command(),
        cwd=IMPORT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    try:
        hello = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "suite", "version": "0"},
            },
        }
        child.stdin.write(json.dumps(hello) + "\n")
        child.stdin.flush()
        assert json.loads(child.stdout.readline())["id"] == 1
        child.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGINT)
        assert child.wait(timeout=30) == 130
        assert child.stderr.read().endswith("interrupted\n")
    finally:
        if child.poll() is None:
            child.kill()
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


def test_the_serving_module_reaches_no_manifest_and_the_package_needs_no_sdk_to_import():
    # In a fresh interpreter: the module a server runs loads the look-ups
    # and the borrow and nothing else of this package — not the manifest,
    # not the bench, not the worlds — and neither it nor the package's CLI
    # imports the SDK until a server is built.
    code = (
        "import json, sys\n"
        "import contrib.discovery.tools_server\n"
        "served = sorted(name for name in sys.modules if name.startswith('contrib.discovery.'))\n"
        "import contrib.discovery.cli\n"
        "print(json.dumps({'served': served, 'sdk': 'mcp' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=IMPORT_ROOT, capture_output=True, text=True, check=True
    )
    assert json.loads(done.stdout) == {
        "served": [
            "contrib.discovery.research_tools",
            "contrib.discovery.tools_server",
            "contrib.discovery.upstream",
        ],
        "sdk": False,
    }


# -- a world and its null twin ---------------------------------------------------------------------


def _shape(value):
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    return type(value).__name__


def _outline(answer):
    """How a call ended, and the keys and types of what it returned — its figures left out."""
    if isinstance(answer, MCPError):
        return ("protocol", answer.message)
    if answer.is_error:
        return ("refused", answer.content[0].text)
    return ("answered", _shape(answer.structured_content))


def test_a_world_and_its_null_twin_are_answered_in_one_shape(landed_world, tmp_path):
    # Same seed, one with a rule planted and one with none: the same split,
    # the same calls. The figures differ; the keys, the types and every
    # refusal's sentence do not, so nothing about HOW the server answers
    # says which of the two it is reading.
    # (The landed artifact is read in place, as in the test above.)
    artifact, _landing = landed_world
    planted, null = example_manifest(), example_manifest(kind=WorldKind.NULL_V1)
    twin = tmp_path / "twin.sqlite"
    with ResearchStore(twin) as store:
        land(generate(null), store)
    experiments = [experiment_for(manifest, "tools") for manifest in (planted, null)]
    assert experiments[0] == experiments[1]

    calls = [*CALLS, *MISSHAPEN, *MISTAKEN]
    outlines, figures = [], []
    for path, experiment in zip((artifact, twin), experiments, strict=True):
        world = load_view(ServerParams.for_experiment(path, experiment))
        _info, _tools, answers = _ask(build_server(world), calls)
        outlines.append([_outline(answer) for answer in answers])
        figures.append([getattr(answer, "structured_content", None) for answer in answers])
    assert outlines[0] == outlines[1]
    assert [kind for kind, _body in outlines[0]] == ["answered"] * len(CALLS) + ["refused"] * (
        len(MISSHAPEN) + len(MISTAKEN)
    )
    # The control: these ARE two worlds, read apart.
    assert figures[0] != figures[1]
