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
from bot.core.logging_setup import get_logger
from bot.core.state import BotState
from bot.dashboard import snapshot
from bot.metrics import discipline, robustness
from bot.metrics.performance import by_dimension, equity_curve, summarize
from bot.risk.engine import RiskEngine

log = get_logger("bot.dashboard.data")
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


EVENT_TEXT = {
    "modo_seguro": "Modo seguro: no abre operaciones y protege las abiertas",
    "reconexion": "Se reconectó a Binance",
    "entrada_sin_stop": "Una entrada no logró poner su stop y se cerró al instante",
    "revision_salida_ia": "La IA revisó una operación abierta",
}


def ai_health(calls: list[dict]) -> dict[str, Any]:
    """Estado de la IA según sus últimas consultas (la más reciente primero)."""
    if not calls:
        return {"code": "sin_consultas", "label": "Sin consultas todavía",
                "detail": "Normal al principio: la IA solo se consulta cuando una oportunidad pasa todos los filtros."}
    last = calls[:3]
    if len(last) == 3 and not any(c["valid"] for c in last):
        reasons = " ".join(str(c["reason"]).lower() for c in last)
        if "credit" in reasons or "balance" in reasons or "billing" in reasons:
            label = "Sin saldo en Anthropic: recarga en console.anthropic.com"
        elif "sin llave" in reasons:
            label = "Falta la llave de la IA (Anthropic): doble clic en configurar_ia.bat"
        elif "api 401" in reasons or "authentication" in reasons or "api key" in reasons:
            label = "La llave de la IA no es válida: vuelve a ejecutar la revisión (python -m bot.revisar)"
        elif "api 429" in reasons:
            label = "Anthropic pidió esperar (límite de uso); se recupera solo"
        else:
            label = "La IA está fallando: mientras tanto el bot no opera"
        return {"code": "falla", "label": label, "detail": f"Últimos motivos: {last[0]['reason']}"[:240]}
    ok = next((c for c in calls if c["valid"]), None)
    return {"code": "ok", "label": "Funcionando",
            "detail": f"Última respuesta válida: {ok['ts'][:16].replace('T', ' ')} UTC" if ok else ""}


def latest_zone(reports_dir: Path) -> dict[str, str] | None:
    zones = sorted(reports_dir.glob("zona_*.md"))
    if not zones:
        return None
    return {"day": zones[-1].stem.removeprefix("zona_"), "text": zones[-1].read_text(encoding="utf-8")[:8000]}


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


def activity(cands: list[dict], entries_24h: int, ai: dict[str, Any], status: dict[str, Any],
             strategies: list[str]) -> dict[str, Any]:
    """¿Por qué no está operando? Explicación simple de las últimas 24 h, la causa principal primero."""
    from collections import Counter

    from bot.dashboard.reports import STRATEGY, plain_reason
    st = Counter(c["status"] for c in cands)
    names = ", ".join(STRATEGY.get(x, x) for x in strategies) or "ninguna (todas apagadas en config.yaml)"
    items: list[dict[str, str]] = []
    if status.get("code") in ("apagado", "detenido", "pausado", "solo_cierre"):
        items.append({"level": "bad", "text": f"{status.get('label')}: {status.get('detail', '')}"})
    if ai.get("code") == "falla":
        items.append({"level": "bad", "text": f"La IA no responde: {ai.get('label')}. Sin IA el bot no abre nada (por seguridad)."})
    if not cands:
        items.append({"level": "info", "text": f"No apareció ninguna oportunidad en 24 h. Busca: {names}. Es normal que "
                                                "pasen horas sin señales cuando el mercado va de lado."})
    reasons: Counter = Counter()
    for c in cands:
        if c["status"] in ("rechazado_filtro", "rechazado_riesgo"):
            for r in json.loads(c.get("reasons") or "[]"):
                reasons[plain_reason(r)] += 1
    if reasons:
        top = "; ".join(f"{k} ({v})" for k, v in reasons.most_common(3))
        items.append({"level": "warn", "text": f"Sus reglas de seguridad descartaron {st['rechazado_filtro'] + st['rechazado_riesgo']}: {top}."})
    if st.get("rechazado_ia"):
        items.append({"level": "warn", "text": f"La IA revisó y dijo que no a {st['rechazado_ia']}: por diseño solo aprueba cuando la "
                                                "evidencia es clara. Su acierto se mide en Rendimiento → ¿Ventaja real o suerte?"})
    if st.get("sin_llenado"):
        items.append({"level": "info", "text": f"{st['sin_llenado']} órdenes no se llenaron (el precio no volvió a su nivel)."})
    if items and items[0]["level"] == "bad":
        head, level = "No está abriendo operaciones por una causa que hay que revisar (abajo).", "bad"
    elif entries_24h:
        head, level = f"Operando: {entries_24h} entrada(s) en las últimas 24 h.", "ok"
    else:
        head, level = "Sin entradas en 24 h: está esperando una oportunidad clara.", "warn" if cands else "info"
    return {"headline": head, "level": level, "items": items, "candidates": len(cands), "strategies": names,
            "expected": "≈ 1 oportunidad al día en promedio (según 13 meses de datos), algunos días 0 y otros 3"}


