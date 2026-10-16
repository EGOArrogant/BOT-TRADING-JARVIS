"""DVOL de Deribit (solo BTC y ETH) + volatilidad realizada local."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

import numpy as np

from bot.core.clock import ms_to_dt, dt_to_ms, utc_now
from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.volatility")

DVOL_CURRENCIES = ("BTC", "ETH")
MINUTES_PER_YEAR = 365 * 24 * 60


def parse_dvol(resp: dict, now: datetime | None = None, resolution_s: int = 3600) -> list[tuple[datetime, float]]:
    """result.data = [[ts_ms_apertura, open, high, low, close], ...]; se usa el cierre.

    El valor de cierre corresponde al FINAL de la vela (apertura + resolución); la vela en curso vale
    "a la hora de la consulta". Fecharlo con la apertura lo hacía parecer una hora más viejo (vencido)
    y, en el backtest, adelantaba hasta una hora un dato futuro."""
    rows = resp.get("result", {}).get("data", [])
    now = now or utc_now()
    step = timedelta(seconds=resolution_s)
    return sorted((min(ms_to_dt(int(r[0])) + step, now), float(r[4])) for r in rows)


def realized_vol(closes: np.ndarray, bar_minutes: float) -> float | None:
    """Volatilidad realizada anualizada (desv. estándar de log-retornos)."""
    if len(closes) < 3 or np.any(closes <= 0):
        return None
    rets = np.diff(np.log(closes))
    return float(np.std(rets, ddof=1) * math.sqrt(MINUTES_PER_YEAR / bar_minutes))


class DvolPoller:
    def __init__(self, base: str, store: FeedStore, http: HttpClient):
        self.base = base.rstrip("/")
        self.store = store
        self.http = http

    async def poll_once(self, lookback: timedelta = timedelta(days=2)) -> None:
        end = utc_now()
        for cur in DVOL_CURRENCIES:
            try:
                resp = await self.http.get_json(
                    f"{self.base}/public/get_volatility_index_data", limit_key="deribit",
                    params={"currency": cur, "start_timestamp": dt_to_ms(end - lookback),
                            "end_timestamp": dt_to_ms(end), "resolution": "3600"},
                )
                for ts, v in parse_dvol(resp, end):
                    self.store.set(f"vol.dvol.{cur}", v, "deribit", ts)
            except Exception as exc:  # noqa: BLE001
                log.warning("DVOL: fallo", extra={"currency": cur, "err": repr(exc)})

    async def run(self, every_s: float) -> None:
        await self.poll_once(lookback=timedelta(days=30))
        while True:
            await asyncio.sleep(every_s)
            await self.poll_once()
