"""Dependency-free headless browser driver for smoke tests (test-only code).

Drives an installed Chromium-family browser (Edge or Chrome) through the Chrome DevTools
Protocol using a minimal standard-library WebSocket client. No Playwright/Selenium.

Browser discovery: ``JOHANNA_BROWSER`` (path to an executable), then common Windows install
locations, then ``google-chrome``/``chromium``/``microsoft-edge`` on PATH. When none is
found, ``find_browser()`` returns ``None`` and the browser tests are skipped.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_PATH_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "msedge",
    "chrome",
)


def find_browser() -> str | None:
    configured = os.environ.get("JOHANNA_BROWSER")
    if configured:
        return configured if Path(configured).is_file() else None
    for candidate in _WINDOWS_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in _PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class _WebSocket:
    """Just enough RFC 6455 for one CDP connection: masked text frames out, frames in."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parts = urlsplit(url)
        self._sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake closed")
            response += chunk
        header, _, self._buffer = response.partition(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket handshake failed: {header[:120]!r}")

    def _exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._sock.recv(max(65536, size - len(self._buffer)))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def send(self, text: str, opcode: int = 0x1) -> None:
        payload = text.encode() if isinstance(text, str) else text
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def recv(self) -> str:
        message = b""
        while True:
            first, second = self._exact(2)
            opcode, fin = first & 0x0F, first & 0x80
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._exact(8))[0]
            mask = self._exact(4) if second & 0x80 else None
            payload = self._exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x9:  # ping
                self.send(payload, opcode=0xA)
                continue
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by browser")
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if fin:
                    return message.decode()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Browser:
    """One headless browser with one page, controlled synchronously over CDP."""

    def __init__(self, executable: str) -> None:
        self._profile = tempfile.mkdtemp(prefix="johanna-browser-")
        self._process = subprocess.Popen(
            [
                executable,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--remote-debugging-port=0",
                f"--user-data-dir={self._profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = Path(self._profile) / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not port_file.is_file() or not port_file.read_text().strip():
            if time.monotonic() > deadline or self._process.poll() is not None:
                self.close()
                raise RuntimeError("Browser did not expose a DevTools port")
            time.sleep(0.05)
        port = int(port_file.read_text().split()[0])
        targets: list[dict[str, Any]] = []
        while not targets:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
                targets = [t for t in json.loads(r.read()) if t.get("type") == "page"]
            if not targets and time.monotonic() > deadline:
                raise RuntimeError("Browser has no page target")
        self._ws = _WebSocket(targets[0]["webSocketDebuggerUrl"])
        self._next_id = 0
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.set_viewport(1600, 1000)

    def send(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error: {result['exceptionDetails'].get('text')}: {expression}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 10.0) -> bool:
        """Poll until the expression is truthy *in JavaScript* (DOM nodes serialize as {})."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = self.evaluate(f"!!({expression})")
            except RuntimeError:
                last = None
            if last:
                return last
            time.sleep(0.05)
        raise AssertionError(f"Timed out waiting for: {expression} (last={last!r})")

    def open(self, url: str) -> None:
        self.send("Page.navigate", url=url)
        self.wait_for("document.readyState === 'complete'")

    def set_viewport(self, width: int, height: int, *, mobile: bool = False) -> None:
        self.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=mobile,
        )

    def text(self, selector: str) -> str:
        return self.evaluate(
            f"(document.querySelector({json.dumps(selector)})?.innerText ?? '').trim()"
        )

    def click(self, selector: str) -> None:
        clicked = self.evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            " if (!el) return false; el.click(); return true; })()"
        )
        if not clicked:
            raise AssertionError(f"No element to click: {selector}")

    def select(self, selector: str, value: str) -> None:
        self.evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" el.value = {json.dumps(value)};"
            " el.dispatchEvent(new Event('change', { bubbles: true })); })()"
        )

    def close(self) -> None:
        try:
            if hasattr(self, "_ws"):
                try:
                    self.send("Browser.close")
                except Exception:
                    pass
                self._ws.close()
        finally:
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
            shutil.rmtree(self._profile, ignore_errors=True)
