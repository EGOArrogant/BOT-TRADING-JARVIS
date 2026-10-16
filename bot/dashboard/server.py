"""Servidor del panel: solo en 127.0.0.1 (no se expone a internet).

Lee el estado del bot y solo puede escribir una cosa: `control.json`
(botón Operar / Solo cierre y agresividad, que nunca superan config.yaml).

    python -m bot.main panel --mode paper [--port 8765]
"""

from __future__ import annotations

import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bot.core import control
from bot.core.config import Settings
from bot.core.logging_setup import get_logger
from bot.dashboard.data import collect

log = get_logger("bot.dashboard")

CSRF_HEADER = "X-Jarvis-Panel"  # una web ajena no puede enviarla sin permiso CORS (que nunca se da)

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
                if self.headers.get("Host") not in self._allowed_hosts():
                    self._send(403, b'{"error": "solo desde este equipo"}', "application/json")
                    return
                try:
                    body = json.dumps(_clean(collect(settings)), ensure_ascii=False, default=str).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: no se pudo leer el estado", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._send(404, b"no encontrado", "text/plain; charset=utf-8")

        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _local_request(self) -> bool:
            """Bloquea peticiones de otras webs abiertas en el navegador (CSRF / DNS rebinding)."""
            allowed = self._allowed_hosts()
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") in allowed and self.headers.get(CSRF_HEADER) == "1"
                    and (origin is None or origin.split("://", 1)[-1] in allowed))

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api/control":
                self._send(404, b"no encontrado", "text/plain; charset=utf-8")
                return
            if not self._local_request():
                self._send(403, b'{"error": "solo desde este equipo"}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 4096:
                    raise ValueError("cuerpo vacío o demasiado grande")
                body = json.loads(self.rfile.read(length))
                path = control.path_for(settings)
                current = control.load(path)
                new = control.Control.model_validate({"mode": body.get("mode", current.mode),
                                                      "aggressiveness": body.get("aggressiveness",
                                                                                 current.aggressiveness)})
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"valor no válido: {exc}"}).encode(), "application/json")
                return
            saved = control.save(path, new)
            log.info("panel: controles cambiados", extra={"mode": saved.mode, "level": saved.aggressiveness})
            self._send(200, saved.model_dump_json().encode(), "application/json; charset=utf-8")

        def log_message(self, *args: Any) -> None:  # sin ruido en consola por cada consulta
            pass

    return Handler


def serve(settings: Settings, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(settings))
