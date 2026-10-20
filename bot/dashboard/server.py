"""Servidor del panel: por defecto solo en 127.0.0.1 (no se expone a internet).

Lee el estado del bot y solo puede escribir una cosa: `control.json`
(botón Operar / Solo cierre y agresividad, que nunca superan config.yaml).

    python -m bot.main panel --mode paper [--port 8765] [--celular]

Con `--celular` también escucha en la red de casa / Tailscale, y todo lo que no venga de la propia PC
necesita la clave del panel (ver `dashboard/remote.py`).
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from bot.core import control
from bot.core.config import Settings
from bot.core.logging_setup import get_logger
from bot.dashboard.data import chart_signals, collect, midas_view
from bot import updater
from bot.dashboard import diagnostics, remote, reports, snapshot
from bot.dashboard.klines import KlineSource

log = get_logger("bot.dashboard")

CSRF_HEADER = "X-Jarvis-Panel"  # una web ajena no puede enviarla sin permiso CORS (que nunca se da)

PAGE = Path(__file__).with_name("page.html")
CHART_JS = Path(__file__).with_name("vendor") / "lightweight-charts.js"  # TradingView Lightweight Charts (Apache 2.0)
QR_JS = Path(__file__).with_name("vendor") / "qrcode.js"  # QR Code Generator, Kazuhiko Arase (MIT)
# Conexión cortada por el navegador (WinError 10053/10054 en Windows, EPIPE/ECONNRESET en Linux)
CLIENT_GONE = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)
DRAW_JS = Path(__file__).with_name("dibujo.js")  # herramientas de dibujo y capa del bot sobre el gráfico
STATIC = Path(__file__).with_name("static")
PUBLIC = {"/logo.svg": ("logo.svg", "image/svg+xml"), "/favicon.ico": ("logo.svg", "image/svg+xml"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json")}  # sin clave: solo logo e iconos
LOCKED_PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS</title><link rel="icon" href="/logo.svg"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<body style="font-family:-apple-system,sans-serif;padding:32px;max-width:520px;margin:auto;line-height:1.5">
<img src="/logo.svg" width="72" height="72" alt=""><h1>JARVIS · Panel</h1><p>Para ver el panel en este dispositivo, abre el panel en tu PC y escanea con la cámara
el código QR de la sección <b>Ver en tu celular</b>.</p></body></html>""".encode()


def _clean(x: Any) -> Any:
    """JSON válido para el navegador: sin Infinity ni NaN."""
    if isinstance(x, float):
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_clean(v) for v in x]
    return x


