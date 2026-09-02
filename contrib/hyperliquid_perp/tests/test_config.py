"""Tests for config loading and the wallet-address guard.

``wallet_address`` is the gate that decides whether ``--context-only`` attempts a
position read: anything empty, whitespace, or the example placeholder must read
as "no wallet configured" (``None``).
"""

from __future__ import annotations

import os

import pytest

from contrib.hyperliquid_perp.config import (
    _EXAMPLE,
    _WALLET_PLACEHOLDER,
    dotenv_diagnosis,
    load_config,
    load_dotenv_files,
    wallet_address,
)
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.risk_gate import (
    RiskConfig,
    validate_risk_decision_config,
)
from contrib.hyperliquid_perp.domains.perp.target_decision import DecisionConfig

from .conftest import config_text, doc_text


@pytest.mark.parametrize(
    "value",
    ["", "   ", _WALLET_PLACEHOLDER, None],
)
def test_wallet_address_unset_or_placeholder_is_none(value):
    assert wallet_address({"wallet_address": value}) is None


def test_wallet_address_missing_key_is_none():
    assert wallet_address({}) is None


def test_wallet_address_real_value_is_stripped():
    assert wallet_address({"wallet_address": "  0xABC123  "}) == "0xABC123"


def test_load_config_reads_example_by_default():
    # No local.yaml in a clean checkout -> falls back to the committed example.
    config = load_config()
    assert config["network"] == "mainnet"
    assert config["coins"] == ["BTC"]
    assert "market_data" in config


def test_example_yaml_risk_decision_blocks_validate():
    # Tuning edits the shipped example config in place (deadband width,
    # resize_min_confidence), and the cross-field invariants — e.g.
    # resize_min_confidence >= min_confidence — fail the process at load with
    # exit 1. Round-trip the real file through the real constructors (the same
    # sequence main.py runs) so a bad edit fails here instead of taking down
    # the paper service on its next deploy. Explicitly target the example file:
    # bare load_config() prefers a developer's gitignored local.yaml, which
    # would silently swap the file under test on any machine that has one.
    config = load_config(_EXAMPLE)
    risk_cfg = RiskConfig.from_dict(config.get("risk"))
    decision_cfg = DecisionConfig.from_dict(config.get("decision"))
    validate_risk_decision_config(risk_cfg, decision_cfg)


def test_example_yaml_ships_the_volume_profile_switched_off():
    # The switch ships OFF, so pulling the feature into a branch (or onto the
    # paper box) changes no prompt until an operator writes a window. If this
    # ever flips to a non-zero default, the flip — not the merge — is the
    # measurement point, and this test is where that gets noticed.
    config = load_config(_EXAMPLE)
    assert config["market_data"]["volume_profile_window_candles"] == 0


@pytest.mark.parametrize("window", [0, 12, 30, 200])
def test_load_config_accepts_a_legal_volume_profile_window(tmp_path, window):
    good = tmp_path / "vp.yaml"
    good.write_text(
        f"market_data:\n  candle_lookback: 200\n  volume_profile_window_candles: {window}\n",
        encoding="utf-8",
    )
    assert load_config(good)["market_data"]["volume_profile_window_candles"] == window


