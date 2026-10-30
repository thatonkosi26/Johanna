"""Manual read-only MetaTrader 5 connection and data smoke test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from johanna.adapters.base import MT5AdapterError
from johanna.adapters.mt5 import MetaTrader5Adapter, metadata_path


def run_smoke(
    adapter: MetaTrader5Adapter,
    *,
    symbol: str | None = None,
    metadata_dir: str | Path = "data/broker_specs",
) -> int:
    """Print connection and sample read results; return nonzero if inaccessible."""
    try:
        adapter.initialize()
    except MT5AdapterError as exc:
        print(f"MT5 connection failed: {exc}", file=sys.stderr)
        return 2

    try:
        status = adapter.connection_status()
        print(f"Terminal connected: {status.terminal_connected}")
        print(f"Account connected: {status.account_connected}")
        if not status.connected:
            print("The terminal and account must both be connected to continue.", file=sys.stderr)
            return 3

        try:
            symbols = adapter.list_symbols()
        except MT5AdapterError as exc:
            print(f"Symbol listing unavailable: {exc}", file=sys.stderr)
            return 4
        print(f"Available symbols ({len(symbols)}): {', '.join(symbols)}")
        chosen = symbol or ("EURUSD" if "EURUSD" in symbols else (symbols[0] if symbols else None))
        if chosen is None:
            print("No symbols are available in this terminal.", file=sys.stderr)
            return 5

        sample_errors: dict[str, str] = {}
        tick = None
        bars = None
        metadata = None
        try:
            adapter.select_symbol(chosen)
        except MT5AdapterError as exc:
            sample_errors["symbol_selection"] = str(exc)
        if not sample_errors:
            try:
                tick = adapter.get_tick(chosen)
            except MT5AdapterError as exc:
                sample_errors["tick"] = str(exc)
            try:
                bars = adapter.get_bars(chosen, "M5", 5)
            except MT5AdapterError as exc:
                sample_errors["m5_bars"] = str(exc)
            try:
                metadata = adapter.capture_symbol_metadata(chosen)
            except MT5AdapterError as exc:
                sample_errors["symbol_metadata"] = str(exc)

        destination = None
        if metadata is not None:
            destination = metadata_path(metadata_dir, chosen)
            adapter.save_metadata(metadata, destination)
        report = {
            "sample_symbol": chosen,
            "sample_tick": tick,
            "sample_m5_bars": bars,
            "sample_errors": sample_errors,
            "symbol_metadata_file": str(destination) if destination is not None else None,
            "server_time": metadata["server_time"] if metadata is not None else None,
        }
        print(json.dumps(report, indent=2, default=_json_default))
        return 6 if sample_errors else 0
    finally:
        adapter.shutdown()


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize smoke-test value of type {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", help="Exact broker symbol name (default: EURUSD if present)")
    parser.add_argument(
        "--terminal-path", help="Optional path to terminal64.exe; never provide credentials here"
    )
    parser.add_argument(
        "--metadata-dir",
        default="data/broker_specs",
        help="Directory for local broker-spec JSON files (default: data/broker_specs)",
    )
    args = parser.parse_args()
    return run_smoke(
        MetaTrader5Adapter(args.terminal_path),
        symbol=args.symbol,
        metadata_dir=args.metadata_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
