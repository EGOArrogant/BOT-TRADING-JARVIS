import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from bot.core.clock import utc_now
from bot.core.config import ExecutionConfig, load_settings
from bot.core.models import ExitReason, OrderSide, OrderStatus, Regime, Side, Signal
from bot.data_engine.market_state import MarketState
from bot.execution.base import ExchangeError, UncertainOrderState
from bot.execution.executor import Executor, client_id
from bot.execution.paper import PaperExchange
from bot.position.manager import PositionManager
from bot.risk.engine import SymbolRules

S = load_settings()
RULES = SymbolRules(tick_size=0.01, step_size=0.001, min_qty=0.001, min_notional=5.0)
FAST = ExecutionConfig(post_only_timeout_s=0.3, post_only_retries=1, market_entry_min_edge=4.0,
                       stop_confirm_timeout_s=0.5, reconcile_interval_s=30)


def market(sym="ETHUSDT", bid=99.99, ask=100.01, mark=100.0) -> MarketState:
    st = MarketState(sym)
    st.book.apply_snapshot({"lastUpdateId": 1, "bids": [[str(bid), "100"], [str(bid - 1), "100"]],
                            "asks": [[str(ask), "100"], [str(ask + 1), "100"]]})
    st.mark_price = mark
    st.last_price = mark
    return st


def trade(st: MarketState, px: float, ex: PaperExchange, mark: float | None = None):
    now_ms = int(utc_now().timestamp() * 1000) + len(st._recent_trades) + 1
    st.on_agg_trade({"p": str(px), "q": "1", "m": False, "T": now_ms})
    st.mark_price = mark if mark is not None else px
    ex.on_market(st.symbol)


def sig(side=Side.LONG, urgent=False) -> Signal:
    entry = 100.0
    return Signal(id=f"sig-{side.value}-{urgent}", symbol="ETHUSDT", strategy="trend_pullback", side=side, entry=entry,
                  stop=entry - 1 if side is Side.LONG else entry + 1,
                  take_profit=entry + 3 if side is Side.LONG else entry - 3, expected_move_pct=0.03,
                  total_cost_pct=0.002, data_quality=1, regime=Regime.TREND_UP, urgent=urgent,
                  invalidation_price=98.5 if side is Side.LONG else 101.5, created_at=utc_now())


def setup(**ex_kw):
    st = market()
    ex = PaperExchange({"ETHUSDT": st}, S.exchange.fees, 10_000, latency_ms=0, **ex_kw)
    alerts: list[str] = []
    exe = Executor(ex, {"ETHUSDT": st}, FAST, S.position, alert=alerts.append, poll_s=0.02)
    return st, ex, exe, alerts


async def fill_soon(st, ex, px, delay=0.1):
    await asyncio.sleep(delay)
    trade(st, px, ex)


def test_maker_entry_places_stop_and_take_profits():
    async def run():
        st, ex, exe, alerts = setup()
        task = asyncio.create_task(fill_soon(st, ex, 99.95))
        res = await exe.open_position(sig(), 10, RULES, edge_ratio=5, still_valid=lambda: True)
        await task
        return res, ex, alerts
    res, ex, alerts = asyncio.run(run())
    pos = res.position
    assert pos is not None and pos.entry_maker and pos.entry_price == Decimal("100.00")
    assert pos.stop_price == Decimal("99.00") and not alerts
    opens = asyncio.run(ex.open_orders("ETHUSDT"))
    kinds = sorted(o.type.value for o in opens)
    assert kinds == ["LIMIT", "LIMIT", "STOP_MARKET"]
    assert all(o.reduce_only or o.close_position for o in opens)
    stop = [o for o in opens if o.type.value == "STOP_MARKET"][0]
    assert stop.is_algo and stop.close_position


def test_no_fill_retries_once_then_cancels():
    async def run():
        st, ex, exe, _ = setup()
        res = await exe.open_position(sig(), 10, RULES, edge_ratio=5, still_valid=lambda: True)
        return res, ex
    res, ex = asyncio.run(run())
    assert res.position is None
    entries = [o for o in ex.orders.values() if o.client_order_id.startswith("cbent")]
    assert len(entries) == 2 and all(o.status is OrderStatus.CANCELED for o in entries)
    assert asyncio.run(ex.positions()) == []


def test_no_retry_when_signal_invalid():
    async def run():
        st, ex, exe, _ = setup()
        return await exe.open_position(sig(), 10, RULES, edge_ratio=5, still_valid=lambda: False), ex
    res, ex = asyncio.run(run())
    assert res.position is None and len(ex.orders) == 1


def test_stop_not_confirmed_closes_at_market():
    class NoStop(PaperExchange):
        async def place_stop(self, *a, **k):
            raise ExchangeError("algo service down")

    async def run():
        st = market()
        ex = NoStop({"ETHUSDT": st}, S.exchange.fees, 10_000, latency_ms=0)
        alerts: list[str] = []
        exe = Executor(ex, {"ETHUSDT": st}, FAST, S.position, alert=alerts.append, poll_s=0.02)
        res = await exe.open_position(sig(urgent=True), 10, RULES, edge_ratio=5, still_valid=lambda: True)
        return res, ex, alerts
    res, ex, alerts = asyncio.run(run())
    assert res.position is None and alerts and "stop no confirmado" in alerts[0]
    assert asyncio.run(ex.positions()) == []  # nunca queda posición sin stop


