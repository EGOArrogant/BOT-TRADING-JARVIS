"""Hyperliquid: funding y OI por activo vía POST /info {"type":"metaAndAssetCtxs"}.

Hyperliquid cobra funding cada hora: su tasa ya es horaria. Binance cobra cada
`funding_interval_hours` (8 h por defecto): se divide para normalizar a 1 h.
"""

from __future__ import annotations

import asyncio

from bot.core.clock import utc_now
from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.dex")

# Símbolo Binance -> coin en Hyperliquid
HL_COINS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "XRPUSDT": "XRP",
            "DOGEUSDT": "DOGE", "XLMUSDT": "XLM"}


def parse_meta_and_ctxs(resp: list) -> dict[str, dict[str, float]]:
    meta, ctxs = resp[0], resp[1]
    out: dict[str, dict[str, float]] = {}
    for asset, ctx in zip(meta["universe"], ctxs):
        try:
            out[asset["name"]] = {
                "funding_1h": float(ctx["funding"]),
                "open_interest": float(ctx["openInterest"]),
                "mark": float(ctx["markPx"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def hourly_funding(rate: float, interval_hours: float) -> float:
    return rate / interval_hours


class HyperliquidPoller:
    def __init__(self, url: str, symbols: list[str], store: FeedStore, http: HttpClient):
        self.url = url
        self.symbols = symbols
        self.store = store
        self.http = http

    async def poll_once(self) -> None:
        now = utc_now()
        try:
            resp = await self.http.post_json(self.url, limit_key="hyperliquid", json={"type": "metaAndAssetCtxs"})
            parsed = parse_meta_and_ctxs(resp)
        except Exception as exc:  # noqa: BLE001
            log.warning("hyperliquid: fallo", extra={"err": repr(exc)})
            return
        for sym in self.symbols:
            coin = HL_COINS.get(sym)
            row = parsed.get(coin or "")
            if row is None:
                self.store.mark_unknown(f"dex.funding_1h.{sym}", "hyperliquid")
                continue
            self.store.set(f"dex.funding_1h.{sym}", row["funding_1h"], "hyperliquid", now)
            self.store.set(f"dex.oi.{sym}", row["open_interest"], "hyperliquid", now)
            self.store.set(f"dex.mark.{sym}", row["mark"], "hyperliquid", now)

    async def run(self, every_s: float) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(every_s)
