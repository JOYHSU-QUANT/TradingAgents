"""The file-target provider: the carry handoff read as the perp leg's decision."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.common.constants import (
    FILE_TARGET_MODEL,
    FILE_TARGET_PENDING_PREFIX,
    FILE_TARGET_UNUSABLE_PREFIX,
    MAX_EPOCH_MS,
    MIN_EPOCH_MS,
)
from contrib.hyperliquid_perp.common.digest import payload_digest
from contrib.hyperliquid_perp.common.sidecar import sidecar_path
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionMode,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.engine_bridge import EngineConfigError
from contrib.hyperliquid_perp.integration import decision_provider as decision_provider_mod
from contrib.hyperliquid_perp.integration.decision_provider import (
    EngineDecisionProvider,
    MarketContextProvider,
    build_decision_provider,
)
from contrib.hyperliquid_perp.integration.file_target_provider import (
    HANDOFF_SIDECAR_SUFFIX,
    HANDOFF_WINDOW,
    FileTarget,
    FileTargetConfigError,
    FileTargetDecisionProvider,
    StaleTarget,
    read_document,
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
# What the market context's ``as_of`` really is at every cycle in the fixtures:
# the last CLOSED candle's close, which for the first interval of the day is
# the day BEFORE the boundary (xx:59:59.999). Fixed here so every test asks
# with a context that would read "before the boundary" if the provider
# measured the window against it instead of the cycle clock.
LAST_CLOSE = BOUNDARY - timedelta(milliseconds=1)


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


def _ask(provider, at: datetime = CYCLE, payload_path: str | None = None):
    """One cycle at ``at``: the clock the driver's ``build_input`` would have stashed, a context at the last close."""
    provider._cycle_at = at
    return provider.request_decision(
        DecisionInput(
            context=market_ctx(LAST_CLOSE),
            input_payload_path=payload_path,
            input_payload_hash=None if payload_path is None else "sha256:0",
        )
    )


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


def test_the_window_is_measured_on_the_cycle_clock_not_the_candle_close(tmp_path):
    # as_of <= cycle clock < as_of + 1 day (carry README 「陳舊政策」). Every
    # ask here hands in a context whose as_of is the LAST CLOSE before the
    # boundary; a provider that read the window off that would call the whole
    # first interval of the day "not reached". The boundary itself acts, the
    # next boundary does not, and the minute before the boundary is pending.
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


def test_build_input_then_request_decision_acts_in_the_first_interval_after_midnight(
    tmp_path, monkeypatch
):
    # The production path end to end: the driver hands build_input the cycle
    # clock (00:07), the market context's as_of is the 23:59:59.999 close of
    # the day before, and request_decision must still act on today's document.
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    cycle = BOUNDARY + timedelta(minutes=7)
    ctx = _perp_ctx(LAST_CLOSE)
    monkeypatch.setattr(bridge_mod, "_build_context", lambda config, coin, **kw: (ctx, None))
    path = _write(tmp_path, _handoff())
    provider = _stub_provider(
        cls=FileTargetDecisionProvider, _payload_dir=tmp_path / "payloads", _target_path=path
    )

    decision_input = provider.build_input(coin="BTC", as_of=cycle)
    parsed = provider.request_decision(decision_input)

    assert decision_input.context.as_of == LAST_CLOSE  # the trap the clock stash avoids
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    assert parsed.decision.target_side is TargetSide.SHORT


def test_request_decision_without_a_build_input_has_no_clock_and_says_so(tmp_path):
    provider = _provider(_write(tmp_path, _handoff()))
    with pytest.raises(RuntimeError, match="before build_input"):
        provider.request_decision(DecisionInput(context=market_ctx(LAST_CLOSE)))


# --------------------------------------------------------------------------
# the document acted on is kept beside the payload
# --------------------------------------------------------------------------


def test_an_applicable_document_is_kept_beside_the_payload_with_its_digest(tmp_path):
    path = _write(tmp_path, _handoff())
    payload = tmp_path / "payloads" / "BTC-20260315T040700_000000Z.json"
    payload.parent.mkdir()
    payload.write_bytes(b"{}")
    parsed = _ask(_provider(path), payload_path=str(payload))
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    sidecar = sidecar_path(payload, HANDOFF_SIDECAR_SUFFIX)
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record["schema"] == 1
    assert record["path"] == str(path)
    assert record["digest"] == payload_digest(path.read_bytes())
    assert record["cycle_at"] == CYCLE.isoformat()
    assert record["handoff"] == _handoff()  # the document, whole, unknown keys included
    assert payload.read_bytes() == b"{}"  # the payload itself is never touched


def test_a_cycle_that_maintains_writes_no_sidecar(tmp_path, caplog):
    payload = tmp_path / "payloads" / "p.json"
    payload.parent.mkdir()
    payload.write_bytes(b"{}")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        _ask(_provider(_write(tmp_path, _handoff(coin="ETH"))), payload_path=str(payload))
    assert not sidecar_path(payload, HANDOFF_SIDECAR_SUFFIX).exists()