@pytest.mark.parametrize(
    ("value", "match"),
    [
        # Every one of these fails SILENTLY without the load-time check: the
        # prompt section just never appears, which looks exactly like the
        # feature being off on purpose. The type refusals are int_from_yaml's
        # (one converter for every integer key in the block), so a quoted
        # "30" is ACCEPTED as 30 — see the test below — while a bool (an int
        # subclass a bare int() would read as 1) and a fraction are not.
        ("30.5", "expected an integer"),
        ("true", "expected an integer, got a YAML boolean"),
        ("-1", "must be >= 0"),
        ("6", "must be 0 .off. or at least 12"),  # the literal "rolling 24h" at 4h candles
        ("11", "must be 0 .off. or at least 12"),
    ],
)
def test_load_config_rejects_a_bad_volume_profile_window(tmp_path, value, match):
    bad = tmp_path / "vp-bad.yaml"
    bad.write_text(
        f"market_data:\n  candle_lookback: 200\n  volume_profile_window_candles: {value}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=match):
        load_config(bad)


def test_market_data_integers_share_one_coercion(tmp_path):
    # Every integer key in the block goes through the same int_from_yaml, so
    # a quoted "30" reads as 30 for the profile window exactly as it always
    # did for candle_lookback — the block no longer has two integer dialects.
    path = tmp_path / "vp-quoted.yaml"
    path.write_text(
        'market_data:\n  candle_lookback: "200"\n  volume_profile_window_candles: "30"\n'
        "  funding_zscore_window_days: 14.0\n",
        encoding="utf-8",
    )
    assert MarketDataConfig.from_dict(load_config(path)["market_data"]) == MarketDataConfig(
        candle_lookback=200, volume_profile_window_candles=30, funding_zscore_window_days=14
    )


def test_load_config_rejects_an_unknown_market_data_key(tmp_path):
    # Issue #96: the block used to have no parser, so a typo'd key fell back
    # to its default in silence — for this key that is the volume profile
    # switched off, indistinguishable from an operator's deliberate 0; for
    # funding_zscore_window_days it is a z-score over the wrong window that
    # still looks like a normal number. Rejected like the other four blocks.
    bad = tmp_path / "md-typo.yaml"
    bad.write_text("market_data:\n  volume_profile_window_candels: 30\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid market_data: config") as exc_info:
        load_config(bad)
    assert "unknown config key(s): 'volume_profile_window_candels'" in str(exc_info.value)


def test_a_legal_market_data_block_loads_byte_for_byte(tmp_path):
    # Loading VALIDATES the block; it does not rewrite it. The drift check and
    # the genesis snapshot compare the raw YAML dict, so a loader that
    # normalised (say) "200" to 200 or filled in defaults would turn an
    # unchanged config into a params-drift signal on the next --resume.
    path = tmp_path / "md-legal.yaml"
    path.write_text(
        "market_data:\n"
        '  candle_interval: "1h"\n'
        "  candle_lookback: 100\n"
        "  funding_zscore_window_days: 14\n"
        "  volume_profile_window_candles: 24\n",
        encoding="utf-8",
    )
    assert load_config(path)["market_data"] == {
        "candle_interval": "1h",
        "candle_lookback": 100,
        "funding_zscore_window_days": 14,
        "volume_profile_window_candles": 24,
    }


def test_the_example_yaml_market_data_block_is_the_parsers_defaults():
    # The example is the documented default config; the dataclass is the
    # default the loader validates against and the fetch uses when a key is
    # absent. A drift between the two would let a config copied from the
    # example behave differently from one that omits the block.
    config = load_config(_EXAMPLE)
    assert MarketDataConfig.from_dict(config["market_data"]) == MarketDataConfig()


@pytest.mark.parametrize(
    ("line", "match"),
    [
        # The interval is the fetch's and the guard's vocabulary; a mis-cased
        # value used to survive the load and raise a bare ValueError from
        # inside the market fetch.
        ('  candle_interval: "4H"', "'market_data.candle_interval'.*unsupported candle interval"),
        ("  candle_interval: 4", "config key 'candle_interval': expected a string"),
        # A lookback that is not a number at all is candle_lookback's own
        # problem — and now it is named as such at load, not at the fetch.
        ("  candle_lookback: not-a-number", "config key 'candle_lookback': expected an integer"),
        ("  candle_lookback: true", "config key 'candle_lookback': expected an integer"),
        ("  candle_lookback: 20.5", "config key 'candle_lookback': expected an integer"),
        ("  candle_lookback: 0", "'market_data.candle_lookback' must be >= 1"),
        # A sub-1-day window keeps no funding points and degrades the z-score
        # to None — the same refusal PerpMarketContext makes, moved to load.
        (
            "  funding_zscore_window_days: 0",
            "'market_data.funding_zscore_window_days' must be >= 1",
        ),
        ("  funding_zscore_window_days: 1.5", "config key 'funding_zscore_window_days'"),
    ],
)
def test_load_config_rejects_a_bad_market_data_value(tmp_path, line, match):
    bad = tmp_path / "md-bad.yaml"
    bad.write_text(f"market_data:\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"invalid market_data: config — {match}"):
        load_config(bad)


def test_load_config_rejects_a_window_wider_than_the_candle_lookback(tmp_path):
    # A window the fetch can never fill would be skipped on every cycle.
    bad = tmp_path / "vp-wide.yaml"
    bad.write_text(
        "market_data:\n  candle_lookback: 20\n  volume_profile_window_candles: 30\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceeds 'market_data.candle_lookback'"):
        load_config(bad)


@pytest.mark.parametrize("lookback", ["20.0", '"20"', "20"])
def test_window_cross_check_reads_the_lookback_after_int_coercion(tmp_path, lookback):
    # int_from_yaml accepts the float-integral and quoted spellings, so the
    # cross-check sees 20 for every one of them — comparing the raw YAML value
    # would wave `20.0` through and skip the profile on every cycle in silence.
    bad = tmp_path / f"vp-coerce-{lookback.strip(chr(34))}.yaml"
    bad.write_text(
        f"market_data:\n  candle_lookback: {lookback}\n  volume_profile_window_candles: 30\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceeds 'market_data.candle_lookback'"):
        load_config(bad)


def test_an_uncoercible_lookback_is_named_rather_than_blamed_on_the_window(tmp_path):
    # A lookback that is not a number at all is candle_lookback's own problem.
    # It used to skip the cross-check and fail at the fetch; now it is refused
    # at load — but the refusal must name candle_lookback, not the profile
    # window, or the operator is sent to the wrong line.
    path = tmp_path / "vp-junk-lookback.yaml"
    path.write_text(
        "market_data:\n  candle_lookback: not-a-number\n  volume_profile_window_candles: 30\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="config key 'candle_lookback'") as exc_info:
        load_config(path)
    assert "volume_profile_window_candles" not in str(exc_info.value)


@pytest.mark.parametrize("lookback_line", ("  candle_lookback:\n", ""))
def test_volume_profile_window_is_checked_against_the_same_lookback_default(
    tmp_path, lookback_line
):
    # ``candle_lookback`` absent or blank means the MarketDataConfig field
    # default (200) — the same object the fetch reads — so the cross-check and
    # the fetch cannot disagree about which windows are legal. 200 must pass,
    # 201 must not. Both forms, because config_overrides treats a present-
    # but-null key and an absent key alike, and that "alike" is the contract
    # being pinned.
    for window, ok in ((200, True), (201, False)):
        path = tmp_path / f"vp-default-{window}.yaml"
        path.write_text(
            f"market_data:\n{lookback_line}  volume_profile_window_candles: {window}\n",
            encoding="utf-8",
        )
        if ok:
            assert load_config(path)["market_data"]["volume_profile_window_candles"] == 200
        else:
            with pytest.raises(ValueError, match="exceeds 'market_data.candle_lookback'"):
                load_config(path)


def test_load_config_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_rejects_unknown_top_level_key(tmp_path):
    # A typo'd block name (`riks:`) would otherwise silently drop the whole
    # risk: block and trade at default caps — reject it loudly instead.
    bad = tmp_path / "typo.yaml"
    bad.write_text("riks:\n  leverage: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown top-level config key"):
        load_config(bad)


def test_load_config_rejects_non_mapping(tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(bad)


@pytest.mark.parametrize("text", ["market_data: 5\n", "engine:\n  - a\n"])
def test_load_config_rejects_non_mapping_block(tmp_path, text):
    # A container block that isn't a mapping would blow up deep in the run
    # (`5.get(...)`) instead of a clean exit-1 — reject it at load time.
    bad = tmp_path / "block.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(bad)


def test_load_config_drops_blank_top_level_keys(tmp_path):
    # ``market_data:`` with nothing after it (a normal state when an operator
    # comments out a block's contents) parses to None, not a missing key. It is
    # treated exactly like absent — dropped — so every consumer's default applies
    # instead of crashing on ``None.get(...)`` deep in the run.
    blank = tmp_path / "blank.yaml"
    blank.write_text("network:\nmarket_data:\nengine:\ncoins: [BTC]\n", encoding="utf-8")
    config = load_config(blank)
    assert "network" not in config
    assert "market_data" not in config
    assert "engine" not in config
    assert config["coins"] == ["BTC"]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("network: mainet\n", "'network' must be"),
        ("network: 5\n", "'network' must be"),
        ("network_timeout_s: abc\n", "'network_timeout_s' must be a number"),
        ("network_timeout_s: [30]\n", "'network_timeout_s' must be a number"),
        ("wallet_address: 123\n", "'wallet_address' must be a string"),
    ],
)
def test_load_config_rejects_bad_phase1_values(tmp_path, text, match):
    # These values are consumed by the Phase-1 client deep inside the run; a bad
    # value there surfaces as an exit-2 traceback instead of a named config error.
    # Rejecting at load keeps operator typos in the CONFIG_LOAD_ERRORS lane.
    bad = tmp_path / "value.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_config(bad)


def test_the_network_refusal_names_every_legal_network(tmp_path):
    # The message enumerates the set the check reads, so it cannot lag a new
    # member the way a hand-typed "'mainnet' or 'testnet'" did (issue #102).
    from contrib.hyperliquid_perp.common.constants import LEGAL_NETWORKS

    bad = tmp_path / "value.yaml"
    bad.write_text("network: mainet\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_config(bad)
    assert f"must be one of {list(LEGAL_NETWORKS)}" in str(caught.value)


def test_load_config_accepts_phase1_value_spellings_the_client_accepts(tmp_path):
    # Mixed-case network and a numeric string timeout are both legal downstream
    # (sdk_client strips/lowers and float()s) — load_config must not be stricter
    # than the consumer it fronts.
    good = tmp_path / "good.yaml"
    good.write_text(
        'network: TestNet\nnetwork_timeout_s: "15"\nwallet_address: "0xabc"\n',
        encoding="utf-8",
    )
    assert load_config(good)["network"] == "TestNet"


@pytest.mark.parametrize(
    "text",
    [
        'engine:\n  structured_output: "false"\n',  # quoted string — truthy!
        "engine:\n  structured_output: 1\n",
        "engine:\n  structured_output: [true]\n",
    ],
)
def test_load_config_rejects_non_bool_structured_output(tmp_path, text):
    # A quoted "false" parses as a truthy string: it would ride through
    # _build_engine_config untouched and silently re-enable structured output —
    # the exact all-cycles-invalid_output incident the key exists to prevent.
    bad = tmp_path / "structured.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="engine.structured_output"):
        load_config(bad)


def test_load_config_accepts_bool_structured_output(tmp_path):
    good = tmp_path / "structured-ok.yaml"
    good.write_text("engine:\n  structured_output: true\n", encoding="utf-8")
    assert load_config(good)["engine"]["structured_output"] is True


def test_load_config_accepts_list_selected_analysts(tmp_path):
    good = tmp_path / "analysts-ok.yaml"
    good.write_text("engine:\n  selected_analysts: [market, news]\n", encoding="utf-8")
    assert load_config(good)["engine"]["selected_analysts"] == ["market", "news"]


@pytest.mark.parametrize(
    "text",
    [
        "engine:\n  max_completion_tokens: 0\n",
        "engine:\n  max_completion_tokens: -1\n",
        "engine:\n  max_completion_tokens: true\n",  # bool is an int subclass
        "engine:\n  max_completion_tokens: 8192.5\n",
        'engine:\n  max_completion_tokens: "8k"\n',
    ],
)
def test_load_config_rejects_bad_max_completion_tokens(tmp_path, text):
    # Anything that is not a positive integer is rejected at load: junk would
    # ride the ``or`` fallback into the LLM client as an illegal cap, and
    # 0/negative would re-open the uncapped path the key exists to close
    # (issue #177).
    bad = tmp_path / "cap.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="engine.max_completion_tokens"):
        load_config(bad)


@pytest.mark.parametrize(
    "text",
    [
        "engine:\n  max_completion_tokens: 8192\n",
        # A quoted integer is a legal spelling through the shared coercion
        # seam (unlike a quoted bool there is no inversion hazard); it int()s
        # cleanly at consumption.
        'engine:\n  max_completion_tokens: "8192"\n',
    ],
)
def test_load_config_accepts_positive_max_completion_tokens(tmp_path, text):
    # Both spellings normalise to int at load, so the value's type does not
    # follow its quoting all the way down to the LLM client.
    good = tmp_path / "cap-ok.yaml"
    good.write_text(text, encoding="utf-8")
    assert load_config(good)["engine"]["max_completion_tokens"] == 8192


def test_the_example_config_and_setup_doc_quote_the_completion_cap_default():
    """The ``8192`` in the example YAML and SETUP.md, derived not retyped.

    ``_DEFAULT_MAX_COMPLETION_TOKENS`` is the single declaration (the example
    ships the key commented out so a copied local.yaml cannot pin yesterday's
    number). Raising it would otherwise leave both prose sites telling the
    operator a cap the code no longer applies, with the suite green — the
    drift shape issue #102 installed these pins for.
    """
    from contrib.hyperliquid_perp.engine_bridge import _DEFAULT_MAX_COMPLETION_TOKENS

    cap = _DEFAULT_MAX_COMPLETION_TOKENS
    assert f"# max_completion_tokens: {cap}" in config_text()
    assert f"perp 預設 {cap}" in doc_text("SETUP.md")


@pytest.mark.parametrize(
    "text",
    [
        "indicators: [rsi14]\n",  # typo'd name — the classic
        "indicators: [rsi_14, atr14]\n",  # one good, one typo'd
        "indicators: [5]\n",  # non-string junk
        "indicators: [[rsi_14]]\n",  # unhashable junk must not TypeError
        # Typo on an otherwise-legal config (regime trio complete): the
        # unknown-name check must fire on its own, not only on configs the
        # regime-trio check would also reject.
        "indicators: [atr_14, ema_20, ema_50, macd2]\n",
    ],
)
def test_load_config_rejects_unknown_indicator_names(tmp_path, text):
    # List shape alone lets a typo'd element load as a permanently-None
    # indicator that every context guard skips (unknown names zero the warm-up
    # threshold and are filtered out of the all-dead guard) — reject unknown
    # names at load instead, naming the offender and the supported set.
    bad = tmp_path / "inds.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown indicator name"):
        load_config(bad)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "indicators: [rsi_14, ema_20, ema_50, atr_14]\n",
            ["rsi_14", "ema_20", "ema_50", "atr_14"],
        ),
        # The three regime names alone are a legal minimal set.
        ("indicators: [atr_14, ema_20, ema_50]\n", ["atr_14", "ema_20", "ema_50"]),
        # An explicit empty list is a deliberate "no indicators" choice
        # (indicator_vocab.indicator_names honours it); element validation must not reject it.
        ("indicators: []\n", []),
    ],
)
def test_load_config_accepts_valid_indicators(tmp_path, text, expected):
    good = tmp_path / "inds.yaml"
    good.write_text(text, encoding="utf-8")
    assert load_config(good)["indicators"] == expected


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        # Each regime name individually absent — the error names the offender.
        ("indicators: [rsi_14, ema_20, ema_50]\n", "'atr_14'"),
        ("indicators: [rsi_14, ema_50, atr_14]\n", "'ema_20'"),
        ("indicators: [rsi_14, ema_20, atr_14]\n", "'ema_50'"),
        # Multiple absent: every missing name lands in one message.
        ("indicators: [rsi_14, atr_14]\n", "'ema_20', 'ema_50'"),
        ("indicators: [macd]\n", "'atr_14', 'ema_20', 'ema_50'"),
    ],
)
def test_load_config_rejects_indicators_missing_regime_names(tmp_path, text, missing):
    # A non-empty list missing ANY of atr_14/ema_20/ema_50 can never trade:
    # classify_regime silently defaults to RANGING without all three, so the
    # regime guard refuses every cycle — fail at load, not as an endless
    # daemon retry ladder.
    bad = tmp_path / "no-regime.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=f"'indicators' must include {missing}"):
        load_config(bad)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("coins: BTC\n", "coins"),
        ("indicators: rsi_14\n", "indicators"),
        # Nested twin: list() in _build_engine_config would explode a bare
        # string into per-character bogus analyst keys that only detonate
        # deep inside build_graph (in the daemon: an endless retry ladder).
        ("engine:\n  selected_analysts: market\n", "engine.selected_analysts"),
    ],
)
def test_load_config_rejects_non_list_container(tmp_path, text, key):
    # A scalar would silently resolve to per-character values downstream
    # (`"BTC"[0]`; per-character indicator names that zero the warm-up gate) —
    # reject it as a config error instead.
    bad = tmp_path / f"{key}.yaml"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=f"'{key}' must be a list"):
        load_config(bad)


