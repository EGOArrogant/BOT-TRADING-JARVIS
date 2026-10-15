"""Prueba de punta a punta del orquestador en modo papel, sin red."""
import asyncio
from datetime import datetime, timedelta

import pytest

from bot.ai_analyst.client import AnalystResult
from bot.core.clock import dt_to_ms, utc_now
from bot.core.config import Secrets, StorageConfig, load_settings
from bot.core.models import AIAction, Decision, NewsImpact, Regime, Side, Signal
from bot.data_engine.market_state import MarketState
from bot.data_engine.store import FeedStore
from bot.engine import Components, Engine
from bot.execution.paper import PaperExchange
from bot.filters import strategies as strat_mod
from bot.learning.journal import AI_REJECTED, Journal, TRADED
from bot.metrics.exchange_reconcile import reconcile_state
from bot.risk.engine import SymbolRules
from bot.tests.helpers import fill_state, trend_series


class FakeMacro:
    events, ok = [], True

    @property
    def updated_at(self):
        return utc_now()


class FakeAnalyst:
    def __init__(self, action=AIAction.LONG, conf=0.8):
        self.action, self.conf, self.calls = action, conf, 0

    async def decide(self, sig, prompt):
        self.calls += 1
        assert "ETHUSDT" in prompt and "lecciones_activas" in prompt
        d = Decision(action=self.action, confidence=self.conf, size_multiplier=0.8, thesis="Prueba.",
                     invalidation="cierre bajo stop" if self.action is not AIAction.NO_TRADE else "",
                     news_impact=NewsImpact.NONE)
        return AnalystResult(d, True, "ok", model="fake")

    async def score_news(self, hs):
        return hs


def always_long(symbol, f, params, fees, now, regime, quality):
    if symbol != "ETHUSDT":
        return None
    c = f["ind.15m.close"].as_float()
    return Signal(id=f"e2e-{int(now.timestamp())}", symbol=symbol, strategy="always", side=Side.LONG, entry=c,
                  stop=c - 10.0, take_profit=c + 30.0, expected_move_pct=30.0 / c, total_cost_pct=2.0 / c,
                  data_quality=quality, regime=Regime.TREND_UP, created_at=now, invalidation_price=c - 20)


def market(sym: str, now: datetime, price: float) -> MarketState:
    st = MarketState(sym)
    end = dt_to_ms(now) // 900_000 * 900_000
    fill_state(st, {"5m": trend_series(400, 0, 0.001, seed=2, start=price),
                    "15m": trend_series(300, 0, 0.001, seed=3, start=price),
                    "1h": trend_series(800, 0, 0.002, seed=4, start=price)}, end_ms=end)
    last = st.candles["15m"].last().close
    st.book.apply_snapshot({"lastUpdateId": 1, "bids": [[str(last - 0.01), "1000"]], "asks": [[str(last + 0.01), "1000"]]})
    st.book.on_event({"U": 1, "u": 2, "pu": 1, "b": [], "a": [], "E": dt_to_ms(now)})
    st.on_agg_trade({"p": str(last), "q": "1", "m": False, "T": dt_to_ms(now) - 500})
    st.on_mark_price({"p": str(last), "i": str(last), "r": "0.0001", "T": dt_to_ms(now) + 3_600_000,
                      "E": dt_to_ms(now) - 500})
    return st


def store_for(syms, now) -> FeedStore:
    s = FeedStore()
    for sym in syms:
        for i in range(100):
            s.set(f"deriv.funding_hist.{sym}", 0.0001 + 0.00002 * ((i % 5) - 2), "t", now - timedelta(hours=8 * (100 - i)))
        for i in range(48):
            s.set(f"deriv.oi.{sym}", 1e9 * (1 + 0.001 * ((i % 7) - 3)), "t", now - timedelta(hours=47 - i))
    return s


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setitem(strat_mod.STRATEGIES, "always", always_long)
    from bot.core.config import StrategyParams
    base = load_settings()
    s = base.model_copy(update={
        "storage": StorageConfig(db_path=tmp_path / "db.sqlite", reports_dir=tmp_path / "reports",
                                 state_path=tmp_path / "state.json"),
        "strategies": {"always": StrategyParams()},
        "data": base.data.model_copy(update={"dir": tmp_path}),
        "execution": base.execution.model_copy(update={"post_only_timeout_s": 0.4}),
    })
    now = utc_now()
    states = {sym: market(sym, now, p) for sym, p in (("ETHUSDT", 2500.0), ("BTCUSDT", 60000.0))}
    ex = PaperExchange(states, s.exchange.fees, 10_000, latency_ms=0,
                       rules={k: SymbolRules(0.01, 0.001, 0.001, 5.0) for k in states})
    return s, states, ex, now


def make_engine(s, states, ex, analyst, now, journal=None):
    comps = Components(states=states, store=store_for(states, now), exchange=ex, analyst=analyst,
                       journal=journal or Journal(s.storage.db_path), macro=FakeMacro())
    return Engine(s, Secrets(), comps)


async def _fill(st, ex, px, delay=0.15):
    await asyncio.sleep(delay)
    st.on_agg_trade({"p": str(px), "q": "1", "m": True, "T": dt_to_ms(utc_now())})
    ex.on_market(st.symbol)


