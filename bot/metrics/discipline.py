"""Disciplina medible: detecta en los datos los sesgos que en una persona serían emociones.

Un programa no siente, pero la IA que decide puede comportarse COMO si sintiera:
rechazar todo tras una mala racha (miedo), aprobar más para recuperar (revancha),
aprobar perdedoras con confianza alta (exceso de confianza) o cerrar antes de
tiempo (angustia). Cada chequeo compara grupos con su tamaño de muestra y solo
alerta con evidencia mínima; las reglas de riesgo ya las impone el código.

Estados: "ok", "alerta", "sin_datos".
"""

from __future__ import annotations

import bisect
from statistics import mean
from typing import Any

MIN_N = 10            # casos por grupo antes de opinar
GAP_RATE = 0.20       # 20 puntos de diferencia en tasa de aprobación
GAP_R = 0.15          # 0.15 R de diferencia en expectativa


def _check(key: str, title: str, state: str, detail: str, n: int) -> dict[str, Any]:
    return {"key": key, "title": title, "state": state, "detail": detail, "n": n}


def _prev_results(trades: list[dict]) -> tuple[list[str], list[float]]:
    rows = sorted(trades, key=lambda t: t["closed_at"])
    return [t["closed_at"] for t in rows], [float(t["pnl_net"]) for t in rows]


def _prev_outcome(ts: str, closes: list[str], pnls: list[float]) -> str | None:
    i = bisect.bisect_left(closes, ts) - 1
    if i < 0:
        return None
    return "perdida" if pnls[i] < 0 else "ganancia"


def check_rules(events: list[dict], trades: list[dict]) -> dict:
    no_stop = sum(1 for e in events if e["kind"] == "entrada_sin_stop")
    if no_stop:
        return _check("reglas", "Siempre con stop-loss", "alerta",
                      f"{no_stop} entrada(s) no lograron poner su stop y se cerraron de inmediato: revisar conexión.",
                      len(trades))
    return _check("reglas", "Siempre con stop-loss", "ok" if trades else "sin_datos",
                  "Todas las operaciones tuvieron su stop-loss en el exchange desde el primer momento.", len(trades))


def check_fear_or_overconfidence(trades: list[dict], candidates: list[dict]) -> list[dict]:
    """¿Lo que la IA rechaza habría ganado (miedo)? ¿Lo que aprueba pierde (exceso de confianza)?"""
    rej = [float(c["counterfactual"]["r_net"]) for c in candidates
           if c["status"] == "rechazado_ia" and c.get("counterfactual")]
    app = [float(t["r"]) for t in trades]
    out = []
    if len(rej) < MIN_N or len(app) < MIN_N:
        detail = f"Hacen falta {MIN_N} rechazos con resultado y {MIN_N} operaciones (hay {len(rej)} y {len(app)})."
        out.append(_check("miedo", "Sin miedo a entrar", "sin_datos", detail, len(rej)))
        out.append(_check("confianza", "Sin exceso de confianza", "sin_datos", detail, len(app)))
        return out
    er, ea = mean(rej), mean(app)
    cmp = f"Rechazadas: {er:+.2f}R promedio (n={len(rej)}); aprobadas: {ea:+.2f}R (n={len(app)})."
    fear = er > GAP_R and er > ea + GAP_R
    out.append(_check("miedo", "Sin miedo a entrar", "alerta" if fear else "ok",
                      ("Rechaza oportunidades que habrían ganado: está siendo demasiado cautelosa. " if fear
                       else "Lo que rechaza no habría ganado más que lo que aprueba. ") + cmp, len(rej)))
    over = ea < -GAP_R and ea < er - GAP_R
    out.append(_check("confianza", "Sin exceso de confianza", "alerta" if over else "ok",
                      ("Aprueba operaciones que terminan perdiendo más que las que rechaza. " if over
                       else "Lo que aprueba rinde igual o mejor que lo que rechaza. ") + cmp, len(app)))
    return out


