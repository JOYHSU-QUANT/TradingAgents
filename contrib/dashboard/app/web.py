"""The web layer: a FastAPI application that renders :mod:`.documents` with Jinja.

Three routes, all ``GET`` and ``HEAD``: ``/`` the overview, ``/line/<name>``
one line with its tables, ``/static/style.css`` the stylesheet. Any other
method is answered ``405`` before routing — the page writes nothing (plan
D5) — and the interactive docs FastAPI would add are off (they load scripts
from a CDN; plan §4 keeps every asset in the repo).

Every response carries the headers plan §4 asks for: a Content Security
Policy that allows the page's own stylesheet and nothing else (no script
from anywhere, no frame, no form), ``nosniff``, no referrer, no caching.
The templates render with autoescape on: a decision's reason is a model's
text and is shown as text.

The clock and the status directory are given to :func:`build_app` so a
test can bind both.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..times import show_when, utc_now
from . import DEFAULT_TITLE
from .documents import load_line, load_page

__all__ = ["SECURITY_HEADERS", "PageSettings", "build_app"]

_HERE = Path(__file__).resolve().parent
_TEMPLATES = _HERE / "templates"
_STATIC = _HERE / "static"
_READ_METHODS = frozenset({"GET", "HEAD"})

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass(frozen=True)
class PageSettings:
    """What the page is started with."""

    status_dir: Path
    stale: timedelta
    refresh_seconds: int
    title: str = DEFAULT_TITLE


def build_app(settings: PageSettings, *, now: Callable[[], datetime] = utc_now) -> FastAPI:
    """The application over ``settings.status_dir``; ``now`` is the clock a test binds."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        # The tree is read-only under the unit and swapped only with the page
        # stopped (install-web.sh): no stat of the templates per request.
        auto_reload=False,
    )
    app = FastAPI(title=settings.title, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def read_only_with_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _READ_METHODS:
            response: Response = PlainTextResponse(
                "this page is read-only\n", status_code=405, headers={"Allow": "GET, HEAD"}
            )
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/static/"):
            # The stylesheet may be kept and revalidated (StaticFiles answers
            # If-None-Match with 304); the pages themselves are never stored.
            response.headers["Cache-Control"] = "no-cache"
        return response

    # The router's own 404 is Starlette's exception and FastAPI's a subclass of
    # it: one handler on the base gives both a plain-text answer, never JSON.
    @app.exception_handler(StarletteHTTPException)
    async def plain_error(_request: Request, exc: StarletteHTTPException) -> Response:
        return PlainTextResponse(
            f"{exc.detail}\n", status_code=exc.status_code, headers=exc.headers
        )

    # Starlette's error middleware runs this outside the header middleware
    # above, so a page that could not be rendered carries the headers too.
    @app.exception_handler(Exception)
    async def failed(_request: Request, _exc: Exception) -> Response:
        return PlainTextResponse(
            "the page could not be rendered\n", status_code=500, headers=SECURITY_HEADERS
        )

    def render(template: str, **context: object) -> HTMLResponse:
        text = env.get_template(template).render(
            site=settings.title, refresh_seconds=settings.refresh_seconds, **context
        )
        return HTMLResponse(text)

    # FastAPI, unlike Starlette, does not answer HEAD for a GET route by itself.
    @app.api_route("/", methods=["GET", "HEAD"])
    def index() -> HTMLResponse:
        page = load_page(settings.status_dir, now=now(), stale=settings.stale)
        return render("index.html", page=page)

    @app.api_route("/line/{name}", methods=["GET", "HEAD"])
    def line(name: str) -> HTMLResponse:
        moment = now()
        card = load_line(settings.status_dir, name, now=moment, stale=settings.stale)
        if card is None:
            raise HTTPException(status_code=404, detail="no such line")
        return render("line.html", line=card, now=show_when(moment))

    app.mount("/static", StaticFiles(directory=str(_STATIC)))
    return app