def test_full_cycle_entry_stop_exit(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eth = states["ETHUSDT"]

    async def run():
        t = asyncio.create_task(_fill(eth, ex, eth.book.best_bid() - 0.5))
        sig = await eng.evaluate_window(now)
        await t
        return sig
    sig = asyncio.run(run())
    assert sig is not None
    cand = [c for c in eng.journal.candidates(now - timedelta(hours=1)) if c["id"] == sig.id][0]
    assert cand["status"] == TRADED, cand["reasons"]
    assert cand["size"]["ai"] == 0.8
    assert len(eng.state.entries_on(now.date())) == 1
    pos = eng.manager.positions["ETHUSDT"]
    assert any(o.type.value == "STOP_MARKET" for o in asyncio.run(ex.open_orders("ETHUSDT")))

    # El precio cae y atraviesa el stop -> el gestor registra la operación cerrada
    stop = float(pos.stop_price)
    eth.book.apply_snapshot({"lastUpdateId": 10, "bids": [[str(stop - 0.2), "1000"]], "asks": [[str(stop - 0.18), "1000"]]})
    eth.mark_price = stop - 0.1
    asyncio.run(eng.tick(utc_now()))
    trades = eng.journal.trades(now - timedelta(hours=1))
    assert len(trades) == 1 and trades[0]["exit_reason"] == "stop" and trades[0]["r"] < -0.9
    assert eng.state.closes[-1].pnl_net < 0 and eng.state.equity < 10_000
    assert "ETHUSDT" not in eng.manager.positions
    # Enfriamiento: la siguiente ventana no puede reabrir ETH
    sig2 = asyncio.run(eng.evaluate_window(now + timedelta(minutes=15)))
    assert sig2 is None


def test_ai_no_trade_places_nothing(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(AIAction.NO_TRADE, 0.0), now)
    sig = asyncio.run(eng.evaluate_window(now))
    cand = [c for c in eng.journal.candidates(now - timedelta(hours=1)) if c["id"] == sig.id][0]
    assert cand["status"] == AI_REJECTED and ex.orders == {}


def test_stale_data_blocks_entry(setup):
    s, states, ex, now = setup
    analyst = FakeAnalyst()
    eng = make_engine(s, states, ex, analyst, now)
    later = now + timedelta(seconds=30)  # precio y mark > 5 s de antigüedad
    sig = asyncio.run(eng.evaluate_window(later))
    assert sig is None and analyst.calls == 0
    cands = eng.journal.candidates(now - timedelta(hours=1))
    assert cands and all(c["status"] == "rechazado_filtro" for c in cands)
    assert any("datos" in r for r in cands[0]["reasons"])


def test_restart_with_open_position_keeps_stop_and_reconciles(setup):
    """§15: caída/reinicio con posición abierta: el stop sigue en el exchange y la conciliación lo detecta."""
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eth = states["ETHUSDT"]

    async def run():
        t = asyncio.create_task(_fill(eth, ex, eth.book.best_bid() - 0.5))
        await eng.evaluate_window(now)
        await t
    asyncio.run(run())
    assert "ETHUSDT" in eng.manager.positions

    # "Reinicio": nuevo motor sin memoria local, mismo exchange (el stop vive allí)
    eng2 = make_engine(s, states, ex, FakeAnalyst(), now, journal=eng.journal)
    issues = reconcile_state(eng2.manager.positions, asyncio.run(ex.positions()), asyncio.run(ex.open_orders()))
    assert [i.kind for i in issues] == ["posicion_huerfana"]  # detectada; el stop sigue presente
    asyncio.run(eng2.adopt_existing(now))
    asyncio.run(eng2.reconcile(now))
    assert not eng2.state.safe_mode and "ETHUSDT" in eng2.manager.positions

    # Si el stop desaparece del exchange: alerta + modo seguro + stop repuesto
    stop = next(o for o in asyncio.run(ex.open_orders("ETHUSDT")) if o.type.value == "STOP_MARKET")
    asyncio.run(ex.cancel("ETHUSDT", stop.client_order_id))
    asyncio.run(eng2.reconcile(now))
    assert eng2.state.safe_mode
    assert any(o.type.value == "STOP_MARKET" for o in asyncio.run(ex.open_orders("ETHUSDT")))
    assert not eng2.risk.global_guard(eng2.state, now).ok  # sin entradas nuevas en modo seguro


def test_hourly_report_and_daily_zone(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.write_report(now)
    assert (s.storage.reports_dir / "reporte.html").exists()
    asyncio.run(eng.daily_tasks(now))
    assert list(s.storage.reports_dir.glob("zona_*.md"))


def test_data_going_stale_during_ai_call_blocks_execution(setup):
    s, states, ex, now = setup

    class SlowAnalyst(FakeAnalyst):
        async def decide(self, sig, prompt):
            # mientras "piensa", el WebSocket se cae: el precio deja de actualizarse
            for st in states.values():
                st.last_trade_ms -= 60_000
                st.mark_ms -= 60_000
            return await super().decide(sig, prompt)

    eng = make_engine(s, states, ex, SlowAnalyst(), now)
    sig = asyncio.run(eng.evaluate_window(now))
    cand = [c for c in eng.journal.candidates(now - timedelta(hours=1)) if c["id"] == sig.id][0]
    assert cand["status"] == "rechazado_riesgo" and "datos_al_ejecutar" in cand["reasons"][0]
    assert ex.orders == {}
