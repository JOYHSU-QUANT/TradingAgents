"""``python -m contrib.hyperliquid_perp`` — the phase2-data §1.1 CLI entry.

Dispatches to :mod:`.cli` (``paper`` / ``export`` / ``validate``); empty argv
and flag-style invocations are delegated to the legacy :mod:`.main` unchanged,
so the Phase 1 ``--context-only`` and single-shot engine invocations keep
working. A bare unknown word is a subcommand typo — named error, exit 1.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
