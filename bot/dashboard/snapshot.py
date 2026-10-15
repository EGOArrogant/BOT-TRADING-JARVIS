"""Foto en vivo que el bot escribe cada minuto para el panel.

El panel corre en otro proceso y no ve la memoria del bot: lo que solo vive en
memoria (posiciones abiertas, precios recientes) se publica aquí. El panel nunca
escribe en este archivo ni en la base de datos.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from bot.core.models import Position, Side
from bot.data_engine.market_state import MarketState

PRICE_BARS = 192  # 48 h de velas de 15 min
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
        "ai_confidence": pos.ai_confidence,
    }


def build(now: datetime, mode: str, states: dict[str, MarketState], positions: dict[str, Position]) -> dict[str, Any]:
    prices: dict[str, list[list[float]]] = {}
    last: dict[str, float | None] = {}
    for sym, st in states.items():
        candles = st.candles["15m"].closed()[-PRICE_BARS:]
        prices[sym] = [[c.open_time + 900_000, c.close] for c in candles]  # hora de cierre de la vela
        last[sym] = st.mark_price or st.last_price or (candles[-1].close if candles else None)
    return {"updated_at": now.isoformat(), "mode": mode, "prices": prices, "last": last,
            "positions": [_position(p, states.get(s)) for s, p in positions.items()]}


def write(path: Path, snap: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".panel-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, separators=(",", ":"))
    os.replace(tmp, path)


def read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
