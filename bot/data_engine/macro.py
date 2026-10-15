"""Calendario macro de EE. UU. y ventanas de bloqueo.

Fuentes: feed semanal JSON de ForexFactory (impacto High, país USD) + archivo
manual `bot/macro_events.yaml` (FOMC, etc.) como respaldo verificable a mano.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import AwareDatetime, BaseModel

from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient

log = get_logger("bot.macro")

HIGH_IMPACT_PATTERNS = (
    "cpi", "non-farm", "nonfarm", "employment", "unemployment rate", "fomc", "federal funds",
    "fed chair", "press conference", "pce", "gdp", "interest rate",
)

MANUAL_EVENTS_PATH = Path(__file__).resolve().parent.parent / "macro_events.yaml"


class MacroEvent(BaseModel):
    title: str
    ts: AwareDatetime
    source: str
    impact: str = "High"


def is_high_impact(title: str) -> bool:
    t = title.lower()
    return any(p in t for p in HIGH_IMPACT_PATTERNS)


def parse_forexfactory(rows: list[dict]) -> list[MacroEvent]:
    out = []
    for r in rows:
        if r.get("country") != "USD" or r.get("impact") != "High":
            continue
        try:
            ts = datetime.fromisoformat(r["date"]).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        out.append(MacroEvent(title=r.get("title", "?"), ts=ts, source="forexfactory"))
    return out


def load_manual_events(path: Path = MANUAL_EVENTS_PATH) -> list[MacroEvent]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for e in raw.get("events", []):
        ts = datetime.fromisoformat(str(e["ts"]))
        if ts.tzinfo is None:
            raise ValueError(f"evento manual sin zona horaria: {e}")
        out.append(MacroEvent(title=e["title"], ts=ts.astimezone(UTC), source="manual"))
    return out


def blocking_event(now: datetime, events: list[MacroEvent], before_min: int, after_min: int) -> MacroEvent | None:
    """Evento que bloquea entradas: [ts − before, ts + after] inclusivo en ambos bordes."""
    for ev in sorted(events, key=lambda e: e.ts):
        if ev.ts - timedelta(minutes=before_min) <= now <= ev.ts + timedelta(minutes=after_min):
            return ev
    return None


def upcoming(now: datetime, events: list[MacroEvent], hours: float = 24) -> list[MacroEvent]:
    end = now + timedelta(hours=hours)
    return sorted((e for e in events if now <= e.ts <= end), key=lambda e: e.ts)


class MacroCalendar:
    def __init__(self, ff_url: str, http: HttpClient):
        self.ff_url = ff_url
        self.http = http
        self.events: list[MacroEvent] = []
        self.updated_at: datetime | None = None
        self.ok = False

    async def refresh(self) -> None:
        events = load_manual_events()
        ok = True
        try:
            rows = await self.http.get_json(self.ff_url, limit_key="forexfactory")
            events += parse_forexfactory(rows)
        except Exception as exc:  # noqa: BLE001
            ok = False
            log.warning("forexfactory: fallo", extra={"err": repr(exc)})
        # deduplicar eventos a <5 min con el mismo título
        dedup: dict[tuple[str, int], MacroEvent] = {}
        for e in events:
            dedup[(e.title.lower()[:20], int(e.ts.timestamp()) // 300)] = e
        self.events = sorted(dedup.values(), key=lambda e: e.ts)
        self.updated_at = datetime.now(UTC)
        self.ok = ok

    async def run(self, every_s: float) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(every_s)
