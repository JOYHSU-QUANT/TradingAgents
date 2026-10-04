"""The package's own SQLite store.

It shares no database with any other package. So far it holds one table of
data, ``bars``: market data, one row per pool per bar boundary, belonging to
no run. Beside it, ``schema_migrations`` records the schema's versions.
:mod:`.schema` creates and versions the tables, :mod:`.repository` reads
and writes rows, and :mod:`.bar_source` builds the engine's
:class:`~..domain.types.Bar` from them.
"""
