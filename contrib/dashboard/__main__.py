"""``python -m contrib.dashboard`` — hand argv to :mod:`.cli`.

Thin on purpose, like ``contrib.carry``'s.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
