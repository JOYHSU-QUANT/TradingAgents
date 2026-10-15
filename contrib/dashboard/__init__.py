"""``contrib.dashboard`` — the status exporter, alerting and read-only page for the paper traders.

The host runs several lines: the Hyperliquid paper daemons (``paper-BTC-*``
and the carry sleeve's perp leg), the Uniswap paper visits (three runs in one
store) and the carry coordinator. Each of their packages already says what
its store holds, as one JSON document (``status --json`` for the perp and
Uniswap packages, ``report --json`` for carry). This package's exporter, a
systemd timer on the trader account, asks each line for that document by
running its command line, adds what only the host knows (the units' states,
the deployed commits, disk and memory), writes it all under one directory as
JSON for the read-only page (:mod:`contrib.dashboard.app`) to serve, and mails the
operator when a line's state changes — never a balance, a position or an
address, only which line, what state, since when.

The package imports nothing from the other ``contrib`` packages but the
shared floor ``contrib.core``: it reads their documents through their
command lines, as a user of the packages would, so a package can live in
its own checkout at its own commit (the Uniswap one does) without this one
noticing.
"""
