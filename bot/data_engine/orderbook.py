"""Libro de órdenes local sincronizado con diff-depth de Binance USDⓈ-M.

Reglas de sincronización (doc oficial "How to manage a local order book"):
1. Bufferizar eventos del stream de depth.
2. Tomar snapshot REST /fapi/v1/depth -> lastUpdateId.
3. Descartar eventos con u < lastUpdateId.
4. El primer evento procesado debe cumplir U <= lastUpdateId <= u.
5. Cada evento siguiente debe cumplir pu == u del anterior; si no -> resync.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class OrderBookDesync(Exception):
    pass


@dataclass
class OrderBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int | None = None
    synced: bool = False
    _buffer: list[dict] = field(default_factory=list)
    event_ts_ms: int | None = None

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.synced = False
        self._buffer.clear()

    def on_event(self, ev: dict) -> None:
        """Procesa un evento depthUpdate (campos U, u, pu, b, a, E)."""
        if self.last_update_id is None:
            self._buffer.append(ev)
            if len(self._buffer) > 5000:
                self._buffer = self._buffer[-1000:]
            return
        self._apply_checked(ev)

    def apply_snapshot(self, snap: dict) -> None:
        self.bids = {float(p): float(q) for p, q in snap["bids"] if float(q) > 0}
        self.asks = {float(p): float(q) for p, q in snap["asks"] if float(q) > 0}
        self.last_update_id = int(snap["lastUpdateId"])
        self.synced = False
        buffered, self._buffer = self._buffer, []
        for ev in buffered:
            self._apply_checked(ev)

    def _apply_checked(self, ev: dict) -> None:
        assert self.last_update_id is not None
        first, final, prev = int(ev["U"]), int(ev["u"]), int(ev.get("pu", -1))
        if not self.synced:
            if final < self.last_update_id:
                return
            if not (first <= self.last_update_id <= final):
                self.reset()
                raise OrderBookDesync(f"{self.symbol}: primer evento no cubre lastUpdateId")
            self.synced = True
        elif prev != self.last_update_id:
            self.reset()
            raise OrderBookDesync(f"{self.symbol}: pu={prev} != último u={self.last_update_id}")
        self._apply_levels(self.bids, ev.get("b", []))
        self._apply_levels(self.asks, ev.get("a", []))
        self.last_update_id = final
        self.event_ts_ms = int(ev.get("E", 0)) or self.event_ts_ms

    @staticmethod
    def _apply_levels(side: dict[float, float], levels: list) -> None:
        for p, q in levels:
            price, qty = float(p), float(q)
            if qty == 0:
                side.pop(price, None)
            else:
                side[price] = qty

    # ------------------------------------------------------------ lecturas

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        return None if b is None or a is None else (a + b) / 2

    def spread_bps(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None or b <= 0:
            return None
        return (a - b) / ((a + b) / 2) * 1e4

    def top(self, n: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bids = sorted(self.bids.items(), key=lambda x: -x[0])[:n]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[:n]
        return bids, asks

    def imbalance(self, n: int = 10) -> float | None:
        """(bid_notional − ask_notional) / total en los n mejores niveles."""
        bids, asks = self.top(n)
        bn = sum(p * q for p, q in bids)
        an = sum(p * q for p, q in asks)
        return None if bn + an == 0 else (bn - an) / (bn + an)

    def depth_notional(self, n: int = 10) -> float:
        bids, asks = self.top(n)
        return min(sum(p * q for p, q in bids), sum(p * q for p, q in asks))

    def walk(self, side: str, qty: float) -> float | None:
        """Precio promedio de llenar `qty` a mercado. side='BUY' consume asks."""
        levels = sorted(self.asks.items()) if side == "BUY" else sorted(self.bids.items(), reverse=True)
        remaining, cost = qty, 0.0
        for price, avail in levels:
            take = min(remaining, avail)
            cost += take * price
            remaining -= take
            if remaining <= 1e-12:
                return cost / qty
        return None  # libro insuficiente