def check_after_losses(trades: list[dict], candidates: list[dict]) -> dict:
    """Una decisión no debe depender de si la operación anterior ganó o perdió."""
    closes, pnls = _prev_results(trades)
    groups: dict[str, list[int]] = {"perdida": [], "ganancia": []}
    for c in candidates:
        ai = c.get("ai")
        if not ai:
            continue
        prev = _prev_outcome(c["ts"], closes, pnls)
        if prev:
            groups[prev].append(0 if ai.get("action") == "NO_TRADE" else 1)
    nl, nw = len(groups["perdida"]), len(groups["ganancia"])
    if nl < MIN_N or nw < MIN_N:
        return _check("tras_perder", "Igual tras ganar o perder", "sin_datos",
                      f"Hacen falta {MIN_N} decisiones tras pérdida y {MIN_N} tras ganancia (hay {nl} y {nw}).",
                      nl + nw)
    al, aw = mean(groups["perdida"]), mean(groups["ganancia"])
    cmp = f"Aprueba {al * 100:.0f}% tras perder (n={nl}) y {aw * 100:.0f}% tras ganar (n={nw})."
    if aw - al > GAP_RATE:
        return _check("tras_perder", "Igual tras ganar o perder", "alerta",
                      "Se vuelve más cautelosa después de perder (parecido al miedo). " + cmp, nl + nw)
    if al - aw > GAP_RATE:
        return _check("tras_perder", "Igual tras ganar o perder", "alerta",
                      "Aprueba más después de perder (parecido a querer recuperar). " + cmp, nl + nw)
    return _check("tras_perder", "Igual tras ganar o perder", "ok",
                  "Decide igual sin importar el resultado anterior. " + cmp, nl + nw)


def check_size(trades: list[dict], capital: float) -> dict:
    """Riesgo por operación (1R en % del capital del momento): no debe crecer tras ganar ni encogerse tras perder."""
    rows = sorted(trades, key=lambda t: t["closed_at"])
    eq, prev, groups = capital, None, {"perdida": [], "ganancia": []}
    for t in rows:
        r, pnl = float(t["r"]), float(t["pnl_net"])
        if abs(r) > 0.05 and eq > 0 and prev is not None:
            groups[prev].append(abs(pnl / r) / eq)
        eq += pnl
        prev = "perdida" if pnl < 0 else "ganancia"
    nl, nw = len(groups["perdida"]), len(groups["ganancia"])
    if nl < MIN_N or nw < MIN_N:
        return _check("tamano", "Mismo tamaño siempre", "sin_datos",
                      f"Hacen falta {MIN_N} operaciones tras pérdida y {MIN_N} tras ganancia (hay {nl} y {nw}).",
                      nl + nw)
    rl, rw = mean(groups["perdida"]), mean(groups["ganancia"])
    cmp = f"Arriesga {rl * 100:.2f}% del capital tras perder y {rw * 100:.2f}% tras ganar."
    ratio = rw / rl if rl else 1.0
    if ratio > 1.25:
        return _check("tamano", "Mismo tamaño siempre", "alerta", "Arriesga más después de ganar. " + cmp, nl + nw)
    if ratio < 0.8:
        return _check("tamano", "Mismo tamaño siempre", "alerta", "Arriesga menos después de perder. " + cmp, nl + nw)
    return _check("tamano", "Mismo tamaño siempre", "ok", cmp, nl + nw)


def check_early_exits(trades: list[dict]) -> dict:
    """Cerrar antes de tiempo por la IA debería ser la excepción."""
    n = len(trades)
    early = [t for t in trades if t["exit_reason"] == "ai_early_exit"]
    if n < MIN_N:
        return _check("paciencia", "Paciencia con las operaciones", "sin_datos",
                      f"Hacen falta {MIN_N} operaciones cerradas (hay {n}).", n)
    share = len(early) / n
    detail = f"La IA cerró antes de tiempo {len(early)} de {n} operaciones ({share * 100:.0f}%)."
    if share > 0.3:
        return _check("paciencia", "Paciencia con las operaciones", "alerta",
                      detail + " Es mucho: puede estar cortando ganadoras por nerviosismo.", n)
    return _check("paciencia", "Paciencia con las operaciones", "ok", detail, n)


def practice(candidates: list[dict]) -> dict:
    """Operaciones de práctica: cada candidato no operado se simula con precios reales posteriores."""
    rows = [float(c["counterfactual"]["r_net"]) for c in candidates
            if c.get("counterfactual") and c["status"] != "operado"]
    if not rows:
        return {"n": 0}
    return {"n": len(rows), "acierto": sum(1 for r in rows if r > 0) / len(rows), "expectativa_r": mean(rows)}


def evaluate(trades: list[dict], candidates: list[dict], events: list[dict], capital: float) -> dict[str, Any]:
    checks = [check_rules(events, trades), *check_fear_or_overconfidence(trades, candidates),
              check_after_losses(trades, candidates), check_size(trades, capital), check_early_exits(trades)]
    judged = [c for c in checks if c["state"] != "sin_datos"]
    return {"checks": checks, "ok": sum(1 for c in judged if c["state"] == "ok"), "judged": len(judged),
            "practice": practice(candidates)}
