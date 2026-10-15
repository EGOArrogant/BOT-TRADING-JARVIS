"""Fear & Greed (alternative.me). Se actualiza una vez al día."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient
from bot.data_engine.store import FeedStore

log = get_logger("bot.sentiment")


def parse_fng(resp: dict) -> list[tuple[datetime, float]]:
    return sorted(
        (datetime.fromtimestamp(int(r["timestamp"]), tz=UTC), float(r["value"])) for r in resp.get("data", [])
    )


class FearGreedPoller:
    def __init__(self, url: str, store: FeedStore, http: HttpClient):
        self.url = url
        self.store = store
        self.http = http

    async def poll_once(self, limit: int = 30) -> None:
        try:
            resp = await self.http.get_json(self.url, limit_key="alternative_me", params={"limit": limit})
            for ts, v in parse_fng(resp):
                self.store.set("sent.fng", v, "alternative.me", ts)
        except Exception as exc:  # noqa: BLE001
            log.warning("fear&greed: fallo", extra={"err": repr(exc)})

    async def run(self, every_s: float) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(every_s)
