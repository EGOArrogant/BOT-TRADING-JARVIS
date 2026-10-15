"""Estado de mercado en memoria por símbolo (velas, último precio, mark, funding, CVD, libro)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from bot.data_engine.orderbook import OrderBook

INTERVALS = ("1m", "5m", "15m", "1h")
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int          # ms
    open: float
    high: float
    low: float
    close: float
    volume: float           # base
    quote_volume: float
    taker_buy_volume: float  # base

    @property
    def delta(self) -> float:
        """Volumen comprador agresor − vendedor agresor (para CVD)."""
        return 2 * self.taker_buy_volume - self.volume

    @classmethod
    def from_rest(cls, row: list) -> Candle:
        # [openTime, o, h, l, c, v, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]
        return cls(int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                   float(row[5]), float(row[7]), float(row[9]))

    @classmethod
    def from_ws(cls, k: dict) -> Candle:
        return cls(int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                   float(k["v"]), float(k["q"]), float(k["V"]))


class CandleSeries:
    def __init__(self, maxlen: int = 1500):
        self._closed: deque[Candle] = deque(maxlen=maxlen)
        self.partial: Candle | None = None

    def __len__(self) -> int:
        return len(self._closed)

    def add_closed(self, c: Candle) -> bool:
        """Agrega vela cerrada; devuelve True si es nueva. Ignora duplicados/antiguas."""
        if self._closed and c.open_time <= self._closed[-1].open_time:
            if c.open_time == self._closed[-1].open_time:
                self._closed[-1] = c
            return False
        self._closed.append(c)
        if self.partial and self.partial.open_time <= c.open_time:
            self.partial = None
        return True

    def closed(self) -> list[Candle]:
        return list(self._closed)

    def last(self) -> Candle | None:
        return self._closed[-1] if self._closed else None

    def arrays(self) -> dict[str, np.ndarray]:
        cs = self._closed
        return {
            "t": np.fromiter((c.open_time for c in cs), dtype=np.int64, count=len(cs)),
            "o": np.fromiter((c.open for c in cs), dtype=float, count=len(cs)),
            "h": np.fromiter((c.high for c in cs), dtype=float, count=len(cs)),
            "l": np.fromiter((c.low for c in cs), dtype=float, count=len(cs)),
            "c": np.fromiter((c.close for c in cs), dtype=float, count=len(cs)),
            "v": np.fromiter((c.volume for c in cs), dtype=float, count=len(cs)),
            "qv": np.fromiter((c.quote_volume for c in cs), dtype=float, count=len(cs)),
            "delta": np.fromiter((c.delta for c in cs), dtype=float, count=len(cs)),
        }

    def gaps(self, interval_ms: int) -> int:
        """Velas faltantes dentro de la serie (para la puerta de fase de `record`)."""
        cs = self._closed
        return sum(max(0, (b.open_time - a.open_time) // interval_ms - 1) for a, b in zip(cs, list(cs)[1:]))


@dataclass
class MarketState:
    symbol: str
    candles: dict[str, CandleSeries] = field(default_factory=lambda: {i: CandleSeries() for i in INTERVALS})
    book: OrderBook = field(init=False)
    last_price: float | None = None
    last_trade_ms: int | None = None
    mark_price: float | None = None
    index_price: float | None = None
    funding_rate: float | None = None
    next_funding_ms: int | None = None
    mark_ms: int | None = None
    funding_interval_hours: float = 8.0
    cvd_live: float = 0.0  # acumulado desde aggTrade (sesión)
    _recent_trades: deque = field(default_factory=lambda: deque(maxlen=5000))

    def __post_init__(self) -> None:
        self.book = OrderBook(self.symbol)

    # ------------------------------------------------------------ handlers WS

    def on_agg_trade(self, d: dict) -> None:
        price, qty = float(d["p"]), float(d["q"])
        self.last_price = price
        self.last_trade_ms = int(d["T"])
        signed = -qty if d["m"] else qty  # m=True: comprador es maker -> agresor vendedor
        self.cvd_live += signed
        self._recent_trades.append((self.last_trade_ms, price, signed))

    def on_kline(self, d: dict) -> Candle | None:
        """Devuelve la vela si cerró (para disparar evaluación)."""
        k = d["k"]
        c = Candle.from_ws(k)
        series = self.candles[k["i"]]
        if k["x"]:
            return c if series.add_closed(c) else None
        series.partial = c
        return None

    def on_mark_price(self, d: dict) -> None:
        self.mark_price = float(d["p"])
        self.index_price = float(d["i"]) if d.get("i") not in (None, "") else None
        self.funding_rate = float(d["r"]) if d.get("r") not in (None, "") else None
        self.next_funding_ms = int(d["T"]) if d.get("T") else None
        self.mark_ms = int(d["E"])

    def recent_trades(self, since_ms: int) -> list[tuple[int, float, float]]:
        return [t for t in self._recent_trades if t[0] >= since_ms]
