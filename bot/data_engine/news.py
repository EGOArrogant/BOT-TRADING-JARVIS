"""Noticias: RSS (principal, gratis) + CryptoPanic (opcional, de pago desde 2026).

Filtro por reglas: palabras clave por moneda, fuentes confiables, antigüedad,
palabras de alto impacto y deduplicación por similitud. Pasan máx. 8 titulares.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from pydantic import AwareDatetime, BaseModel, Field

from bot.core.clock import utc_now
from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient

log = get_logger("bot.news")


class Headline(BaseModel):
    title: str
    source: str
    url: str = ""
    published: AwareDatetime
    symbols: list[str] = Field(default_factory=list)
    score: float = 0.0
    high_impact: bool = False
    # Rellenado por el clasificador barato (ai_analyst.news_scorer)
    ai_relevance: float | None = None
    ai_direction: str | None = None


def _source_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    parts = [p for p in host.split(".") if p not in ("www", "com", "co", "io", "org", "net")]
    return parts[0] if parts else host


def parse_rss(xml_text: str, feed_url: str) -> list[Headline]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    source = _source_from_url(feed_url)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not pub:
            continue
        try:
            ts = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        out.append(Headline(title=title, source=source, url=(item.findtext("link") or "").strip(),
                            published=ts.astimezone(UTC)))
    return out


def parse_cryptopanic(resp: dict) -> list[Headline]:
    out = []
    for r in resp.get("results", []):
        try:
            ts = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        src = (r.get("source") or {}).get("domain") or "cryptopanic"
        out.append(Headline(title=r.get("title", ""), source=_source_from_url("https://" + src),
                            url=r.get("url", ""), published=ts))
    return out


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def score_headlines(
    items: list[Headline],
    now: datetime,
    symbol_keywords: dict[str, list[str]],
    trusted: list[str],
    high_impact: list[str],
    max_age_min: int,
    similarity: float,
    top_n: int = 8,
) -> list[Headline]:
    fresh = [h for h in items if timedelta(0) <= now - h.published <= timedelta(minutes=max_age_min)]
    scored: list[Headline] = []
    for h in fresh:
        words = _words(h.title)
        syms = [s for s, kws in symbol_keywords.items() if words & {k.lower() for k in kws}]
        hi = bool(words & {k.lower() for k in high_impact})
        if not syms and not hi:
            continue
        age_min = (now - h.published).total_seconds() / 60
        score = (2.0 if hi else 0.0) + (1.0 if h.source in trusted else 0.0) + 0.5 * len(syms)
        score *= max(0.2, 1 - age_min / max_age_min)
        scored.append(h.model_copy(update={"symbols": syms, "high_impact": hi, "score": round(score, 4)}))
    scored.sort(key=lambda h: -h.score)
    kept: list[Headline] = []
    for h in scored:
        if any(SequenceMatcher(None, h.title.lower(), k.title.lower()).ratio() >= similarity for k in kept):
            continue
        kept.append(h)
    return kept[:top_n]


class NewsFeed:
    def __init__(self, rss_feeds: list[str], http: HttpClient, cryptopanic_url: str | None = None,
                 cryptopanic_key: str | None = None):
        self.rss_feeds = rss_feeds
        self.http = http
        self.cp_url = cryptopanic_url
        self.cp_key = cryptopanic_key
        self.items: list[Headline] = []
        self.updated_at: datetime | None = None
        self.sources_ok = 0

    async def refresh(self) -> None:
        collected: list[Headline] = []
        ok = 0
        for url in self.rss_feeds:
            try:
                collected += parse_rss(await self.http.get_text(url, limit_key="rss"), url)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("rss: fallo", extra={"url": url, "err": repr(exc)})
        if self.cp_url and self.cp_key:
            try:
                resp = await self.http.get_json(self.cp_url, limit_key="cryptopanic",
                                                params={"auth_token": self.cp_key, "public": "true"})
                collected += parse_cryptopanic(resp)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("cryptopanic: fallo", extra={"err": type(exc).__name__})
        cutoff = utc_now() - timedelta(hours=6)
        self.items = [h for h in collected if h.published >= cutoff]
        self.sources_ok = ok
        self.updated_at = utc_now()

    async def run(self, every_s: float) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(every_s)
