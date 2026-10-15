"""Métricas de desempeño. Toda métrica va acompañada de su tamaño de muestra (n)."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime

import numpy as np


def _r(rows: list[dict]) -> np.ndarray:
    return np.array([float(t["r"]) for t in rows], dtype=float)


def equity_curve(trades: list[dict], capital: float) -> list[tuple[str, float]]:
    eq = capital
    out = []
    for t in sorted(trades, key=lambda x: x["closed_at"]):
        eq += float(t["pnl_net"])
        out.append((t["closed_at"], eq))
    return out


def max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    arr = np.array(curve, dtype=float)
    peak = np.maximum.accumulate(arr)
    return float(np.max(1 - arr / peak))


def daily_returns(trades: list[dict], capital: float) -> np.ndarray:
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[t["closed_at"][:10]] += float(t["pnl_net"])
    eq, rets = capital, []
    for day in sorted(by_day):
        rets.append(by_day[day] / eq if eq else 0.0)
        eq += by_day[day]
    return np.array(rets)


def summarize(trades: list[dict], capital: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pnl = np.array([float(t["pnl_net"]) for t in trades])
    gross = np.array([float(t["pnl_gross"]) for t in trades])
    fees = sum(float(t["fees"]) for t in trades)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    rets = daily_returns(trades, capital)
    sd = rets.std(ddof=1) if len(rets) > 1 else 0.0
    downside = rets[rets < 0]
    dsd = math.sqrt(float(np.mean(downside ** 2))) if len(downside) else 0.0
    curve = [capital] + [e for _, e in equity_curve(trades, capital)]
    durations = [(datetime.fromisoformat(t["closed_at"]) - datetime.fromisoformat(t["opened_at"])).total_seconds() / 3600
                 for t in trades]
    gross_abs = float(np.sum(np.abs(gross))) or 1e-12
    out = {
        "n": n,
        "pnl_net": float(pnl.sum()),
        "expectancy_r": float(_r(trades).mean()),
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else (math.inf if len(wins) else 0.0),
        "win_rate": float(len(wins) / n),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown": max_drawdown(curve),
        "sharpe_daily": float(rets.mean() / sd * math.sqrt(365)) if sd > 0 else None,
        "sortino_daily": float(rets.mean() / dsd * math.sqrt(365)) if dsd > 0 else None,
        "fees": fees,
        "fees_pct_of_gross": fees / gross_abs,
        "avg_slippage_pct": float(np.mean([float(t.get("slippage_pct") or 0) for t in trades])),
        "maker_entry_rate": float(np.mean([int(t.get("entry_maker") or 0) for t in trades])),
        "avg_duration_h": float(np.mean(durations)),
    }
    data = [_trade_data(t) for t in trades]
    maes = [d["mae_r"] for d in data if d]
    mfes = [d["mfe_r"] for d in data if d]
    if maes:
        out["avg_mae_r"], out["avg_mfe_r"] = float(np.mean(maes)), float(np.mean(mfes))
    return out


def _trade_data(t: dict) -> dict | None:
    raw = t.get("data")
    if not raw:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def by_dimension(trades: list[dict], key: str, capital: float) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        k = t[key] if key != "hour" else datetime.fromisoformat(t["opened_at"]).strftime("%H")
        groups[str(k)].append(t)
    return {k: summarize(v, capital) for k, v in sorted(groups.items())}


def ai_calibration(trades: list[dict], buckets: tuple[float, ...] = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01)) -> list[dict]:
    """Acierto real (r > 0) por tramo de confianza de la IA."""
    out = []
    for lo, hi in zip(buckets, buckets[1:]):
        rows = [t for t in trades if t.get("ai_confidence") is not None and lo <= float(t["ai_confidence"]) < hi]
        if rows:
            out.append({"tramo": f"{lo:.1f}-{min(hi, 1.0):.1f}", "n": len(rows),
                        "acierto": sum(1 for t in rows if float(t["r"]) > 0) / len(rows),
                        "expectativa_r": float(np.mean(_r(rows)))})
    return out


def rejection_stats(candidates: list[dict]) -> dict:
    n = len(candidates)
    if n == 0:
        return {"n": 0}
    by_status: dict[str, int] = defaultdict(int)
    cf_by_status: dict[str, list[float]] = defaultdict(list)
    for c in candidates:
        by_status[c["status"]] += 1
        cf = c.get("counterfactual")
        if cf and c["status"] != "operado":
            cf_by_status[c["status"]].append(float(cf["r_net"]))
    return {
        "n": n,
        "rejection_rate": 1 - by_status.get("operado", 0) / n,
        "by_status": dict(by_status),
        "counterfactual": {k: {"n": len(v), "expectativa_r": float(np.mean(v))} for k, v in cf_by_status.items()},
    }
