"""Apagón en modo papel: al volver, el bot sigue con su saldo, su posición y su stop."""
import asyncio

from bot.core.clock import utc_now
from bot.execution.paper import PaperExchange
from bot.risk.engine import SymbolRules
from bot.tests.test_engine_paper import FakeAnalyst, _fill, make_engine, setup  # noqa: F401


def fresh_exchange(s, states):
    return PaperExchange(states, s.exchange.fees, s.paper.initial_capital, latency_ms=0,
                         rules={k: SymbolRules(0.01, 0.001, 0.001, 5.0) for k in states})


def test_restart_keeps_position_stop_and_balance(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eth = states["ETHUSDT"]

    async def run():
        t = asyncio.create_task(_fill(eth, ex, eth.book.best_bid() - 0.5))
        await eng.evaluate_window(now)
        await t
    asyncio.run(run())
    before = eng.manager.positions["ETHUSDT"]
    wallet = ex.wallet
    assert eng.paper_path.exists()  # se guardó al abrir

    # Apagón: proceso nuevo, exchange de papel vacío, mismo disco
    ex2 = fresh_exchange(s, states)
    eng2 = make_engine(s, states, ex2, FakeAnalyst(), now, journal=eng.journal)
    asyncio.run(eng2.restore_paper())
    asyncio.run(eng2.adopt_existing(now))
    after = eng2.manager.positions["ETHUSDT"]
    assert (after.signal_id, after.strategy, after.qty, after.stop_price) == \
           (before.signal_id, before.strategy, before.qty, before.stop_price)
    assert ex2.wallet == wallet
    stops = [o for o in asyncio.run(ex2.open_orders("ETHUSDT")) if o.type.value == "STOP_MARKET"]
    assert len(stops) == 1 and stops[0].stop_price == before.stop_price

    # El precio cayó mientras estaba apagado: el stop recuperado cierra la operación y se registra
    stop = float(after.stop_price)
    eth.book.apply_snapshot({"lastUpdateId": 10, "bids": [[str(stop - 0.2), "1000"]], "asks": [[str(stop - 0.18), "1000"]]})
    eth.mark_price = stop - 0.1
    asyncio.run(eng2.tick(utc_now()))
    trades = eng2.journal.trades(now.replace(year=2000))
    assert len(trades) == 1 and trades[0]["strategy"] == "always" and trades[0]["exit_reason"] == "stop"

    # Y un tercer arranque continúa con el saldo después de esa pérdida, no con el capital inicial
    eng2.save_paper()
    ex3 = fresh_exchange(s, states)
    eng3 = make_engine(s, states, ex3, FakeAnalyst(), now, journal=eng.journal)
    asyncio.run(eng3.restore_paper())
    assert ex3.wallet == ex2.wallet < 10_000 and eng3.manager.positions == {}  # el escenario empieza con 10 000


def test_without_paper_file_uses_saved_equity(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.state.update_equity(1_950.0)
    asyncio.run(eng.restore_paper())
    assert float(ex.wallet) == 1_950.0


def test_corrupt_paper_file_falls_back_to_state(setup):
    s, states, ex, now = setup
    eng = make_engine(s, states, ex, FakeAnalyst(), now)
    eng.paper_path.parent.mkdir(parents=True, exist_ok=True)
    eng.paper_path.write_text("{roto", encoding="utf-8")
    eng.state.update_equity(2_100.0)
    asyncio.run(eng.restore_paper())
    assert float(ex.wallet) == 2_100.0 and eng.manager.positions == {}
