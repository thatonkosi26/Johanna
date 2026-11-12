"""Run the persistent Johanna backend application."""

from __future__ import annotations

import argparse
import logging
import signal
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, ConfigurationError
from johanna.instance_lock import InstanceLockError
from johanna.logging_config import configure_logging

ACCESS_LOGGER = logging.getLogger("johanna.access")


class ExclusiveWSGIServer(ThreadingMixIn, WSGIServer):
    """Threaded (a slow MT5 request never blocks other requests) and refuses to share a
    port: without ``allow_reuse_address = False`` Windows lets a second backend bind it."""

    allow_reuse_address = False
    daemon_threads = True


class LoggingRequestHandler(WSGIRequestHandler):
    """Send access lines through structured logging instead of raw stderr."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        ACCESS_LOGGER.info(
            "%s %s",
            self.address_string(),
            format % args,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Johanna backend")
    parser.add_argument("--config", help="TOML configuration file (defaults to built-in settings)")
    args = parser.parse_args(argv)
    try:
        config = AppConfig.load(args.config)
        application = JohannaApplication(config)
    except ConfigurationError as exc:
        print(f"Johanna configuration error: {exc}")
        return 2
    configure_logging(config.log_level)
    logger = logging.getLogger("johanna")
    previous_term = None

    def request_shutdown(signum, frame) -> None:
        del signum, frame
        raise KeyboardInterrupt

    # Bind first: if the port is taken, no lock is acquired and no worker starts.
    try:
        server = make_server(
            config.server.host,
            config.server.port,
            BackendAPI(application),
            server_class=ExclusiveWSGIServer,
            handler_class=LoggingRequestHandler,
        )
    except OSError as exc:
        logger.error(
            "Johanna backend could not bind its address: %s",
            exc,
            extra={"host": config.server.host, "port": config.server.port},
        )
        return 1
    try:
        if hasattr(signal, "SIGTERM"):
            previous_term = signal.signal(signal.SIGTERM, request_shutdown)
        with server:
            application.start()
            logger.info(
                "Johanna backend started",
                extra={"host": config.server.host, "port": config.server.port},
            )
            server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except InstanceLockError as exc:
        logger.error("Johanna backend not started: %s", exc)
        return 1
    except Exception:
        logger.exception("Johanna backend failed")
        return 1
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        application.stop()
        logger.info("Johanna backend stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