SECTION_ERRORS: list[str] = []


def _safe(name: str, fn, default):
    """Una sección del panel que falla no debe tumbar todo el panel: se registra y se muestra 'no disponible'."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.error("panel: sección falló", extra={"section": name, "err": repr(exc)}, exc_info=True)
        SECTION_ERRORS.append(f"{name}: {exc.__class__.__name__}: {exc}"[:300])
        return default


def notifications(events: list[dict]) -> list[dict[str, Any]]:
    """Campana del panel: entradas, salidas y reportes (y avisos importantes), lo más reciente primero."""
    out = []
    for e in events:
        payload = json.loads(e["payload"] or "{}")
        if e["kind"] == "notif":
            out.append({"ts": e["ts"], **{k: payload.get(k, "") for k in ("kind", "title", "text", "level")}})
        elif e["kind"] == "alerta" and payload.get("level") in ("warning", "error"):
            icon = "⛔" if payload["level"] == "error" else "⚠️"
            out.append({"ts": e["ts"], "kind": "alerta", "title": f"{icon} Aviso del bot", "text": payload.get("msg", ""),
                        "level": payload["level"]})
    return out[:50]


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


def _brief_ai(raw: str | None) -> dict[str, Any] | None:
    ai = json.loads(raw or "null")
    if not isinstance(ai, dict):
        return None
    return {k: ai.get(k) for k in ("action", "confidence", "size_multiplier", "thesis", "invalidation", "key_evidence",
                                   "risk_flags")}


def chart_signals(settings: Settings, symbol: str, now: datetime | None = None, days: int = 14) -> dict[str, Any]:
    """Para dibujar en el gráfico lo que hizo el bot en una moneda: cada operación (entrada, stop inicial, objetivo,
    salida y lo que dijo la IA) y cada candidato de los últimos `days` días con su motivo. El nivel de ruptura
    (`level`) es el máximo/mínimo de 6 h que vio la estrategia en ese momento (tomado del snapshot del diario)."""
    if symbol not in settings.exchange.symbols:
        raise ValueError("moneda desconocida")
    now = now or datetime.now(UTC)
    db = _connect(settings.storage.db_path)
    level_sql = ("CASE side WHEN 'LONG' THEN json_extract(snapshot, '$.\"ind.15m.high_n\"') "
                 "ELSE json_extract(snapshot, '$.\"ind.15m.low_n\"') END AS level")
    try:
        cands = _rows(db, f"SELECT id, ts, strategy, side, entry, stop, take_profit, status, reasons, ai, {level_sql} "
                          "FROM candidates WHERE symbol = ? AND ts >= ? ORDER BY ts DESC LIMIT 300",
                      (symbol, (now - timedelta(days=days)).isoformat()))
        trades = _rows(db, "SELECT * FROM trades WHERE symbol = ? ORDER BY closed_at DESC LIMIT 200", (symbol,))
        ids = [t["signal_id"] for t in trades if t.get("signal_id")]
        linked = {r["id"]: r for r in _rows(
            db, f"SELECT id, take_profit, ai, {level_sql} FROM candidates WHERE id IN ({','.join('?' * len(ids))})",
            tuple(ids))} if ids else {}
    finally:
        if db is not None:
            db.close()
    signals = [{"id": c["id"], "ts": c["ts"], "strategy": c["strategy"], "side": c["side"], "entry": c["entry"],
                "stop": c["stop"], "take_profit": c["take_profit"], "level": c["level"],
                "status": CANDIDATE_TEXT.get(c["status"], c["status"]), "traded": c["status"] == "operado",
                "reasons": json.loads(c["reasons"] or "[]"), "ai": _brief_ai(c["ai"])} for c in cands]
    out_trades = []
    for t in trades:
        data = json.loads(t.get("data") or "{}")
        cand = linked.get(t.get("signal_id")) or {}
        out_trades.append({**_trade_row(t), "initial_stop": float(data.get("initial_stop_price", 0) or 0) or None,
                           "take_profit": cand.get("take_profit"), "level": cand.get("level"),
                           "ai": _brief_ai(cand.get("ai")), "mfe_r": data.get("mfe_r"), "mae_r": data.get("mae_r")})
    return {"symbol": symbol, "signals": signals, "trades": out_trades}


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
    state = BotState.load_or_new(st.state_path, settings.paper.initial_capital, now.date(), repair=False) \
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
        since30 = (now - timedelta(days=30)).isoformat()
        cands30 = [{**c, "ai": json.loads(c["ai"] or "null"), "counterfactual": json.loads(c["counterfactual"] or "null")}
                   for c in _rows(db, "SELECT ts, status, ai, counterfactual FROM candidates WHERE ts >= ?", (since30,))]
        events30 = _rows(db, "SELECT ts, kind FROM events WHERE ts >= ?", (since30,))
        ai_last = _rows(db, "SELECT ts, valid, reason FROM ai_calls ORDER BY ts DESC LIMIT 10")
        cands24 = _rows(db, "SELECT status, reasons FROM candidates WHERE ts >= ?", ((now - timedelta(hours=24)).isoformat(),))
        log_rows = _rows(db, "SELECT ts, kind, payload FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT 150",
                         ((now - timedelta(days=7)).isoformat(),))
    finally:
        if db is not None:
            db.close()

    ctl = control_view(settings)
    status_info = _status(settings, state, live, now, ctl["mode"] == "solo_cierre")
    eff, _ = control.apply(settings, control.Control(aggressiveness=ctl["aggressiveness"]))
    capital = settings.paper.initial_capital
    equity = state.equity if state else capital
    positions = (live or {}).get("positions", [])
    unrealized = sum(p["pnl_unrealized"] or 0 for p in positions)
    s = summarize(trades, capital)
    trades30 = [t for t in trades if t["closed_at"] >= since30]
    bitacora = []
    for e in log_rows:
        payload = json.loads(e["payload"] or "{}")
        if e["kind"] == "notif":
            lv = {"good": "info", "bad": "info"}.get(payload.get("level", "info"), payload.get("level", "info"))
            bitacora.append({"ts": e["ts"], "level": lv, "text": f"{payload.get('title', '')} — {payload.get('text', '')}"})
        elif e["kind"] == "alerta":
            bitacora.append({"ts": e["ts"], "level": payload.get("level", "info"), "text": payload.get("msg", "")})
        elif e["kind"] in EVENT_TEXT:
            extra = payload.get("reason") or payload.get("symbol") or ""
            bitacora.append({"ts": e["ts"], "level": "warning" if e["kind"] != "reconexion" else "info",
                             "text": EVENT_TEXT[e["kind"]] + (f" ({extra})" if extra else "")})
    SECTION_ERRORS.clear()
    disc = _safe("disciplina", lambda: discipline.evaluate(trades30, cands30, events30, capital),
                 {"checks": [], "ok": 0, "judged": 0, "practice": {"n": 0}})
    risk_now, halt = eff.risk.risk_per_trade, settings.risk.max_drawdown
    stats = _safe("estadística", lambda: {"trades": robustness.analyze(robustness.from_trades(trades), risk_now, halt),
             "practice": robustness.analyze(robustness.from_counterfactuals(cands30), risk_now, halt)},
                  {"trades": {"n": 0, "enough": False}, "practice": {"n": 0, "enough": False}})
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
        "status": status_info,
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
        "candles": (live or {}).get("candles", {}),
        "levels": (live or {}).get("levels", {}),
        "last": (live or {}).get("last", {}),
        "positions": positions,
        "top_traders": (live or {}).get("top_traders"),
        "system": {**((live or {}).get("system") or {}), "ai": ai_health(ai_last)},
        "bitacora": bitacora[:40],
        "notifications": _safe("notificaciones", lambda: notifications(log_rows), []),
        "activity": _safe("actividad", lambda: activity(
            cands24, len([e for e in (state.entries if state else []) if e.ts >= now - timedelta(hours=24)]),
            ai_health(ai_last), status_info, [k for k, v in settings.strategies.items() if v.enabled]), None),
        "performance": _safe("rendimiento", lambda: {k: by_dimension(trades, k, capital) if trades else {}
                                                      for k in ("strategy", "symbol", "side")}, {}),
        "zone": _safe("reporte del día", lambda: latest_zone(st.reports_dir), None),
        "section_errors": SECTION_ERRORS[:],
        "trades": [_trade_row(t) for t in reversed(trades[-200:])],
        "decisions": decisions,
        "discipline": disc,
        "stats": stats,
        **learning,
    }
