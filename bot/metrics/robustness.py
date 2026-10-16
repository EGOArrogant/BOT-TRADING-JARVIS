"""Estadística de robustez: ¿la ventaja es real o suerte, y qué puede pasar después?

Una estrategia es una distribución de resultados, no una operación. A partir de los resultados en R
(operaciones reales o de práctica) calcula:

- esperanza y su intervalo de confianza 95 % (bootstrap) y el del acierto (Wilson);
- prueba de estrés: resultados un poco peores (acierto −4 pp, ganancia media ×0.9, pérdida ×1.05);
- dispersión (desviación estándar, asimetría) y dependencia de pocas operaciones (sin las 5 mejores/peores);
- rachas: la peor vista y la que es razonable esperar (Monte Carlo) y cuánto capital costaría;
- Monte Carlo de secuencias (bootstrap del orden): caída máxima típica y en el 5 % peor, probabilidad de
  tocar la detención, crecimiento compuesto (geométrico) frente a la media aritmética;
- Kelly como referencia (el riesgo lo fija el código y NUNCA se ajusta solo con esto);
- MAE/MFE: cuánto van en contra las ganadoras y cuánto llegan a favor, para estudiar stops y objetivos.

Todo es determinista (semilla fija) para que el panel no cambie de un refresco a otro.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

Z95 = 1.96
MIN_N = 5
N_SIM = 2000


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def expectancy(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """avg_loss en positivo (tamaño de la pérdida media en R)."""
    return win_rate * avg_win - (1 - win_rate) * avg_loss


def _longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for x in mask:
        cur = cur + 1 if x else 0
        best = max(best, cur)
    return best


def _max_dd(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    return float(np.max(1 - eq / peak))


def analyze(rows: list[dict], risk_per_trade: float, halt_drawdown: float, horizon: int = 100,
            seed: int = 7) -> dict[str, Any]:
    """rows: [{"r": float, "mae_r": float|None, "mfe_r": float|None}] en orden cronológico."""
    r = np.array([float(x["r"]) for x in rows], dtype=float)
    n = len(r)
    if n < MIN_N:
        return {"n": n, "enough": False}
    rng = np.random.default_rng(seed)
    wins, losses = r[r > 0], r[r <= 0]
    wr = len(wins) / n
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    exp = float(r.mean())
    boot = rng.choice(r, size=(N_SIM, n), replace=True).mean(axis=1)
    lo, hi = float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))
    edge = "confirmada" if lo > 0 else ("negativa" if hi < 0 else "incierta")
    wlo, whi = wilson(len(wins), n)
    stressed = expectancy(max(0.0, wr - 0.04), avg_win * 0.9, avg_loss * 1.05)

    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    skew = float(np.mean(((r - exp) / sd) ** 3)) if sd > 0 else 0.0
    srt = np.sort(r)
    tails = {}
    if n >= 15:
        tails = {"sin_5_mejores": float(srt[:-5].mean()), "sin_5_peores": float(srt[5:].mean()),
                 "parte_de_las_5_mejores": float(srt[-5:].sum() / r.sum()) if r.sum() > 0 else None}

    # Rachas y Monte Carlo: el orden de los mismos resultados cambia mucho la experiencia
    hz = max(horizon, n)
    seqs = rng.choice(r, size=(N_SIM, hz), replace=True)
    streaks = np.array([_longest_run(s <= 0) for s in seqs])
    eq = np.cumprod(1 + risk_per_trade * seqs, axis=1)
    eq = np.concatenate([np.ones((N_SIM, 1)), eq], axis=1)
    dds = np.array([_max_dd(e) for e in eq])
    final = eq[:, -1] - 1
    reasonable_streak = int(np.percentile(streaks, 95))
    worst_seen = _longest_run(r <= 0)

    payoff = avg_win / avg_loss if avg_loss > 0 else None
    kelly = (wr - (1 - wr) / payoff) if payoff else None

    out: dict[str, Any] = {
        "n": n, "enough": True,
        "expectancy_r": exp, "expectancy_ci": [lo, hi], "edge": edge,
        "win_rate": wr, "win_rate_ci": [wlo, whi], "avg_win_r": avg_win, "avg_loss_r": avg_loss, "payoff": payoff,
        "stress_expectancy_r": stressed, "fragile": exp > 0 and stressed <= 0,
        "std_r": sd, "skew": skew, "tails": tails,
        "streak_worst_seen": worst_seen, "streak_reasonable": reasonable_streak,
        "streak_cost_pct": 1 - (1 - risk_per_trade) ** reasonable_streak,
        "horizon": hz,
        "mc_dd_median": float(np.median(dds)), "mc_dd_p95": float(np.percentile(dds, 95)),
        "mc_prob_halt": float(np.mean(dds >= halt_drawdown)),
        "mc_final_p5": float(np.percentile(final, 5)), "mc_final_p50": float(np.median(final)),
        "mc_final_p95": float(np.percentile(final, 95)),
        "growth_arith_pct": float(risk_per_trade * exp),
        "growth_geom_pct": float(np.mean(np.log1p(risk_per_trade * r))),
        "kelly": kelly, "risk_per_trade": risk_per_trade,
        "kelly_fraction_used": (risk_per_trade / kelly) if kelly and kelly > 0 else None,
    }

    # MAE/MFE: el recorrido dentro de la operación
    mae_w = np.array([float(x["mae_r"]) for x in rows if x.get("mae_r") is not None and float(x["r"]) > 0])
    mfe_all = np.array([float(x["mfe_r"]) for x in rows if x.get("mfe_r") is not None])
    mfe_l = np.array([float(x["mfe_r"]) for x in rows if x.get("mfe_r") is not None and float(x["r"]) <= 0])
    if len(mae_w) >= 3:
        out["mae_winners"] = {"p50": float(np.percentile(mae_w, 50)), "p80": float(np.percentile(mae_w, 20)),
                              "p95": float(np.percentile(mae_w, 5))}  # MAE es negativo: el 80 % no pasa de p80
    if len(mfe_all) >= 3:
        out["mfe_reach"] = {f"{k}R": float(np.mean(mfe_all >= k)) for k in (1, 2, 3)}
    if len(mfe_l) >= 3:
        out["losers_were_up_1r"] = float(np.mean(mfe_l >= 1.0))
    return out


def from_trades(trades: list[dict]) -> list[dict]:
    import json
    out = []
    for t in sorted(trades, key=lambda x: x["closed_at"]):
        d = t.get("data")
        d = json.loads(d) if isinstance(d, str) else (d or {})
        out.append({"r": float(t["r"]), "mae_r": d.get("mae_r"), "mfe_r": d.get("mfe_r")})
    return out


def from_counterfactuals(candidates: list[dict]) -> list[dict]:
    rows = [c for c in candidates if c.get("counterfactual") and c.get("status") != "operado"]
    return [{"r": float(c["counterfactual"]["r_net"]), "mae_r": c["counterfactual"].get("mae_r"),
             "mfe_r": c["counterfactual"].get("mfe_r")} for c in sorted(rows, key=lambda c: c["ts"])]
