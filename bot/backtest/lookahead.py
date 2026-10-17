"""¿El backtest mira al futuro? (idea del `lookahead-analysis` de freqtrade).

Corre el backtest completo y guarda los indicadores de cada vela. Luego lo repite cortando los datos justo en
varios momentos elegidos y compara: si un indicador cambia cuando se quitan los datos posteriores, estaba usando
información que en ese momento todavía no existía (y el backtest se veía mejor de lo real).

    python -m bot.backtest.lookahead --days 20 --samples 6
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bot.backtest.engine import Backtester
from bot.data_engine.market_state import INTERVAL_MS, Candle

TOL = 1e-9


def _values(f: dict) -> dict[str, Any]:
    out = {}
    for k, dp in f.items():
        v = dp.value if dp.is_ok else None
        out[k] = float(v) if isinstance(v, int | float) and not isinstance(v, bool) else v
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or abs(a - b) <= TOL * max(1.0, abs(a), abs(b))
    return a == b


def check(make_bt: Callable[[], Backtester], data_15m: dict[str, list[Candle]], samples: int = 6) -> dict[str, Any]:
    full: dict[tuple[str, datetime], dict[str, Any]] = {}
    bt = make_bt()
    bt.on_features = lambda s, t, f: full.__setitem__((s, t), _values(f))
    bt.run(data_15m)
    times = sorted({t for _, t in full})
    if len(times) < 2:
        return {"ok": True, "checked": 0, "bars": 0, "leaks": {}, "nota": "datos insuficientes"}
    step = max(1, (len(times) - 1) // samples)
    picks = times[step - 1:-1:step][:samples]  # nunca la última vela (ahí no hay futuro que quitar)
    leaks: dict[str, int] = {}
    compared = 0
    for t in picks:
        cut = int(t.timestamp() * 1000)
        trunc = {s: [c for c in cs if c.open_time + INTERVAL_MS["15m"] <= cut] for s, cs in data_15m.items()}
        got: dict[tuple[str, datetime], dict[str, Any]] = {}
        bt2 = make_bt()
        bt2.on_features = lambda s, tt, f, t=t: got.__setitem__((s, tt), _values(f)) if tt == t else None
        bt2.run(trunc)
        for key, vals in got.items():
            ref = full.get(key, {})
            compared += 1
            for k in set(ref) | set(vals):
                if not _same(ref.get(k), vals.get(k)):
                    leaks[k] = leaks.get(k, 0) + 1
    return {"ok": not leaks, "checked": len(picks), "bars": compared, "leaks": dict(sorted(leaks.items()))}


async def _main(days: int, samples: int) -> int:
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
    res = check(lambda: Backtester(s, store=store, warmup_bars_15m=min(800, days * 96 // 2)), data, samples)
    print(f"Velas revisadas: {res['bars']} en {res['checked']} cortes.")
    if res["ok"]:
        print("[OK] Ningún indicador cambia al quitar los datos posteriores: el backtest no mira al futuro.")
        return 0
    print("[X] Estos indicadores cambian al quitar datos del futuro (fuga):")
    for k, n in res["leaks"].items():
        print(f"   - {k}: {n} veces")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--samples", type=int, default=6)
    a = ap.parse_args()
    return asyncio.run(_main(a.days, a.samples))


if __name__ == "__main__":
    sys.exit(main())
