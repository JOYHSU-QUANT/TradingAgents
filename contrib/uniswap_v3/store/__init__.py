"""The package's own SQLite store.

It shares no database with any other package. ``bars`` is market data, one
row per pool per bar boundary, belonging to no run, and so are ``verdicts``,
what an outside judge said of a token at a bar. ``runs``, ``decisions``,
``fills`` and ``valuations`` are what a run writes, the same tables in every
run mode. Beside them, ``schema_migrations`` records the schema's versions.
:mod:`.schema` creates and versions the tables, :mod:`.repository` reads
and writes rows, :mod:`.bar_source` builds the engine's
:class:`~..domain.types.Bar` from the readings, and :mod:`.verdict_source`
picks the verdicts a config reads at a bar.
"""
