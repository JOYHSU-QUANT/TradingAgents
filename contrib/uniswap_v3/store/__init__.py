"""The package's own SQLite store.

It shares no database with any other package. ``bars`` is market data, one
row per pool per bar boundary, belonging to no run. ``runs``, ``decisions``,
``fills`` and ``valuations`` are what a run writes, the same tables in every
run mode. Beside them, ``schema_migrations`` records the schema's versions.
:mod:`.schema` creates and versions the tables, :mod:`.repository` reads
and writes rows, and :mod:`.bar_source` builds the engine's
:class:`~..domain.types.Bar` from the readings.
"""