# --------------------------------------------------------------------------
# load_dotenv_files — repo-root .env must satisfy the CLI startup key checks
# --------------------------------------------------------------------------


def test_load_dotenv_files_reads_env_from_cwd(tmp_path, monkeypatch):
    # The engine package loads .env on import, but the CLIs check
    # OPENROUTER_API_KEY before that lazy import — so this helper must find the
    # same file (find_dotenv walks up from the CWD) on its own.
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    load_dotenv_files()

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-from-dotenv"


def test_load_dotenv_files_never_overrides_exported_vars(tmp_path, monkeypatch):
    # Same contract as tradingagents/__init__: an exported variable always wins
    # over the file, so a shell override for one run cannot be clobbered.
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-exported")

    load_dotenv_files()

    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-exported"


def test_load_dotenv_files_reads_env_enterprise(tmp_path, monkeypatch):
    # Both upstream files are mirrored; .env.enterprise is the second load.
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / ".env.enterprise").write_text("HL_ENTERPRISE_MARKER=yes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HL_ENTERPRISE_MARKER", raising=False)

    load_dotenv_files()

    assert os.environ["HL_ENTERPRISE_MARKER"] == "yes"


def test_load_dotenv_files_warns_and_continues_on_undecodable_file(tmp_path, monkeypatch, capsys):
    # PowerShell's bare `>>` redirection writes UTF-16: the loader must degrade
    # to env-vars-only with a stderr warning — it runs as the first statement of
    # both CLI entry points, before any exit-code mapping exists, so a raw
    # UnicodeDecodeError here would break the named-exit contract.
    (tmp_path / ".env").write_bytes("OPENROUTER_API_KEY=sk\n".encode("utf-16"))
    monkeypatch.chdir(tmp_path)

    load_dotenv_files()  # must not raise

    err = capsys.readouterr().err
    assert "could not read" in err
    assert "UTF-8" in err


