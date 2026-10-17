"""Comandos de Telegram: solo el dueño, nunca abren operaciones, Solo cierre igual que el panel."""
import asyncio
from datetime import UTC, datetime

import httpx

from bot.core import control
from bot.core.telegram_commands import TelegramCommands
from bot.tests.test_engine_paper import FakeAnalyst, make_engine, setup  # noqa: F401


def test_commands_answer_and_switch_close_only(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    tc = TelegramCommands("T", "42", eng, now=lambda: now)
    assert "Operando" in tc.handle("/estado") and "Capital" in tc.handle("/estado")
    assert tc.handle("/posiciones") == "No hay operaciones abiertas."
    assert "No operó" in tc.handle("/hoy")
    assert "Solo cierre" in tc.handle("/solo_cierre@mi_bot")
    assert control.load(control.path_for(s)).close_only and "Solo cierre" in tc.handle("/estado")
    tc.handle("/operar")
    assert not control.load(control.path_for(s)).close_only
    assert "/ayuda" in tc.handle("/comprar BTC")  # no existe ningún comando para operar


def test_only_the_owner_chat_is_answered(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/solo_cierre"}},
                {"update_id": 2, "message": {"chat": {"id": 42}, "text": "/estado"}}]})
        sent.append(req)
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            tc = TelegramCommands("T", "42", eng, http=http, now=lambda: datetime.now(UTC))
            return await tc.poll_once(), tc.offset
    handled, offset = asyncio.run(go())
    assert handled == 1 and offset == 3 and len(sent) == 1
    assert not control.load(control.path_for(s)).close_only  # el extraño no pudo pausar el bot
