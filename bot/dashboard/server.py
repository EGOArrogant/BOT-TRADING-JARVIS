"""Servidor del panel: solo en 127.0.0.1 (no se expone a internet) y solo lectura.

    python -m bot.main panel --mode paper [--port 8765]
"""

from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bot.core.config import Settings
from bot.core.logging_setup import get_logger
from bot.dashboard.data import collect

log = get_logger("bot.dashboard")

PAGE = Path(__file__).with_name("page.html")


def _clean(x: Any) -> Any:
    """JSON válido para el navegador: sin Infinity ni NaN."""
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    return x


def make_handler(settings: Settings) -> type[BaseHTTPRequestHandler]:
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/estado":
                try:
                    body = json.dumps(_clean(collect(settings)), ensure_ascii=False, default=str).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: no se pudo leer el estado", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
            else:
                self._send(404, b"no encontrado", "text/plain; charset=utf-8")

        def log_message(self, *args: Any) -> None:  # sin ruido en consola por cada consulta
            pass

    return Handler


def serve(settings: Settings, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(settings))
