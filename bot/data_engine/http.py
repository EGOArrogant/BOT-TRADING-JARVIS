"""HTTP compartido: reintentos con backoff y límite de tasa por endpoint."""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any

import httpx

from bot.core.logging_setup import get_logger

log = get_logger("bot.http")


class RateLimiter:
    """Ventana deslizante de peso por clave (endpoint o host)."""

    def __init__(self, limits: dict[str, tuple[int, float]] | None = None, default: tuple[int, float] = (60, 60.0)):
        self._limits = limits or {}
        self._default = default
        self._events: dict[str, deque[tuple[float, int]]] = {}
        self._lock = asyncio.Lock()

    def _limit(self, key: str) -> tuple[int, float]:
        return self._limits.get(key, self._default)

    def used(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        _, window = self._limit(key)
        q = self._events.setdefault(key, deque())
        while q and now - q[0][0] > window:
            q.popleft()
        return sum(w for _, w in q)

    def try_acquire(self, key: str, weight: int = 1, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cap, _ = self._limit(key)
        if self.used(key, now) + weight > cap:
            return False
        self._events[key].append((now, weight))
        return True

    async def acquire(self, key: str, weight: int = 1) -> None:
        while True:
            async with self._lock:
                if self.try_acquire(key, weight):
                    return
            await asyncio.sleep(0.25)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Backoff exponencial con jitter completo."""
    return random.uniform(0, min(cap, base * (2**attempt)))


class HttpClient:
    def __init__(self, limiter: RateLimiter | None = None, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.limiter = limiter or RateLimiter()
        self._client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "crypto-perp-bot/0.1"})

    async def close(self) -> None:
        await self._client.aclose()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        limit_key: str,
        weight: int = 1,
        retries: int = 3,
        **kwargs: Any,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            await self.limiter.acquire(limit_key, weight)
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in (418, 429):
                    retry_after = float(resp.headers.get("Retry-After", "5"))
                    log.warning("rate limit del servidor", extra={"url": url, "status": resp.status_code})
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                await asyncio.sleep(backoff_delay(attempt))
        raise RuntimeError(f"fallo tras {retries + 1} intentos: {url}") from last_exc

    async def get_json(self, url: str, *, limit_key: str, **kw: Any) -> Any:
        return await self.request_json("GET", url, limit_key=limit_key, **kw)

    async def post_json(self, url: str, *, limit_key: str, **kw: Any) -> Any:
        return await self.request_json("POST", url, limit_key=limit_key, **kw)

    async def get_text(self, url: str, *, limit_key: str) -> str:
        await self.limiter.acquire(limit_key)
        resp = await self._client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.text