def test_dotenv_diagnosis_distinguishes_missing_file_and_missing_key(tmp_path, monkeypatch):
    # The key-check failure messages lean on these wordings to tell the
    # operator whether the problem is the file's location or its contents.
    monkeypatch.chdir(tmp_path)
    assert "no .env or .env.enterprise found" in dotenv_diagnosis("OPENROUTER_API_KEY")

    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")
    assert "does not set OPENROUTER_API_KEY" in diagnosis


def test_dotenv_diagnosis_names_unreadable_file_and_empty_export(tmp_path, monkeypatch):
    # The remaining two operator situations: an undecodable file (the loader
    # already warned, the diagnosis repeats the cause next to the abort), and
    # the subtle override=False trap — an exported *empty* var blocks the
    # file's value, so "but my .env sets it!" needs its own wording.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes("OPENROUTER_API_KEY=sk\n".encode("utf-16"))
    assert "could not read" in dotenv_diagnosis("OPENROUTER_API_KEY")

    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-x\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert "exported empty" in dotenv_diagnosis("OPENROUTER_API_KEY")


def test_dotenv_diagnosis_covers_env_enterprise(tmp_path, monkeypatch):
    # The loader reads .env AND .env.enterprise: the diagnosis must model the
    # same two-file set, or an enterprise-file-only setup gets told "no .env
    # found" moments after the loader read one.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.enterprise").write_text("OTHER=1\n", encoding="utf-8")
    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")
    assert ".env.enterprise" in diagnosis
    assert "does not set OPENROUTER_API_KEY" in diagnosis

    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")
    assert "neither sets OPENROUTER_API_KEY" in diagnosis

    (tmp_path / ".env.enterprise").write_text("OPENROUTER_API_KEY=sk-e\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert "exported empty" in dotenv_diagnosis("OPENROUTER_API_KEY")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env.enterprise").write_bytes("OPENROUTER_API_KEY=sk\n".encode("utf-16"))
    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")
    assert ".env.enterprise" in diagnosis
    assert "could not read" in diagnosis


