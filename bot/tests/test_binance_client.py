import asyncio
import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

from bot.core.models import OrderSide, OrderStatus, OrderType, Side
from bot.execution.base import DuplicateClientId, ExchangeError, UncertainOrderState
from bot.execution.binance import BinanceFutures, sign

SECRET = "s3cr3t"


def make(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BinanceFutures("https://fapi.test", "KEY", SECRET, client=client)


def params_of(req: httpx.Request) -> dict:
    raw = req.content.decode() if req.method in ("POST", "PUT") else urlparse(str(req.url)).query
    return {k: v[0] for k, v in parse_qs(raw).items()}


def check_signature(p: dict) -> None:
    sig = p.pop("signature")
    assert sig == hmac.new(SECRET.encode(), urlencode(p).encode(), hashlib.sha256).hexdigest()


def test_sign_matches_hmac():
    assert sign("k", {"a": 1, "b": "x"}) == hmac.new(b"k", b"a=1&b=x", hashlib.sha256).hexdigest()


def test_stop_goes_to_algo_endpoint_with_close_position():
    seen = {}

    def handler(req):
        seen["path"], seen["p"], seen["key"] = req.url.path, params_of(req), req.headers["X-MBX-APIKEY"]
        return httpx.Response(200, json={"algoId": 77, "clientAlgoId": "cbstp0-x", "algoStatus": "NEW",
                                         "symbol": "ETHUSDT", "side": "SELL", "triggerPrice": "99.5",
                                         "createTime": 1760000000000})
    ex = make(handler)
    o = asyncio.run(ex.place_stop("ETHUSDT", OrderSide.SELL, Decimal("99.5"), "cbstp0-x"))
    assert seen["path"] == "/fapi/v1/algoOrder" and seen["key"] == "KEY"
    p = seen["p"]
    check_signature(p)
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET" and p["closePosition"] == "true"
    assert p["triggerPrice"] == "99.5" and p["workingType"] == "MARK_PRICE" and "quantity" not in p
    assert o.type is OrderType.STOP_MARKET and o.is_algo and o.close_position and o.status is OrderStatus.NEW


def test_post_only_limit_uses_gtx_and_reduce_only_flag():
    seen = {}

    def handler(req):
        seen["p"] = params_of(req)
        return httpx.Response(200, json={"orderId": 5, "clientOrderId": "cbent0-a", "status": "NEW", "symbol": "ETHUSDT",
                                         "side": "BUY", "type": "LIMIT", "origQty": "1", "price": "100",
                                         "executedQty": "0", "avgPrice": "0", "timeInForce": "GTX", "updateTime": 1})
    ex = make(handler)
    o = asyncio.run(ex.place_limit("ETHUSDT", OrderSide.BUY, Decimal("1"), Decimal("100"), "cbent0-a"))
    assert seen["p"]["timeInForce"] == "GTX" and "reduceOnly" not in seen["p"]
    assert o.post_only and o.status is OrderStatus.NEW
    asyncio.run(ex.place_limit("ETHUSDT", OrderSide.SELL, Decimal("1"), Decimal("101"), "cbptp0-b", post_only=False,
                               reduce_only=True))
    assert seen["p"]["timeInForce"] == "GTC" and seen["p"]["reduceOnly"] == "true"


def test_errors_map_to_safe_exceptions():
    def unknown(req):
        return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
    assert asyncio.run(make(unknown).get_order("ETHUSDT", "cbent0-zz")) is None

    def dup(req):
        return httpx.Response(400, json={"code": -4116, "msg": "ClientOrderId is duplicated."})
    with pytest.raises(DuplicateClientId):
        asyncio.run(make(dup).place_market("ETHUSDT", OrderSide.BUY, Decimal(1), "x"))

    def timeout(req):
        raise httpx.ReadTimeout("t", request=req)
    with pytest.raises(UncertainOrderState):
        asyncio.run(make(timeout).place_market("ETHUSDT", OrderSide.BUY, Decimal(1), "x"))
    with pytest.raises(ExchangeError):
        asyncio.run(make(timeout).positions())  # lectura: error normal, no "incierto"

    def legacy(req):
        return httpx.Response(400, json={"code": -4120, "msg": "use algo order"})
    with pytest.raises(ExchangeError, match="-4120"):
        asyncio.run(make(legacy).place_market("ETHUSDT", OrderSide.BUY, Decimal(1), "x"))


def test_positions_open_orders_fills_income_rules():
    def handler(req):
        path = req.url.path
        if path == "/fapi/v3/positionRisk":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT", "positionAmt": "-2", "entryPrice": "100",
                                              "markPrice": "99", "unRealizedProfit": "2"},
                                             {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"}])
        if path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[{"orderId": 1, "clientOrderId": "cbftp0-q", "status": "NEW",
                                              "symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT", "origQty": "2",
                                              "price": "97", "reduceOnly": True, "time": 1}])
        if path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json=[{"algoId": 9, "clientAlgoId": "cbstp0-q", "algoStatus": "NEW",
                                              "symbol": "ETHUSDT", "side": "BUY", "orderType": "STOP_MARKET",
                                              "triggerPrice": "101", "closePosition": True, "createTime": 1}])
        if path == "/fapi/v1/userTrades":
            return httpx.Response(200, json=[{"orderId": 1, "side": "BUY", "qty": "2", "price": "97",
                                              "commission": "0.04", "maker": True, "realizedPnl": "6", "time": 2}])
        if path == "/fapi/v1/income":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT", "incomeType": "FUNDING_FEE", "income": "-0.1",
                                              "time": 3}])
        if path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "ETHUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "stepSize": "0.001",
                                                                     "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "20"}]}]})
        return httpx.Response(404)
    ex = make(handler)
    pos = asyncio.run(ex.positions())
    assert len(pos) == 1 and pos[0].side is Side.SHORT and pos[0].qty == Decimal(2)
    orders = asyncio.run(ex.open_orders())
    assert {o.type for o in orders} == {OrderType.LIMIT, OrderType.STOP_MARKET}
    fills = asyncio.run(ex.fills("ETHUSDT", __import__("datetime").datetime(2026, 1, 1,
                                                                            tzinfo=__import__("datetime").UTC)))
    assert fills[0].order_client_id == "cbftp0-q" and fills[0].realized_pnl == Decimal(6)
    inc = asyncio.run(ex.income(__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC)))
    assert inc[0].kind == "FUNDING_FEE" and inc[0].amount == Decimal("-0.1")
    r = asyncio.run(ex.symbol_rules("ETHUSDT"))
    assert r.tick_size == 0.01 and r.min_notional == 20