def test_no_payload_means_no_sidecar_and_still_a_decision(tmp_path):
    # The one-shot and test harnesses carry no payload path; the decision is
    # the same, there is just nowhere to put the record.
    parsed = _ask(_provider(_write(tmp_path, _handoff())))
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    assert not list(tmp_path.glob("*.handoff.json"))


# --------------------------------------------------------------------------
# anything unusable is a VALID maintain_current with the reason, and a WARNING
# --------------------------------------------------------------------------


def _assert_maintains(parsed, caplog, reason: str) -> None:
    assert parsed.is_valid, parsed.invalid_reason  # not a parse refusal
    assert parsed.decision.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert parsed.decision.target_side is None
    assert parsed.decision.requested_target_margin_pct is None
    assert parsed.decision.rationale.startswith(FILE_TARGET_UNUSABLE_PREFIX)
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


def test_a_handoff_for_a_boundary_not_yet_reached_is_pending_not_a_fault(tmp_path, caplog):
    # 23:50: the coordinator has written tomorrow's document; the cycle before
    # midnight maintains, at INFO, under the pending prefix — the report must
    # not count the normal schedule as a day the coordinator missed.
    provider = _provider(_write(tmp_path, _handoff()))
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        parsed = _ask(provider, BOUNDARY - timedelta(minutes=10))
    assert parsed.is_valid
    assert parsed.decision.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert parsed.decision.rationale.startswith(FILE_TARGET_PENDING_PREFIX)
    assert "has not reached" in parsed.decision.rationale
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("maintaining until the boundary" in r.getMessage() for r in caplog.records)


def test_a_handoff_more_than_a_day_ahead_is_a_fault_not_pending(tmp_path, caplog):
    # The coordinator writes at most the NEXT boundary's document; anything
    # further ahead is a clock that is off or a hand-written --as-of, and it
    # must not sit at INFO forever under the pending prefix.
    provider = _provider(_write(tmp_path, _handoff()))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider, BOUNDARY - HANDOFF_WINDOW - timedelta(seconds=1))
    _assert_maintains(parsed, caplog, "more than a day after this cycle")
    # ...while exactly one day ahead is still the schedule's lead.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        pending = _ask(provider, BOUNDARY - HANDOFF_WINDOW)
    assert pending.decision.rationale.startswith(FILE_TARGET_PENDING_PREFIX)
    assert not caplog.records


def test_another_coins_handoff_maintains(tmp_path, caplog):
    provider = _provider(_write(tmp_path, _handoff(coin="ETH")))
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "ETH's and this run trades BTC")


@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        ("{not json", "is not UTF-8 JSON"),
        ("[]", "not a JSON object but list"),
        (_handoff(version=2), "version 2 is not 1"),
        (_handoff(version="1"), "version '1' is not 1"),
        (_handoff(coin=""), "coin must be a symbol"),
        (_handoff(coin=7), "coin must be a symbol"),
        (_handoff(as_of_ms="1773532800000"), "as_of_ms must be a whole number"),
        (_handoff(as_of_ms=True), "as_of_ms must be a whole number"),
        (_handoff(as_of_ms=10**22), "is not an instant"),
        # 9999-12-31 decodes; the day AFTER it does not exist, and that is
        # caught at construction rather than inside check_applicable, where an
        # OverflowError would escape the StaleTarget net.
        (_handoff(as_of_ms=MAX_EPOCH_MS), "has no day after it"),
        (_handoff(as_of_ms=MIN_EPOCH_MS), "has no day after it"),  # nor a day before it
        (_handoff(perp="short"), "perp block must be an object"),
        (_handoff(perp={"side": "long", "margin_pct": 30}), "perp.side must be one of"),
        (_handoff(perp={"side": None, "margin_pct": 30}), "perp.side must be one of"),
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


def test_a_path_that_is_not_a_readable_file_is_stale_not_a_crash(tmp_path, caplog):
    # A directory where the file should be (a mount that came up empty) is an
    # OSError on read: the same maintain-and-warn as a missing file, never a
    # traceback out of request_decision.
    provider = _provider(tmp_path)  # the directory itself
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(provider)
    _assert_maintains(parsed, caplog, "cannot read")


def test_read_document_names_the_file_in_its_refusals(tmp_path):
    with pytest.raises(StaleTarget, match="absent.json"):
        read_document(tmp_path / "absent.json")
    with pytest.raises(StaleTarget, match="is not UTF-8 JSON"):
        read_document(_write(tmp_path, "{"))


