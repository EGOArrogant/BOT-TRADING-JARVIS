"""Exchange simulado para modo `paper`, sobre datos de mercado reales.

- Límite post-only: se rechaza si cruzaría el libro; se llena solo cuando el
  precio negociado ATRAVIESA el límite (supuesto conservador de cola).
- Mercado: recorre el libro real (deslizamiento real) + latencia simulada.
- Stops: se disparan con el mark price y se llenan como taker recorriendo el libro.
- Funding: se cobra/paga en cada `next_funding_ms` sobre el nocional a mark.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from bot.core.clock import ms_to_dt, utc_now
from bot.core.config import Fees
from bot.core.logging_setup import get_logger
from bot.core.models import Order, OrderSide, OrderStatus, OrderType, Side
from bot.data_engine.market_state import MarketState
from bot.execution.base import DuplicateClientId, ExchangeError, ExchangePosition, Fill, IncomeRecord
from bot.risk.costs import fee_rate
from bot.risk.engine import SymbolRules

log = get_logger("bot.paper")

D = Decimal


def _d(x: float) -> Decimal:
    return Decimal(str(x))


@dataclass
class _Pos:
    qty: Decimal = D(0)          # con signo: + long, − short
    entry: Decimal = D(0)


@dataclass
class PaperExchange:
    states: dict[str, MarketState]
    fees: Fees
    initial_balance: float
    rules: dict[str, SymbolRules] = field(default_factory=dict)
    latency_ms: int = 150
    name: str = "paper"

    def __post_init__(self) -> None:
        self.wallet = _d(self.initial_balance)
        self.orders: dict[str, Order] = {}
        self.pos: dict[str, _Pos] = {s: _Pos() for s in self.states}
        self._fills: list[Fill] = []
        self._income: list[IncomeRecord] = []
        self._last_trade_check: dict[str, int] = {s: 0 for s in self.states}
        self._last_funding_ms: dict[str, int] = {s: 0 for s in self.states}
        self.leverage: dict[str, int] = {}
        self._pending_rate: dict[str, float] = {}

    # ------------------------------------------------------------ info

    async def server_time(self) -> datetime:
        return utc_now()

    async def symbol_rules(self, symbol: str) -> SymbolRules:
        return self.rules.get(symbol) or SymbolRules(tick_size=0.0001, step_size=0.001, min_qty=0.001,
                                                     min_notional=5.0)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage[symbol] = leverage

    async def balance(self) -> Decimal:
        return self.wallet

    def equity(self) -> Decimal:
        eq = self.wallet
        for sym, p in self.pos.items():
            mark = self.states[sym].mark_price or self.states[sym].last_price
            if p.qty != 0 and mark:
                eq += p.qty * (_d(mark) - p.entry)
        return eq

    # ------------------------------------------------------------ órdenes

    def _new(self, **kw) -> Order:
        cid = kw["client_order_id"]
        if cid in self.orders:
            raise DuplicateClientId(cid)
        o = Order(created_at=utc_now(), status=OrderStatus.NEW, **kw)
        self.orders[cid] = o
        return o

    async def place_limit(self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True, reduce_only: bool = False) -> Order:
        await asyncio.sleep(self.latency_ms / 1000)
        book = self.states[symbol].book
        if post_only:
            ask, bid = book.best_ask(), book.best_bid()
            if ask is None or bid is None:
                raise ExchangeError("libro no disponible")
            if (side is OrderSide.BUY and float(price) >= ask) or (side is OrderSide.SELL and float(price) <= bid):
                o = self._new(client_order_id=client_id, symbol=symbol, side=side, type=OrderType.LIMIT, qty=qty,
                              price=price, post_only=True, reduce_only=reduce_only)
                o.status = OrderStatus.EXPIRED  # GTX: cruzaría -> expira
                return o.model_copy()
        o = self._new(client_order_id=client_id, symbol=symbol, side=side, type=OrderType.LIMIT, qty=qty,
                      price=price, post_only=post_only, reduce_only=reduce_only)
        return o.model_copy()

    async def place_market(self, symbol: str, side: OrderSide, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> Order:
        await asyncio.sleep(self.latency_ms / 1000)
        o = self._new(client_order_id=client_id, symbol=symbol, side=side, type=OrderType.MARKET, qty=qty,
                      reduce_only=reduce_only)
        if reduce_only:
            qty = min(qty, self._reducible(symbol, side))
            if qty <= 0:
                o.status = OrderStatus.EXPIRED
                return o.model_copy()
        px = self.states[symbol].book.walk(side.value, float(qty))
        if px is None:
            o.status = OrderStatus.REJECTED
            raise ExchangeError("libro insuficiente para orden a mercado")
        self._fill(o, qty, _d(px), maker=False)
        return o.model_copy()

    async def place_stop(self, symbol: str, side: OrderSide, stop_price: Decimal, client_id: str) -> Order:
        await asyncio.sleep(self.latency_ms / 1000)
        mark = self.states[symbol].mark_price
        if mark is not None:
            # Binance rechaza un stop que se dispararía inmediatamente
            if (side is OrderSide.SELL and float(stop_price) >= mark) or (side is OrderSide.BUY and float(stop_price) <= mark):
                raise ExchangeError("stop se dispararía de inmediato")
        o = self._new(client_order_id=client_id, symbol=symbol, side=side, type=OrderType.STOP_MARKET, qty=D(0),
                      stop_price=stop_price, close_position=True, is_algo=True)
        return o.model_copy()

    async def cancel(self, symbol: str, client_id: str) -> None:
        o = self.orders.get(client_id)
        if o is None or not o.is_open:
            raise ExchangeError(f"orden {client_id} no abierta")
        o.status = OrderStatus.CANCELED
        o.updated_at = utc_now()

    async def get_order(self, symbol: str, client_id: str) -> Order | None:
        o = self.orders.get(client_id)
        return o.model_copy() if o else None

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o.model_copy() for o in self.orders.values() if o.is_open and (symbol is None or o.symbol == symbol)]

    async def positions(self) -> list[ExchangePosition]:
        out = []
        for sym, p in self.pos.items():
            if p.qty != 0:
                mark = self.states[sym].mark_price
                out.append(ExchangePosition(sym, Side.LONG if p.qty > 0 else Side.SHORT, abs(p.qty), p.entry,
                                            _d(mark) if mark else None))
        return out

    async def fills(self, symbol: str, since: datetime) -> list[Fill]:
        return [f for f in self._fills if f.symbol == symbol and f.ts >= since]

    async def income(self, since: datetime) -> list[IncomeRecord]:
        return [i for i in self._income if i.ts >= since]

    # ------------------------------------------------------------ simulación

    def _reducible(self, symbol: str, side: OrderSide) -> Decimal:
        q = self.pos[symbol].qty
        if side is OrderSide.SELL and q > 0:
            return q
        if side is OrderSide.BUY and q < 0:
            return -q
        return D(0)

    def _fill(self, o: Order, qty: Decimal, price: Decimal, maker: bool) -> None:
        now = utc_now()
        fee = qty * price * _d(fee_rate(self.fees, maker))
        signed = qty if o.side is OrderSide.BUY else -qty
        p = self.pos[o.symbol]
        realized = D(0)
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            new_qty = p.qty + signed
            p.entry = (p.entry * abs(p.qty) + price * qty) / abs(new_qty)
            p.qty = new_qty
        else:
            closing = min(abs(signed), abs(p.qty))
            realized = closing * (price - p.entry) * (1 if p.qty > 0 else -1)
            remaining = abs(signed) - closing
            p.qty += signed
            if p.qty == 0:
                p.entry = D(0)
            elif remaining > 0:  # dio vuelta
                p.entry = price
        self.wallet += realized - fee
        prev_val = (o.avg_fill_price or D(0)) * o.filled_qty
        o.filled_qty += qty
        o.avg_fill_price = (prev_val + price * qty) / o.filled_qty
        o.fee += fee
        o.maker = maker
        o.status = OrderStatus.FILLED if (o.qty == 0 or o.filled_qty >= o.qty) else OrderStatus.PARTIALLY_FILLED
        o.updated_at = now
        self._fills.append(Fill(o.symbol, o.client_order_id, o.side, qty, price, fee, maker, realized, now))
        self._income.append(IncomeRecord(o.symbol, "COMMISSION", -fee, now))
        if realized:
            self._income.append(IncomeRecord(o.symbol, "REALIZED_PNL", realized, now))
        if p.qty == 0:
            # sin posición: los stops close-position y reduce-only restantes ya no aplican
            for other in self.orders.values():
                if other.symbol == o.symbol and other.is_open and (other.close_position or other.reduce_only):
                    other.status = OrderStatus.EXPIRED
                    other.updated_at = now

    def on_market(self, symbol: str) -> None:
        """Llamar tras cada actualización de mercado del símbolo."""
        st = self.states[symbol]
        since = self._last_trade_check[symbol]
        trades = st.recent_trades(since + 1)
        if trades:
            self._last_trade_check[symbol] = trades[-1][0]
        for o in list(self.orders.values()):
            if o.symbol != symbol or not o.is_open:
                continue
            if o.type is OrderType.LIMIT and o.price is not None:
                lim = float(o.price)
                crossed = any(px < lim for _, px, _ in trades) if o.side is OrderSide.BUY \
                    else any(px > lim for _, px, _ in trades)
                if crossed:
                    qty = o.qty - o.filled_qty
                    if o.reduce_only:
                        qty = min(qty, self._reducible(symbol, o.side))
                        if qty <= 0:
                            o.status = OrderStatus.EXPIRED
                            continue
                    self._fill(o, qty, o.price, maker=True)
            elif o.type is OrderType.STOP_MARKET and o.stop_price is not None and st.mark_price is not None:
                trig = float(o.stop_price)
                hit = st.mark_price <= trig if o.side is OrderSide.SELL else st.mark_price >= trig
                if hit:
                    qty = self._reducible(symbol, o.side)
                    if qty <= 0:
                        o.status = OrderStatus.EXPIRED
                        continue
                    px = st.book.walk(o.side.value, float(qty)) or st.mark_price
                    self._fill(o, qty, _d(px), maker=False)
        self._apply_funding(symbol)

    def _apply_funding(self, symbol: str) -> None:
        st = self.states[symbol]
        p = self.pos[symbol]
        if st.next_funding_ms is None or st.funding_rate is None or st.mark_ms is None:
            return
        last = self._last_funding_ms[symbol]
        # El stream ya apunta al próximo cobro: si cambió, el anterior se cobró
        if last and st.next_funding_ms > last and p.qty != 0 and st.mark_price:
            amount = -p.qty * _d(st.mark_price) * _d(self._pending_rate.get(symbol, st.funding_rate))
            self.wallet += amount
            self._income.append(IncomeRecord(symbol, "FUNDING_FEE", amount, ms_to_dt(last)))
        self._last_funding_ms[symbol] = st.next_funding_ms
        self._pending_rate[symbol] = st.funding_rate

    def funding_since(self, symbol: str, since: datetime) -> Decimal:
        return sum((i.amount for i in self._income if i.symbol == symbol and i.kind == "FUNDING_FEE"
                    and i.ts >= since - timedelta(seconds=1)), D(0))
