"""Reporte de errores del panel y diagnóstico en palabras simples con IA.

- `error_report` junta lo que sirve para encontrar un problema: errores y avisos recientes del registro
  del bot y del panel, fuentes de datos caídas, la revisión de la copia y datos del equipo. Sin llaves
  ni claves: se borran antes de mostrarse, para que se pueda copiar y pegar en el chat con Claude.
- `diagnose` le pide a la IA (Anthropic) que lo explique y diga qué hacer. Solo da consejos: NUNCA
  cambia código, configuración ni límites (CLAUDE.md §1 y §7); los arreglos de código se hacen aparte.
"""

from __future__ import annotations

import json
import platform
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bot.core.config import Settings

MAX_LINES = 40
TAIL_BYTES = 2_000_000
MAX_REPORT = 20_000
MIN_INTERVAL_S = 60.0

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)(api[_-]?key|secret|token|signature|listenkey|password)([\"'=:\s]+)[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"[?&]t=[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b[A-Za-z0-9]{64}\b"),  # llaves de Binance
]

DIAG_SYSTEM = """Eres el asistente de soporte del bot de trading JARVIS (Python, corre en la PC con Windows del usuario,
modo demo con dinero ficticio y precios reales de Binance). El usuario no programa y habla español de México.

Te llega un reporte de errores del panel. Tu tarea:
1. Explica en palabras simples qué está pasando (2–4 frases). Si no hay errores reales, dilo con calma.
2. Di si es grave o no (¿el bot sigue protegido? ¿puede seguir corriendo?).
3. Da pasos concretos y seguros que el usuario pueda hacer (reiniciar iniciar_demo.bat, revisar internet,
   ejecutar instalar.bat, recargar saldo de Anthropic, etc.).
4. Si el problema necesita cambiar código del bot, dilo claramente y pide que copie el reporte y se lo
   pase a Claude Code en el chat del proyecto.

Reglas: no inventes causas que el reporte no muestre; nunca sugieras desactivar stops, límites de riesgo,
filtros de seguridad ni operar con dinero real; nunca pidas llaves o contraseñas. Responde en texto breve con
títulos cortos (Qué pasa / ¿Es grave? / Qué hacer)."""


def scrub(text: str, extra: list[str] | None = None) -> str:
    for word in extra or []:
        if word:
            text = text.replace(word, "[oculto]")
    for pat in SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + m.group(2) + "[oculto]") if m.lastindex and m.lastindex >= 2 else "[oculto]",
                       text)
    return text


def _tail(path: Path, nbytes: int = TAIL_BYTES) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > nbytes else lines


def log_problems(log_dir: Path, since: datetime) -> list[dict[str, Any]]:
    """Errores y avisos recientes (los repetidos se agrupan) del registro del bot y del panel."""
    seen: dict[str, dict[str, Any]] = {}
    for src, path in (("bot", log_dir / "bot.jsonl"), ("panel", log_dir / "panel" / "bot.jsonl")):
        for line in _tail(path):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("level") not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            try:
                ts = datetime.fromisoformat(rec["ts"])
            except (KeyError, ValueError):
                continue
            if ts < since:
                continue
            extra = {k: v for k, v in rec.items() if k not in ("ts", "level", "logger", "msg", "exc")}
            key = f"{src}|{rec.get('level')}|{rec.get('msg')}"
            item = seen.get(key)
            if item is None:
                seen[key] = {"src": src, "level": rec["level"], "msg": rec.get("msg", ""), "first": rec["ts"],
                             "last": rec["ts"], "count": 1, "detail": json.dumps(extra, ensure_ascii=False)[:400],
                             "exc": (rec.get("exc") or "")[-1500:]}
            else:
                item["count"] += 1
                item["last"] = rec["ts"]
                item["detail"] = json.dumps(extra, ensure_ascii=False)[:400]
                if rec.get("exc"):
                    item["exc"] = rec["exc"][-1500:]
    out = sorted(seen.values(), key=lambda x: x["last"], reverse=True)
    return out[:MAX_LINES]


