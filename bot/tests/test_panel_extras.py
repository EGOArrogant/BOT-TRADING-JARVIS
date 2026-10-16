"""Reportes en palabras simples, notificaciones, reporte de errores, Telegram y demo con capital nuevo."""
import json
from datetime import UTC, datetime

from bot.core import notify
from bot.core.config import Mode, StorageConfig, load_settings
from bot.dashboard import diagnostics, reports


def _settings(tmp_path, capital=27):
    s = load_settings(mode="paper")
    st = StorageConfig(db_path=tmp_path / "bot_paper.sqlite", reports_dir=tmp_path / "rep" / "paper",
                       state_path=tmp_path / "state_paper.json")
    return s.model_copy(update={"storage": st, "paper": s.paper.model_copy(update={"initial_capital": capital}),
                                "logging": s.logging.model_copy(update={"dir": tmp_path / "logs"})})


def test_day_report_in_plain_words():
    t = {"symbol": "ETHUSDT", "side": "LONG", "strategy": "trend_pullback", "exit_reason": "take_profit", "pnl_net": 1.25,
         "fees": 0.02, "closed_at": "2026-10-15T10:00:00+00:00", "data": json.dumps({"entry_price": 2600, "exit_price": 2650})}
    cands = [{"status": "rechazado_filtro", "reasons": ["bloqueo macro: CPI 2026-10-15T12:30:00+00:00"], "ai": None},
             {"status": "rechazado_ia", "reasons": [], "ai": {"action": "NO_TRADE"}, "counterfactual": {"r_net": -1.0}},
             {"status": "operado", "reasons": [], "ai": {"action": "LONG"}}]
    r = reports.day_report("2026-10-15", [t], cands, 0.005, 27)
    assert r["headline"] == "Ganó 1,25 $ en 1 operación" and r["tone"] == "pos"
    text = " ".join(r["paragraphs"])
    assert "noticia económica importante" in text and "acertó 1 de 1" in text
    assert "Compró Ethereum" in r["trades"][0]["text"] and "llegó a su objetivo" in r["trades"][0]["text"]
    empty = reports.day_report("2026-10-16", [], [], 0.005, 27)
    assert empty["headline"] == "No operó" and "resultado válido" in empty["paragraphs"][0]
    assert reports.plain_reason("techo diario alcanzado (5/5)") == "ya había hecho el máximo de operaciones del día"


def test_notifications_read_like_messages():
    e = notify.entry("SOLUSDT", "SHORT", "range_breakout", 150.0, 152.0, 146.0, 0.1, 0.8)
    assert e.title == "🔴 Entrada SHORT en Solana" and "Arriesga como máximo 0,20 $" in e.text and "80 %" in e.text
    x = notify.exit_("SOLUSDT", "SHORT", "stop", -0.21, -1.05, 26.79)
    assert x.title.startswith("🔻 Perdió 0,21 $") and x.level == "bad" and "26,79 $" in x.text


def test_error_report_hides_secrets_and_groups_repeats(tmp_path):
    s = _settings(tmp_path)
    s.logging.dir.mkdir(parents=True)
    now = datetime(2026, 10, 16, 8, tzinfo=UTC)
    lines = [{"ts": "2026-10-16T07:00:00+00:00", "level": "ERROR", "logger": "x", "msg": "fuente caída",
              "err": "key sk-ant-abc123456789 fallo"}] * 3 + \
            [{"ts": "2026-10-16T07:30:00+00:00", "level": "INFO", "logger": "x", "msg": "todo bien"}]
    (s.logging.dir / "bot.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    r = diagnostics.error_report(s, {"sources": [{"name": "Binance", "ok": False, "age_s": 99}]}, ["boom (page.html:1)"],
                                 now=now, hide=["CLAVEPANEL123"])
    assert "sk-ant" not in r["text"] and "×3" in r["text"] and "Binance (99 s)" in r["text"] and "boom" in r["text"]
    assert r["errors"] >= 2 and "todo bien" not in r["text"]
    assert diagnostics.scrub("url ?t=ABCDEFGHIJKLMNOPQRS y CLAVEPANEL123", ["CLAVEPANEL123"]).count("[oculto]") == 2


def test_ai_diagnosis_is_rate_limited_and_never_needs_code_changes():
    calls, t = [], [0.0]
    d = diagnostics.Diagnoser("k", "m", ask=lambda m, s, u: calls.append((s, u)) or "Qué pasa: nada grave.",
                              clock=lambda: t[0])
    assert d.diagnose("reporte")["ok"] and "nunca sugieras desactivar stops" in calls[0][0]
    assert not d.diagnose("otra")["ok"] and len(calls) == 1  # una por minuto
    t[0] += 61
    assert d.diagnose("otra")["ok"]
    assert not diagnostics.Diagnoser(None, "m").diagnose("x")["ok"]


def test_demo_restarts_cleanly_when_capital_changes(tmp_path):
    from bot import demo_reset
    s = _settings(tmp_path, capital=2000)
    assert demo_reset.check(s) is None  # instalación nueva: solo anota el capital
    s.storage.db_path.write_text("db"), s.storage.state_path.write_text("{}")
    s.storage.reports_dir.mkdir(parents=True)
    (s.storage.reports_dir / "zona_2026-10-15.md").write_text("z")
    (s.storage.reports_dir / "panel_clave.txt").write_text("clave")
    assert demo_reset.check(s) is None  # mismo capital: no toca nada
    moved = demo_reset.check(_settings(tmp_path, capital=27))
    assert moved is not None and "capital_2000" in moved.name
    assert not s.storage.db_path.exists() and (moved / "bot_paper.sqlite").exists()
    assert (moved / "reports" / "zona_2026-10-15.md").exists() and (s.storage.reports_dir / "panel_clave.txt").exists()
    assert demo_reset.check(_settings(tmp_path, capital=27)) is None
    live = _settings(tmp_path, capital=5).model_copy(update={"mode": Mode.LIVE})
    assert demo_reset.check(live) is None  # nunca en dinero real


def test_telegram_setup_finds_chat_and_saves_env(tmp_path):
    from bot import telegram_setup
    seq = [[], [{"update_id": 5, "message": {"chat": {"id": 777}, "text": "hola"}}]]
    assert telegram_setup.wait_chat_id("T", timeout_s=5, ask=lambda *a, **k: seq.pop(0)) == "777"
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=x\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    telegram_setup.set_env({"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "777"}, env)
    assert env.read_text().splitlines() == ["ANTHROPIC_API_KEY=x", "TELEGRAM_BOT_TOKEN=T", "TELEGRAM_CHAT_ID=777"]
