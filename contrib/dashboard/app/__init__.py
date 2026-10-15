"""``contrib.dashboard.app`` — the read-only page over the exporter's documents (dashboard plan PR 4).

The exporter (:mod:`contrib.dashboard.exporter`) writes one directory of
JSON every two minutes: each line's own document, the host's facts, the
alerts holding, how the tick went. This subpackage serves that directory as
a web page and nothing more: it reads the files, never a store, a config or
a ``.env``; it binds the loopback interface for ``tailscale serve`` to front
(plan D1); it has no endpoint that writes (plan D5).

:mod:`.documents` turns the files into view models — plain strings, every
free text masked for a quoted key or address and cut — and needs no web
library, so it is tested wherever the exporter is; :mod:`.web` is the
FastAPI and Jinja layer over it, installed from this package's
``requirements.txt`` on the ``dashboard`` account's own virtualenv (the
trader's has neither); :mod:`.cli` is ``python -m contrib.dashboard.app``.
"""

from __future__ import annotations

# The page title, the one default the command line and the settings share.
DEFAULT_TITLE = "paper traders"
