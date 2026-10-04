"""The package's own SQLite store.

It shares no database with any other package. So far it holds one table,
``bars``: market data, one row per pool per bar boundary, belonging to no
run. :mod:`.schema` creates and versions the tables, :mod:`.repository`
reads and writes rows, and :mod:`.bar_source` builds the engine's
:class:`~..domain.types.Bar` from them.
"""