def test_idempotent_after_uncertain_state():
    class Flaky(PaperExchange):
        calls = 0

        async def place_market(self, symbol, side, qty, client_id, reduce_only=False):
            Flaky.calls += 1
            await super().place_market(symbol, side, qty, client_id, reduce_only)
            raise UncertainOrderState("timeout tras enviar")

    async def run():
        st = market()
        ex = Flaky({"ETHUSDT": st}, S.exchange.fees, 10_000, latency_ms=0)
        exe = Executor(ex, {"ETHUSDT": st}, FAST, S.position, poll_s=0.02)
        res = await exe.open_position(sig(urgent=True), 10, RULES, edge_ratio=5, still_valid=lambda: True)
        # reintento del mismo flujo: el ID determinista evita duplicar
        res2_order = await exe._place_idempotent("ETHUSDT", client_id("ent", sig(urgent=True).id, 9),
                                                 lambda: ex.place_market("ETHUSDT", OrderSide.BUY, Decimal(10), "x"))
        return res, res2_order, ex
    res, again, ex = asyncio.run(run())
    assert res.position is not None and Flaky.calls == 1
    assert again.client_order_id == client_id("ent", sig(urgent=True).id, 9)
    assert asyncio.run(ex.positions())[0].qty == Decimal(10)


def _open(ex, exe, st, side=Side.LONG):
    async def run():
        t = asyncio.create_task(fill_soon(st, ex, 99.95 if side is Side.LONG else 100.05))
        res = await exe.open_position(sig(side), 10, RULES, edge_ratio=5, still_valid=lambda: True)
        await t
        return res.position
    return asyncio.run(run())


def test_stop_hit_produces_losing_trade():
    st, ex, exe, _ = setup()
    pos = _open(ex, exe, st)
    closed = []
    pm = PositionManager(ex, exe, {"ETHUSDT": st}, S.position, on_closed=lambda t, p: closed.append(t))
    pm.add(pos, RULES)
    st.book.apply_snapshot({"lastUpdateId": 2, "bids": [["98.9", "100"]], "asks": [["98.92", "100"]]})
    trade(st, 98.9, ex, mark=98.95)  # mark atraviesa el stop 99.00
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.8}, {}, macro_soon=False))
    assert len(closed) == 1
    t = closed[0]
    assert t.exit_reason is ExitReason.STOP and t.pnl_net < 0
    assert float(t.r_multiple) == pytest.approx(-1.1 - float(t.fees) / 10, abs=0.15)
    assert asyncio.run(ex.open_orders("ETHUSDT")) == []  # TPs cancelados


def test_partial_then_breakeven_stop():
    st, ex, exe, _ = setup()
    pos = _open(ex, exe, st)
    pm = PositionManager(ex, exe, {"ETHUSDT": st}, S.position, on_closed=lambda t, p: None)
    pm.add(pos, RULES)
    trade(st, 101.6, ex)  # atraviesa 1.5R = 101.5 -> parcial maker
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.01}, {}, macro_soon=False))
    assert pos.partial_taken and pos.qty == Decimal("5.000")
    assert pos.stop_price >= pos.entry_price  # stop a equilibrio o mejor
    stops = [o for o in asyncio.run(ex.open_orders("ETHUSDT")) if o.type.value == "STOP_MARKET"]
    assert len(stops) == 1


def test_invalidation_and_time_exit():
    st, ex, exe, _ = setup()
    pos = _open(ex, exe, st)
    closed = []
    pm = PositionManager(ex, exe, {"ETHUSDT": st}, S.position, on_closed=lambda t, p: closed.append(t))
    pm.add(pos, RULES)
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.5}, {"ETHUSDT": 98.4}, macro_soon=False))
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.5}, {}, macro_soon=False))
    assert closed and closed[0].exit_reason is ExitReason.INVALIDATION

    st, ex, exe, _ = setup()
    pos = _open(ex, exe, st)
    closed = []
    pm = PositionManager(ex, exe, {"ETHUSDT": st}, S.position, on_closed=lambda t, p: closed.append(t))
    pos.opened_at = utc_now() - timedelta(hours=S.position.time_exit_hours + 0.1)
    pm.add(pos, RULES)
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.5}, {}, macro_soon=False))
    asyncio.run(pm.tick(utc_now(), {"ETHUSDT": 0.5}, {}, macro_soon=False))
    assert closed and closed[0].exit_reason is ExitReason.TIME


def test_post_only_rejected_if_crossing():
    st, ex, exe, _ = setup()
    o = asyncio.run(ex.place_limit("ETHUSDT", OrderSide.BUY, Decimal(1), Decimal("100.02"), "x1", post_only=True))
    assert o.status is OrderStatus.EXPIRED
