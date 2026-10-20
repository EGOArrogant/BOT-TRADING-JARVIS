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


def test_clave_only_when_phone_mode_is_on(setup, tmp_path, monkeypatch):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    tc = TelegramCommands("T", "42", eng, now=lambda: now, root=tmp_path)
    assert "apagado" in tc.handle("/clave") and not (s.storage.reports_dir / "panel_clave.txt").exists()
    (tmp_path / "celular_activado.txt").write_text("celular")
    monkeypatch.setattr("bot.dashboard.remote.local_ips", lambda: [{"ip": "192.168.1.20", "kind": "casa"}])
    out = tc.handle("/clave")
    key = (s.storage.reports_dir / "panel_clave.txt").read_text().strip()  # la misma que usa el panel
    assert key in out and f"http://192.168.1.20:8765/?t={key}" in out


def test_actualizar_replies_first_then_restarts(setup, tmp_path, monkeypatch):
    from bot import updater
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    calls, sent = [], []

    def fake_apply(root, client=None, install=True, restart=True):
        calls.append(restart)
        assert not updater.restart_flag(root).exists()  # todavía no: primero se contesta
        return {"updated": True, "changes": [{"title": "Mejora nueva"}]}
    monkeypatch.setattr(updater, "apply", fake_apply)

    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read().decode()
        if req.url.path.endswith("getUpdates") and '"timeout":0' not in body.replace(" ", ""):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/actualizar"}}]})
        sent.append((req.url.path.rsplit("/", 1)[-1], body, updater.restart_flag(tmp_path).exists()))
        return httpx.Response(200, json={"ok": True, "result": []})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            tc = TelegramCommands("T", "42", eng, http=http, now=lambda: datetime.now(UTC), root=tmp_path)
            await tc.poll_once()
    asyncio.run(go())
    assert calls == [False]
    (m, body, flag_then), (ack, ack_body, _) = sent
    assert m == "sendMessage" and "Actualizado" in body and "Mejora nueva" in body and not flag_then
    assert ack == "getUpdates" and '"offset":8' in ack_body.replace(" ", "")  # ya leído: no se repite al reiniciar
    assert updater.restart_flag(tmp_path).exists()


def test_actualizar_when_already_current_or_failing(setup, tmp_path, monkeypatch):
    from bot import updater
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    tc = TelegramCommands("T", "42", eng, now=lambda: now, root=tmp_path)
    monkeypatch.setattr(updater, "apply", lambda *a, **k: {"updated": False})
    assert "más reciente" in tc.handle("/actualizar") and not tc.restart_after_reply

    def boom(*a, **k):
        raise RuntimeError("sin internet")
    monkeypatch.setattr(updater, "apply", boom)
    assert "No se pudo" in tc.handle("/actualizar") and not tc.restart_after_reply
