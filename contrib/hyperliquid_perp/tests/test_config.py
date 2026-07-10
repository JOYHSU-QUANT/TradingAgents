"""Tests for config loading and the wallet-address guard.

``wallet_address`` is the gate that decides whether ``--context-only`` attempts a
position read: anything empty, whitespace, or the example placeholder must read
as "no wallet configured" (``None``).
"""

from __future__ import annotations

import os

import pytest

from contrib.hyperliquid_perp.config import (
    _WALLET_PLACEHOLDER,
    dotenv_diagnosis,
    load_config,
    load_dotenv_files,
    wallet_address,
)


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


def test_load_config_rejects_non_list_coins(tmp_path):
    # `coins: BTC` (scalar, not a list) would otherwise silently resolve to the
    # first character "B" — reject it as a config error instead.
    bad = tmp_path / "coins.yaml"
    bad.write_text("coins: BTC\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'coins' must be a list"):
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
