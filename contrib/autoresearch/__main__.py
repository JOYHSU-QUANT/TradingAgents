"""``python -m contrib.autoresearch`` — hand argv to :mod:`.cli`.

Thin on purpose. The perp package's entry splits legacy flag-style argv from
subcommands before importing either half, because reaching its subcommands
loads a daemon surface a preview run has no use for. This package has no
legacy lane and no daemon, so there is nothing to split: one import, one
dispatch, and the Hyperliquid SDK still stays out of the store-only commands
because :mod:`.upstream` imports the reader inside the call that builds it.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
