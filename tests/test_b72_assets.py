"""B7.2 static assets: the ES-module dashboard is served from an explicit allowlist with a
JavaScript MIME type; no path is derived from the request."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_decision_journal import _app

from johanna import api as api_module
from johanna.api import BackendAPI

WEB = Path(api_module.__file__).with_name("web")


def _get(api, path: str, method: str = "GET"):
    response: dict = {}

    def start(status, headers):
        response["status"], response["headers"] = status, dict(headers)

    body = b"".join(
        api({"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "127.0.0.1:8765"}, start)
    )
    return response["status"], response["headers"], body


@pytest.fixture
def api(tmp_path):
    app = _app(tmp_path)
    app.start()
    try:
        yield BackendAPI(app)
    finally:
        app.stop()


def test_allowlist_matches_the_module_files_exactly() -> None:
    files = {p.stem for p in (WEB / "modules").glob("*.js")}
    assert set(api_module._WEB_MODULES) == files
    assert len(api_module._WEB_MODULES) == len(set(api_module._WEB_MODULES))


def test_every_module_is_served_as_javascript(api) -> None:
    for name in ("app.js", *(f"modules/{m}.js" for m in api_module._WEB_MODULES)):
        status, headers, body = _get(api, f"/assets/{name}")
        assert status == "200 OK", name
        assert headers["Content-Type"] == "text/javascript; charset=utf-8", name
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert body == (WEB / name).read_bytes()
    status, headers, _ = _get(api, "/assets/styles.css")
    assert status == "200 OK" and headers["Content-Type"] == "text/css; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/assets/modules/unknown.js",
        "/assets/modules/../app.js",
        "/assets/../api.py",
        "/assets/modules/%2e%2e/app.js",
        "/assets/modules/",
        "/assets/modules/state.js.map",
        "/assets/index.html",
    ],
)
def test_unlisted_or_traversal_paths_are_not_served(api, path) -> None:
    status, _, _ = _get(api, path)
    assert status.startswith("404")


def test_assets_are_get_only(api) -> None:
    status, _, _ = _get(api, "/assets/modules/state.js", method="POST")
    assert status.startswith("405")


def test_index_loads_the_entry_as_a_module() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<script type="module" src="/assets/app.js"></script>' in html
