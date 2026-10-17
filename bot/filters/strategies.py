"""Estrategias candidatas deterministas (2–3, simples). Generan Signal o None.

Trabajan sobre los campos `ind.*` (indicadores) y del snapshot crudo. Si un
dato que la estrategia necesita es desconocido, no hay señal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from bot.core.config import Fees, StrategyParams
from bot.core.models import DataPoint, Regime, Side, Signal
from bot.risk.costs import estimate_costs

Fields = dict[str, DataPoint]


def _v(f: Fields, key: str) -> float | None:
    dp = f.get(key)
    return dp.as_float() if dp is not None else None


def _p(params: StrategyParams, key: str, default: float) -> float:
    extra = params.model_extra or {}
    return float(extra.get(key, default))


def signal_id(symbol: str, strategy: str, side: Side, ts: datetime) -> str:
    raw = f"{symbol}|{strategy}|{side.value}|{ts.isoformat()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _build(symbol: str, strategy: str, side: Side, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
           regime: Regime, invalidation_price: float | None, invalidation: str, urgent: bool,
           quality: float) -> Signal | None:
    entry = _v(f, "ind.15m.close")
    atr = _v(f, "ind.15m.atr")
    if entry is None or atr is None or atr <= 0:
        return None
    stop_dist = max(params.stop_atr_multiple, 1.0) * atr  # mínimo 1.0 × ATR(15m)
    target_dist = params.target_atr_multiple * atr
    sign = 1 if side is Side.LONG else -1
    stop = entry - sign * stop_dist
    tp = entry + sign * target_dist
    if stop <= 0 or tp <= 0:
        return None
    costs = estimate_costs(fees, side, entry_maker=not urgent, spread_bps=_v(f, "market.spread_bps"),
                           funding_rate_1h=_v(f, "funding.rate_1h"), hold_hours=params.max_hold_hours)
    return Signal(
        id=signal_id(symbol, strategy, side, now), symbol=symbol, strategy=strategy, side=side,
        entry=entry, stop=stop, take_profit=tp, expected_move_pct=target_dist / entry,
        total_cost_pct=max(costs.total_pct, 1e-6), data_quality=quality, regime=regime, urgent=urgent,
        invalidation=invalidation, invalidation_price=invalidation_price, max_hold_hours=params.max_hold_hours,
        created_at=now,
    )


def trend_pullback(symbol: str, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
                   regime: Regime, quality: float) -> Signal | None:
    """Retroceso a la EMA20 de 15m dentro de una tendencia de 1h, con CVD a favor."""
    if regime not in (Regime.TREND_UP, Regime.TREND_DOWN):
        return None
    need = [_v(f, k) for k in ("ind.15m.close", "ind.15m.ema20", "ind.15m.ema50", "ind.15m.atr",
                               "ind.1h.adx", "ind.15m.cvd_ratio", "ind.1h.structure")]
    if any(x is None for x in need):
        return None
    close, e20, e50, atr, adx1h, cvd, struct1h = need  # type: ignore[misc]
    if adx1h < _p(params, "min_adx", 20):
        return None
    near = abs(close - e20) <= _p(params, "max_ema_distance_atr", 0.5) * atr
    if regime is Regime.TREND_UP and near and close > e50 and cvd > 0 and struct1h >= 0:
        return _build(symbol, "trend_pullback", Side.LONG, f, params, fees, now, regime, e50,
                      "cierre de 15m bajo la EMA50 de 15m", urgent=False, quality=quality)
    if regime is Regime.TREND_DOWN and near and close < e50 and cvd < 0 and struct1h <= 0:
        return _build(symbol, "trend_pullback", Side.SHORT, f, params, fees, now, regime, e50,
                      "cierre de 15m sobre la EMA50 de 15m", urgent=False, quality=quality)
    return None


def range_breakout(symbol: str, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
                   regime: Regime, quality: float) -> Signal | None:
    """Ruptura de un rango comprimido de 15m con volumen y open interest subiendo."""
    if regime in (Regime.HIGH_VOL, Regime.INSUFFICIENT):
        return None
    need = [_v(f, k) for k in ("ind.15m.close", "ind.15m.high_n", "ind.15m.low_n", "ind.15m.atr",
                               "ind.15m.rel_volume", "ind.oi_chg_1h")]
    if any(x is None for x in need):
        return None
    close, hi, lo, atr, relvol, oi_chg = need  # type: ignore[misc]
    if atr <= 0 or (hi - lo) / atr > _p(params, "max_range_atr", 4.0):
        return None
    if relvol < _p(params, "min_rel_volume", 1.5) or oi_chg <= 0:
        return None
    if close > hi:
        return _build(symbol, "range_breakout", Side.LONG, f, params, fees, now, regime, hi,
                      "cierre de 15m de vuelta dentro del rango", urgent=True, quality=quality)
    if close < lo:
        return _build(symbol, "range_breakout", Side.SHORT, f, params, fees, now, regime, lo,
                      "cierre de 15m de vuelta dentro del rango", urgent=True, quality=quality)
    return None


def funding_reversion(symbol: str, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
                      regime: Regime, quality: float) -> Signal | None:
    """Funding extremo + divergencia DEX/CEX + sentimiento extremo + impulso agotándose."""
    if regime is Regime.INSUFFICIENT:
        return None
    need = [_v(f, k) for k in ("ind.funding_z", "ind.fund_diff_z", "sent.fng", "ind.15m.structure",
                               "ind.15m.cvd_ratio")]
    if any(x is None for x in need):
        return None
    fz, dz, fng, struct15, cvd = need  # type: ignore[misc]
    zmin = _p(params, "min_funding_z", 2.0)
    extreme = _p(params, "min_fng_extreme", 20)
    hi = _v(f, "ind.15m.high_n")
    lo = _v(f, "ind.15m.low_n")
    if fz >= zmin and dz >= 1.0 and fng >= 100 - extreme and struct15 <= 0 and cvd < 0:
        return _build(symbol, "funding_reversion", Side.SHORT, f, params, fees, now, regime, hi,
                      "nuevo máximo de 15m sobre el rango previo", urgent=False, quality=quality)
    if fz <= -zmin and dz <= -1.0 and fng <= extreme and struct15 >= 0 and cvd > 0:
        return _build(symbol, "funding_reversion", Side.LONG, f, params, fees, now, regime, lo,
                      "nuevo mínimo de 15m bajo el rango previo", urgent=False, quality=quality)
    return None


def trend_breakout(symbol: str, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
                   regime: Regime, quality: float) -> Signal | None:
    """Ruptura del máximo/mínimo de las últimas 6 h a favor de la tendencia de 1 h, con volumen.

    Validada con 13 meses de datos (walk-forward, guardias reales): la única variante que no tocó la detención
    por caída; casi en equilibrio antes de la IA (ver README, "Investigación de estrategias").
    """
    if regime not in (Regime.TREND_UP, Regime.TREND_DOWN):
        return None
    need = [_v(f, k) for k in ("ind.15m.close", "ind.15m.high_n", "ind.15m.low_n", "ind.15m.atr", "ind.1h.adx",
                               "ind.15m.rel_volume")]
    if any(x is None for x in need):
        return None
    close, hi, lo, atr, adx1h, relvol = need  # type: ignore[misc]
    if adx1h < _p(params, "min_adx", 20) or relvol < _p(params, "min_rel_volume", 1.2):
        return None
    if regime is Regime.TREND_UP and close > hi:
        return _build(symbol, "trend_breakout", Side.LONG, f, params, fees, now, regime, None,
                      "la tendencia de 1 h deja de ser alcista", urgent=False, quality=quality)
    if regime is Regime.TREND_DOWN and close < lo:
        return _build(symbol, "trend_breakout", Side.SHORT, f, params, fees, now, regime, None,
                      "la tendencia de 1 h deja de ser bajista", urgent=False, quality=quality)
    return None


StrategyFn = Callable[[str, Fields, StrategyParams, Fees, datetime, Regime, float], Signal | None]

STRATEGIES: dict[str, StrategyFn] = {
    "trend_pullback": trend_pullback,
    "range_breakout": range_breakout,
    "funding_reversion": funding_reversion,
    "trend_breakout": trend_breakout,
}


def generate(symbol: str, f: Fields, strategies: dict[str, StrategyParams], fees: Fees, now: datetime,
             quality: float) -> list[Signal]:
    reg_dp = f.get("ind.regime")
    regime = Regime(reg_dp.value) if reg_dp is not None and reg_dp.is_ok else Regime.INSUFFICIENT
    out = []
    for name, params in strategies.items():
        fn = STRATEGIES.get(name)
        if fn is None or not params.enabled:
            continue
        sig = fn(symbol, f, params, fees, now, regime, quality)
        if sig is not None:
            out.append(sig)
    return out
