"""Foto en vivo que el bot escribe cada minuto para el panel.

El panel corre en otro proceso y no ve la memoria del bot: lo que solo vive en
memoria (posiciones abiertas, precios recientes) se publica aquí. El panel nunca
escribe en este archivo ni en la base de datos.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.core.fsutil import atomic_write_text, read_text_retry
from bot.core.config import StrategyParams
from bot.core.models import DataPoint, Position, Side
from bot.filters.strategies import absorption_checklist, breakout_checklist
from bot.data_engine.market_state import MarketState
from bot.indicators import ta

PRICE_BARS = 192  # 48 h de velas de 15 min
CANDLE_BARS = {"15m": 192, "1h": 168}  # velas para el gráfico: 48 h en 15 min y 7 días en 1 h
FILENAME = "panel_vivo.json"


def _position(pos: Position, st: MarketState | None) -> dict[str, Any]:
    mark = (st.mark_price or st.last_price) if st is not None else None
    entry, qty = float(pos.entry_price), float(pos.qty)
    sign = 1 if pos.side is Side.LONG else -1
    return {
        "symbol": pos.symbol, "side": pos.side.value, "qty": qty, "entry": entry,
        "stop": float(pos.stop_price), "take_profit": float(pos.take_profit_price) if pos.take_profit_price else None,
        "opened_at": pos.opened_at.isoformat(), "strategy": pos.strategy, "mark": mark,
        "pnl_unrealized": (mark - entry) * qty * sign if mark else None,
        "ai_confidence": pos.ai_confidence, "initial_stop": float(pos.initial_stop_price),
        "invalidation": pos.invalidation,
        "invalidation_price": float(pos.invalidation_price) if pos.invalidation_price else None,
        "signal_id": pos.signal_id, "max_hold_hours": pos.max_hold_hours,
    }


VIEW_KEYS = ("ind.1h.adx", "ind.15m.rel_volume", "ind.15m.cvd_ratio", "ind.funding_z", "funding.rate", "ind.oi_chg_1h",
             "ind.oi_chg_24h", "ind.1h.fib_pos", "ind.1h.room_up_atr", "ind.1h.room_down_atr", "ind.vol_pct_30d",
             "ind.corr_btc_24h", "sent.fng", "ind.fund_diff_z", "dex.smart_ratio", "market.spread_bps")


def view(fields: dict[str, DataPoint], strategies: dict[str, StrategyParams], quality: float,
         now: datetime) -> dict[str, Any]:
    """Lo que vio el bot en una moneda en la última ventana de 15 min: régimen, datos clave y qué le falta a la
    estrategia activa para dar señal. Solo para mostrar; las decisiones salen de los filtros, no de aquí."""
    reg = fields.get("ind.regime")
    out: dict[str, Any] = {"ts": now.isoformat(), "quality": round(quality, 3),
                           "regime": reg.value if reg is not None and reg.is_ok else None,
                           "fields": {k: (fields[k].value if k in fields and fields[k].is_ok else None) for k in VIEW_KEYS}}
    out["checklists"] = []
    for name, fn in (("trend_breakout", breakout_checklist), ("discount_absorption", absorption_checklist)):
        p = strategies.get(name)
        if p is not None and p.enabled:
            out["checklists"].append(fn(fields, p))
    return out


def levels(st: MarketState) -> dict[str, Any] | None:
    """Lo que mira el bot en el gráfico: giros de 1 h (soportes, resistencias, Fibonacci) y el rango de 6 h
    (24 velas de 15 min) cuya ruptura busca trend_breakout. Mismo cálculo que `features` (ind.1h.*, high_n)."""
    a1, a15 = st.candles["1h"].arrays(), st.candles["15m"].arrays()
    if len(a1["c"]) < 60:
        return None
    atr = ta.atr(a1["h"], a1["l"], a1["c"], 14)
    atr_last = float(atr[-1]) if len(atr) and atr[-1] == atr[-1] else None
    highs, lows = ta.pivots(a1["h"], a1["l"])
    close = float(a1["c"][-1])
    t = a1["t"]
    out: dict[str, Any] = {"atr_1h": atr_last,
                           "res": sorted({round(p, 10) for _, p in highs if p > close})[:3],
                           "sup": sorted({round(p, 10) for _, p in lows if p < close}, reverse=True)[:3]}
    if highs and lows:
        out["swing_high"] = [int(t[highs[-1][0]]), highs[-1][1]]
        out["swing_low"] = [int(t[lows[-1][0]]), lows[-1][1]]
    if len(a15["h"]) >= 25:
        out["range_15m"] = {"from": int(a15["t"][-24]), "high": float(a15["h"][-24:].max()),
                            "low": float(a15["l"][-24:].min())}
    return out


def build(now: datetime, mode: str, states: dict[str, MarketState], positions: dict[str, Position],
          top_traders: dict[str, Any] | None = None, views: dict[str, Any] | None = None) -> dict[str, Any]:
    prices: dict[str, list[list[float]]] = {}
    last: dict[str, float | None] = {}
    ohlc: dict[str, dict[str, list[list[float]]]] = {}
    for sym, st in states.items():
        ohlc[sym] = {}
        for tf, n in CANDLE_BARS.items():
            series = st.candles[tf]
            rows = list(series.closed()[-n:])
            if series.partial is not None and (not rows or series.partial.open_time > rows[-1].open_time):
                rows = rows[-(n - 1):] + [series.partial]  # la vela en curso, para ver el precio de ahora
            ohlc[sym][tf] = [[c.open_time, c.open, c.high, c.low, c.close] for c in rows]
        candles = st.candles["15m"].closed()[-PRICE_BARS:]
        prices[sym] = [[c.open_time + 900_000, c.close] for c in candles]  # hora de cierre de la vela
        last[sym] = st.mark_price or st.last_price or (candles[-1].close if candles else None)
    return {"updated_at": now.isoformat(), "mode": mode, "prices": prices, "candles": ohlc, "last": last,
            "positions": [_position(p, states.get(s)) for s, p in positions.items()],
            "views": views or {},
            "market": {sym: {"mark": st.mark_price, "index": st.index_price, "funding_rate": st.funding_rate,
                             "next_funding_ms": st.next_funding_ms} for sym, st in states.items()},
            "levels": {sym: lv for sym, st in states.items() if (lv := _safe_levels(st)) is not None},
            "top_traders": top_traders}


def _safe_levels(st: MarketState) -> dict[str, Any] | None:
    try:
        return levels(st)
    except Exception:  # noqa: BLE001  (los niveles son solo para ver: nunca tumban la foto del panel)
        return None


def write(path: Path, snap: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(snap, separators=(",", ":")))


def read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(read_text_retry(path))
    except (OSError, ValueError):
        return None
