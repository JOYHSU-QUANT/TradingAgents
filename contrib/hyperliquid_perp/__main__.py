"""``python -m contrib.hyperliquid_perp`` — the phase2-data §1.1 CLI entry.

Dispatches to :mod:`.cli` (``paper`` / ``export`` / ``validate``); any other
argv shape is delegated to the legacy :mod:`.main` unchanged, so the Phase 1
``--context-only`` and single-shot engine invocations keep working.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
