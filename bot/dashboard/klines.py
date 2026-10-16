"""Velas para el gráfico del panel en cualquier temporalidad (de 1 minuto a 1 mes).

El panel las pide a la API pública de Binance (`/fapi/v1/klines`: datos de mercado, sin llave
ni cuenta) y las guarda unos segundos para no repetir consultas. Si Binance no responde, el
gráfico usa las velas de 15 min y 1 h que el bot publica en `panel_vivo.json`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

# Temporalidades de Binance USDⓈ-M (las mismas que ofrece el gráfico de Hyperliquid y más)
INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M")
LIMIT = 1000
CACHE_S = 10.0

Fetch = Callable[[str, dict[str, Any]], Any]


def _http_fetch(url: str, params: dict[str, Any]) -> Any:
    r = httpx.get(url, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


class KlineSource:
    def __init__(self, rest: str, symbols: list[str], fetch: Fetch = _http_fetch, clock=time.monotonic):
        self.url = f"{rest}/fapi/v1/klines"
        self.symbols = set(symbols)
        self.fetch, self.clock = fetch, clock
        self._cache: dict[tuple[str, str], tuple[float, list[list[float]]]] = {}
        self._lock = threading.Lock()

    def get(self, symbol: str, interval: str) -> list[list[float]]:
        """[[apertura_ms, open, high, low, close, volumen], ...]; ValueError si la petición no es válida."""
        if symbol not in self.symbols:
            raise ValueError("moneda no seguida por el bot")
        if interval not in INTERVALS:
            raise ValueError("temporalidad no válida")
        key = (symbol, interval)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < CACHE_S:
                return hit[1]
        raw = self.fetch(self.url, {"symbol": symbol, "interval": interval, "limit": LIMIT})
        rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in raw]
        with self._lock:
            self._cache[key] = (self.clock(), rows)
        return rows