def test_dotenv_scan_failure_degrades_not_raises(monkeypatch, capsys):
    # find_dotenv(usecwd=True) calls os.getcwd(), which raises OSError when the
    # working directory was deleted under a long-lived daemon — both functions
    # must extend their degradation contract to the scan, not just the read.
    import dotenv

    def _raise(*args, **kwargs):
        raise OSError("[Errno 2] no such file or directory")

    monkeypatch.setattr(dotenv, "find_dotenv", _raise)

    load_dotenv_files()  # must not raise
    assert "could not read" in capsys.readouterr().err

    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")
    assert "could not scan" in diagnosis


def test_load_dotenv_files_corrupt_env_does_not_suppress_enterprise(tmp_path, monkeypatch, capsys):
    # The per-file try exists precisely so a corrupt .env degrades only itself:
    # the healthy .env.enterprise after it must still load. Guards against the
    # plausible refactor of hoisting the try/except out of the loop (or breaking
    # out of it on failure), which every other test would miss.
    (tmp_path / ".env").write_bytes("OPENROUTER_API_KEY=sk\n".encode("utf-16"))
    (tmp_path / ".env.enterprise").write_text("HL_ENTERPRISE_MARKER=yes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HL_ENTERPRISE_MARKER", raising=False)

    load_dotenv_files()  # must not raise

    assert os.environ["HL_ENTERPRISE_MARKER"] == "yes"
    assert "could not read" in capsys.readouterr().err


