"""``python -m contrib.discovery`` — hand argv to :mod:`.cli`.

Thin on purpose, like the research package's and replay's: no legacy lane
and no daemon, so there is nothing to split before importing the CLI.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
