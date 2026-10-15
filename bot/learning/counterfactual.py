"""Resultado contrafactual: qué habría pasado con un candidato no operado.

Simulación conservadora sobre velas posteriores: si stop y objetivo caen en
la misma vela, cuenta el stop; si ninguno se toca en `max_hold_hours`, sale al
cierre. El costo estimado del candidato se descuenta (en R).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from bot.data_engine.market_state import Candle


def simulate(side: str, entry: float, stop: float, take_profit: float | None, start: datetime,
             max_hold_hours: float, candles: list[Candle], cost_pct: float = 0.0,
             interval_ms: int = 900_000) -> dict | None:
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    long = side == "LONG"
    start_ms = int(start.timestamp() * 1000)
    end_ms = start_ms + int(max_hold_hours * 3_600_000)
    future = [c for c in candles if c.open_time >= start_ms and c.open_time < end_ms]
    if not future or future[-1].open_time + interval_ms < end_ms:
        return None  # todavía no hay datos suficientes
    exit_px, reason, mae, mfe = future[-1].close, "tiempo", 0.0, 0.0
    for c in future:
        lo_r = ((c.low - entry) if long else (entry - c.high)) / risk
        hi_r = ((c.high - entry) if long else (entry - c.low)) / risk
        mae, mfe = min(mae, lo_r), max(mfe, hi_r)
        if (c.low <= stop) if long else (c.high >= stop):
            exit_px, reason = stop, "stop"
            break
        if take_profit is not None and ((c.high >= take_profit) if long else (c.low <= take_profit)):
            exit_px, reason = take_profit, "take_profit"
            break
    gross_r = ((exit_px - entry) if long else (entry - exit_px)) / risk
    cost_r = cost_pct * entry / risk
    return {"r_gross": round(gross_r, 4), "r_net": round(gross_r - cost_r, 4), "exit": reason,
            "mae_r": round(mae, 4), "mfe_r": round(mfe, 4)}


def due(candidate_ts: datetime, max_hold_hours: float, now: datetime) -> bool:
    return now >= candidate_ts + timedelta(hours=max_hold_hours, minutes=15)
