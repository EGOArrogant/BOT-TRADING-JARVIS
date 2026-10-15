"""Descarga y caché de velas históricas (Binance REST /fapi/v1/klines, paginado)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bot.core.clock import dt_to_ms
from bot.data_engine.http import HttpClient
from bot.data_engine.market_state import INTERVAL_MS, Candle

FIELDS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume")


def save_candles(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {f: [getattr(c, f) for c in candles] for f in FIELDS}
    pq.write_table(pa.table(cols), path, compression="zstd")


def load_candles(path: Path) -> list[Candle]:
    t = pq.read_table(path).to_pydict()
    return [Candle(*(t[f][i] for f in FIELDS)) for i in range(len(t["open_time"]))]


async def download_klines(http: HttpClient, rest: str, symbol: str, interval: str, start: datetime,
                          end: datetime) -> list[Candle]:
    out: list[Candle] = []
    cursor = dt_to_ms(start)
    end_ms = dt_to_ms(end)
    step = INTERVAL_MS[interval]
    while cursor < end_ms:
        rows = await http.get_json(f"{rest.rstrip('/')}/fapi/v1/klines", limit_key="binance", weight=5,
                                   params={"symbol": symbol, "interval": interval, "startTime": cursor,
                                           "endTime": end_ms, "limit": 1500})
        if not rows:
            break
        out.extend(Candle.from_rest(r) for r in rows if int(r[6]) <= end_ms)
        cursor = int(rows[-1][0]) + step
    return out


async def cached_klines(http: HttpClient, rest: str, cache_dir: Path, symbol: str, interval: str,
                        start: datetime, end: datetime) -> list[Candle]:
    path = cache_dir / f"{symbol}_{interval}_{start:%Y%m%d}_{end:%Y%m%d}.parquet"
    if path.exists():
        return load_candles(path)
    candles = await download_klines(http, rest, symbol, interval, start, end)
    save_candles(path, candles)
    return candles