def error_report(settings: Settings, live_system: dict | None, client_errors: list[str] | None = None,
                 now: datetime | None = None, hide: list[str] | None = None) -> dict[str, Any]:
    from bot import integridad

    now = now or datetime.now(UTC)
    probs = log_problems(settings.logging.dir, now - timedelta(hours=48))
    copy_issues = integridad.check()
    sources_down = [s for s in (live_system or {}).get("sources", []) if not s.get("ok")]
    lines = [f"Reporte de errores de JARVIS — {now.strftime('%Y-%m-%d %H:%M')} UTC",
             f"Modo: {settings.mode.value} · Python {sys.version.split()[0]} · {platform.system()} {platform.release()}",
             f"Copia del bot: {'completa' if not copy_issues else 'CON PROBLEMAS: ' + '; '.join(copy_issues[:5])}"]
    if live_system is None:
        lines.append("El bot no está publicando su estado (¿está apagado?).")
    if sources_down:
        lines.append("Fuentes de datos sin actualizar: " + ", ".join(f"{s.get('name')} ({s.get('age_s')} s)" for s in sources_down))
    ai = (live_system or {}).get("ai") or {}
    if ai.get("code") == "falla":
        lines.append(f"IA: {ai.get('label')} — {ai.get('detail', '')}")
    if client_errors:
        lines.append("")
        lines.append("Errores de la página del panel:")
        lines += [f"- {e[:300]}" for e in client_errors[:10]]
    lines.append("")
    if probs:
        lines.append(f"Errores y avisos de las últimas 48 h ({len(probs)} distintos, el más reciente primero):")
        for p in probs:
            rep = f" ×{p['count']} (último {p['last'][:16]})" if p["count"] > 1 else ""
            lines.append(f"- [{p['level']}] {p['first'][:16]} {p['src']}: {p['msg']}{rep} {p['detail']}")
            if p["exc"]:
                lines.append("  " + p["exc"].replace("\n", "\n  "))
    else:
        lines.append("Sin errores ni avisos en el registro de las últimas 48 h.")
    text = scrub("\n".join(lines), hide)[:MAX_REPORT]
    n_err = sum(1 for p in probs if p["level"] in ("ERROR", "CRITICAL")) + len(copy_issues) + len(client_errors or [])
    return {"text": text, "errors": n_err, "warnings": sum(1 for p in probs if p["level"] == "WARNING") + len(sources_down),
            "items": probs[:10]}


Ask = Callable[[str, str, str], str]


def _anthropic_ask(api_key: str, model: str, system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=60, max_retries=1)
    msg = client.messages.create(model=model, max_tokens=1200, system=system,
                                 messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


class Diagnoser:
    """Diagnóstico con IA, como mucho una consulta por minuto (cuesta centavos)."""

    def __init__(self, api_key: str | None, model: str, ask: Ask | None = None, clock=time.monotonic):
        self.api_key, self.model = api_key, model
        self.ask = ask or (lambda m, s, u: _anthropic_ask(self.api_key or "", m, s, u))
        self.clock = clock
        self._last = -1e9
        self._lock = threading.Lock()

    def diagnose(self, report: str) -> dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "text": "No hay llave de Anthropic en .env: ejecuta instalar.bat (o python -m bot.revisar) "
                                         "para agregarla. Mientras tanto puedes copiar el reporte y pegarlo en el chat."}
        with self._lock:
            wait = MIN_INTERVAL_S - (self.clock() - self._last)
            if wait > 0:
                return {"ok": False, "text": f"Espera {int(wait) + 1} segundos antes de pedir otro diagnóstico."}
            self._last = self.clock()
        try:
            text = self.ask(self.model, DIAG_SYSTEM, "Reporte:\n\n" + scrub(report)[:MAX_REPORT])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": f"La IA no respondió ({exc.__class__.__name__}). Copia el reporte y pégalo en el chat."}
        return {"ok": True, "text": text or "La IA no devolvió texto."}