def test_load_dotenv_files_scan_failure_on_one_file_does_not_suppress_other(
    tmp_path, monkeypatch, capsys
):
    # The read half of the degradation contract has the mixed-scenario test
    # above; this is the scan half's counterpart — a find_dotenv failure on
    # .env must degrade only that file, not break/return past .env.enterprise.
    import dotenv

    real_find = dotenv.find_dotenv

    def _selective(name, *args, **kwargs):
        if name == ".env":
            raise OSError("scan failed for .env")
        return real_find(name, *args, **kwargs)

    monkeypatch.setattr(dotenv, "find_dotenv", _selective)
    (tmp_path / ".env.enterprise").write_text("HL_ENTERPRISE_MARKER=yes\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HL_ENTERPRISE_MARKER", raising=False)

    load_dotenv_files()  # must not raise

    assert os.environ["HL_ENTERPRISE_MARKER"] == "yes"
    assert "could not read" in capsys.readouterr().err


def test_dotenv_functions_degrade_without_python_dotenv(tmp_path, monkeypatch, capsys):
    # The optional-dependency branch: without python-dotenv the loader is a
    # silent no-op (env-vars-only operation, same as upstream) and the
    # diagnosis says so — neither may raise. Poisoning sys.modules makes the
    # in-function ``from dotenv import ...`` raise ImportError.
    import sys

    monkeypatch.setitem(sys.modules, "dotenv", None)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-x\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    load_dotenv_files()  # must not raise
    assert "OPENROUTER_API_KEY" not in os.environ  # nothing was loaded
    assert capsys.readouterr().err == ""  # and silently so

    assert "python-dotenv is not importable" in dotenv_diagnosis("OPENROUTER_API_KEY")


