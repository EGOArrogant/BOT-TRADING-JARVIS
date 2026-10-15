"""Calcula los indicadores por moneda (campos `ind.*`) y el régimen.

Todo resultado es un DataPoint: si faltan barras o datos de origen, `unknown`.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from bot.core.clock import ms_to_dt
from bot.core.models import DataPoint, Regime
from bot.data_engine.market_state import INTERVAL_MS, MarketState
from bot.data_engine.store import FeedStore
from bot.data_engine.volatility import realized_vol
from bot.indicators import ta

SRC = "calc"
TIMEFRAMES = ("5m", "15m", "1h")


def _put(out: dict[str, DataPoint], key: str, value: float | None, ts: datetime | None) -> None:
    if value is None or ts is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        out[key] = DataPoint.unknown(SRC)
    else:
        out[key] = DataPoint.ok(float(value), SRC, ts)


def _last(x: np.ndarray) -> float | None:
    return None if len(x) == 0 or np.isnan(x[-1]) else float(x[-1])


def timeframe_features(a: dict[str, np.ndarray], tf: str, out: dict[str, DataPoint]) -> None:
    n = len(a["c"])
    ts = ms_to_dt(int(a["t"][-1]) + INTERVAL_MS[tf]) if n else None
    p = f"ind.{tf}."
    if n < 60:
        for k in ("close", "ema20", "ema50", "ema200", "atr", "adx", "ema20_slope_atr", "bb_width", "rel_volume",
                  "cvd_ratio", "structure", "high_n", "low_n"):
            _put(out, p + k, None, None)
        return
    c, h, l, v = a["c"], a["h"], a["l"], a["v"]
    e20, e50, e200 = ta.ema(c, 20), ta.ema(c, 50), ta.ema(c, 200)
    atr_ = ta.atr(h, l, c, 14)
    atr_last = _last(atr_)
    _put(out, p + "close", float(c[-1]), ts)
    _put(out, p + "ema20", _last(e20), ts)
    _put(out, p + "ema50", _last(e50), ts)
    _put(out, p + "ema200", _last(e200), ts)
    _put(out, p + "atr", atr_last, ts)
    _put(out, p + "adx", _last(ta.adx(h, l, c, 14)), ts)
    slope = ta.slope_per_bar(e20, 10)
    _put(out, p + "ema20_slope_atr", slope / atr_last if slope is not None and atr_last else None, ts)
    _put(out, p + "bb_width", ta.bollinger_width(c), ts)
    _put(out, p + "rel_volume", ta.relative_volume(v), ts)
    vol12 = v[-12:].sum()
    _put(out, p + "cvd_ratio", float(a["delta"][-12:].sum() / vol12) if vol12 > 0 else None, ts)
    _put(out, p + "structure", float(ta.structure(h, l)), ts)
    _put(out, p + "high_n", float(h[-25:-1].max()), ts)  # rango previo (24 barras, sin la actual)
    _put(out, p + "low_n", float(l[-25:-1].min()), ts)
    if tf == "15m":
        vwap = ta.daily_vwap(a["t"], h, l, c, v)
        _put(out, "ind.vwap_dist_atr", (c[-1] - vwap) / atr_last if vwap and atr_last else None, ts)


def _hist_change(store: FeedStore, key: str, now: datetime, delta: timedelta) -> float | None:
    cur = store.get(key)
    past = store.value_at_or_before(key, now - delta)
    if not cur.is_ok or past is None or past == 0:
        return None
    return float(cur.value) / past - 1  # type: ignore[arg-type]


def compute_features(state: MarketState, store: FeedStore, now: datetime,
                     btc_state: MarketState | None = None) -> dict[str, DataPoint]:
    out: dict[str, DataPoint] = {}
    arrays = {tf: state.candles[tf].arrays() for tf in TIMEFRAMES}
    for tf in TIMEFRAMES:
        timeframe_features(arrays[tf], tf, out)

    sym = state.symbol
    one_m = state.candles["1m"].arrays()
    h1 = arrays["1h"]
    ts_1h = ms_to_dt(int(h1["t"][-1]) + INTERVAL_MS["1h"]) if len(h1["t"]) else None
    ts_1m = ms_to_dt(int(one_m["t"][-1]) + INTERVAL_MS["1m"]) if len(one_m["t"]) else None

    # --- volatilidad realizada
    _put(out, "ind.rv_1h", realized_vol(one_m["c"][-61:], 1) if len(one_m["c"]) >= 61 else None, ts_1m)
    _put(out, "ind.rv_24h", realized_vol(h1["c"][-25:], 60) if len(h1["c"]) >= 25 else None, ts_1h)
    _put(out, "ind.rv_7d", realized_vol(h1["c"][-169:], 60) if len(h1["c"]) >= 169 else None, ts_1h)
    if len(h1["c"]) >= 200:
        pv = ta.parkinson_vol(h1["h"], h1["l"])[-720:]
        _put(out, "ind.vol_pct_30d", ta.percentile_rank_last(pv), ts_1h)
    else:
        _put(out, "ind.vol_pct_30d", None, None)

    coin = sym.removesuffix("USDT")
    dvol = store.get(f"vol.dvol.{coin}")
    if dvol.is_ok and out["ind.rv_24h"].is_ok:
        _put(out, "ind.var_premium", float(dvol.value) - 100 * float(out["ind.rv_24h"].value), dvol.ts)  # type: ignore[arg-type]
        _put(out, "ind.dvol_chg_24h", _hist_change(store, f"vol.dvol.{coin}", now, timedelta(hours=24)), dvol.ts)
    else:
        _put(out, "ind.var_premium", None, None)
        _put(out, "ind.dvol_chg_24h", None, None)

    # --- derivados
    fhist = store.values(f"deriv.funding_hist.{sym}", since=now - timedelta(days=30))
    rate = state.funding_rate
    mark_ts = ms_to_dt(state.mark_ms) if state.mark_ms else None
    if rate is not None and len(fhist) >= 20:
        _put(out, "ind.funding_z", ta.zscore_last(np.append(fhist, rate)), mark_ts)
    else:
        _put(out, "ind.funding_z", None, None)

    oi_key = f"deriv.oi.{sym}"
    oi = store.get(oi_key)
    _put(out, "ind.oi_chg_1h", _hist_change(store, oi_key, now, timedelta(hours=1)), oi.ts)
    _put(out, "ind.oi_chg_24h", _hist_change(store, oi_key, now, timedelta(hours=24)), oi.ts)
    oi_hist = store.values(oi_key, since=now - timedelta(days=30))
    if oi.is_ok and len(oi_hist) >= 24:
        _put(out, "ind.oi_at_30d_high", 1.0 if float(oi.value) >= 0.99 * oi_hist.max() else 0.0, oi.ts)  # type: ignore[arg-type]
    else:
        _put(out, "ind.oi_at_30d_high", None, None)
    # precio/OI: +1 posiciones nuevas a favor del movimiento, −1 cierre
    if out["ind.oi_chg_1h"].is_ok and len(h1["c"]) >= 2:
        p_up = h1["c"][-1] > h1["c"][-2]
        oi_up = float(out["ind.oi_chg_1h"].value) > 0  # type: ignore[arg-type]
        code = {(True, True): 2.0, (False, True): -2.0, (True, False): 1.0, (False, False): -1.0}[(p_up, oi_up)]
        _put(out, "ind.price_oi_code", code, oi.ts)
    else:
        _put(out, "ind.price_oi_code", None, None)

    # --- diferencial de funding DEX vs CEX (ambos por hora)
    dex = store.get(f"dex.funding_1h.{sym}")
    if dex.is_ok and rate is not None:
        diff = float(rate) / state.funding_interval_hours - float(dex.value)  # type: ignore[arg-type]
        store.set(f"calc.fund_diff.{sym}", diff, SRC, dex.ts)  # type: ignore[arg-type]
        _put(out, "ind.fund_diff_1h", diff, dex.ts)
        _put(out, "ind.fund_diff_z", ta.zscore_last(store.values(f"calc.fund_diff.{sym}",
                                                                 since=now - timedelta(days=30))), dex.ts)
    else:
        _put(out, "ind.fund_diff_1h", None, None)
        _put(out, "ind.fund_diff_z", None, None)

    # --- contexto
    fng = store.get("sent.fng")
    fng_past = store.value_at_or_before("sent.fng", now - timedelta(days=7))
    _put(out, "ind.fng_chg_7d", float(fng.value) - fng_past if fng.is_ok and fng_past is not None else None, fng.ts)  # type: ignore[arg-type]
    sc = store.get("onchain.stablecoin_supply")
    _put(out, "ind.stable_chg_7d", _hist_change(store, "onchain.stablecoin_supply", now, timedelta(days=7)), sc.ts)
    if btc_state is not None and btc_state is not state:
        b5 = btc_state.candles["5m"].arrays()["c"][-289:]
        _put(out, "ind.corr_btc_24h", ta.correlation(arrays["5m"]["c"][-289:], b5),
             out.get("ind.5m.close", DataPoint.unknown(SRC)).ts)
    elif btc_state is state:
        _put(out, "ind.corr_btc_24h", 1.0, ts_1h)
    else:
        _put(out, "ind.corr_btc_24h", None, None)

    out["ind.regime"] = DataPoint.ok(detect_regime(out).value, SRC, ts_1h) if ts_1h else DataPoint.unknown(SRC)
    return out


def detect_regime(f: dict[str, DataPoint]) -> Regime:
    need = ("ind.1h.ema20", "ind.1h.ema50", "ind.1h.ema200", "ind.1h.adx", "ind.1h.ema20_slope_atr",
            "ind.vol_pct_30d")
    if any(not f.get(k, DataPoint.unknown(SRC)).is_ok for k in need):
        return Regime.INSUFFICIENT
    g = {k: float(f[k].value) for k in need}  # type: ignore[arg-type]
    if g["ind.vol_pct_30d"] >= 90:
        return Regime.HIGH_VOL
    if g["ind.1h.adx"] >= 20:
        if g["ind.1h.ema20"] > g["ind.1h.ema50"] > g["ind.1h.ema200"] and g["ind.1h.ema20_slope_atr"] > 0:
            return Regime.TREND_UP
        if g["ind.1h.ema20"] < g["ind.1h.ema50"] < g["ind.1h.ema200"] and g["ind.1h.ema20_slope_atr"] < 0:
            return Regime.TREND_DOWN
    return Regime.RANGE
