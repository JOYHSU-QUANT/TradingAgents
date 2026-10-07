"""``python -m contrib.carry`` — hand argv to :mod:`.cli`.

Thin on purpose, like ``contrib.replay``'s: no legacy lane, no daemon.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