# --------------------------------------------------------------------------
# the type holds its invariants, however it was built
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        # The one rule only a direct construction can break (the decoder's
        # from_epoch_ms is always aware) ...
        ({"as_of": datetime(2026, 3, 15)}, "must be timezone-aware"),
        # ... and one shared with the document path, to pin that the rules
        # live on the dataclass: the parametrize above reaches them through it.
        ({"side": "long", "margin_pct": 30}, "perp.side must be one of"),
        # ... and the one type rule the decoder also enforces, since a direct
        # construction could hand in a float or a bool.
        ({"margin_pct": 30.5}, "whole number"),
    ],
)
def test_a_file_target_built_directly_is_held_to_the_same_rules(kwargs, reason):
    # A caller that bypasses target_from_document cannot hand decision_text a
    # long perp: the dataclass refuses, not the decoder.
    fields = {"coin": "BTC", "as_of": BOUNDARY, "side": "short", "margin_pct": 30, "action": None}
    fields.update(kwargs)
    with pytest.raises(StaleTarget, match=reason):
        FileTarget(**fields)


def test_the_market_context_base_cannot_be_built_half_finished():
    # Abstract: a provider that forgets _model or request_decision fails at
    # construction, not after its first payload write.
    with pytest.raises(TypeError, match="abstract"):
        object.__new__(MarketContextProvider)

    class Forgetful(MarketContextProvider):
        def request_decision(self, decision_input):
            return None

    with pytest.raises(TypeError, match="_model"):
        object.__new__(Forgetful)


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
    payload = tmp_path / "payloads" / "p.json"
    payload.parent.mkdir()
    payload.write_bytes(b"{}")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        parsed = _ask(_provider(_write(tmp_path, doc), decision), payload_path=str(payload))
    assert not parsed.is_valid
    assert parsed.invalid_reason == "margin_off_step_grid"
    assert parsed.decision == TargetDecision.fail_closed()
    assert not caplog.records  # the file was fine; the grid said no
    # The document applied, so it is kept — presence means "read and
    # applicable", not "traded" (module docstring).
    assert sidecar_path(payload, HANDOFF_SIDECAR_SUFFIX).exists()


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
    assert provider._cycle_at == as_of  # the clock request_decision windows by
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


def _file_target_config(path: Path) -> dict:
    return {"decision_source": {"provider": "file_target", "target_path": str(path)}}


def test_the_factory_builds_the_file_provider_without_touching_the_engine(
    tmp_path, monkeypatch, caplog
):
    import contrib.hyperliquid_perp.engine_bridge as bridge_mod

    def _no_engine(config):
        raise AssertionError("a file_target run must not build the engine config")

    monkeypatch.setattr(bridge_mod, "_build_engine_config", _no_engine)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        provider = build_decision_provider(
            _file_target_config(tmp_path / "h.json"), **_factory_kwargs(tmp_path)
        )
    assert isinstance(provider, FileTargetDecisionProvider)
    assert provider._target_path == tmp_path / "h.json"
    assert provider._model == FILE_TARGET_MODEL
    # The real __init__ ran: the position section's pricing is derived here too.
    assert provider._max_pct == 60
    # The directory exists and the file does not yet: said once, not refused.
    assert len(caplog.records) == 1
    assert "does not exist yet" in caplog.records[0].getMessage()


def test_a_target_path_in_a_missing_directory_is_refused_at_construction(tmp_path):
    # A typo'd path would otherwise look exactly like a coordinator that never
    # ran; the refusal is an EngineConfigError, so the CLIs' startup handling
    # (exit 1 fresh, protection-only over live work) applies unchanged.
    path = tmp_path / "no-such-dir" / "h.json"
    with pytest.raises(FileTargetConfigError, match="does not exist") as excinfo:
        build_decision_provider(_file_target_config(path), **_factory_kwargs(tmp_path))
    assert isinstance(excinfo.value, EngineConfigError)
    assert str(path.parent) in str(excinfo.value)


def test_a_target_path_that_is_a_directory_is_refused_at_construction(tmp_path):
    # exists() is true and the parent is a directory, yet the coordinator could
    # never write it — without this check every cycle would warn "cannot read",
    # the very symptom the directory check exists to catch early.
    target = tmp_path / "handoff-BTC.json"
    target.mkdir()
    with pytest.raises(FileTargetConfigError, match="is a directory"):
        build_decision_provider(_file_target_config(target), **_factory_kwargs(tmp_path))


def test_a_filesystem_error_while_checking_the_path_is_the_same_startup_fault(
    tmp_path, monkeypatch
):
    # Path.is_dir()/exists() swallow only ENOENT-class errors; a dead mount
    # raises. A raw OSError out of the constructor would skip the CLIs'
    # EngineConfigError fork (protection-only over live work) and exit 2.
    import errno

    def _dead_mount(self):
        raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")

    monkeypatch.setattr(Path, "is_dir", _dead_mount)
    with pytest.raises(FileTargetConfigError, match="cannot be checked") as excinfo:
        build_decision_provider(
            _file_target_config(tmp_path / "h.json"), **_factory_kwargs(tmp_path)
        )
    assert isinstance(excinfo.value, EngineConfigError)
    assert "not connected" in str(excinfo.value)


def test_an_existing_target_file_is_built_quietly(tmp_path, caplog):
    path = _write(tmp_path, _handoff())
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        build_decision_provider(_file_target_config(path), **_factory_kwargs(tmp_path))
    assert not caplog.records


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
