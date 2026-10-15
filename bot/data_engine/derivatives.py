"""Open interest y ratios de Binance (REST /futures/data/*), cada 5 min.

Funding viene del stream markPrice (market_state). Aquí además se guarda el
histórico de funding (/fapi/v1/fundingRate) para el z-score de 30 días.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from bot.core.clock import ms_to_dt
from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.derivatives")

SOURCE = "binance_rest"

# endpoint -> (clave en store, campo del JSON)
RATIO_ENDPOINTS = {
    "/futures/data/openInterestHist": ("deriv.oi", "sumOpenInterestValue"),
    "/futures/data/topLongShortPositionRatio": ("deriv.top_ls_ratio", "longShortRatio"),
    "/futures/data/globalLongShortAccountRatio": ("deriv.global_ls_ratio", "longShortRatio"),
    "/futures/data/takerlongshortRatio": ("deriv.taker_ratio", "buySellRatio"),
}


def parse_series(rows: list[dict], field: str) -> list[tuple[datetime, float]]:
    out = []
    for r in rows:
        if r.get(field) in (None, ""):
            continue
        out.append((ms_to_dt(int(r["timestamp"])), float(r[field])))
    return sorted(out)


def parse_funding_history(rows: list[dict]) -> list[tuple[datetime, float]]:
    return sorted((ms_to_dt(int(r["fundingTime"])), float(r["fundingRate"])) for r in rows)


class DerivativesPoller:
    def __init__(self, rest_base: str, symbols: list[str], store: FeedStore, http: HttpClient, period: str = "5m"):
        self.rest = rest_base.rstrip("/")
        self.symbols = symbols
        self.store = store
        self.http = http
        self.period = period

    def key(self, symbol: str, base: str) -> str:
        return f"{base}.{symbol}"

    async def poll_once(self, limit: int = 30) -> None:
        for sym in self.symbols:
            for path, (base, field) in RATIO_ENDPOINTS.items():
                try:
                    rows = await self.http.get_json(
                        f"{self.rest}{path}", limit_key="binance_data",
                        params={"symbol": sym, "period": self.period, "limit": limit},
                    )
                    for ts, v in parse_series(rows, field):
                        self.store.set(self.key(sym, base), v, SOURCE, ts)
                except Exception as exc:  # noqa: BLE001
                    log.warning("derivados: fallo", extra={"symbol": sym, "path": path, "err": repr(exc)})

    async def load_funding_history(self, limit: int = 200) -> None:
        for sym in self.symbols:
            try:
                rows = await self.http.get_json(f"{self.rest}/fapi/v1/fundingRate", limit_key="binance",
                                                params={"symbol": sym, "limit": limit})
                for ts, v in parse_funding_history(rows):
                    self.store.set(self.key(sym, "deriv.funding_hist"), v, SOURCE, ts)
            except Exception as exc:  # noqa: BLE001
                log.warning("historial funding: fallo", extra={"symbol": sym, "err": repr(exc)})

    async def backfill(self) -> None:
        """Hasta 30 días de OI/ratios de 1 h (el histórico gratuito es corto)."""
        saved = self.period
        self.period = "1h"
        await self.poll_once(limit=500)
        self.period = saved
        await self.load_funding_history()

    async def run(self, every_s: float) -> None:
        await self.backfill()
        while True:
            await self.poll_once()
            await asyncio.sleep(every_s)
