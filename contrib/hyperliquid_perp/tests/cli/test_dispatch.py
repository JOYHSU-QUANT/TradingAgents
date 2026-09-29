"""Tests for the entry point's dispatch between the subcommands and the legacy lane."""

from __future__ import annotations

import sys

import pytest

from contrib.hyperliquid_perp.cli import main as cli_main

# --------------------------------------------------------------------------
# dispatch: legacy delegation vs unknown-subcommand typos
# --------------------------------------------------------------------------


def test_unknown_bare_word_is_an_error_not_legacy_usage(capsys):
    # A subcommand typo must name the real subcommands under exit 1 — not fall
    # through to the legacy parser's usage (which never mentions them) under
    # exit 2 ("unexpected error").
    assert cli_main(["expot"]) == 1
    err = capsys.readouterr().err
    assert "unknown subcommand 'expot'" in err
    assert "paper" in err and "export" in err and "validate" in err


def test_cli_main_loads_dotenv_on_every_invocation(monkeypatch):
    # The subcommand entry loads .env as its first act — before dispatch and
    # before anything reads os.environ — so a key kept only in the repo-root
    # .env satisfies the paper startup checks (main.py's legacy path has the
    # companion ordering test in test_main.py).
    import contrib.hyperliquid_perp.cli as cli_mod

    calls = []
    monkeypatch.setattr(cli_mod, "load_dotenv_files", lambda: calls.append(True))
    assert cli_main(["expot"]) == 1  # even a subcommand typo went through the load
    assert calls == [True]


def test_empty_and_flag_style_argv_delegate_to_legacy(monkeypatch):
    # The Phase 1/2 compatibility promise: empty argv and flag invocations
    # (legacy accepts no positionals) flow to .main verbatim.
    from contrib.hyperliquid_perp import main as legacy_mod

    seen = []

    def fake_legacy(argv):
        seen.append(argv)
        return 7

    monkeypatch.setattr(legacy_mod, "main", fake_legacy)
    assert cli_main([]) == 7
    assert cli_main(["--context-only", "--coin", "BTC"]) == 7
    assert seen == [[], ["--context-only", "--coin", "BTC"]]


# The argv shapes the two entries split on, and which side each goes to.
# One table for both pins below: both entries route through
# ``common.entry_argv.is_legacy_argv``, and this is what says the two
# CALL SITES agree — the package entry must not import ``cli`` to borrow its
# answer (issue #221), so it is the table, not a shared import, that holds
# them together.
_ENTRY_ROUTES = [
    ([], "legacy"),
    (["--context-only", "--coin", "BTC"], "legacy"),
    (["-h"], "legacy"),
    (["export", "--run-id", "r"], "cli"),
    (["expot"], "cli"),  # a bare unknown word is cli's named error, not legacy usage
]


def test_the_package_entry_routes_every_argv_shape_like_cli_main(monkeypatch):
    # ``python -m contrib.hyperliquid_perp`` makes the legacy-vs-subcommand
    # split itself and only THEN imports one of the two; ``cli.main`` keeps the
    # same split for callers that reach it directly. Same table, same answers.
    import contrib.hyperliquid_perp.__main__ as entry_mod
    import contrib.hyperliquid_perp.cli as cli_mod
    from contrib.hyperliquid_perp import main as legacy_mod

    seen = []
    monkeypatch.setattr(legacy_mod, "main", lambda argv: seen.append(("legacy", argv)) or 7)
    monkeypatch.setattr(cli_mod, "main", lambda argv: seen.append(("cli", argv)) or 9)
    for argv, side in _ENTRY_ROUTES:
        seen.clear()
        assert entry_mod.main(list(argv)) == {"legacy": 7, "cli": 9}[side], argv
        assert seen == [(side, argv)], argv
    # cli.main's own split, against the same table: the legacy rows delegate
    # (7), the cli rows are handled in place and never reach the stub.
    monkeypatch.undo()
    monkeypatch.setattr(legacy_mod, "main", lambda argv: seen.append(("legacy", argv)) or 7)
    for argv, side in _ENTRY_ROUTES:
        seen.clear()
        try:
            rc = cli_main(list(argv))
        except SystemExit as exc:  # a subcommand's own argparse exit: handled in place
            rc = exc.code
        if side == "legacy":
            assert (rc, seen) == (7, [("legacy", argv)]), argv
        else:
            assert rc != 7 and seen == [], argv


def test_the_legacy_lane_leaves_the_cli_package_unimported_from_both_entries(
    monkeypatch, request
):
    """Issue #221 (after #197): ``--context-only`` is the keyless preview.

    ``.main`` stopped importing ``cli`` for one string in PR #220; the package
    entry then still reached ``.main`` THROUGH ``from .cli import main``, so
    the lighter lane existed only for operators who typed the longer module
    name. Pinned by evicting every ``cli`` module and watching the import
    system: an entry that imports the package puts it back in ``sys.modules``.
    (The eviction is scoped — monkeypatch restores the real modules after.)
    """
    import contrib.hyperliquid_perp.__main__ as entry_mod
    from contrib import hyperliquid_perp as package
    from contrib.hyperliquid_perp import main as legacy_mod

    def cli_modules():
        return sorted(m for m in sys.modules if m.startswith("contrib.hyperliquid_perp.cli"))

    # The positive control at the end re-imports the package, which rebinds
    # ``package.cli`` to a SECOND module tree; ``import a.b.c as m`` reads
    # that attribute before ``sys.modules``, so a later test would patch the
    # new tree while ``cli_main`` (this file's, from the old one) ran the real
    # thing. monkeypatch puts the ``sys.modules`` entries back; this puts the
    # attribute back beside them.
    original_cli = package.cli
    request.addfinalizer(lambda: setattr(package, "cli", original_cli))
    for name in cli_modules():
        monkeypatch.delitem(sys.modules, name)
    assert cli_modules() == []
    # The legacy main's own argparse ``--help`` is the cheapest full trip
    # through that entry — .env load, parse, exit 0 — with no config read.
    with pytest.raises(SystemExit) as excinfo:
        legacy_mod.main(["--context-only", "--help"])
    assert excinfo.value.code == 0
    assert cli_modules() == [], "the .main entry imported the cli package"
    with pytest.raises(SystemExit) as excinfo:
        entry_mod.main(["--context-only", "--help"])
    assert excinfo.value.code == 0
    assert cli_modules() == [], "the package entry imported the cli package on the legacy lane"
    # And the same entry DOES load it for a subcommand — the eviction above
    # was real, and this is the one lane that needs the package.
    entry_mod.main(["expot"])
    assert "contrib.hyperliquid_perp.cli" in cli_modules()


def test_cli_main_wrapper_maps_interrupt_and_unexpected_error(monkeypatch, capsys):
    # The documented top-level contract for the new subcommands: Ctrl-C -> 130,
    # anything unexpected -> 2 (distinct from named operator errors -> 1).
    import contrib.hyperliquid_perp.cli as cli_mod

    def interrupt(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod, "_cmd_validate", interrupt)
    assert cli_main(["validate", "--run-id", "r"]) == 130
    assert "interrupted" in capsys.readouterr().err

    def boom(argv):
        raise RuntimeError("wires crossed")

    monkeypatch.setattr(cli_mod, "_cmd_export", boom)
    assert cli_main(["export", "whatever"]) == 2
    err = capsys.readouterr().err
    assert "unexpected error" in err and "wires crossed" in err
