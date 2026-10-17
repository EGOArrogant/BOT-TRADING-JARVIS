"""WebSocket de mercado Binance USDⓈ-M con reconexión y resincronización.

Rutas (verificadas 2026-10-15, ver config.yaml): desde 2026 los streams están
separados en /public (depth, bookTicker) y /market (aggTrade, kline, markPrice).
Formato combinado: <base>/stream?streams=a/b/c. Una conexión dura máx. 24 h.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from bot.core.logging_setup import get_logger
from bot.data_engine.http import HttpClient, backoff_delay
from bot.data_engine.market_state import INTERVALS, Candle, MarketState
from bot.data_engine.orderbook import OrderBookDesync

log = get_logger("bot.market_ws")

MAX_CONN_SECONDS = 23 * 3600  # reconectar antes del corte de 24 h

KlineCallback = Callable[[str, str, Candle], Awaitable[None] | None]
RawCallback = Callable[[str, dict], None]


def public_streams(symbols: list[str]) -> list[str]:
    return [f"{s.lower()}@depth@100ms" for s in symbols]


def market_streams(symbols: list[str]) -> list[str]:
    out: list[str] = []
    for s in symbols:
        low = s.lower()
        out.append(f"{low}@aggTrade")
        out.append(f"{low}@markPrice@1s")
        out.extend(f"{low}@kline_{i}" for i in INTERVALS)
    return out


def combined_url(base: str, streams: list[str]) -> str:
    return f"{base.rstrip('/')}/stream?streams={'/'.join(streams)}"


class MarketDispatcher:
    """Enruta mensajes a MarketState. Separado del socket para poder testearlo."""

    def __init__(self, states: dict[str, MarketState], on_kline_closed: KlineCallback | None = None,
                 on_raw: RawCallback | None = None):
        self.states = states
        self.on_kline_closed = on_kline_closed
        self.on_raw = on_raw
        self.desynced: set[str] = set()
        self.last_msg_monotonic: float = time.monotonic()
        self.msg_count = 0

    async def handle(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        data: dict[str, Any] = msg.get("data", msg)
        stream = msg.get("stream", "")
        self.last_msg_monotonic = time.monotonic()
        self.msg_count += 1
        sym = data.get("s")
        state = self.states.get(sym) if sym else None
        if state is None:
            return
        if self.on_raw:
            self.on_raw(stream or data.get("e", "?"), data)
        etype = data.get("e")
        if etype == "aggTrade":
            state.on_agg_trade(data)
        elif etype == "kline":
            closed = state.on_kline(data)
            if closed is not None and self.on_kline_closed:
                res = self.on_kline_closed(sym, data["k"]["i"], closed)
                if asyncio.iscoroutine(res):
                    await res
        elif etype == "markPriceUpdate":
            state.on_mark_price(data)
        elif etype == "depthUpdate":
            try:
                state.book.on_event(data)
            except OrderBookDesync as exc:
                log.warning("libro desincronizado", extra={"symbol": sym, "err": str(exc)})
                self.desynced.add(sym)


class BinanceMarketData:
    """Mantiene las conexiones /public y /market vivas y los libros sincronizados."""

    def __init__(self, rest_base: str, ws_public: str, ws_market: str, states: dict[str, MarketState],
                 http: HttpClient, dispatcher: MarketDispatcher):
        self.rest = rest_base.rstrip("/")
        self.ws_public = ws_public
        self.ws_market = ws_market
        self.states = states
        self.http = http
        self.dispatcher = dispatcher
        self.connected = {"public": False, "market": False}
        self.reconnects = 0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ REST

    async def verify_symbols(self, allow_missing: bool = False) -> dict[str, dict]:
        """Perpetuos activos (status TRADING). Con allow_missing, un símbolo en mantenimiento o retirado
        no detiene el bot: se devuelve sin él y el motor bloquea sus entradas. Si no queda ninguno, error."""
        info = await self.http.get_json(f"{self.rest}/fapi/v1/exchangeInfo", limit_key="binance", weight=1)
        found = {s["symbol"]: s for s in info["symbols"]
                 if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"}
        missing = [s for s in self.states if s not in found]
        if missing and (not allow_missing or len(missing) == len(self.states)):
            raise RuntimeError(f"símbolos no disponibles como perpetuos activos: {missing}")
        if missing:
            log.warning("símbolos no disponibles (mantenimiento o retiro): sin entradas nuevas",
                        extra={"symbols": missing})
        return {s: found[s] for s in self.states if s in found}

    async def server_offset(self, samples: int = 3) -> float:
        """Desfase (s) del reloj local frente a Binance, a mitad del viaje de ida y vuelta.

        Toma varias lecturas y usa la de viaje más corto (la menos afectada por la red)."""
        import time
        best: tuple[float, float] | None = None
        for _ in range(max(1, samples)):
            t0 = time.time()
            data = await self.http.get_json(f"{self.rest}/fapi/v1/time", limit_key="binance", weight=1)
            t1 = time.time()
            off = int(data["serverTime"]) / 1000 - (t0 + t1) / 2
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, off)
        return best[1]

    async def load_funding_intervals(self) -> None:
        """/fapi/v1/fundingInfo solo lista símbolos con intervalo ajustado; el resto es 8 h."""
        try:
            rows = await self.http.get_json(f"{self.rest}/fapi/v1/fundingInfo", limit_key="binance")
        except Exception as exc:  # noqa: BLE001
            log.warning("fundingInfo no disponible", extra={"err": str(exc)})
            return
        for r in rows:
            st = self.states.get(r.get("symbol"))
            if st and r.get("fundingIntervalHours"):
                st.funding_interval_hours = float(r["fundingIntervalHours"])

    async def bootstrap_klines(self, limit: int = 1000) -> None:
        for sym, st in self.states.items():
            for interval in INTERVALS:
                rows = await self.http.get_json(
                    f"{self.rest}/fapi/v1/klines", limit_key="binance", weight=5,
                    params={"symbol": sym, "interval": interval, "limit": limit},
                )
                now_ms = int(time.time() * 1000)
                for row in rows:
                    if int(row[6]) < now_ms:  # solo velas cerradas
                        st.candles[interval].add_closed(Candle.from_rest(row))

    async def _backfill_recent(self) -> None:
        """Tras reconectar, rellena velas perdidas durante la caída."""
        try:
            await self.bootstrap_klines(limit=60)
        except Exception as exc:  # noqa: BLE001
            log.error("backfill de velas falló", extra={"err": repr(exc)})

    async def resync_book(self, symbol: str) -> None:
        st = self.states[symbol]
        st.book.reset()
        await asyncio.sleep(0.5)  # dejar que lleguen eventos al buffer
        snap = await self.http.get_json(f"{self.rest}/fapi/v1/depth", limit_key="binance", weight=20,
                                        params={"symbol": symbol, "limit": 1000})
        try:
            st.book.apply_snapshot(snap)
        except OrderBookDesync:
            self.dispatcher.desynced.add(symbol)
            return
        self.dispatcher.desynced.discard(symbol)

    # ------------------------------------------------------------ WS

    async def _run_socket(self, name: str, url: str, after_connect: Callable[[], Awaitable[None]] | None) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=None, max_queue=10_000,
                                              open_timeout=15, close_timeout=5) as ws:
                    self.connected[name] = True
                    attempt = 0
                    log.info("ws conectado", extra={"socket": name})
                    if after_connect:
                        asyncio.create_task(after_connect())
                    started = time.monotonic()
                    async for raw in ws:
                        await self.dispatcher.handle(raw)
                        if time.monotonic() - started > MAX_CONN_SECONDS:
                            log.info("rotación preventiva de conexión", extra={"socket": name})
                            break
                        if self._stop.is_set():
                            break
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
                log.warning("ws caído", extra={"socket": name, "err": repr(exc)})
            finally:
                self.connected[name] = False
            if self._stop.is_set():
                return
            self.reconnects += 1
            delay = backoff_delay(attempt, base=1.0, cap=60.0)
            attempt += 1
            await asyncio.sleep(delay)

    async def _resync_all_books(self) -> None:
        for sym in self.states:
            try:
                await self.resync_book(sym)
            except Exception as exc:  # noqa: BLE001
                log.error("resync de libro falló", extra={"symbol": sym, "err": repr(exc)})
                self.dispatcher.desynced.add(sym)

    async def _watch_desync(self) -> None:
        while not self._stop.is_set():
            for sym in list(self.dispatcher.desynced):
                try:
                    await self.resync_book(sym)
                except Exception as exc:  # noqa: BLE001
                    log.error("resync falló", extra={"symbol": sym, "err": repr(exc)})
            await asyncio.sleep(2)

    async def run(self) -> None:
        syms = list(self.states)
        await asyncio.gather(
            self._run_socket("public", combined_url(self.ws_public, public_streams(syms)), self._resync_all_books),
            self._run_socket("market", combined_url(self.ws_market, market_streams(syms)), self._backfill_recent),
            self._watch_desync(),
        )

    def stop(self) -> None:
        self._stop.set()
