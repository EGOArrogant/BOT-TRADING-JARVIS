"""Cliente Binance USDⓈ-M (REST firmado) que implementa el protocolo Exchange.

Rutas verificadas 2026-10-15 en la documentación oficial (vía búsqueda):
- Órdenes condicionales (STOP_MARKET, TAKE_PROFIT_MARKET, ...) migraron el
  2025-12-09 al Algo Service: POST/GET/DELETE /fapi/v1/algoOrder,
  GET /fapi/v1/openAlgoOrders (algoType=CONDITIONAL, triggerPrice, clientAlgoId).
  /fapi/v1/order rechaza esos tipos con el código -4120.
- Posiciones: /fapi/v3/positionRisk; cuenta: /fapi/v3/account.
Volver a verificar contra developers.binance.com antes de pasar a `live`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from bot.core.clock import dt_to_ms, ms_to_dt
from bot.core.logging_setup import get_logger
from bot.core.models import Order, OrderSide, OrderStatus, OrderType, Side
from bot.data_engine.http import RateLimiter
from bot.execution.base import (
    DuplicateClientId,
    ExchangeError,
    ExchangePosition,
    Fill,
    IncomeRecord,
    UncertainOrderState,
)
from bot.risk.engine import SymbolRules

log = get_logger("bot.binance")

D = Decimal
ORDER_STATUS = {
    "NEW": OrderStatus.NEW, "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED, "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED, "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}
ALGO_STATUS = {
    "NEW": OrderStatus.NEW, "TRIGGERING": OrderStatus.NEW, "TRIGGERED": OrderStatus.FILLED,
    "FINISHED": OrderStatus.FILLED, "CANCELED": OrderStatus.CANCELED, "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}
UNKNOWN_ORDER_CODES = {-2011, -2013}
DUPLICATE_CODES = {-4015, -4116}


def sign(secret: str, params: dict[str, Any]) -> str:
    query = urlencode(params)
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def _is_algo(cid: str) -> bool:
    return cid.startswith("cbstp")


class BinanceFutures:
    def __init__(self, base_url: str, api_key: str, api_secret: str, recv_window_ms: int = 5000,
                 name: str = "binance", client: httpx.AsyncClient | None = None, timeout: float = 10.0):
        if not api_key or not api_secret:
            raise ValueError("faltan llaves de API")
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.secret = api_secret
        self.recv_window = recv_window_ms
        self.name = name
        self.http = client or httpx.AsyncClient(timeout=timeout)
        self.limiter = RateLimiter({"orders": (250, 10.0), "weight": (2000, 60.0)})
        self._time_offset_ms = 0
        self._rules: dict[str, SymbolRules] = {}
        self._order_ids: dict[int, str] = {}  # orderId -> clientOrderId (para fills)

    # ------------------------------------------------------------ transporte

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = False,
                       weight: int = 1, order: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        await self.limiter.acquire("weight", weight)
        if order:
            await self.limiter.acquire("orders", 1)
        headers = {"X-MBX-APIKEY": self.key}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = self.recv_window
            params["signature"] = sign(self.secret, params)
        url = f"{self.base}{path}"
        try:
            if method in ("GET", "DELETE"):
                resp = await self.http.request(method, url, params=params, headers=headers)
            else:
                resp = await self.http.request(method, url, content=urlencode(params),
                                               headers={**headers, "Content-Type": "application/x-www-form-urlencoded"})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if order:
                raise UncertainOrderState(repr(exc)) from exc
            raise ExchangeError(f"red: {exc!r}") from exc
        if resp.status_code >= 500 or resp.status_code == 503:
            if order:
                raise UncertainOrderState(f"HTTP {resp.status_code}")
            raise ExchangeError(f"HTTP {resp.status_code}")
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            msg = data.get("msg") if isinstance(data, dict) else str(data)
            if code in DUPLICATE_CODES:
                raise DuplicateClientId(msg)
            err = ExchangeError(f"{code}: {msg}")
            err.code = code  # type: ignore[attr-defined]
            raise err
        return data

    async def sync_time(self) -> int:
        data = await self._request("GET", "/fapi/v1/time")
        self._time_offset_ms = int(data["serverTime"]) - int(time.time() * 1000)
        return self._time_offset_ms

    # ------------------------------------------------------------ info

    async def server_time(self) -> datetime:
        data = await self._request("GET", "/fapi/v1/time")
        return ms_to_dt(int(data["serverTime"]))

    async def load_rules(self) -> dict[str, SymbolRules]:
        info = await self._request("GET", "/fapi/v1/exchangeInfo")
        for s in info["symbols"]:
            f = {x["filterType"]: x for x in s.get("filters", [])}
            try:
                self._rules[s["symbol"]] = SymbolRules(
                    tick_size=float(f["PRICE_FILTER"]["tickSize"]), step_size=float(f["LOT_SIZE"]["stepSize"]),
                    min_qty=float(f["LOT_SIZE"]["minQty"]),
                    min_notional=float(f.get("MIN_NOTIONAL", {}).get("notional", 5)))
            except KeyError:
                continue
        return self._rules

    async def symbol_rules(self, symbol: str) -> SymbolRules:
        if symbol not in self._rules:
            await self.load_rules()
        return self._rules[symbol]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    async def balance(self) -> Decimal:
        acc = await self._request("GET", "/fapi/v3/account", signed=True, weight=5)
        return D(str(acc["totalMarginBalance"]))

    # ------------------------------------------------------------ órdenes

    def _parse_order(self, d: dict) -> Order:
        cid = d.get("clientOrderId") or d.get("clientAlgoId") or ""
        if "orderId" in d:
            self._order_ids[int(d["orderId"])] = cid
        is_algo = "algoId" in d
        status = (ALGO_STATUS if is_algo else ORDER_STATUS).get(d.get("algoStatus") or d.get("status", ""),
                                                                 OrderStatus.NEW)
        typ = OrderType(d.get("orderType") or d.get("type") or "LIMIT")
        qty = D(str(d.get("origQty") or d.get("quantity") or "0"))
        close_pos = str(d.get("closePosition", "false")).lower() == "true"
        filled = D(str(d.get("executedQty") or "0"))
        avg = D(str(d["avgPrice"])) if d.get("avgPrice") not in (None, "", "0", "0.0", "0.00000") else None
        upd = d.get("updateTime") or d.get("time") or d.get("createTime")
        return Order(
            client_order_id=cid, exchange_order_id=str(d.get("orderId") or d.get("algoId")), symbol=d["symbol"],
            side=OrderSide(d["side"]), type=typ, qty=qty if (qty > 0 or close_pos) else D(0),
            price=D(str(d["price"])) if d.get("price") not in (None, "", "0", "0.00") and typ is OrderType.LIMIT else None,
            stop_price=D(str(d.get("triggerPrice") or d.get("stopPrice"))) if typ is OrderType.STOP_MARKET else None,
            reduce_only=str(d.get("reduceOnly", "false")).lower() == "true", post_only=d.get("timeInForce") == "GTX",
            status=status, filled_qty=filled, avg_fill_price=avg, close_position=close_pos, is_algo=is_algo,
            created_at=ms_to_dt(int(d.get("createTime") or d.get("time") or d.get("updateTime") or time.time() * 1000)),
            updated_at=ms_to_dt(int(upd)) if upd else None,
        )

    async def place_limit(self, symbol: str, side: OrderSide, qty: Decimal, price: Decimal, client_id: str,
                          post_only: bool = True, reduce_only: bool = False) -> Order:
        d = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side.value, "type": "LIMIT", "timeInForce": "GTX" if post_only else "GTC",
            "quantity": str(qty), "price": str(price), "newClientOrderId": client_id,
            "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT"}, signed=True, order=True)
        return self._parse_order(d)

    async def place_market(self, symbol: str, side: OrderSide, qty: Decimal, client_id: str,
                           reduce_only: bool = False) -> Order:
        d = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side.value, "type": "MARKET", "quantity": str(qty),
            "newClientOrderId": client_id, "reduceOnly": "true" if reduce_only else None,
            "newOrderRespType": "RESULT"}, signed=True, order=True)
        return self._parse_order(d)

    async def place_stop(self, symbol: str, side: OrderSide, stop_price: Decimal, client_id: str) -> Order:
        d = await self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": side.value, "type": "STOP_MARKET",
            "triggerPrice": str(stop_price), "closePosition": "true", "workingType": "MARK_PRICE",
            "priceProtect": "true", "clientAlgoId": client_id}, signed=True, order=True)
        d.setdefault("orderType", "STOP_MARKET")
        d.setdefault("closePosition", "true")
        return self._parse_order(d)

    async def cancel(self, symbol: str, client_id: str) -> None:
        try:
            if _is_algo(client_id):
                await self._request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "clientAlgoId": client_id},
                                    signed=True)
            else:
                await self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id},
                                    signed=True)
        except ExchangeError as exc:
            if getattr(exc, "code", None) in UNKNOWN_ORDER_CODES:
                raise ExchangeError(f"orden {client_id} no abierta") from exc
            raise

    async def get_order(self, symbol: str, client_id: str) -> Order | None:
        try:
            if _is_algo(client_id):
                d = await self._request("GET", "/fapi/v1/algoOrder", {"symbol": symbol, "clientAlgoId": client_id},
                                        signed=True)
            else:
                d = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id},
                                        signed=True)
        except ExchangeError as exc:
            if getattr(exc, "code", None) in UNKNOWN_ORDER_CODES:
                return None
            raise
        return self._parse_order(d) if d else None

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        params = {"symbol": symbol} if symbol else {}
        regular = await self._request("GET", "/fapi/v1/openOrders", params, signed=True, weight=1 if symbol else 40)
        algo = await self._request("GET", "/fapi/v1/openAlgoOrders", params, signed=True, weight=1 if symbol else 40)
        algo_rows = algo.get("orders", algo) if isinstance(algo, dict) else algo
        return [self._parse_order(d) for d in regular] + [self._parse_order(d) for d in algo_rows]

    async def positions(self) -> list[ExchangePosition]:
        rows = await self._request("GET", "/fapi/v3/positionRisk", signed=True, weight=5)
        out = []
        for r in rows:
            amt = D(str(r["positionAmt"]))
            if amt == 0:
                continue
            out.append(ExchangePosition(r["symbol"], Side.LONG if amt > 0 else Side.SHORT, abs(amt),
                                        D(str(r["entryPrice"])), D(str(r.get("markPrice") or "0")) or None,
                                        D(str(r.get("unRealizedProfit") or "0"))))
        return out

    async def fills(self, symbol: str, since: datetime) -> list[Fill]:
        rows = await self._request("GET", "/fapi/v1/userTrades",
                                   {"symbol": symbol, "startTime": dt_to_ms(since), "limit": 1000},
                                   signed=True, weight=5)
        return [Fill(symbol, self._order_ids.get(int(r["orderId"]), ""), OrderSide(r["side"]), D(str(r["qty"])),
                     D(str(r["price"])), D(str(r["commission"])), bool(r["maker"]), D(str(r["realizedPnl"])),
                     ms_to_dt(int(r["time"]))) for r in rows]

    async def income(self, since: datetime) -> list[IncomeRecord]:
        rows = await self._request("GET", "/fapi/v1/income", {"startTime": dt_to_ms(since), "limit": 1000},
                                   signed=True, weight=30)
        return [IncomeRecord(r.get("symbol", ""), r["incomeType"], D(str(r["income"])), ms_to_dt(int(r["time"])))
                for r in rows]

    # ------------------------------------------------------------ user data stream

    async def new_listen_key(self) -> str:
        d = await self._request("POST", "/fapi/v1/listenKey")
        return d["listenKey"]

    async def keepalive_listen_key(self) -> None:
        await self._request("PUT", "/fapi/v1/listenKey")
