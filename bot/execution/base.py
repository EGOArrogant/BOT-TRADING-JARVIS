"""Capa de exchange abstracta. Binance, testnet y papel implementan este protocolo;
cambiar a Bybit = escribir otra implementación sin tocar el resto del código."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from bot.core.models import Order, OrderSide, Side
from bot.risk.engine import SymbolRules


class ExchangeError(Exception):
    """Error del exchange con resultado conocido (orden rechazada)."""


class UncertainOrderState(Exception):
    """Timeout/red: no se sabe si la orden llegó. Consultar por client id antes de reintentar."""


class DuplicateClientId(ExchangeError):
    pass


@dataclass(frozen=True)
class ExchangePosition:
    symbol: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal = Decimal(0)


@dataclass(frozen=True)
class Fill:
    symbol: str
    order_client_id: str
    side: OrderSide
    qty: Decimal
    price: Decimal
    fee: Decimal
    maker: bool
    realized_pnl: Decimal
    ts: datetime


@dataclass(frozen=True)
class IncomeRecord:
    symbol: str
    kind: str        # REALIZED_PNL | COMMISSION | FUNDING_FEE | ...
    amount: Decimal  # signo del exchange: negativo = pagado
    ts: datetime


class Exchange(Protocol):
    name: str

    async def server_time(self) -> datetime: ...
    async def symbol_rules(self, symbol: str) -> SymbolRules: ...
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...
    async def balance(self) -> Decimal: ...

    async def place_limit(self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True, reduce_only: bool = False) -> Order: ...
    async def place_market(self, symbol: str, side: OrderSide, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> Order: ...
    async def place_stop(self, symbol: str, side: OrderSide, stop_price: Decimal, client_id: str) -> Order: ...

    async def cancel(self, symbol: str, client_id: str) -> None: ...
    async def get_order(self, symbol: str, client_id: str) -> Order | None: ...
    async def open_orders(self, symbol: str | None = None) -> list[Order]: ...
    async def positions(self) -> list[ExchangePosition]: ...

    async def fills(self, symbol: str, since: datetime) -> list[Fill]: ...
    async def income(self, since: datetime) -> list[IncomeRecord]: ...
