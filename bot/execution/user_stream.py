"""Stream privado de usuario (Binance USDⓈ-M).

Ruta verificada 2026-10-15: wss://fstream.binance.com/private/ws/<listenKey>, clave por
POST /fapi/v1/listenKey y renovada con PUT cada ~30 min (caduca a los 60 min).

No se depende del formato de cada evento: cualquier evento de cuenta (órdenes, algo
órdenes, posiciones) despierta al gestor para que consulte el exchange enseguida.
La verdad sigue siendo el REST; el stream solo acelera la detección.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import websockets

from bot.core.logging_setup import get_logger
from bot.data_engine.http import backoff_delay

log = get_logger("bot.user_stream")

KEEPALIVE_S = 30 * 60
MAX_CONN_S = 23 * 3600
EXPIRED_EVENT = "listenKeyExpired"


class UserStream:
    def __init__(self, exchange, ws_private_base: str, on_event: Callable[[str, dict], None]):
        self.ex = exchange
        self.base = ws_private_base.rstrip("/")
        self.on_event = on_event
        self.connected = False
        self.last_event_monotonic: float | None = None
        self.reconnects = 0
        self._stop = asyncio.Event()

    def url(self, listen_key: str) -> str:
        return f"{self.base}/ws/{listen_key}"

    def handle(self, raw: str | bytes) -> bool:
        """Procesa un mensaje. Devuelve False si la clave caducó (hay que reconectar)."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return True
        data = msg.get("data", msg) if isinstance(msg, dict) else {}
        event = data.get("e", "") if isinstance(data, dict) else ""
        self.last_event_monotonic = time.monotonic()
        if event == EXPIRED_EVENT:
            log.warning("listenKey caducada: se renueva")
            return False
        if event:
            self.on_event(event, data)
        return True

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_S)
            try:
                await self.ex.keepalive_listen_key()
            except Exception as exc:  # noqa: BLE001
                log.warning("keepalive de listenKey falló", extra={"err": repr(exc)})

    async def run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            keepalive: asyncio.Task | None = None
            try:
                key = await self.ex.new_listen_key()
                async with websockets.connect(self.url(key), ping_interval=None, open_timeout=15,
                                              close_timeout=5) as ws:
                    self.connected, attempt = True, 0
                    keepalive = asyncio.create_task(self._keepalive())
                    log.info("stream de usuario conectado")
                    # Tras (re)conectar, forzar una consulta por si algo pasó mientras no escuchábamos
                    self.on_event("reconnected", {})
                    started = time.monotonic()
                    async for raw in ws:
                        if not self.handle(raw) or time.monotonic() - started > MAX_CONN_S or self._stop.is_set():
                            break
            except Exception as exc:  # noqa: BLE001
                log.warning("stream de usuario caído", extra={"err": repr(exc)})
            finally:
                self.connected = False
                if keepalive is not None:
                    keepalive.cancel()
            if self._stop.is_set():
                return
            self.reconnects += 1
            await asyncio.sleep(backoff_delay(attempt, base=1.0, cap=60.0))
            attempt += 1

    def stop(self) -> None:
        self._stop.set()
