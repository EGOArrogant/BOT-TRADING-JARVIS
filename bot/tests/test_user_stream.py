import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from bot.core.config import Secrets, load_settings
from bot.execution.user_stream import UserStream


def test_handle_dispatches_and_detects_expiry():
    seen = []
    us = UserStream(SimpleNamespace(), "wss://fstream.binance.com/private/", lambda e, d: seen.append(e))
    assert us.url("abc") == "wss://fstream.binance.com/private/ws/abc"
    assert us.handle(json.dumps({"e": "ORDER_TRADE_UPDATE", "o": {}}))
    assert us.handle(json.dumps({"stream": "x", "data": {"e": "ALGO_UPDATE"}}))
    assert us.handle("no-json")  # basura: se ignora sin romper
    assert not us.handle(json.dumps({"e": "listenKeyExpired"}))
    assert seen == ["ORDER_TRADE_UPDATE", "ALGO_UPDATE"]


def test_engine_tick_interval_and_wake(tmp_path):
    from bot.core.config import StorageConfig
    from bot.data_engine.store import FeedStore
    from bot.engine import Components, Engine
    from bot.learning.journal import Journal

    s = load_settings().model_copy(update={"storage": StorageConfig(
        db_path=tmp_path / "db", reports_dir=tmp_path, state_path=tmp_path / "st.json")})
    real_ex = SimpleNamespace(positions=None)
    eng = Engine(s, Secrets(), Components(states={}, store=FeedStore(), exchange=real_ex, analyst=None,
                                          journal=Journal(":memory:")))
    assert eng.tick_interval() == 1.0  # sin stream: sondeo rápido
    eng.user_stream = SimpleNamespace(connected=True)
    assert eng.tick_interval() == 5.0

    ticks = []

    async def fake_tick(now):
        ticks.append(now)
    eng.tick = fake_tick

    async def run():
        task = asyncio.create_task(eng._tick_loop())
        await asyncio.sleep(0.05)
        eng.on_account_event("ORDER_TRADE_UPDATE", {})  # despierta antes de los 5 s
        await asyncio.sleep(0.05)
        task.cancel()
    asyncio.run(run())
    assert len(ticks) == 2


def test_paper_gate_fails_with_9pct_drawdown(tmp_path):
    from bot import gates
    from bot.core.models import ExitReason, Regime, Side, Trade
    from bot.learning.journal import Journal
    j = Journal(tmp_path / "bot_paper.sqlite")
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    pnl = [300.0] * 40 + [-90.0] * 22 + [300.0] * 8  # pico 22 000, racha de −1 980 = 9 % de drawdown
    for i, p in enumerate(pnl):
        j.record_trade(Trade(id=f"t{i}", signal_id=f"s{i}", symbol="ETHUSDT", side=Side.LONG, strategy="x",
                             regime=Regime.RANGE, qty=Decimal(1), entry_price=Decimal(100),
                             exit_price=Decimal(100), expected_entry_price=Decimal(100),
                             initial_stop_price=Decimal(99), entry_maker=True, fees=Decimal(0),
                             pnl_gross=Decimal(str(p)), mae_r=0, mfe_r=0,
                             opened_at=t0 + timedelta(days=i * 0.7), closed_at=t0 + timedelta(days=i * 0.7, hours=1),
                             exit_reason=ExitReason.TAKE_PROFIT))
    res = gates.paper_gate(tmp_path / "bot_paper.sqlite", 10_000)
    assert abs(res["resumen"]["max_drawdown"] - 0.09) < 1e-9
    assert res["checks"]["factor de beneficio > 1.3"] and not res["checks"]["drawdown < 8%"]
    assert not res["ok"]
