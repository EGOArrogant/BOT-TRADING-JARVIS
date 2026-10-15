"""On-chain gratuito: oferta de stablecoins (DefiLlama) y actividad de Stellar (Horizon).

Flujos hacia exchanges (CryptoQuant/Glassnode) son de pago: quedan `unknown`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bot.core.clock import utc_now
from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.onchain")


def parse_stablecoin_chart(rows: list[dict]) -> list[tuple[datetime, float]]:
    """/stablecoincharts/all -> [{date, totalCirculatingUSD: {peggedUSD: x}}]."""
    out = []
    for r in rows:
        circ = r.get("totalCirculatingUSD") or r.get("totalCirculating") or {}
        val = circ.get("peggedUSD")
        if val is None:
            continue
        out.append((datetime.fromtimestamp(int(r["date"]), tz=UTC), float(val)))
    return sorted(out)


def parse_fee_stats(resp: dict) -> float | None:
    """Uso de capacidad del último ledger de Stellar (0..1)."""
    v = resp.get("ledger_capacity_usage")
    return None if v in (None, "") else float(v)


class OnchainPoller:
    def __init__(self, llama_base: str, horizon_base: str, store: FeedStore, http: HttpClient):
        self.llama = llama_base.rstrip("/")
        self.horizon = horizon_base.rstrip("/")
        self.store = store
        self.http = http

    async def poll_once(self) -> None:
        try:
            rows = await self.http.get_json(f"{self.llama}/stablecoincharts/all", limit_key="defillama")
            for ts, v in parse_stablecoin_chart(rows)[-60:]:
                self.store.set("onchain.stablecoin_supply", v, "defillama", ts)
        except Exception as exc:  # noqa: BLE001
            log.warning("defillama: fallo", extra={"err": repr(exc)})
        try:
            resp = await self.http.get_json(f"{self.horizon}/fee_stats", limit_key="horizon")
            usage = parse_fee_stats(resp)
            if usage is not None:
                self.store.set("onchain.xlm_ledger_usage", usage, "stellar_horizon", utc_now())
        except Exception as exc:  # noqa: BLE001
            log.warning("horizon: fallo", extra={"err": repr(exc)})
        # De pago, sin llave configurada.
        self.store.mark_unknown("onchain.exchange_netflow", "cryptoquant")

    async def run(self, every_s: float) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(every_s)