def make_handler(settings: Settings, klines: KlineSource | None = None, remote_key: str | None = None,
                 diagnoser: diagnostics.Diagnoser | None = None) -> type[BaseHTTPRequestHandler]:
    page = PAGE.read_bytes()
    chart_js = CHART_JS.read_bytes()
    qr_js = QR_JS.read_bytes()
    draw_js = DRAW_JS.read_bytes()
    public = {k: ((STATIC / f).read_bytes(), t) for k, (f, t) in PUBLIC.items()}
    klines = klines or KlineSource(settings.endpoints.binance.rest, settings.exchange.symbols)
    diagnoser = diagnoser or diagnostics.Diagnoser(None, settings.ai.reflection_model)
    version_cache: dict[str, Any] = {}
    update_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except CLIENT_GONE:  # el navegador cerró la pestaña o el celular se bloqueó a mitad: no es un error
                self.close_connection = True

        def _from_pc(self) -> bool:
            return remote.is_loopback(self.client_address[0])

        def _authorized(self) -> bool:
            """La PC entra siempre (con Host local); un celular solo con la cookie de la clave."""
            if self._from_pc():
                return self.headers.get("Host") in self._allowed_hosts()
            return remote_key is not None and remote.key_ok(remote.cookie_value(self.headers.get("Cookie")), remote_key)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if not self._from_pc() and path in ("/", "/index.html") and remote_key is not None \
                    and remote.key_ok(q.get("t", [""])[0], remote_key):
                # Primera visita desde el QR: guarda la clave en una cookie y quita la clave de la dirección
                self.send_response(302)
                self.send_header("Set-Cookie", f"{remote.COOKIE}={remote_key}; Max-Age=31536000; Path=/; HttpOnly; "
                                               "SameSite=Strict")
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in public:
                self._send(200, *public[path])
                return
            if not self._authorized():
                if path in ("/", "/index.html"):
                    self._send(401, LOCKED_PAGE, "text/html; charset=utf-8")
                else:
                    self._send(403, b'{"error": "solo desde este equipo"}', "application/json")
                return
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/acceso":
                # Solo la PC ve la clave: con ella arma el QR para el celular
                if not self._from_pc():
                    self._send(403, b'{"error": "solo desde la PC"}', "application/json")
                    return
                port = self.server.server_address[1]
                body = {"enabled": remote_key is not None, "port": port,
                        "urls": [{**ip, "url": f"http://{ip['ip']}:{port}/?t={remote_key}"} for ip in remote.local_ips()]
                        if remote_key else []}
                self._send(200, json.dumps(body).encode(), "application/json; charset=utf-8")
            elif path == "/qr.js":
                self._send(200, qr_js, "text/javascript; charset=utf-8")
            elif path == "/api/estado":
                try:
                    body = json.dumps(_clean(collect(settings)), ensure_ascii=False, default=str).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: no se pudo leer el estado", extra={"err": repr(exc)}, exc_info=True)
                    import traceback
                    where = " ← ".join(f"{fr.filename.replace(chr(92), '/').split('/bot/')[-1]}:{fr.lineno}"
                                       for fr in traceback.extract_tb(exc.__traceback__)[-3:][::-1])
                    self._send(500, json.dumps({"error": f"{exc.__class__.__name__}: {exc}", "donde": where},
                                               ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif path == "/api/version":
                try:
                    if not version_cache or time.monotonic() - version_cache["at"] > 60 or "force" in q:
                        version_cache.update(at=time.monotonic(), data=updater.check())
                    self._send(200, json.dumps(version_cache["data"]).encode(), "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._send(502, json.dumps({"error": f"no se pudo consultar GitHub ({exc.__class__.__name__})",
                                                "local": updater.local_version()}).encode(), "application/json")
            elif path == "/api/ideas":
                f = settings.storage.reports_dir / "ideas.md"
                text = f.read_text(encoding="utf-8")[-6000:] if f.exists() else ""
                self._send(200, json.dumps({"ideas": text}, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif path == "/api/reportes":
                try:
                    body = json.dumps(_clean(reports.collect(settings)), ensure_ascii=False, default=str).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: no se pudieron armar los reportes", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
            elif path == "/grafico.js":
                self._send(200, chart_js, "text/javascript; charset=utf-8")
            elif path in ("/api/mercado", "/api/libro"):
                try:
                    sym = q.get("symbol", [""])[0]
                    body = klines.ticker(sym) if path == "/api/mercado" else klines.depth(sym)
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                except Exception as exc:  # noqa: BLE001  (sin Binance: el panel usa lo que publica el bot)
                    self._send(502, json.dumps({"error": repr(exc)}).encode(), "application/json")
                    return
                self._send(200, json.dumps(_clean(body), separators=(",", ":")).encode(), "application/json")
            elif path == "/api/midas":
                try:
                    body = json.dumps(_clean(midas_view(settings, float(q.get("margin", ["20"])[0]),
                                                        int(q.get("days", ["30"])[0]))), default=str).encode()
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: MIDAS falló", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
                    return
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/dibujo.js":
                self._send(200, draw_js, "text/javascript; charset=utf-8")
            elif path == "/api/senales":
                try:
                    body = json.dumps(_clean(chart_signals(settings, q.get("symbol", [""])[0])), ensure_ascii=False,
                                      default=str).encode()
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: no se pudieron leer las señales", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
                    return
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/api/velas":
                try:
                    rows = klines.get(q.get("symbol", [""])[0], q.get("interval", [""])[0])
                except ValueError as exc:
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                except Exception as exc:  # noqa: BLE001  (sin internet / Binance caído: el panel usa su respaldo)
                    self._send(502, json.dumps({"error": repr(exc)}).encode(), "application/json")
                    return
                self._send(200, json.dumps(rows, separators=(",", ":")).encode(), "application/json")
            else:
                self._send(404, b"no encontrado", "text/plain; charset=utf-8")

        def _allowed_hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _local_request(self) -> bool:
            """Bloquea peticiones de otras webs abiertas en el navegador (CSRF / DNS rebinding)."""
            if not self._authorized() or self.headers.get(CSRF_HEADER) != "1":
                return False
            allowed = self._allowed_hosts() if self._from_pc() else {self.headers.get("Host")}
            origin = self.headers.get("Origin")
            return origin is None or origin.split("://", 1)[-1] in allowed

        def _json_body(self, limit: int) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= limit:
                raise ValueError("cuerpo vacío o demasiado grande")
            return json.loads(self.rfile.read(length))

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/api/control", "/api/errores", "/api/diagnostico", "/api/actualizar", "/api/idea",
                            "/api/midas/control"):
                self._send(404, b"no encontrado", "text/plain; charset=utf-8")
                return
            if not self._local_request():
                self._send(403, b'{"error": "solo desde este equipo"}', "application/json")
                return
            if path == "/api/actualizar":
                if not update_lock.acquire(blocking=False):
                    self._send(409, b'{"error": "ya se esta actualizando"}', "application/json")
                    return
                try:
                    out = updater.apply()
                except Exception as exc:  # noqa: BLE001
                    update_lock.release()
                    log.error("panel: actualización falló", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": f"no se pudo actualizar: {exc}"}, ensure_ascii=False).encode(),
                               "application/json")
                    return
                self._send(200, json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                if out.get("updated"):  # el panel también se reinicia para cargar la página nueva
                    log.info("panel: actualizado, reiniciando")
                    threading.Timer(1.5, lambda: os._exit(updater.RESTART_CODE)).start()
                else:
                    update_lock.release()
                return
            if path == "/api/midas/control":
                from bot.execution import midas as midas_acct
                if settings.mode.value != "paper":
                    self._send(403, b'{"error": "MIDAS solo existe en modo demo"}', "application/json")
                    return
                try:
                    f = settings.storage.reports_dir / midas_acct.CONTROL_FILENAME
                    cur = midas_acct.load_control(f, settings.midas).model_dump()
                    ctl = midas_acct.MidasControl.model_validate({**cur, **{k: v for k, v in self._json_body(2048).items()
                                                                           if k in cur}})
                    f.parent.mkdir(parents=True, exist_ok=True)
                    midas_acct.save_control(f, ctl)
                except Exception as exc:  # noqa: BLE001
                    self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json")
                    return
                log.info("panel: MIDAS cambiado", extra=ctl.model_dump())
                self._send(200, json.dumps({"ok": True, "control": ctl.model_dump()}).encode(), "application/json")
                return
            if path == "/api/idea":
                try:
                    text = str(self._json_body(8000).get("text") or "").strip()
                    if not text:
                        raise ValueError("idea vacía")
                    f = settings.storage.reports_dir / "ideas.md"
                    f.parent.mkdir(parents=True, exist_ok=True)
                    with open(f, "a", encoding="utf-8") as fh:
                        fh.write(f"- {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC — {text}\n")
                    ideas = f.read_text(encoding="utf-8")[-6000:]
                except Exception as exc:  # noqa: BLE001
                    self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
                    return
                self._send(200, json.dumps({"ok": True, "ideas": ideas}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
                return
            if path in ("/api/errores", "/api/diagnostico"):
                try:
                    body = self._json_body(64_000)
                    if path == "/api/errores":
                        live = snapshot.read(settings.storage.reports_dir / snapshot.FILENAME)
                        errs = [str(e) for e in (body.get("client_errors") or [])][:20]
                        out = diagnostics.error_report(settings, (live or {}).get("system"), errs,
                                                       hide=[remote_key] if remote_key else None)
                    else:
                        out = diagnoser.diagnose(str(body.get("report") or ""))
                except Exception as exc:  # noqa: BLE001
                    log.error("panel: reporte de errores falló", extra={"err": repr(exc)}, exc_info=True)
                    self._send(500, json.dumps({"error": repr(exc)}).encode(), "application/json")
                    return
                self._send(200, json.dumps(out, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            try:
                body = self._json_body(4096)
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


def serve(settings: Settings, port: int = 8765, host: str | None = None,
          klines: KlineSource | None = None, phone: bool = False, ai_key: str | None = None,
          diagnoser: diagnostics.Diagnoser | None = None) -> ThreadingHTTPServer:
    """phone=True: también escucha en la red (0.0.0.0) y exige la clave a todo lo que no sea esta PC."""
    key = remote.load_or_create_key(settings.storage.reports_dir) if phone else None
    host = host or ("0.0.0.0" if phone else "127.0.0.1")
    diagnoser = diagnoser or diagnostics.Diagnoser(ai_key, settings.ai.reflection_model)
    return _Server((host, port), make_handler(settings, klines, key, diagnoser))


class _Server(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """Una conexión que el navegador corta (pestaña cerrada, celular bloqueado, recarga) no es un error."""
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)
