"""Tests for the helpers the subcommands share."""

from __future__ import annotations

import signal

import pytest

from contrib.hyperliquid_perp.cli import _common as common_mod


def test_require_agent_key_returns_the_key_or_prints_the_composed_refusal(capsys, monkeypatch):
    # Issue #126: the one agent-key refusal both signing entry points route
    # through. With the key set it returns it and prints nothing; without it
    # the message is assembled here — variable, the caller's why/remedy, and
    # the dotenv diagnosis for that SAME variable — so no caller can drop or
    # misdirect the suffix again (#82).
    require = common_mod._require_agent_key
    monkeypatch.setattr(common_mod, "dotenv_diagnosis", lambda var: f"DIAG[{var}]")
    monkeypatch.setenv("HYPERLIQUID_AGENT_KEY_MAINNET", "0x" + "ab" * 32)
    assert require("mainnet", remedy="x") == "0x" + "ab" * 32
    assert capsys.readouterr().err == ""

    monkeypatch.delenv("HYPERLIQUID_AGENT_KEY_MAINNET", raising=False)
    # The grammar is the helper's: the " but " joiner and the remedy's
    # terminating period are added here, whether or not the caller wrote one.
    assert require("mainnet", demanded_by="Y", remedy="do Z.") is None
    assert capsys.readouterr().err == (
        "error: HYPERLIQUID_AGENT_KEY_MAINNET is not set but Y — do Z. "
        "(DIAG[HYPERLIQUID_AGENT_KEY_MAINNET].)\n"
    )
    assert require("mainnet", remedy="do Z") is None
    assert capsys.readouterr().err == (
        "error: HYPERLIQUID_AGENT_KEY_MAINNET is not set — do Z. "
        "(DIAG[HYPERLIQUID_AGENT_KEY_MAINNET].)\n"
    )


def test_sigterm_shim_raises_keyboard_interrupt():
    # systemd/docker stop with SIGTERM; the handler must funnel it into the
    # KeyboardInterrupt shutdown-export path.
    with pytest.raises(KeyboardInterrupt):
        common_mod._raise_keyboard_interrupt(signal.SIGTERM, None)
