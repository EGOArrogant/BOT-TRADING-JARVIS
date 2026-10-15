"""Alertas: Telegram opcional; siempre se registran en el log.

Envío asíncrono y no bloqueante: una alerta que falla nunca detiene el bot.
Con deduplicación para no inundar el chat con la misma alerta.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from bot.core.logging_setup import get_logger

log = get_logger("bot.alerts")


class Alerter:
    def __init__(self, enabled: bool, token: str | None, chat_id: str | None, dedupe_s: float = 300):
        self.enabled = bool(enabled and token and chat_id)
        self._url = f"https://api.telegram.org/bot{token}/sendMessage" if token else ""
        self._chat = chat_id
        self._dedupe_s = dedupe_s
        self._last: dict[str, float] = {}
        self.sent: list[str] = []

    def __call__(self, msg: str, level: str = "error") -> None:
        now = time.monotonic()
        key = msg[:80]
        if now - self._last.get(key, -1e9) < self._dedupe_s:
            return
        self._last[key] = now
        getattr(log, level if level in ("info", "warning", "error") else "error")("ALERTA", extra={"alert": msg})
        self.sent.append(msg)
        if self.enabled:
            try:
                asyncio.get_running_loop().create_task(self._send(msg))
            except RuntimeError:
                pass  # sin loop (tests/CLI): queda en el log

    async def _send(self, msg: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(self._url, json={"chat_id": self._chat, "text": f"[bot] {msg}"[:4000]})
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram falló", extra={"err": type(exc).__name__})
