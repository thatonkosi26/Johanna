"""Small standard-library HTTP API for a separately deployed frontend."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from johanna.application import JohannaApplication
from johanna.live_market import TIMEFRAMES
from johanna.market_analysis import ANALYSIS_TIMEFRAMES
from johanna.persistence import MAX_WATCHLIST, SYMBOL_PATTERN

LOGGER = logging.getLogger("johanna.api")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


_ROUTES: dict[str, frozenset[str]] = {
    "/": frozenset({"GET"}),
    "/health": frozenset({"GET"}),
    "/status": frozenset({"GET"}),
    "/api/dashboard": frozenset({"GET"}),
    "/api/preferences": frozenset({"POST"}),
    "/api/market/quotes": frozenset({"GET"}),
    "/api/market/bars": frozenset({"GET"}),
    "/api/market/snapshot": frozenset({"GET"}),
    "/api/market/analysis": frozenset({"GET"}),
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class BackendAPI:
    def __init__(self, application: JohannaApplication) -> None:
        self.application = application
        configured = application.config.server.host.strip().lower()
        self._allow_any_host = configured in {"0.0.0.0", "::", ""}
        self._allowed_hosts = _LOOPBACK_HOSTS | {configured}

    def __call__(self, environ: dict, start_response: Callable) -> list[bytes]:
        try:
            method = environ.get("REQUEST_METHOD", "GET")
            path = environ.get("PATH_INFO", "/")
            if not self._host_allowed(environ):
                return self._response(
                    start_response, HTTPStatus.MISDIRECTED_REQUEST, {"error": "host_not_allowed"}
                )
            if path.startswith("/assets/"):
                if method != "GET":
                    return self._method_not_allowed(start_response, {"GET"})
                asset = path.removeprefix("/assets/")
                if asset not in {"app.js", "styles.css"}:
                    return self._response(
                        start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"}
                    )
                return self._static(start_response, asset)
            allowed = _ROUTES.get(path)
            if allowed is None:
                return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
            if method not in allowed:
                return self._method_not_allowed(start_response, allowed)
            if path == "/":
                return self._static(start_response, "index.html")
            if path == "/health":
                healthy, payload = self.application.health()
                status = HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE
                return self._response(start_response, status, payload)
            if path == "/status":
                return self._response(start_response, HTTPStatus.OK, self.application.status())
            if path == "/api/dashboard":
                return self._response(
                    start_response, HTTPStatus.OK, self.application.dashboard_data()
                )
            if path == "/api/market/quotes":
                return self._market_quotes(environ, start_response)
            if path == "/api/market/bars":
                return self._market_bars(environ, start_response)
            if path == "/api/market/snapshot":
                return self._market_bars(environ, start_response, snapshot=True)
            if path == "/api/market/analysis":
                return self._market_analysis(environ, start_response)
            return self._save_preferences(environ, start_response)
        except Exception:
            LOGGER.exception("Unhandled backend request error")
            return self._response(
                start_response, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}
            )

    def _host_allowed(self, environ: dict) -> bool:
        """Reject DNS-rebinding hosts and cross-origin browser requests.

        A missing Host header (only possible for HTTP/1.0 or in-process WSGI calls) is
        allowed. Binding to 0.0.0.0/:: disables the Host check by explicit operator choice;
        the Origin check still applies.
        """
        host = environ.get("HTTP_HOST")
        if host and not self._allow_any_host and _hostname(host) not in self._allowed_hosts:
            return False
        origin = environ.get("HTTP_ORIGIN")
        if origin and origin != "null":
            parsed = urlsplit(origin)
            if not host or parsed.netloc.lower() != host.lower():
                return False
        elif origin == "null":
            return False
        return True

    def _method_not_allowed(self, start_response: Callable, allowed: set | frozenset):
        return self._response(
            start_response,
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "method_not_allowed"},
            extra_headers=[("Allow", ", ".join(sorted(allowed)))],
        )

    def _market_quotes(self, environ: dict, start_response: Callable) -> list[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        requested = query.get("symbols", [""])[0]
        if requested:
            symbols = [item.strip().upper() for item in requested.split(",") if item.strip()]
        else:
            symbols = list(self.application.preferences.get("currency_pair_watchlist") or [])
        invalid = [symbol for symbol in symbols if not SYMBOL_PATTERN.fullmatch(symbol)]
        message = None
        if not symbols:
            message = "No symbols requested and the watchlist is empty."
        elif len(symbols) > MAX_WATCHLIST:
            message = (
                f"At most {MAX_WATCHLIST} symbols per request (the watchlist limit); "
                f"received {len(symbols)}."
            )
        elif invalid:
            message = f"Invalid instrument symbols: {invalid[:5]}"
        if message:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_symbols", "message": message},
            )
        payload = self.application.live_market.quotes(symbols)
        return self._response(start_response, HTTPStatus.OK, payload)

    def _market_bars(
        self, environ: dict, start_response: Callable, *, snapshot: bool = False
    ) -> list[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        symbol = query.get("symbol", [""])[0].strip().upper()
        timeframe = query.get("timeframe", ["H1"])[0].strip().upper()
        try:
            count = int(query.get("count", ["300"])[0])
        except ValueError:
            count = -1
        if (
            not SYMBOL_PATTERN.fullmatch(symbol)
            or timeframe not in TIMEFRAMES
            or not 10 <= count <= 1000
        ):
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid_bar_request",
                    "message": "Need a valid symbol, a timeframe in "
                    f"{', '.join(TIMEFRAMES)} and a count between 10 and 1000.",
                },
            )
        live = self.application.live_market
        payload = (live.snapshot if snapshot else live.bars)(symbol, timeframe, count)
        return self._response(start_response, HTTPStatus.OK, payload)

    def _market_analysis(self, environ: dict, start_response: Callable) -> list[bytes]:
        """T6 analysis of a new snapshot (read-only). Only ``symbol`` and ``timeframe`` are
        read; the evaluation time and bar count are decided by the backend."""
        query = parse_qs(environ.get("QUERY_STRING", ""))
        symbol = query.get("symbol", [""])[0].strip().upper()
        timeframe = query.get("timeframe", ["H1"])[0].strip().upper()
        if not SYMBOL_PATTERN.fullmatch(symbol) or timeframe not in TIMEFRAMES:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid_analysis_request",
                    "message": f"Need a valid symbol and a timeframe in {', '.join(TIMEFRAMES)}.",
                },
            )
        if timeframe not in ANALYSIS_TIMEFRAMES:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "analysis_unsupported_timeframe",
                    "message": f"Analysis supports {', '.join(ANALYSIS_TIMEFRAMES)}.",
                },
            )
        payload = self.application.market_analysis.analyse(symbol, timeframe)
        return self._response(start_response, HTTPStatus.OK, payload)

    def _save_preferences(self, environ: dict, start_response: Callable) -> list[bytes]:
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            # Browsers send text/plain or form bodies cross-site without a CORS preflight;
            # requiring JSON forces a preflight that this API never approves.
            return self._response(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "unsupported_media_type", "message": "Content-Type must be JSON"},
            )
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length < 1 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            raw = environ["wsgi.input"].read(length)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("preferences body must be a JSON object")
            self.application.save_preferences(payload)
        except (ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_preferences", "message": str(exc)},
            )
        return self._response(
            start_response,
            HTTPStatus.OK,
            {"preferences": self.application.preferences},
        )

    @staticmethod
    def _static(start_response: Callable, filename: str) -> list[bytes]:
        asset_path = Path(__file__).with_name("web") / filename
        body = asset_path.read_bytes()
        content_type = guess_type(filename)[0] or "application/octet-stream"
        start_response(
            "200 OK",
            [
                ("Content-Type", f"{content_type}; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-cache"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                ("Content-Security-Policy", _CSP),
                ("Referrer-Policy", "no-referrer"),
            ],
        )
        return [body]

    @staticmethod
    def _response(
        start_response: Callable,
        status: HTTPStatus,
        payload: dict,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> list[bytes]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        start_response(
            f"{status.value} {status.phrase}",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
                *(extra_headers or []),
            ],
        )
        return [body]


def _hostname(host_header: str) -> str:
    """Host header without port; handles bracketed IPv6 such as ``[::1]:8765``."""
    value = host_header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value
