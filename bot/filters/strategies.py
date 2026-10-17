"""Estrategias candidatas deterministas (2–3, simples). Generan Signal o None.

Trabajan sobre los campos `ind.*` (indicadores) y del snapshot crudo. Si un
dato que la estrategia necesita es desconocido, no hay señal.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

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


def discount_absorption(symbol: str, f: Fields, params: StrategyParams, fees: Fees, now: datetime,
                        regime: Regime, quality: float) -> Signal | None:
    """Retroceso al "descuento" con absorción (idea de Chris Kmer, campeón del Robbins World Cup, en reglas).

    Ubicación: el precio retrocede al 70,5–88,6 % del último impulso de 1 h (Fibonacci) sin haber pasado del 88,6 %.
    Confirmación: en la vela anterior de 15 min los agresores contrarios dominaron (delta en contra) pero la vela
    cerró en su mitad buena (absorción: esfuerzo sin resultado); la vela actual gira a favor y su extremo queda más
    adentro (fallan otra vez, más arriba). Stop detrás de los agresores que fallaron (mín. 1 ATR de 15 min);
    objetivo = el extremo del impulso, con al menos `min_rr` a 1. Sin la confirmación pierde (ver README).
    """
    need = [_v(f, k) for k in ("ind.15m.close", "ind.15m.open", "ind.15m.high", "ind.15m.low", "ind.15m.atr",
                               "ind.15m.prev_high", "ind.15m.prev_low", "ind.15m.prev_close",
                               "ind.15m.prev_delta_ratio", "ind.1h.atr")]
    if any(x is None for x in need):
        return None
    close, open_, high, low, atr, ph, pl, pc, pdelta, atr1h = need  # type: ignore[misc]
    if atr <= 0 or ph <= pl:
        return None
    zlo, zhi, inval = _p(params, "zone_low", 0.705), _p(params, "zone_high", 0.886), _p(params, "invalid_beyond", 0.886)
    for side in (Side.LONG, Side.SHORT):
        sg = 1 if side is Side.LONG else -1
        pre = "up" if side is Side.LONG else "dn"
        extreme, origin, ext = (_v(f, f"ind.1h.imp_{pre}_{k}") for k in (("high", "low", "ext") if sg > 0 else
                                                                           ("low", "high", "ext")))
        if extreme is None or origin is None or ext is None:
            continue
        span = extreme - origin
        if sg * span <= 0 or abs(span) < _p(params, "min_impulse_atr1h", 2.0) * atr1h:
            continue
        r = (extreme - close) / span                     # 0 = en el extremo del impulso, 1 = en su origen
        if not zlo <= r <= zhi or (extreme - ext) / span > inval:
            continue
        closepos = (pc - pl) / (ph - pl) if sg > 0 else (ph - pc) / (ph - pl)
        absorbed = sg * pdelta <= -_p(params, "min_delta", 0.05) and closepos >= 0.5
        shift = (close > open_ and low > pl) if sg > 0 else (close < open_ and high < ph)
        if not (absorbed and shift):
            continue
        stop = (min(pl, low) if sg > 0 else max(ph, high)) - sg * _p(params, "stop_buffer_atr", 0.1) * atr
        if sg * (close - stop) < params.stop_atr_multiple * atr:   # mínimo 1 ATR de 15 min (CLAUDE.md §9)
            stop = close - sg * params.stop_atr_multiple * atr
        risk, reward = sg * (close - stop), sg * (extreme - close)
        if risk <= 0 or reward <= 0 or reward / risk < _p(params, "min_rr", 1.5):
            continue
        costs = estimate_costs(fees, side, entry_maker=True, spread_bps=_v(f, "market.spread_bps"),
                               funding_rate_1h=_v(f, "funding.rate_1h"), hold_hours=params.max_hold_hours)
        return Signal(
            id=signal_id(symbol, "discount_absorption", side, now), symbol=symbol, strategy="discount_absorption",
            side=side, entry=close, stop=stop, take_profit=extreme, expected_move_pct=reward / close,
            total_cost_pct=max(costs.total_pct, 1e-6), data_quality=quality, regime=regime, urgent=False,
            invalidation=f"el precio pasa del {inval:.1%} del impulso de 1 h", invalidation_price=None,
            max_hold_hours=params.max_hold_hours, created_at=now,
        )
    return None


def breakout_checklist(f: Fields, params: StrategyParams) -> dict[str, Any]:
    """Qué le falta a trend_breakout para dar señal, con las mismas condiciones que la estrategia (para el panel)."""
    reg = f.get("ind.regime")
    regime = reg.value if reg is not None and reg.is_ok else None
    side = "LONG" if regime == Regime.TREND_UP.value else "SHORT" if regime == Regime.TREND_DOWN.value else None
    close, hi, lo = _v(f, "ind.15m.close"), _v(f, "ind.15m.high_n"), _v(f, "ind.15m.low_n")
    adx, relvol = _v(f, "ind.1h.adx"), _v(f, "ind.15m.rel_volume")
    min_adx, min_rv = _p(params, "min_adx", 20), _p(params, "min_rel_volume", 1.2)
    level = hi if side == "LONG" else lo if side == "SHORT" else None
    broke = None if close is None or level is None else (close > level if side == "LONG" else close < level)
    items = [
        {"key": "trend", "label": "Tendencia clara en 1 h (EMAs alineadas)", "ok": side is not None, "value": regime},
        {"key": "adx", "label": f"Fuerza de la tendencia (ADX 1 h) ≥ {min_adx:g}", "ok": adx is not None and adx >= min_adx,
         "value": adx},
        {"key": "volume", "label": f"Volumen ≥ {min_rv:g}× lo normal", "ok": relvol is not None and relvol >= min_rv,
         "value": relvol},
        {"key": "break", "label": "Una vela de 15 min cierra fuera del rango de 6 h", "ok": bool(broke), "value": close,
         "level": level, "gap_pct": (level - close) / close if level is not None and close else None},
    ]
    return {"strategy": "trend_breakout", "side": side, "items": items, "ready": all(i["ok"] for i in items)}


def absorption_checklist(f: Fields, params: StrategyParams) -> dict[str, Any]:
    """Qué le falta a discount_absorption (mismas condiciones que la estrategia), en el sentido más cercano a darse."""
    zlo, zhi, inval = _p(params, "zone_low", 0.705), _p(params, "zone_high", 0.886), _p(params, "invalid_beyond", 0.886)
    close, open_, high, low = (_v(f, f"ind.15m.{k}") for k in ("close", "open", "high", "low"))
    ph, pl, pc, pdelta = (_v(f, f"ind.15m.prev_{k}") for k in ("high", "low", "close", "delta_ratio"))
    atr1h = _v(f, "ind.1h.atr")
    best = None
    for side, pre, keys in (("LONG", "up", ("high", "low", "ext")), ("SHORT", "dn", ("low", "high", "ext"))):
        extreme, origin, ext = (_v(f, f"ind.1h.imp_{pre}_{k}") for k in keys)
        sg = 1 if side == "LONG" else -1
        if None in (extreme, origin, ext, close) or sg * (extreme - origin) <= 0:
            continue
        span = extreme - origin
        r = (extreme - close) / span
        big = atr1h is not None and abs(span) >= _p(params, "min_impulse_atr1h", 2.0) * atr1h
        alive = (extreme - ext) / span <= inval
        absorbed = None not in (ph, pl, pc, pdelta) and ph > pl and sg * pdelta <= -_p(params, "min_delta", 0.05) and \
            ((pc - pl) if sg > 0 else (ph - pc)) / (ph - pl) >= 0.5
        shift = None not in (open_, low, high, pl, ph) and ((close > open_ and low > pl) if sg > 0 else
                                                           (close < open_ and high < ph))
        items = [
            {"key": "impulse", "label": f"Impulso de 1 h {'alcista' if sg > 0 else 'bajista'} de al menos "
                                        f"{_p(params, 'min_impulse_atr1h', 2.0):g} ATR", "ok": big, "value": origin,
             "level": extreme},
            {"key": "zone", "label": f"Precio en el descuento de Fibonacci ({zlo:.1%}–{zhi:.1%})",
             "ok": zlo <= r <= zhi and alive, "value": r, "level": extreme - zlo * span, "level2": extreme - zhi * span},
            {"key": "absorb", "label": "Vela anterior: agresores en contra sin resultado (absorción)",
             "ok": bool(absorbed), "value": pdelta},
            {"key": "shift", "label": "Vela actual gira a favor y fallan otra vez más adentro", "ok": bool(shift)},
        ]
        score = sum(i["ok"] for i in items) + (0.5 if alive else 0) - abs(r - (zlo + zhi) / 2) / 10
        if best is None or score > best[0]:
            best = (score, {"strategy": "discount_absorption", "side": side, "items": items,
                            "ready": all(i["ok"] for i in items)})
    return best[1] if best else {"strategy": "discount_absorption", "side": None, "items": [], "ready": False}


StrategyFn = Callable[[str, Fields, StrategyParams, Fees, datetime, Regime, float], Signal | None]

STRATEGIES: dict[str, StrategyFn] = {
    "trend_pullback": trend_pullback,
    "range_breakout": range_breakout,
    "funding_reversion": funding_reversion,
    "trend_breakout": trend_breakout,
    "discount_absorption": discount_absorption,
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
