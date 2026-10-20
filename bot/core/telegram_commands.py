"""Comandos de Telegram: ver y pausar el bot desde el teléfono, con cualquier red de internet.

Solo responde al chat configurado en `.env` (TELEGRAM_CHAT_ID); cualquier otro chat se ignora. Ningún
comando abre operaciones ni cambia límites de riesgo: lo más que hace es lo mismo que el botón del panel
(Operar / Solo cierre), que nunca afloja config.yaml (CLAUDE.md §1).

    /estado      cómo está el bot, capital y ganancia de hoy
    /posiciones  operaciones abiertas con su stop
    /hoy         resumen del día en palabras simples
    /solo_cierre no abrir nada nuevo (sigue cuidando lo abierto)
    /operar      volver a buscar oportunidades
    /clave       enlace y clave para entrar al panel desde el celular (solo si el modo celular está activo)
    /actualizar  baja la versión nueva y reinicia el bot y el panel (lo mismo que el botón del panel)
    /ayuda       esta lista

/clave manda la clave del panel por Telegram: solo al chat del dueño, y el enlace solo funciona en la red de casa o
por Tailscale (el panel nunca se expone a internet).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from bot import updater
from bot.core import control
from bot.core.logging_setup import get_logger
from bot.dashboard import remote
from bot.dashboard.reports import COIN, amount, day_report, money, num, price

log = get_logger("bot.telegram")

HELP = ("Comandos de JARVIS:\n"
        "/estado — cómo está el bot, capital y ganancia de hoy\n"
        "/posiciones — operaciones abiertas con su stop\n"
        "/hoy — resumen del día en palabras simples\n"
        "/solo_cierre — no abrir nada nuevo (sigue cuidando lo abierto)\n"
        "/operar — volver a buscar oportunidades\n"
        "/clave — enlace y clave para entrar al panel desde el celular\n"
        "/actualizar — instalar la versión nueva (el bot y el panel se reinician solos)\n"
        "/ayuda — esta lista")
PANEL_PORT = 8765  # el de abrir_panel.bat
SLOW = {"/actualizar"}  # van a internet: se atienden en otro hilo para no frenar el motor


class TelegramCommands:
    def __init__(self, token: str, chat_id: str, engine: Any, http: httpx.AsyncClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), root: Path = updater.ROOT,
                 update_client: Any = None):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)
        self.engine = engine
        self.http = http
        self.now = now
        self.offset: int | None = None
        self.root, self.update_client = root, update_client
        self.restart_after_reply = False

    # ------------------------------------------------------------ respuestas
    def handle(self, text: str) -> str:
        cmd = (text or "").strip().split()[0].split("@")[0].lower() if text and text.strip() else ""
        if cmd in ("/start", "/ayuda", "/help"):
            return HELP
        if cmd == "/estado":
            return self.status()
        if cmd == "/posiciones":
            return self.positions()
        if cmd == "/hoy":
            return self.today()
        if cmd in ("/solo_cierre", "/pausa", "/stopentry"):
            return self.set_mode("solo_cierre")
        if cmd == "/operar":
            return self.set_mode("operar")
        if cmd == "/clave":
            return self.panel_key()
        if cmd == "/actualizar":
            return self.update()
        return "No conozco ese comando.\n\n" + HELP

    def status(self) -> str:
        e = self.engine
        st = e.state
        ctl = control.load(control.path_for(e.s))
        if st.halted:
            label = f"⛔ Detenido por seguridad: {st.halt_reason}"
        elif st.safe_mode:
            label = f"⚠️ Modo seguro: {st.safe_mode_reason}"
        elif st.pause_until and st.pause_until > self.now():
            label = "⏸ En pausa tras varias pérdidas seguidas"
        elif ctl.close_only:
            label = "🟠 Solo cierre (no abre nada nuevo)"
        else:
            label = "🟢 Operando: buscando oportunidades"
        day = st.equity - st.day_start_equity
        n_pos = len(e.manager.positions) if e.manager else 0
        return (f"{label}\nCapital: {amount(st.equity)} (empezó con {amount(e.s.paper.initial_capital)})\n"
                f"Hoy: {money(day)} · operaciones nuevas hoy: {len(st.entries_on(self.now().date()))}\n"
                f"Posiciones abiertas: {n_pos} · agresividad {ctl.aggressiveness}/10")

    def positions(self) -> str:
        e = self.engine
        pos = e.manager.positions if e.manager else {}
        if not pos:
            return "No hay operaciones abiertas."
        lines = []
        for sym, p in pos.items():
            st = e.c.states.get(sym)
            mark = (st.mark_price or st.last_price) if st else None
            sign = 1 if p.side.value == "LONG" else -1
            pnl = (mark - float(p.entry_price)) * float(p.qty) * sign if mark else None
            lines.append(f"{'🟢 LONG' if sign > 0 else '🔴 SHORT'} {COIN.get(sym, sym)}: entrada {price(float(p.entry_price))}, "
                         f"stop {price(float(p.stop_price))}" + (f", ahora {money(pnl)}" if pnl is not None else ""))
        return "\n".join(lines)

    def today(self) -> str:
        e = self.engine
        start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rep = day_report(start.date().isoformat(), e.journal.trades(start, start + timedelta(days=1)),
                         e.journal.candidates(start, start + timedelta(days=1)), e.s.risk.risk_per_trade,
                         e.s.paper.initial_capital)
        return f"📊 {rep['headline']}\n" + "\n".join(rep["paragraphs"]) + f"\n💡 {rep['lesson']}"

    def set_mode(self, mode: str) -> str:
        path = control.path_for(self.engine.s)
        cur = control.load(path)
        control.save(path, control.Control(mode=mode, aggressiveness=cur.aggressiveness))
        log.info("telegram: modo cambiado", extra={"mode": mode})
        if mode == "solo_cierre":
            return "🟠 Listo: Solo cierre. No abrirá operaciones nuevas; sigue cuidando las abiertas. /operar para volver."
        return "🟢 Listo: Operando. Vuelve a buscar oportunidades con las reglas de seguridad de siempre."

    def panel_key(self) -> str:
        if not (self.root / "celular_activado.txt").exists():
            return ("El modo celular está apagado en la PC. Para usarlo: en la PC ejecuta activar_celular.bat y luego "
                    "abrir_panel.bat.")
        key = remote.load_or_create_key(self.engine.s.storage.reports_dir)
        ips = remote.local_ips()
        links = "\n".join(f"{'Tailscale' if ip['kind'] == 'tailscale' else 'Wi-Fi de casa'}: "
                          f"http://{ip['ip']}:{PANEL_PORT}/?t={key}" for ip in ips) or "(no encontré la dirección de la PC)"
        log.info("telegram: se envió el acceso al panel")
        return (f"🔑 Clave del panel: {key}\n\nEnlaces (ábrelos en el celular):\n{links}\n\n"
                "Si ya ves la pantalla de \"Panel protegido\", pega la clave ahí. El enlace de Wi-Fi de casa solo "
                "funciona conectado a esa red; fuera de casa necesitas Tailscale. No reenvíes este mensaje.")

    def update(self) -> str:
        """Igual que el botón Actualizar del panel. El reinicio se pide después de contestar (`restart_after_reply`)."""
        try:
            out = updater.apply(self.root, self.update_client, restart=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram: no se pudo actualizar", extra={"err": repr(exc)})
            return f"❌ No se pudo actualizar: {exc}. El bot sigue con la versión actual."
        if not out.get("updated"):
            return "✅ Ya tienes la versión más reciente."
        self.restart_after_reply = True
        titles = "\n".join(f"• {c['title']}" for c in (out.get("changes") or [])[:8])
        log.info("telegram: actualizado, se reinicia")
        return ("⬆️ Actualizado. El bot y el panel se reinician solos en unos segundos; en el celular la página se "
                f"recarga sola con la versión nueva.\n{titles}").rstrip()

    # ------------------------------------------------------------ red
    async def poll_once(self) -> int:
        http = self.http or httpx.AsyncClient(timeout=40)
        try:
            params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message"]}
            if self.offset is not None:
                params["offset"] = self.offset
            r = await http.post(self.url + "getUpdates", json=params)
            updates = r.json().get("result", []) if r.status_code == 200 else []
            handled = 0
            for u in updates:
                self.offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue  # solo el dueño
                text = msg.get("text", "")
                slow = (text or "").strip().split(" ")[0].split("@")[0].lower() in SLOW
                reply = await asyncio.to_thread(self.handle, text) if slow else self.handle(text)
                await http.post(self.url + "sendMessage", json={"chat_id": self.chat_id, "text": f"JARVIS · {reply}"[:4000]})
                handled += 1
            if self.restart_after_reply:
                # Confirma a Telegram que ya se leyó (si no, tras reiniciar volvería a llegar /actualizar) y reinicia
                await http.post(self.url + "getUpdates", json={"offset": self.offset, "timeout": 0})
                self.restart_after_reply = False
                updater.request_restart(self.root)
            return handled
        finally:
            if self.http is None:
                await http.aclose()

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram: no se pudieron leer comandos", extra={"err": type(exc).__name__})
                await asyncio.sleep(15)
            await asyncio.sleep(1)
