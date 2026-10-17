"""¿Qué datos predicen de verdad el precio? Coeficiente de información (IC) por indicador (Jansen, cap. 7).

Para cada indicador mide la correlación de rangos (Spearman) entre su valor en una vela y el rendimiento de las
siguientes `horizon` velas, por moneda y en conjunto, más la diferencia entre el 20 % más alto y el más bajo
(¿alcanza para pagar comisiones?). Corrige por probar muchos indicadores a la vez con Benjamini-Hochberg.

    python -m bot.research.ic --days 90 --horizon 16

Solo informa: no cambia nada del bot. Úsalo antes de proponer una estrategia nueva.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import NormalDist
from typing import Any

import numpy as np

from bot.metrics.multiple_testing import bh_fdr

ROUND_TRIP_COST = 0.0007  # entrada maker + salida taker + deslizamiento (~0,07 %)


def _rank(a: np.ndarray) -> np.ndarray:
    order = a.argsort(kind="mergesort")
    r = np.empty(len(a))
    r[order] = np.arange(len(a))
    return r


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0
    return float(np.corrcoef(_rank(x), _rank(y))[0, 1])


def screen(rows: list[dict[str, float]], fwd: list[float], groups: list[str], min_n: int = 200,
           overlap: int = 1) -> list[dict[str, Any]]:
    """rows[i] = indicadores en la vela i; fwd[i] = rendimiento futuro; groups[i] = moneda.

    El IC se calcula por moneda y se promedia (evita que una moneda tape a otra). `overlap` = velas que se solapan
    los rendimientos futuros: el error estándar se ajusta para no exagerar la significancia.
    """
    y_all = np.asarray(fwd, dtype=float)
    keys = sorted({k for r in rows for k, v in r.items() if isinstance(v, int | float) and math.isfinite(v)})
    out = []
    for k in keys:
        per = defaultdict(lambda: ([], []))
        for r, y, g in zip(rows, y_all, groups, strict=True):
            v = r.get(k)
            if isinstance(v, int | float) and math.isfinite(v) and math.isfinite(y):
                per[g][0].append(v)
                per[g][1].append(y)
        ics, n_tot, spreads = [], 0, []
        for xs, ys in per.values():
            if len(xs) < min_n // max(1, len(per)):
                continue
            x, y = np.asarray(xs), np.asarray(ys)
            ics.append((spearman(x, y), len(x)))
            n_tot += len(x)
            q_lo, q_hi = np.quantile(x, [0.2, 0.8])
            if q_hi > q_lo:
                spreads.append(float(y[x >= q_hi].mean() - y[x <= q_lo].mean()))
        if n_tot < min_n or not ics:
            continue
        ic = sum(i * n for i, n in ics) / n_tot
        n_eff = max(n_tot / max(overlap, 1), 3)
        t = ic * math.sqrt((n_eff - 2) / max(1 - ic * ic, 1e-9))
        p = 2 * (1 - NormalDist().cdf(abs(t)))
        spread = float(np.mean(spreads)) if spreads else 0.0
        out.append({"feature": k, "ic": ic, "t": t, "p": p, "n": n_tot, "spread": spread,
                    "covers_costs": abs(spread) > ROUND_TRIP_COST,
                    "consistent": all(np.sign(i) == np.sign(ic) for i, _ in ics if abs(i) > 0.005)})
    keep = bh_fdr([r["p"] for r in out], q=0.05)
    for r, ok in zip(out, keep, strict=True):
        r["significant"] = ok
    return sorted(out, key=lambda r: -abs(r["ic"]))


def collect(settings, data_15m, store, horizon: int) -> tuple[list[dict], list[float], list[str]]:
    from bot.backtest.engine import Backtester

    rows, groups, times = [], [], []
    bt = Backtester(settings, store=store, warmup_bars_15m=min(800, len(next(iter(data_15m.values()))) // 3))

    def grab(sym, t, f):
        rows.append({k: (float(dp.value) if dp.is_ok and isinstance(dp.value, int | float) else float("nan"))
                     for k, dp in f.items() if k.startswith(("ind.", "deriv.", "sent.", "dex.", "funding."))})
        groups.append(sym)
        times.append(int(t.timestamp() * 1000))
    bt.on_features = grab
    bt.run(data_15m)
    closes = {s: {c.open_time + 900_000: c.close for c in cs} for s, cs in data_15m.items()}
    fwd = []
    for sym, t in zip(groups, times, strict=True):
        a, b = closes[sym].get(t), closes[sym].get(t + horizon * 900_000)
        fwd.append(b / a - 1 if a and b else float("nan"))
    return rows, fwd, groups


async def _main(days: int, horizon: int) -> int:
    from bot.backtest.data import cached_klines
    from bot.core.config import load_settings
    from bot.data_engine.http import HttpClient
    from bot.data_engine.store import load_recorded_store

    s = load_settings(mode="backtest")
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    http = HttpClient()
    data = {sym: await cached_klines(http, s.endpoints.binance.rest, s.data.dir / "klines", sym, "15m", start, end)
            for sym in s.exchange.symbols}
    await http.close()
    store = load_recorded_store(s.data.dir / "raw", [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)])
    rows, fwd, groups = collect(s, data, store, horizon)
    res = screen(rows, fwd, groups, overlap=horizon)
    print(f"{len(rows)} velas · rendimiento a {horizon * 15} min · costo ida y vuelta {ROUND_TRIP_COST:.2%}")
    print(f"{'indicador':32s} {'IC':>7s} {'t':>6s} {'20% alto−bajo':>14s}  ¿significativo? ¿paga costos? ¿estable?")
    for r in res[:25]:
        print(f"{r['feature']:32s} {r['ic']:+.3f} {r['t']:+6.1f} {r['spread']:+13.3%}  "
              f"{'sí' if r['significant'] else 'no':>14s} {'sí' if r['covers_costs'] else 'no':>12s} "
              f"{'sí' if r['consistent'] else 'no':>9s}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--horizon", type=int, default=16, help="velas de 15 min hacia adelante (16 = 4 h)")
    a = ap.parse_args()
    return asyncio.run(_main(a.days, a.horizon))


if __name__ == "__main__":
    sys.exit(main())
