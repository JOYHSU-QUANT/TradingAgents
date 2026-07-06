"""SQLite persistence for Phase 2 paper trading (source of truth, phase2-data.md).

The store is the single source of truth for restart recovery, dedup and
accounting replay. Submodules:

- :mod:`.schema` — table DDL and migration list;
- :mod:`.db` — connection, transaction boundary, migration runner;
- :mod:`.repository` — typed insert / update / query helpers;
- :mod:`.models` — the mutable ``current_*`` state dataclasses;
- :mod:`.ids` — deterministic dedup / exactly-once keys.
"""

from __future__ import annotations

from .db import Database, apply_migrations, connect
from .schema import SCHEMA_VERSION

__all__ = ["Database", "SCHEMA_VERSION", "apply_migrations", "connect"]
