"""Velas para el gráfico del panel en cualquier temporalidad (de 1 minuto a 1 mes), más la franja de mercado
(precio de marca, cambio y volumen de 24 h, interés abierto, funding) y el libro de órdenes.

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
TICKER_CACHE_S = 5.0
DEPTH_CACHE_S = 2.0
DEPTH_LIMIT = 20

Fetch = Callable[[str, dict[str, Any]], Any]


def _http_fetch(url: str, params: dict[str, Any]) -> Any:
    r = httpx.get(url, params=params, timeout=8)
    r.raise_for_status()
    return r.json()


class KlineSource:
    def __init__(self, rest: str, symbols: list[str], fetch: Fetch = _http_fetch, clock=time.monotonic):
        self.url = f"{rest}/fapi/v1/klines"
        self.rest = rest
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

    def _cached(self, key: tuple[str, str], ttl: float, make: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < ttl:
                return hit[1]
        val = make()
        with self._lock:
            self._cache[key] = (self.clock(), val)
        return val

    def _check(self, symbol: str) -> None:
        if symbol not in self.symbols:
            raise ValueError("moneda no seguida por el bot")

    def ticker(self, symbol: str) -> dict[str, Any]:
        """Franja de mercado. Endpoints verificados en la doc oficial (2026-10-17): premiumIndex (marca, índice,
        funding, próximo cobro), openInterest y klines; el cambio, rango y volumen de 24 h salen de 24 velas de 1 h."""
        self._check(symbol)

        def make() -> dict[str, Any]:
            prem = self.fetch(f"{self.rest}/fapi/v1/premiumIndex", {"symbol": symbol})
            k = self.fetch(self.url, {"symbol": symbol, "interval": "1h", "limit": 25})[-24:]
            try:
                oi = float(self.fetch(f"{self.rest}/fapi/v1/openInterest", {"symbol": symbol})["openInterest"])
            except Exception:  # noqa: BLE001  (sin interés abierto la franja sigue)
                oi = None
            mark = float(prem["markPrice"])
            last, open24 = float(k[-1][4]), float(k[0][1])
            return {"symbol": symbol, "mark": mark, "index": float(prem["indexPrice"]), "last": last,
                    "funding_rate": float(prem["lastFundingRate"]), "next_funding_ms": int(prem["nextFundingTime"]),
                    "change_pct": (last / open24 - 1) if open24 else None,
                    "high_24h": max(float(x[2]) for x in k), "low_24h": min(float(x[3]) for x in k),
                    "quote_volume_24h": sum(float(x[7]) for x in k),
                    "open_interest": oi, "oi_usd": oi * mark if oi is not None else None}
        return self._cached((symbol, "ticker"), TICKER_CACHE_S, make)

    def depth(self, symbol: str) -> dict[str, Any]:
        """Libro de órdenes, 20 niveles por lado (/fapi/v1/depth, público)."""
        self._check(symbol)

        def make() -> dict[str, Any]:
            d = self.fetch(f"{self.rest}/fapi/v1/depth", {"symbol": symbol, "limit": DEPTH_LIMIT})
            side = lambda rows: [[float(p), float(q)] for p, q in rows[:DEPTH_LIMIT]]  # noqa: E731
            return {"symbol": symbol, "bids": side(d["bids"]), "asks": side(d["asks"]), "time": d.get("T")}
        return self._cached((symbol, "depth"), DEPTH_CACHE_S, make)
