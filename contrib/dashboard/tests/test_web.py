"""The FastAPI layer: routes, methods, headers, escaping.

Needs FastAPI and httpx (the test client), which the trader's venv does not
have: :func:`support.need_web` skips the module there and fails it where CI
says the install is required.
"""

from __future__ import annotations

from datetime import timedelta

from .support import (
    HL_KEY,
    LINE_NAMES,
    NOW,
    STALE,
    WALLET,
    hyperliquid_document,
    need_web,
    quiet_documents,
    status_dir,
)

need_web()

from fastapi.testclient import TestClient  # noqa: E402

from contrib.dashboard.app.web import SECURITY_HEADERS, PageSettings, build_app  # noqa: E402

XSS = "<script>alert(1)</script><img src=x onerror=alert(2)>"


def client_over(tmp_path, documents=None, *, later=timedelta(minutes=1), refresh=60):
    directory = status_dir(tmp_path, documents)
    settings = PageSettings(directory, STALE, refresh, title="paper traders")
    return TestClient(build_app(settings, now=lambda: NOW + later))


def test_the_overview_renders_every_line_with_the_headers_and_no_script(tmp_path):
    response = client_over(tmp_path).get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    body = response.text
    for name in LINE_NAMES:
        assert f'href="/line/{name}"' in body
    assert "<script" not in body
    assert 'content="60"' in body
    assert "10234.56" in body
    assert WALLET not in body


def test_a_lines_page_renders_its_tables_and_links_back(tmp_path):
    response = client_over(tmp_path).get("/line/hl-paper")
    assert response.status_code == 200
    assert "recent cycles (newest first)" in response.text
    assert 'href="/"' in response.text
    # The verdict's reason quotes the wallet: shown folded, never whole.
    assert WALLET not in response.text and "wallet 0xabab...abab fine" in response.text
    assert "<title>hl-paper · paper traders</title>" in response.text


def test_a_models_text_is_shown_as_text(tmp_path):
    documents = quiet_documents()
    document = hyperliquid_document()
    document["cycles"]["recent"][0]["verdict"]["decision_reason"] = XSS
    documents[HL_KEY] = document
    body = client_over(tmp_path, documents).get("/line/hl-paper").text
    assert XSS not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<img" not in body


def test_an_unknown_line_is_404_in_plain_text(tmp_path):
    response = client_over(tmp_path).get("/line/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "no such line\n"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_the_docs_fastapi_would_add_are_off(tmp_path):
    client = client_over(tmp_path)
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/plain")


def test_anything_but_get_and_head_is_405_before_routing(tmp_path):
    client = client_over(tmp_path)
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    for method in ("post", "put", "patch", "delete"):
        for path in ("/", "/line/hl-paper", "/static/style.css", "/anything"):
            response = client.request(method, path)
            assert response.status_code == 405, (method, path)
            assert response.headers["allow"] == "GET, HEAD"
            assert response.headers["Content-Security-Policy"] == csp


def test_head_is_answered_like_get_without_a_body(tmp_path):
    client = client_over(tmp_path)
    for path in ("/", "/line/hl-paper", "/static/style.css"):
        response = client.head(path)
        assert response.status_code == 200, path
        assert response.content == b""


def test_the_stylesheet_is_served_from_this_origin(tmp_path):
    response = client_over(tmp_path).get("/static/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    # Kept and revalidated, unlike the pages (never stored).
    assert response.headers["Cache-Control"] == "no-cache"
    assert (
        response.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
    )
    assert ".card.tone-bad" in response.text


def test_a_stale_tick_shows_the_banner_and_the_refresh_setting_is_the_pages(tmp_path):
    body = client_over(tmp_path, later=timedelta(minutes=9), refresh=30).get("/").text
    assert "every card may be out of date" in body
    assert 'content="30"' in body
    assert 'class="card tone-bad stale"' in body


def test_an_empty_status_directory_still_renders(tmp_path):
    directory = tmp_path / "status"
    directory.mkdir()
    client = TestClient(build_app(PageSettings(directory, STALE, 60), now=lambda: NOW))
    response = client.get("/")
    assert response.status_code == 200
    assert "No exporter tick to show: exporter.json not written yet." in response.text
    assert "No lines: exporter.json names none." in response.text


def test_a_page_that_cannot_be_rendered_is_a_plain_500_with_the_headers(tmp_path, monkeypatch):
    import contrib.dashboard.app.web as web

    def boom(*_args, **_kwargs):
        raise RuntimeError("a template field went missing")

    monkeypatch.setattr(web, "load_page", boom)
    directory = status_dir(tmp_path)
    app = build_app(PageSettings(directory, STALE, 60), now=lambda: NOW)
    response = TestClient(app, raise_server_exceptions=False).get("/")
    assert response.status_code == 500
    assert response.text == "the page could not be rendered\n"
    assert "went missing" not in response.text
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
