"""``python -m contrib.dashboard.app``: serve the read-only page on the loopback interface.

The page binds a loopback address and nothing else (plan D1: ``tailscale
serve`` fronts it inside the tailnet; no public port) — any other
``--host`` is refused, and there is no flag to allow one. ``--status-dir``
is the exporter's directory; ``--stale-minutes`` how old a document may be
before its card is marked stale (default three ticks of the two-minute
timer); ``--refresh-seconds`` how often the browser reloads the page.

Exit codes: ``0`` the server stopped; ``1`` a setting that cannot be used;
``2`` usage. The web library is imported only once the settings hold, so a
refusal needs none of it.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import DEFAULT_TITLE

__all__ = ["DEFAULT_PORT", "main"]

DEFAULT_PORT = 8787
DEFAULT_STALE_MINUTES = 6
DEFAULT_REFRESH_SECONDS = 60
# What runs the application: ``(app, host, port)``; a test binds its own.
Server = Callable[[Any, str, int], None]


def _positive(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _port(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be a port number, 1 to 65535")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.dashboard.app",
        description="Serve the read-only status page over the exporter's documents.",
    )
    parser.add_argument(
        "--status-dir", required=True, type=Path, help="the directory the exporter writes"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="the loopback address to bind (default 127.0.0.1; any other address is refused)",
    )
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    parser.add_argument(
        "--stale-minutes",
        type=_positive,
        default=DEFAULT_STALE_MINUTES,
        help=f"a document older than this marks its card stale (default {DEFAULT_STALE_MINUTES})",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=_positive,
        default=DEFAULT_REFRESH_SECONDS,
        help=f"how often the browser reloads the page (default {DEFAULT_REFRESH_SECONDS})",
    )
    parser.add_argument("--title", default=DEFAULT_TITLE, help="the page's title")
    return parser


def _serve(app: Any, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


def main(argv: Sequence[str] | None = None, *, serve: Server = _serve) -> int:
    """Run the server; ``serve`` is the seam a test binds."""
    args = _build_parser().parse_args(argv)
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError:
        print(f"dashboard: --host {args.host!r} is not an IP address", file=sys.stderr)
        return 1
    if not address.is_loopback:
        print(
            f"dashboard: --host {args.host} is not a loopback address; the page binds the "
            "loopback only and tailscale serve fronts it (plan D1)",
            file=sys.stderr,
        )
        return 1
    status_dir: Path = args.status_dir
    if not status_dir.is_dir():
        print(f"dashboard: --status-dir {status_dir} is not a directory", file=sys.stderr)
        return 1
    from .web import PageSettings, build_app

    settings = PageSettings(
        status_dir=status_dir,
        stale=timedelta(minutes=args.stale_minutes),
        refresh_seconds=args.refresh_seconds,
        title=args.title,
    )
    serve(build_app(settings), args.host, args.port)
    return 0