def test_dotenv_diagnosis_blames_earlier_empty_file_not_export(tmp_path, monkeypatch):
    # A blank assignment in .env (``OPENROUTER_API_KEY=``) loads first and
    # blocks .env.enterprise's real key exactly like an exported empty value
    # would — but no export exists, so the "exported empty" wording would send
    # the operator to the wrong place. The diagnosis must blame the fixable
    # line in the earlier file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    (tmp_path / ".env.enterprise").write_text("OPENROUTER_API_KEY=sk-e\n", encoding="utf-8")

    diagnosis = dotenv_diagnosis("OPENROUTER_API_KEY")

    assert ".env.enterprise sets OPENROUTER_API_KEY" in diagnosis  # the real key…
    assert "sets it to an empty value" in diagnosis  # …lost to the blank line
    assert "exported" not in diagnosis  # and the export is not blamed


def test_the_setup_doc_quotes_the_volume_profile_floor_the_loader_enforces():
    """SETUP.md's ``12`` is the loader's floor, derived rather than retyped.

    The doc states the legal range, the too-short band, and the loader's own
    refusal message — four spellings of one constant, none of them tied to it.
    Lowering MIN_VOLUME_PROFILE_WINDOW would have left every one wrong with the
    suite green, and the only symptom is a config the operator was told is legal
    being refused at load (issue #100). Same shape as the §20.3 pins in
    ``tests/cli/test_smoke.py``.
    """
    from contrib.hyperliquid_perp.common.constants import MIN_VOLUME_PROFILE_WINDOW

    setup = doc_text("SETUP.md")
    # The legal range, and the band below it the loader names as too short.
    assert f"`{MIN_VOLUME_PROFILE_WINDOW}`–`candle_lookback`" in setup
    assert f"1–{MIN_VOLUME_PROFILE_WINDOW - 1}" in setup
    # The loader's refusal message, quoted verbatim in the troubleshooting table.
    assert f"or at least {MIN_VOLUME_PROFILE_WINDOW}" in setup
    # And the "it is on" side of the same threshold, one row further down.
    assert f"視窗 ≥ {MIN_VOLUME_PROFILE_WINDOW}" in setup


def test_the_example_config_quotes_the_market_data_defaults_the_loader_enforces():
    """The example YAML's ``market_data`` comments, derived rather than retyped.

    ``candle_lookback: 200`` is the field default (PR #125 made the field its
    single declaration), and the volume-profile comment restates the loader's
    floor. The SETUP pin above guards the doc; nothing guarded the file an
    operator actually copies (issue #102).
    """
    from contrib.hyperliquid_perp.common.constants import MIN_VOLUME_PROFILE_WINDOW

    example = config_text()
    assert f"candle_lookback: {MarketDataConfig().candle_lookback} " in example
    assert f"Must be 0 or >= {MIN_VOLUME_PROFILE_WINDOW}, and <= candle_lookback" in example
