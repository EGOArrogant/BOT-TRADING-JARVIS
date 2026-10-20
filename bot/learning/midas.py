"""MIDAS: laboratorio de apalancamiento. Solo simula: nunca envía órdenes, ni en papel ni en real.

Para cada señal del bot, operada o rechazada, se toma su resultado ya conocido: el contrafactual, con la misma
entrada, el mismo stop, el mismo objetivo y el mismo tiempo máximo que usaría el bot. MIDAS calcula qué habría pasado
poniendo un margen fijo (por ejemplo 20 US$) con apalancamiento x5 a x25 y margen aislado:

    ganancia = margen × apalancamiento × movimiento a favor − comisiones
    si antes del stop el precio llega a la liquidación (≈ 1/apalancamiento − margen de mantenimiento), se pierde el margen

"Todas las señales" deja de lado la disciplina (filtros, IA, límites del día); "Solo las aprobadas" son las que el bot
realmente operó. Comparar ambas muestra cuánto vale la disciplina. Es información para observar y aprender: no
cambia el riesgo, el apalancamiento ni los stops del bot (CLAUDE.md §1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

LEVERAGES = (5, 10, 15, 20, 25)
MAINT_MARGIN = 0.005  # margen de mantenimiento aproximado de Binance (0,4–1 % según moneda y tamaño): conservador
SCOPES = ("aprobadas", "todas")


def liq_distance(leverage: float, mmr: float = MAINT_MARGIN) -> float:
    """Movimiento en contra que liquida una posición aislada (fracción del precio de entrada)."""
    return max(1.0 / leverage - mmr, 0.0)


def outcome(cand: dict, leverage: float, margin: float, fee_round_trip: float) -> dict[str, Any] | None:
    """Resultado de una señal resuelta a un apalancamiento. None si le faltan datos."""
    cf = cand.get("counterfactual") or {}
    entry, stop = cand.get("entry"), cand.get("stop")
    if not entry or not stop or "r_gross" not in cf:
        return None
    risk_pct = abs(entry - stop) / entry
    move = cf["r_gross"] * risk_pct                      # fracción a favor al salir (negativa si perdió)
    adverse = max(0.0, -(cf.get("mae_r") or 0.0)) * risk_pct  # lo más en contra que llegó antes de salir
    notional = margin * leverage
    liquidated = adverse >= liq_distance(leverage)
    pnl = -margin if liquidated else notional * move - notional * fee_round_trip
    return {"pnl": max(pnl, -margin), "liquidated": liquidated, "move_pct": move, "notional": notional}


def _stats(rows: list[dict], key: int) -> dict[str, Any]:
    pnls = [r["res"][key]["pnl"] for r in rows]
    cum, peak, dd, curve = 0.0, 0.0, 0.0, []
    for r, p in zip(rows, pnls, strict=True):
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append([r["ts"], round(cum, 2)])
    wins = sum(p > 0 for p in pnls)
    return {"leverage": key, "n": len(pnls), "wins": wins, "win_rate": wins / len(pnls) if pnls else None,
            "liquidated": sum(r["res"][key]["liquidated"] for r in rows), "total": round(sum(pnls), 2),
            "avg": round(sum(pnls) / len(pnls), 2) if pnls else None, "best": round(max(pnls), 2) if pnls else None,
            "worst": round(min(pnls), 2) if pnls else None, "max_drawdown": round(dd, 2), "curve": curve}


def live(cand: dict, price: float | None, leverage: float, margin: float) -> dict[str, Any] | None:
    """Señal todavía en curso: cuánto iría ganando o perdiendo ahora con el precio actual."""
    entry = cand.get("entry")
    if not entry or not price:
        return None
    sign = 1 if cand.get("side") == "LONG" else -1
    move = sign * (price - entry) / entry
    liq = -move >= liq_distance(leverage)
    return {"pnl": round(-margin if liq else margin * leverage * move, 2), "move_pct": move, "liquidated": liq}


def lessons(summary: dict[str, Any], margin: float) -> list[str]:
    """Lo que enseña el laboratorio, en palabras simples (solo informa: nunca cambia el riesgo)."""
    out: list[str] = []
    for scope, label in (("aprobadas", "las que el bot aprobó"), ("todas", "todas las señales")):
        stats = summary.get(scope, {}).get("by_leverage", [])
        if not stats or stats[0]["n"] < 3:
            continue  # con 1 o 2 señales no hay nada que concluir
        best = max(stats, key=lambda s: s["total"] / (s["max_drawdown"] or margin))
        worst_liq = max(stats, key=lambda s: s["liquidated"])
        out.append(f"Con {label} ({best['n']} señales), la mejor relación ganancia/caída fue x{best['leverage']}: "
                   f"{best['total']:+.2f} US$ con una caída máxima de {best['max_drawdown']:.2f} US$.")
        if worst_liq["liquidated"]:
            out.append(f"A x{worst_liq['leverage']} se liquidaron {worst_liq['liquidated']} de {worst_liq['n']}: con ese "
                       f"apalancamiento el precio llega a la liquidación antes que al stop del bot.")
    a, t = (summary.get(s, {}).get("by_leverage", []) for s in SCOPES)
    if a and t and a[0]["n"] and t[0]["n"] > a[0]["n"]:
        ax = next(s for s in a if s["leverage"] == 10)
        tx = next(s for s in t if s["leverage"] == 10)
        better = "mejor" if ax["avg"] >= tx["avg"] else "peor"
        out.append(f"A x10, seguir la disciplina dio {ax['avg']:+.2f} US$ por operación contra {tx['avg']:+.2f} US$ de "
                   f"entrar en todo: la disciplina salió {better}.")
    return out


def simulate(cands: list[dict], margin: float, fee_round_trip: float, last: dict[str, float | None] | None = None,
             now: datetime | None = None, leverages: tuple[int, ...] = LEVERAGES) -> dict[str, Any]:
    """`cands`: filas del diario (con `counterfactual` ya decodificado), de la más vieja a la más nueva."""
    resolved, pending = [], []
    for c in cands:
        if c.get("counterfactual"):
            res = {lv: outcome(c, lv, margin, fee_round_trip) for lv in leverages}
            if all(v is not None for v in res.values()):
                resolved.append({**c, "res": res})
        elif last is not None:
            pending.append(c)
    out: dict[str, Any] = {"margin": margin, "leverages": list(leverages), "fee_round_trip": fee_round_trip,
                           "liq_distance": {lv: liq_distance(lv) for lv in leverages}}
    for scope in SCOPES:
        rows = [r for r in resolved if scope == "todas" or r.get("status") == "operado"]
        out[scope] = {"by_leverage": [_stats(rows, lv) for lv in leverages],
                      "trades": [{"ts": r["ts"], "symbol": r["symbol"], "side": r["side"], "strategy": r["strategy"],
                                  "status": r.get("status"), "exit": r["counterfactual"].get("exit"),
                                  "move_pct": r["res"][leverages[0]]["move_pct"],
                                  "pnl": {lv: round(r["res"][lv]["pnl"], 2) for lv in leverages},
                                  "liquidated": [lv for lv in leverages if r["res"][lv]["liquidated"]]}
                                 for r in reversed(rows[-200:])]}
    out["live"] = [{"ts": c["ts"], "symbol": c["symbol"], "side": c["side"], "status": c.get("status"),
                    "entry": c["entry"], "price": (last or {}).get(c["symbol"]),
                    "pnl": {lv: (live(c, (last or {}).get(c["symbol"]), lv, margin) or {}).get("pnl") for lv in leverages}}
                   for c in reversed(pending[-20:])]
    out["lessons"] = lessons(out, margin)
    return out
