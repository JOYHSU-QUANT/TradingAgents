"""``python -m contrib.hyperliquid_perp`` — the phase2-data §1.1 CLI entry.

Dispatches to :mod:`.cli` (``paper`` / ``export`` / ``validate`` / ``live`` /
``live-smoke`` / ``safe-mode``); empty argv and flag-style invocations are
delegated to the legacy :mod:`.main` unchanged, so the Phase 1
``--context-only`` and single-shot engine invocations keep working. A bare
unknown word is a subcommand typo — named error, exit 1 (:mod:`.cli` says so).

The split is made HERE, before either module is imported, and not by handing
every argv to :func:`.cli.main` (which makes the same split, through the same
:func:`.common.entry_argv.is_legacy_argv`, for its direct callers):
``--context-only`` is the keyless preview an operator runs before a deploy,
and importing the ``cli`` package to reach it would load the whole daemon
surface — every subcommand module, the exchange client, the store — for one
string comparison (issue #221). Both entries therefore leave
``contrib.hyperliquid_perp.cli`` unimported on the legacy lane; the legacy
:func:`.main.main` loads ``.env`` itself, so nothing :func:`.cli.main` did
before delegating is skipped.
"""

from __future__ import annotations

import sys

from .common.entry_argv import is_legacy_argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if is_legacy_argv(argv):
        from .main import main as legacy_main

        return legacy_main(argv)
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
