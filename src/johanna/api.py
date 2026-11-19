"""Small standard-library HTTP API for a separately deployed frontend."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import date, timedelta
from http import HTTPStatus
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from johanna.application import JohannaApplication
from johanna.journal_store import JournalError
from johanna.live_market import TIMEFRAMES
from johanna.market_analysis import ANALYSIS_TIMEFRAMES
from johanna.notifications import LIFECYCLE
from johanna.operations import readiness
from johanna.persistence import MAX_WATCHLIST, SYMBOL_PATTERN
from johanna.trade_logbook import LogbookError

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
    "/api/account/summary": frozenset({"GET"}),
    "/api/account/instruments": frozenset({"GET"}),
    "/api/risk/proposal": frozenset({"GET"}),
    "/api/journal/decisions": frozenset({"GET"}),
    # P6.1 shadow evaluation (read-only diagnostics; the only write is the enable switch)
    "/api/shadow/status": frozenset({"GET"}),
    "/api/shadow/scheduler": frozenset({"POST"}),
    "/api/shadow/evaluations": frozenset({"GET"}),
    "/api/shadow/diagnostics": frozenset({"GET"}),
    # P6.2a shadow trade intents and funnel: GET only (HYPOTHETICAL -- NOT AN ORDER)
    "/api/shadow/intents": frozenset({"GET"}),
    "/api/shadow/funnel": frozenset({"GET"}),
    # P6.2b.2 cost readiness (diagnostic, read-only; approves nothing)
    "/api/costs/readiness": frozenset({"GET"}),
    # P6.3 monitoring (read-only; reports the last explicit observation, triggers no MT5 read)
    "/api/monitoring/status": frozenset({"GET"}),
    "/api/monitoring/reconciliation": frozenset({"GET"}),
    # P6.4 notifications (read-only list; a GET never changes a notification's state)
    "/api/notifications": frozenset({"GET"}),
    # P6.R operations readiness (diagnostic, read-only; enables nothing)
    "/api/operations/readiness": frozenset({"GET"}),
    # P6.2b.4 M-9 user-confirmed trade links: GET lists decisions (read-only); POST is the
    # future confirmation endpoint and answers 403 not_activated (separate approval needed)
    "/api/reconciliation/links": frozenset({"GET", "POST"}),
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# B7.2 static assets: an explicit allowlist (no path is derived from the request). The dashboard
# is native ES modules: app.js imports these files from /assets/modules/.
_WEB_MODULES = (
    "hooks",
    "state",
    "format",
    "status",
    "api",
    "components",
    "quotes",
    "chart",
    "analysis",
    "overlay",
    "panels",
    "account",
    "risk",
    "decisions",
    "trades",
    "shadow",
    "control",
    "positions",
    "evidence",
    "settings",
    "overview",
    "pages",
    "data",
    "polling",
    "navigation",
)
_ASSETS = frozenset({"app.js", "styles.css", *(f"modules/{name}.js" for name in _WEB_MODULES)})
# Explicit types: module scripts must be served as JavaScript (with nosniff); the platform
# MIME registry is not trusted for this.
_ASSET_TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}
# GET /api/account/instruments/{symbol}: a supported pair or its broker symbol.
_INSTRUMENT_PREFIX = "/api/account/instruments/"
# GET /api/journal/decisions/{id}: a journal decision (ULID).
_DECISION_PREFIX = "/api/journal/decisions/"
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_JOURNAL_FILTERS = {
    "outcome": ("TRADE_CANDIDATE", "MARKET_CONDITION", "NO_TRADE"),
    "eligibility": ("APPROVED", "NOT_APPROVED"),
    "execution": ("NONE", "MANUALLY_RECORDED", "BROKER_VERIFIED"),
}
_INSTRUMENT_SYMBOL = re.compile(r"[A-Za-z0-9._-]{3,20}")
# P5.2 manual trade logbook. No DELETE route exists for any trade record.
_TRADES = "/api/trades"
_ID = r"([0-9A-HJKMNP-TV-Z]{26})"
_TRADE_ROUTES: tuple[tuple[re.Pattern[str], frozenset[str], str], ...] = (
    (re.compile(rf"/api/trades/{_ID}"), frozenset({"GET"}), "detail"),
    (re.compile(rf"/api/trades/{_ID}/fills"), frozenset({"POST"}), "add_fill"),
    (re.compile(rf"/api/trades/{_ID}/fills/{_ID}/void"), frozenset({"POST"}), "void_fill"),
    (re.compile(rf"/api/trades/{_ID}/void"), frozenset({"POST"}), "void_trade"),
    (re.compile(rf"/api/trades/{_ID}/protection"), frozenset({"POST"}), "add_protection"),
    (re.compile(rf"/api/trades/{_ID}/notes"), frozenset({"POST"}), "add_note"),
    (re.compile(rf"/api/trades/{_ID}/links"), frozenset({"POST"}), "add_link"),
    (re.compile(rf"/api/trades/{_ID}/links/{_ID}/void"), frozenset({"POST"}), "void_link"),
)
_SHADOW_PREFIX = "/api/shadow/evaluations/"
_SHADOW_FILTERS = {
    "symbol": ("EURUSD", "GBPUSD", "USDJPY"),
    "timeframe": ("H1", "H4", "D1"),
    "status": ("PENDING", "RUNNING", "COMPLETED", "SKIPPED", "MISSED", "FAILED"),
}
# GET /api/shadow/intents/{id}: one hypothetical intent (ULID). No other method exists.
_INTENT_PREFIX = "/api/shadow/intents/"
_INTENT_FILTERS = {
    "symbol": ("EURUSD", "GBPUSD", "USDJPY"),
    "timeframe": ("H1", "H4", "D1"),
    "eligibility": ("RISK_APPROVED", "REJECTED", "UNVERIFIED"),
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TRADE_FILTERS = {
    "status": ("OPEN", "PARTIALLY_CLOSED", "CLOSED", "VOIDED"),
    "source": ("manual", "automated"),
}


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
                if asset not in _ASSETS:
                    return self._response(
                        start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"}
                    )
                return self._static(start_response, asset)
            if path == _TRADES or path.startswith(_TRADES + "/"):
                return self._trades(environ, method, path, start_response)
            if path.startswith(_INTENT_PREFIX):
                if method != "GET":
                    return self._method_not_allowed(start_response, {"GET"})
                return self._shadow_intent(path, start_response)
            if path.startswith(_SHADOW_PREFIX):
                if method != "GET":
                    return self._method_not_allowed(start_response, {"GET"})
                return self._shadow_cycle(path, start_response)
            if path.startswith("/api/shadow/"):
                allowed = _ROUTES.get(path)
                if allowed is None:
                    return self._response(
                        start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"}
                    )
                if method not in allowed:
                    return self._method_not_allowed(start_response, allowed)
                return self._shadow(environ, path, start_response)
            if path.startswith(_DECISION_PREFIX):
                if method != "GET":
                    return self._method_not_allowed(start_response, {"GET"})
                return self._journal_decision(path, start_response)
            if path.startswith(_INSTRUMENT_PREFIX):
                if method != "GET":
                    return self._method_not_allowed(start_response, {"GET"})
                return self._account_instrument(path, start_response)
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
            if path == "/api/journal/decisions":
                return self._journal_decisions(environ, start_response)
            if path == "/api/monitoring/status":
                return self._response(
                    start_response, HTTPStatus.OK, self.application.monitoring.status()
                )
            if path == "/api/monitoring/reconciliation":
                unavailable = self._journal_unavailable(start_response)
                if unavailable is not None:
                    return unavailable
                return self._response(
                    start_response, HTTPStatus.OK, self.application.monitoring.latest()
                )
            if path == "/api/operations/readiness":
                return self._response(start_response, HTTPStatus.OK, readiness(self.application))
            if path == "/api/notifications":
                return self._notifications(environ, start_response)
            if path == "/api/reconciliation/links":
                if method == "POST":  # inactive: no body is read, nothing is written
                    return self._response(
                        start_response,
                        HTTPStatus.FORBIDDEN,
                        {
                            "error": "not_activated",
                            "message": "user-confirmed trade linking is not activated "
                            "(separate approval required)",
                        },
                    )
                payload = self.application.trade_links.links()
                payload["service"] = self.application.trade_links.status()
                return self._response(start_response, HTTPStatus.OK, payload)
            if path == "/api/costs/readiness":
                return self._response(
                    start_response, HTTPStatus.OK, self.application.cost_readiness()
                )
            if path == "/api/risk/proposal":
                return self._risk_proposal(environ, start_response)
            if path == "/api/account/summary":
                return self._account_payload(start_response, self.application.account.summary())
            if path == "/api/account/instruments":
                payload = self.application.account.instruments()
                return self._account_payload(start_response, payload)
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
        # P5.1 request-time journal capture: isolated, never alters or delays this response.
        self.application.capture_analysis(payload)
        return self._response(start_response, HTTPStatus.OK, payload)

    def _journal_unavailable(self, start_response: Callable) -> list[bytes] | None:
        store = self.application.journal_store
        if store.ready:
            return None
        status = store.status()
        return self._response(
            start_response,
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "error": "journal_unavailable",
                "reason": status["reason"] or status["state"],
                "journal": status,
            },
        )

    def _journal_decisions(self, environ: dict, start_response: Callable) -> list[bytes]:
        """P5.1 decision history (read-only). Every response discloses request-time coverage."""
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        query = parse_qs(environ.get("QUERY_STRING", ""))
        filters: dict[str, object] = {}
        for name in ("symbol", "timeframe", "outcome", "eligibility", "execution"):
            value = query.get(name, [""])[0].strip()
            if not value:
                continue
            value = value.upper()
            allowed = _JOURNAL_FILTERS.get(name)
            valid = (
                value in allowed
                if allowed
                else (SYMBOL_PATTERN.fullmatch(value) if name == "symbol" else value in TIMEFRAMES)
            )
            if not valid:
                return self._response(
                    start_response,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_journal_request", "message": f"invalid {name}"},
                )
            filters[name] = value
        try:
            limit = int(query.get("limit", ["50"])[0])
            cursor_text = query.get("cursor", [""])[0]
            cursor = int(cursor_text) if cursor_text else None
            if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
                raise ValueError
        except ValueError:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_journal_request", "message": "limit 1-100; cursor from a page"},
            )
        payload = self.application.decisions.list(**filters, cursor=cursor, limit=limit)
        payload["journal"] = self._journal_status()
        return self._response(start_response, HTTPStatus.OK, payload)

    def _journal_decision(self, path: str, start_response: Callable) -> list[bytes]:
        decision_id = path.removeprefix(_DECISION_PREFIX)
        if not _ULID.fullmatch(decision_id):
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        payload = self.application.decisions.get(decision_id)
        if payload is None:
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        payload["journal"] = self._journal_status()
        return self._response(start_response, HTTPStatus.OK, payload)

    # -- P5.2 manual trade logbook ------------------------------------------------------
    def _trades(
        self, environ: dict, method: str, path: str, start_response: Callable
    ) -> list[bytes]:
        if path == _TRADES:
            route, allowed, ids = "collection", frozenset({"GET", "POST"}), ()
        else:
            for pattern, allowed, route in _TRADE_ROUTES:
                match = pattern.fullmatch(path)
                if match:
                    ids = match.groups()
                    break
            else:
                return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        if method not in allowed:
            return self._method_not_allowed(start_response, allowed)
        logbook = self.application.trades
        try:
            if method == "GET":
                unavailable = self._journal_unavailable(start_response)
                if unavailable is not None:
                    return unavailable
                if route == "collection":
                    return self._trade_list(environ, start_response)
                payload = logbook.get(ids[0])
                if payload is None:
                    return self._response(
                        start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"}
                    )
                payload["journal"] = self.application.journal_store.status()
                return self._response(start_response, HTTPStatus.OK, payload)
            body = self._json_body(environ)
            if isinstance(body, tuple):
                return self._response(start_response, *body)
            if route == "collection":
                status, payload = logbook.create_trade(body)
            else:
                status, payload = getattr(logbook, route)(*ids, body)
            return self._response(start_response, HTTPStatus(status), payload)
        except LogbookError as exc:
            if exc.status == HTTPStatus.SERVICE_UNAVAILABLE:
                return self._journal_unavailable(start_response) or self._response(
                    start_response,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "journal_unavailable", "reason": exc.message},
                )
            return self._response(
                start_response,
                HTTPStatus(exc.status),
                {"error": exc.code, "message": exc.message},
            )

    @staticmethod
    def _json_body(environ: dict) -> object:
        """A JSON object body, or ``(status, payload)`` describing why it was refused."""
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return (
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "unsupported_media_type", "message": "Content-Type must be JSON"},
            )
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length < 1 or length > 16_384:
                raise ValueError("request body must be between 1 and 16384 bytes")
            payload = json.loads(environ["wsgi.input"].read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
        except (ValueError, KeyError, UnicodeDecodeError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid_body", "message": str(exc)}
        return payload

    def _trade_list(self, environ: dict, start_response: Callable) -> list[bytes]:
        query = parse_qs(environ.get("QUERY_STRING", ""))
        filters: dict[str, object] = {}
        try:
            for name in ("status", "source"):
                value = query.get(name, [""])[0].strip()
                if value:
                    value = value.lower() if name == "source" else value.upper()
                    if value not in _TRADE_FILTERS[name]:
                        raise ValueError(f"invalid {name}")
                    filters[name] = value
            symbol = query.get("symbol", [""])[0].strip()
            if symbol:
                filters["symbol"] = symbol
            limit = int(query.get("limit", ["50"])[0])
            cursor_text = query.get("cursor", [""])[0]
            cursor = int(cursor_text) if cursor_text else None
            if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
                raise ValueError("limit 1-100; cursor from a page")
            payload = self.application.trades.list(
                **filters, cursor=cursor, limit=limit, unrealised=True
            )
        except LogbookError as exc:
            return self._response(
                start_response, HTTPStatus.BAD_REQUEST, {"error": exc.code, "message": exc.message}
            )
        except ValueError as exc:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_trade_request", "message": str(exc)},
            )
        payload["journal"] = self.application.journal_store.status()
        return self._response(start_response, HTTPStatus.OK, payload)

    # -- P6.1 shadow evaluation --------------------------------------------------------
    def _shadow(self, environ: dict, path: str, start_response: Callable) -> list[bytes]:
        service = self.application.shadow
        if path == "/api/shadow/status":
            payload = service.status()
            payload["journal"] = self.application.journal_store.status()
            return self._response(start_response, HTTPStatus.OK, payload)
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        if path == "/api/shadow/scheduler":
            body = self._json_body(environ)
            if isinstance(body, tuple):
                return self._response(start_response, *body)
            if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
                return self._response(
                    start_response,
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_body", "message": 'body must be {"enabled": true|false}'},
                )
            try:
                payload = service.set_enabled(body["enabled"])
            except JournalError:
                return self._journal_unavailable(start_response) or self._response(
                    start_response,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "journal_unavailable"},
                )
            return self._response(start_response, HTTPStatus.OK, payload)
        if path == "/api/shadow/diagnostics":
            return self._response(start_response, HTTPStatus.OK, service.diagnostics())
        if path in ("/api/shadow/intents", "/api/shadow/funnel"):
            return self._shadow_intents(environ, path, start_response)
        query = parse_qs(environ.get("QUERY_STRING", ""))
        filters: dict[str, object] = {}
        try:
            for name, allowed in _SHADOW_FILTERS.items():
                value = query.get(name, [""])[0].strip().upper()
                if value:
                    if name == "symbol" and value.endswith("C"):
                        value = value[:-1]  # broker form EURUSDc
                    if value not in allowed:
                        raise ValueError(f"invalid {name}")
                    filters[name] = value
            limit = int(query.get("limit", ["50"])[0])
            cursor_text = query.get("cursor", [""])[0]
            cursor = int(cursor_text) if cursor_text else None
            if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
                raise ValueError("limit 1-100; cursor from a page")
        except ValueError as exc:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_shadow_request", "message": str(exc)},
            )
        payload = service.cycles(**filters, cursor=cursor, limit=limit)
        return self._response(start_response, HTTPStatus.OK, payload)

    def _shadow_cycle(self, path: str, start_response: Callable) -> list[bytes]:
        cycle_id = path.removeprefix(_SHADOW_PREFIX)
        if not _ULID.fullmatch(cycle_id):
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        payload = self.application.shadow.cycle(cycle_id)
        if payload is None:
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return self._response(start_response, HTTPStatus.OK, payload)

    def _shadow_intents(self, environ: dict, path: str, start_response: Callable) -> list[bytes]:
        """P6.2a: list of hypothetical intents or the opportunity funnel. Reads the journal
        only; every filter is validated against a fixed list or format."""
        query = parse_qs(environ.get("QUERY_STRING", ""))
        try:
            start, end = _date_range(query)
            if path == "/api/shadow/funnel":
                if set(query) - {"from", "to"}:
                    raise ValueError("funnel filters: from, to (YYYY-MM-DD)")
                payload = self.application.shadow.funnel(start, end)
                return self._response(start_response, HTTPStatus.OK, payload)
            unknown = set(query) - {*_INTENT_FILTERS, "from", "to", "limit", "cursor"}
            if unknown:
                raise ValueError(f"unknown filter {sorted(unknown)[0]}")
            filters: dict[str, object] = {}
            for name, allowed in _INTENT_FILTERS.items():
                value = query.get(name, [""])[0].strip().upper()
                if value:
                    if name == "symbol" and value.endswith("C"):
                        value = value[:-1]  # broker form EURUSDc
                    if value not in allowed:
                        raise ValueError(f"invalid {name}")
                    filters[name] = value
            limit = int(query.get("limit", ["50"])[0])
            cursor_text = query.get("cursor", [""])[0]
            cursor = int(cursor_text) if cursor_text else None
            if not 1 <= limit <= 100 or (cursor is not None and cursor < 1):
                raise ValueError("limit 1-100; cursor from a page")
        except ValueError as exc:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_shadow_request", "message": str(exc)},
            )
        payload = self.application.shadow.intents(
            **filters, start=start, end=end, cursor=cursor, limit=limit
        )
        return self._response(start_response, HTTPStatus.OK, payload)

    def _shadow_intent(self, path: str, start_response: Callable) -> list[bytes]:
        intent_id = path.removeprefix(_INTENT_PREFIX)
        if not _ULID.fullmatch(intent_id):
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        payload = self.application.shadow.intent(intent_id)
        if payload is None:
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return self._response(start_response, HTTPStatus.OK, payload)

    def _notifications(self, environ: dict, start_response: Callable) -> list[bytes]:
        unavailable = self._journal_unavailable(start_response)
        if unavailable is not None:
            return unavailable
        query = parse_qs(environ.get("QUERY_STRING", ""))
        try:
            if set(query) - {"state", "limit"}:
                raise ValueError("filters: state, limit")
            state = query.get("state", [""])[0].strip().lower() or None
            if state is not None and state not in LIFECYCLE:
                raise ValueError(f"state must be one of {', '.join(LIFECYCLE)}")
            limit = int(query.get("limit", ["50"])[0])
            if not 1 <= limit <= 100:
                raise ValueError("limit 1-100")
            payload = self.application.notifications.list(state=state, limit=limit)
        except ValueError as exc:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_notification_request", "message": str(exc)},
            )
        payload["service"] = self.application.notifications.status()
        return self._response(start_response, HTTPStatus.OK, payload)

    def _journal_status(self) -> dict[str, object]:
        return {**self.application.journal_store.status(), **self.application.decisions.status()}

    def _risk_proposal(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Phase 4 proposed trade parameters (read-only). Only ``symbol`` and ``timeframe``
        are read: the risk policy is fixed in code and cannot be set through the API."""
        query = parse_qs(environ.get("QUERY_STRING", ""))
        # The symbol keeps its case: a broker symbol such as EURUSDc is accepted (F3).
        symbol = query.get("symbol", [""])[0].strip()
        timeframe = query.get("timeframe", ["H1"])[0].strip().upper()
        service = self.application.risk
        problem = (
            "invalid symbol"
            if not SYMBOL_PATTERN.fullmatch(symbol)
            else service.supported(symbol, timeframe)
        )
        if problem:
            return self._response(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_risk_request", "message": problem},
            )
        payload = self.application.risk_proposal(symbol, timeframe)  # journals the assessment
        return self._response(start_response, HTTPStatus.OK, payload)

    def _account_payload(self, start_response: Callable, payload: dict) -> list[bytes]:
        """Phase 3 read-only account data. 503 when MT5 is unavailable and nothing validated
        is cached; otherwise 200 with explicit ``status`` (LIVE/CACHED) and validation."""
        if payload["status"] == "UNAVAILABLE":
            return self._response(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "account_unavailable", **payload},
            )
        return self._response(start_response, HTTPStatus.OK, payload)

    def _account_instrument(self, path: str, start_response: Callable) -> list[bytes]:
        symbol = path.removeprefix(_INSTRUMENT_PREFIX)
        service = self.application.account
        if not _INSTRUMENT_SYMBOL.fullmatch(symbol) or service.supported_pair(symbol) is None:
            return self._response(
                start_response,
                HTTPStatus.NOT_FOUND,
                {
                    "error": "unsupported_symbol",
                    "message": f"Supported instruments: {', '.join(service.pairs)}.",
                },
            )
        payload = service.instrument(symbol)
        if payload is None:
            return self._response(
                start_response,
                HTTPStatus.NOT_FOUND,
                {
                    "error": "unsupported_symbol",
                    "message": "That broker symbol is not the one this broker uses for the pair.",
                },
            )
        return self._account_payload(start_response, payload)

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
        content_type = (
            _ASSET_TYPES.get(Path(filename).suffix)
            or guess_type(filename)[0]
            or "application/octet-stream"
        )
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


def _date_range(query: dict) -> tuple[str | None, str | None]:
    """``from`` / ``to`` as UTC calendar dates (YYYY-MM-DD, both inclusive) -> candle-close
    bounds [start, end) in the journal's ``window_close_utc`` format."""
    bounds: list[str | None] = []
    days = []
    for name in ("from", "to"):
        text = query.get(name, [""])[0].strip()
        if not text:
            bounds.append(None)
            days.append(None)
            continue
        if not _DATE.fullmatch(text):
            raise ValueError(f"{name} must be a date YYYY-MM-DD")
        day = date.fromisoformat(text)
        days.append(day)
        bounds.append(
            (day if name == "from" else day + timedelta(days=1)).isoformat() + "T00:00:00Z"
        )
    if days[0] is not None and days[1] is not None and days[0] > days[1]:
        raise ValueError("from must not be after to")
    return bounds[0], bounds[1]


def _hostname(host_header: str) -> str:
    """Host header without port; handles bracketed IPv6 such as ``[::1]:8765``."""
    value = host_header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value
