"""The file-target provider: the carry handoff read as the perp leg's decision."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.common.constants import FILE_TARGET_MODEL
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionMode,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.integration import decision_provider as decision_provider_mod
from contrib.hyperliquid_perp.integration.decision_provider import (
    EngineDecisionProvider,
    build_decision_provider,
)
from contrib.hyperliquid_perp.integration.file_target_provider import (
    HANDOFF_WINDOW,
    FileTargetDecisionProvider,
    StaleTarget,
    read_target,
)
from contrib.hyperliquid_perp.runtime.decision import DecisionInput

from ..fakes.decisions import market_ctx
from .test_decision_provider import _perp_ctx, _stub_provider

D = Decimal
_LOGGER = "contrib.hyperliquid_perp.integration.file_target_provider"

# The boundary the fixture handoff is for, and a cycle inside its day.
BOUNDARY = datetime(2026, 3, 15, tzinfo=timezone.utc)
BOUNDARY_MS = 1_773_532_800_000
CYCLE = BOUNDARY + timedelta(hours=4, minutes=7)


def _handoff(**overrides) -> dict:
    """A handoff as ``contrib/carry`` writes it (its README, 「交接檔」), the perp leg short."""
    doc = {
        "version": 1,
        "coin": "BTC",
        "as_of_ms": BOUNDARY_MS,
        "as_of": "2026-03-15T00:00:00+00:00",
        "written_at_ms": BOUNDARY_MS - 600_000,
        "written_at": "2026-03-14T23:50:00+00:00",
        "action": "enter",
        "position": {"side": "in", "entered_at_ms": BOUNDARY_MS},
        "params": {
            "window_days": 30,
            "z_in": 1.5,
            "z_out": 0.5,
            "min_hold_days": 3,
            "margin_pct": 30,
        },
        "perp": {"side": "short", "margin_pct": 30},
        "spot": {"token": "WBTC", "weight": "0.3000"},
        "signal": None,
        "equity": {
            "perp": None,
            "perp_at_ms": None,
            "perp_at": None,
            "spot": None,
            "spot_at_ms": None,
            "spot_at": None,
        },
    }
    doc.update(overrides)
    return doc


def _write(tmp_path: Path, doc: object) -> Path:
    path = tmp_path / "handoff-BTC.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return path


def _provider(path: Path, decision: DecisionConfig | None = None) -> FileTargetDecisionProvider:
    """A provider past ``__init__`` (which prices the position section): what ``request_decision`` reads."""
    provider = object.__new__(FileTargetDecisionProvider)
    provider._decision = decision or DecisionConfig()
    provider._target_path = path
    return provider


def _ask(provider, at: datetime = CYCLE):
    return provider.request_decision(DecisionInput(context=market_ctx(at)))


# --------------------------------------------------------------------------
# a fresh handoff is the leg it names, through the decision contract
# --------------------------------------------------------------------------


def test_a_short_handoff_is_a_set_target_short_at_its_margin(tmp_path, caplog):
    path = _write(tmp_path, _handoff())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        parsed = _ask(_provider(path))
    assert parsed.is_valid
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    assert parsed.decision.target_side is TargetSide.SHORT
    assert parsed.decision.requested_target_margin_pct == 30
    assert parsed.decision.confidence == D(1)
    # The rationale says where the target came from and what the coordinator did.
    assert "handoff-BTC.json" in parsed.decision.rationale
    assert "enter" in parsed.decision.rationale
    assert "no model asked" in parsed.decision.rationale
    # The raw response IS the contract text: the audit row shows what was parsed.
    assert json.loads(parsed.raw_response)["decision_mode"] == "set_target"
    assert "file target" in caplog.text and "short 30%" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_flat_handoff_is_a_set_target_flat_at_zero(tmp_path):
    doc = _handoff(action="exit", perp={"side": "flat", "margin_pct": 0})
    doc["position"] = {"side": "out", "entered_at_ms": None}
    doc["spot"] = {"token": "WBTC", "weight": "0"}
    parsed = _ask(_provider(_write(tmp_path, doc)))
    assert parsed.is_valid
    assert parsed.decision.target_side is TargetSide.FLAT
    assert parsed.decision.requested_target_margin_pct == 0


def test_unknown_keys_are_ignored_under_the_same_version(tmp_path):
    # The version policy (carry plan R4): fields may be added under version
    # 1 and a reader ignores what it does not know — top level and inside
    # the perp block alike.
    doc = _handoff(basis_annualized="0.05")
    doc["perp"] = {"side": "short", "margin_pct": 30, "venue": "hyperliquid"}
    parsed = _ask(_provider(_write(tmp_path, doc)))
    assert parsed.is_valid and parsed.decision.target_side is TargetSide.SHORT


def test_every_cycle_inside_the_day_acts_and_the_edges_are_the_documents(tmp_path):
    # as_of <= now < as_of + 1 day (carry README 「陳舊政策」): the boundary
    # itself acts, the next boundary does not, and the cycle just before the
    # boundary — the 23:50 coordinator run has already written the file —
    # holds until the day begins.
    provider = _provider(_write(tmp_path, _handoff()))
    inside = (
        BOUNDARY,
        BOUNDARY + timedelta(hours=20),
        BOUNDARY + HANDOFF_WINDOW - timedelta(seconds=1),
    )
    for at in inside:
        assert _ask(provider, at).decision.decision_mode is DecisionMode.SET_TARGET, at
    for at in (BOUNDARY - timedelta(seconds=1), BOUNDARY + HANDOFF_WINDOW):
        assert _ask(provider, at).decision.decision_mode is DecisionMode.MAINTAIN_CURRENT, at


# --------------------------------------------------------------------------
# anything unusable is a VALID maintain_current with the reason, and a WARNING
# --------------------------------------------------------------------------


def _assert_maintains(parsed, caplog, reason: str) -> None:
    assert parsed.is_valid, parsed.invalid_reason  # not a parse refusal
    assert parsed.decision.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert parsed.decision.target_side is None
    assert parsed.decision.requested_target_margin_pct is None
    assert parsed.decision.rationale.startswith("carry handoff unusable: ")
    assert reason in parsed.decision.rationale
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert reason in warnings[0].getMessage()
    assert "carry plan D5" in warnings[0].getMessage()


def test_a_missing_file_maintains_with_a_warning(tmp_path, caplog):
    provider = _provider(tmp_path / "absent.json")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "no handoff at")


def test_a_handoff_for_a_day_ago_maintains_with_a_warning(tmp_path, caplog):
    provider = _provider(_write(tmp_path, _handoff()))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider, BOUNDARY + timedelta(days=1, hours=3))
    _assert_maintains(parsed, caplog, "a day or more before this cycle")


def test_a_handoff_for_a_boundary_not_yet_reached_maintains(tmp_path, caplog):
    provider = _provider(_write(tmp_path, _handoff()))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider, BOUNDARY - timedelta(minutes=10))
    _assert_maintains(parsed, caplog, "has not reached")


def test_another_coins_handoff_maintains(tmp_path, caplog):
    provider = _provider(_write(tmp_path, _handoff(coin="ETH")))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "ETH's and this run trades BTC")


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ("{not json", "is not JSON"),
        ("[]", "not a JSON object but list"),
        (_handoff(version=2), "version 2 is not 1"),
        (_handoff(version="1"), "version '1' is not 1"),
        (_handoff(coin=""), "coin must be a symbol"),
        (_handoff(as_of_ms="1773532800000"), "as_of_ms must be a whole number"),
        (_handoff(as_of_ms=True), "as_of_ms must be a whole number"),
        (_handoff(perp="short"), "perp block must be an object"),
        (_handoff(perp={"side": "long", "margin_pct": 30}), "perp.side must be one of"),
        (_handoff(perp={"side": "short", "margin_pct": 0}), "contradicts perp.margin_pct 0"),
        (_handoff(perp={"side": "flat", "margin_pct": 30}), "contradicts perp.margin_pct 30"),
        (_handoff(perp={"side": "short", "margin_pct": 130}), "within 0..100"),
        (
            _handoff(perp={"side": "short", "margin_pct": "30"}),
            "perp.margin_pct must be a whole number",
        ),
        (_handoff(perp={"side": "short"}), "perp.margin_pct must be a whole number"),
    ],
)
def test_an_unreadable_or_contradictory_document_maintains_by_name(tmp_path, caplog, doc, reason):
    provider = _provider(_write(tmp_path, doc))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, reason)


def test_read_target_names_the_file_in_its_refusals(tmp_path):
    with pytest.raises(StaleTarget, match="absent.json"):
        read_target(tmp_path / "absent.json")
    with pytest.raises(StaleTarget, match="is not JSON"):
        read_target(_write(tmp_path, "{"))


def test_a_path_that_is_not_a_readable_file_is_stale_not_a_crash(tmp_path, caplog):
    # A directory where the file should be (a mount that came up empty, a
    # typo'd target_path) is an OSError on read: the same maintain-and-warn
    # as a missing file, never a traceback out of request_decision.
    provider = _provider(tmp_path)  # the directory itself
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "cannot read")


def test_an_as_of_beyond_the_epoch_range_is_stale_not_a_crash(tmp_path, caplog):
    # ``from_epoch_ms`` overflows on a value no instant answers to; the reader
    # turns that into a reason rather than letting it escape.
    provider = _provider(_write(tmp_path, _handoff(as_of_ms=10**22)))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "is not an instant")


# --------------------------------------------------------------------------
# one contract: the margin grid judges the file's leg as it judges a model's
# --------------------------------------------------------------------------


def test_an_off_grid_margin_fails_closed_through_the_parsers_own_tag(tmp_path, caplog):
    # Carry plan R3: the RUNBOOK keeps --margin-pct on the run's grid; when
    # it is not, the refusal is the one an LLM answer would get — the same
    # vocabulary the validation counters already read — not a stale target.
    decision = DecisionConfig(ai_target_margin_max_pct=100, target_margin_step_pct=5)
    doc = _handoff(perp={"side": "short", "margin_pct": 33})
    doc["params"]["margin_pct"] = 33
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(_provider(_write(tmp_path, doc), decision))
    assert not parsed.is_valid
    assert parsed.invalid_reason == "margin_off_step_grid"
    assert parsed.decision == TargetDecision.fail_closed()
    assert not caplog.records  # the file was fine; the grid said no


# --------------------------------------------------------------------------
# build_input is the shared half, and records that no model was asked
# --------------------------------------------------------------------------


def test_build_input_records_the_file_target_marker_as_the_model(tmp_path, monkeypatch):
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    as_of = datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc)
    ctx = _perp_ctx(as_of - timedelta(hours=1))
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))
    provider = _stub_provider(
        cls=FileTargetDecisionProvider,
        _payload_dir=tmp_path / "payloads",
        _target_path=tmp_path / "handoff.json",
    )

    decision_input = provider.build_input(coin="BTC", as_of=as_of)

    assert decision_input.model == FILE_TARGET_MODEL == "file-target"
    # The same artifact every provider writes: the replay refusal keys on the
    # ROW's model, and the payload stays self-describing.
    with open(decision_input.input_payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["context_shape"] == decision_input.context_shape
    assert "format_instructions" in payload


# --------------------------------------------------------------------------
# the factory picks the provider from the decision_source: block
# --------------------------------------------------------------------------


def _factory_kwargs(tmp_path: Path) -> dict:
    return {
        "db": None,
        "run_id": "carry-BTC-1",
        "coin": "BTC",
        "risk_cfg": RiskConfig(leverage=D(1), max_target_margin_pct=60),
        "decision_cfg": DecisionConfig(),
        "payload_dir": tmp_path / "payloads",
    }


def test_the_factory_builds_the_file_provider_without_touching_the_engine(tmp_path, monkeypatch):
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    def _no_engine(config):
        raise AssertionError("a file_target run must not build the engine config")

    monkeypatch.setattr(bridge_mod, "_build_engine_config", _no_engine)
    config = {
        "decision_source": {"provider": "file_target", "target_path": str(tmp_path / "h.json")}
    }
    provider = build_decision_provider(config, **_factory_kwargs(tmp_path))
    assert isinstance(provider, FileTargetDecisionProvider)
    assert provider._target_path == tmp_path / "h.json"
    assert provider._model == FILE_TARGET_MODEL
    # The real __init__ ran: the position section's pricing is derived here too.
    assert provider._max_pct == 60


def test_the_factory_defaults_to_the_engine_provider(tmp_path, monkeypatch):
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    monkeypatch.setattr(
        bridge_mod, "_build_engine_config", lambda config: ({"deep_think_llm": "m"}, [])
    )
    provider = build_decision_provider({}, **_factory_kwargs(tmp_path))
    assert isinstance(provider, EngineDecisionProvider)
    assert provider._model == "m"


def test_the_factory_looks_the_engine_class_up_at_call_time(tmp_path, monkeypatch):
    # What lets the CLI tests stub the engine provider on the module: the
    # factory must not bind the class early.
    class Sentinel:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", Sentinel)
    assert isinstance(build_decision_provider({}, **_factory_kwargs(tmp_path)), Sentinel)
