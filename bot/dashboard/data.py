"""Reúne lo que muestra el panel: estado del bot, operaciones, decisiones y aprendizaje.

Solo lectura: la base de datos se abre en modo `ro` y el estado se lee del disco.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bot.core import control
from bot.core.config import Settings
from bot.core.state import BotState
from bot.dashboard import snapshot
from bot.metrics.performance import equity_curve, summarize
from bot.risk.engine import RiskEngine

STALE_AFTER = timedelta(minutes=3)  # el bot escribe la foto cada minuto

CANDIDATE_TEXT = {
    "operado": "Operó",
    "rechazado_filtro": "Descartado por filtros",
    "no_seleccionado": "Había uno mejor",
    "rechazado_ia": "La IA dijo que no",
    "rechazado_riesgo": "Descartado por riesgo",
    "sin_llenado": "La orden no se llenó",
}

EXIT_TEXT = {
    "stop": "Stop-loss", "take_profit": "Take-profit", "trailing": "Stop móvil", "time": "Por tiempo",
    "invalidation": "Tesis invalidada", "macro": "Antes de noticia macro", "funding": "Por funding",
    "ai_early_exit": "La IA pidió cerrar", "manual": "Manual", "safe_mode": "Modo seguro",
}


def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _rows(db: sqlite3.Connection | None, sql: str, args: tuple = ()) -> list[dict]:
    if db is None:
        return []
    try:
        return [dict(r) for r in db.execute(sql, args)]
    except sqlite3.OperationalError:  # tabla aún no creada (bot nunca llegó a esa parte)
        return []


def control_view(settings: Settings) -> dict[str, Any]:
    """Controles actuales y lo que significa cada nivel de agresividad (para el panel)."""
    ctl = control.load(control.path_for(settings))
    levels = []
    for lv in range(1, control.MAX_LEVEL + 1):
        eff, p = control.apply(settings, control.Control(aggressiveness=lv))
        levels.append({"level": lv, "risk_per_trade": eff.risk.risk_per_trade, "min_edge": p.min_edge,
                       "max_trades": p.max_trades, "min_ai_confidence": p.min_ai_confidence})
    return {"mode": ctl.mode, "aggressiveness": ctl.aggressiveness,
            "updated_at": ctl.updated_at.isoformat() if ctl.updated_at else None, "levels": levels}


def _status(settings: Settings, state: BotState | None, live: dict | None, now: datetime,
            close_only: bool = False) -> dict[str, Any]:
    if live is None or now - datetime.fromisoformat(live["updated_at"]) > STALE_AFTER:
        return {"code": "apagado", "label": "Apagado o sin conexión",
                "detail": "El bot no ha actualizado el panel en los últimos minutos. Revisa que siga corriendo."}
    if state is None:
        return {"code": "operando", "label": "Operando", "detail": "Arrancando."}
    guard = RiskEngine(settings.risk, settings.limits, settings.exchange).global_guard(state, now)
    if guard.ok and close_only:
        return {"code": "solo_cierre", "label": "Solo cierre",
                "detail": "No abre operaciones nuevas; sigue vigilando y cerrando las abiertas. Cámbialo con el botón."}
    if guard.ok:
        return {"code": "operando", "label": "Operando", "detail": "Buscando oportunidades."}
    code = "detenido" if state.halted else "pausado"
    return {"code": code, "label": "Detenido" if state.halted else "En pausa (no abre operaciones)",
            "detail": guard.reason}


def _trade_row(t: dict) -> dict[str, Any]:
    data = json.loads(t.get("data") or "{}")
    return {
        "id": t["id"], "symbol": t["symbol"], "side": t["side"], "strategy": t["strategy"],
        "opened_at": t["opened_at"], "closed_at": t["closed_at"],
        "entry": float(data.get("entry_price", 0) or 0), "exit": float(data.get("exit_price", 0) or 0),
        "exit_reason": EXIT_TEXT.get(t["exit_reason"], t["exit_reason"]), "pnl_net": float(t["pnl_net"]),
        "fees": float(t["fees"]), "r": float(t["r"]), "ai_confidence": t.get("ai_confidence"),
    }


def _learning(db: sqlite3.Connection | None, min_cases: int, min_trades: int) -> dict[str, list]:
    learning: list[dict] = []
    pending: list[dict] = []
    learned: list[dict] = []
    for h in _rows(db, "SELECT id, created_at, status, data, evaluation FROM hypotheses ORDER BY created_at DESC"):
        hyp = json.loads(h["data"] or "{}")
        ev = json.loads(h["evaluation"] or "{}")
        item = {"id": h["id"], "text": hyp.get("description", ""), "since": h["created_at"][:10],
                "cases": ev.get("n_cases", 0), "cases_needed": min_cases, "trades": ev.get("n_trades", 0),
                "trades_needed": min_trades, "challenger_r": ev.get("challenger_exp"),
                "champion_r": ev.get("champion_exp")}
        if h["status"] == "activa":
            learning.append(item)
        elif h["status"] == "pendiente_aprobacion":
            pending.append(item)
    for les in _rows(db, "SELECT id, text, last_confirmed_at FROM lessons WHERE active=1 "
                         "ORDER BY last_confirmed_at DESC"):
        learned.append({"id": les["id"], "text": les["text"], "confirmed": (les["last_confirmed_at"] or "")[:10]})
    return {"learning": learning, "pending": pending, "learned": learned}


def collect(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """`settings` ya trae las rutas por modo (ver `per_mode_storage`)."""
    now = now or datetime.now(UTC)
    st = settings.storage
    state = BotState.load_or_new(st.state_path, settings.paper.initial_capital, now.date()) \
        if st.state_path.exists() else None
    live = snapshot.read(st.reports_dir / snapshot.FILENAME)
    db = _connect(st.db_path)
    try:
        trades = _rows(db, "SELECT * FROM trades ORDER BY closed_at")
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [t for t in trades if t["closed_at"] >= day_start.isoformat()]
        cands = _rows(db, "SELECT id, ts, symbol, strategy, side, status, reasons, ai FROM candidates "
                          "WHERE ts >= ? ORDER BY ts DESC LIMIT 60", ((now - timedelta(hours=48)).isoformat(),))
        ai_today = _rows(db, "SELECT COUNT(*) AS n FROM ai_calls WHERE ts >= ?", (day_start.isoformat(),))
        learning = _learning(db, settings.learning.challenger_min_cases, settings.learning.challenger_min_trades)
    finally:
        if db is not None:
            db.close()

    ctl = control_view(settings)
    eff, _ = control.apply(settings, control.Control(aggressiveness=ctl["aggressiveness"]))
    capital = settings.paper.initial_capital
    equity = state.equity if state else capital
    positions = (live or {}).get("positions", [])
    unrealized = sum(p["pnl_unrealized"] or 0 for p in positions)
    s = summarize(trades, capital)
    curve = equity_curve(trades, capital)
    risk = RiskEngine(eff.risk, eff.limits, settings.exchange)
    decisions = []
    for c in cands:
        reasons = json.loads(c["reasons"] or "[]")
        ai = json.loads(c["ai"] or "null") or {}
        decisions.append({"ts": c["ts"], "symbol": c["symbol"], "strategy": c["strategy"], "side": c["side"],
                          "status": CANDIDATE_TEXT.get(c["status"], c["status"]), "traded": c["status"] == "operado",
                          "reason": "; ".join(reasons) if reasons else (ai.get("thesis") or "")})
    return {
        "now": now.isoformat(),
        "mode": settings.mode.value,
        "symbols": settings.exchange.symbols,
        "status": _status(settings, state, live, now, ctl["mode"] == "solo_cierre"),
        "control": ctl,
        "updated_at": (live or {}).get("updated_at"),
        "kpis": {
            "capital_inicial": capital, "equity": equity, "unrealized": unrealized,
            "pnl_total": equity - capital,
            "pnl_hoy": (equity - state.day_start_equity) if state else 0.0,
            "pnl_hoy_pct": state.day_pnl_pct if state else 0.0,
            "drawdown": state.drawdown if state else 0.0,
            "entradas_hoy": len(state.entries_on(now.date())) if state else 0,
            "techo_hoy": risk.daily_cap(state, now) if state else eff.limits.max_trades_per_day,
            "operaciones": s.get("n", 0), "acierto": s.get("win_rate"), "factor_beneficio": s.get("profit_factor"),
            "comisiones": s.get("fees", 0.0), "llamadas_ia_hoy": ai_today[0]["n"] if ai_today else 0,
            "cerradas_hoy": len(today), "pnl_cerradas_hoy": sum(float(t["pnl_net"]) for t in today),
        },
        "equity_curve": [[capital_ts, eq] for capital_ts, eq in curve],
        "prices": (live or {}).get("prices", {}),
        "last": (live or {}).get("last", {}),
        "positions": positions,
        "top_traders": (live or {}).get("top_traders"),
        "trades": [_trade_row(t) for t in reversed(trades[-200:])],
        "decisions": decisions,
        **learning,
    }
