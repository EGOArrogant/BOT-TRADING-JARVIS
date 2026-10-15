from datetime import UTC, datetime
import time

import numpy as np
import pytest

from bot.backtest.engine import Backtester, aggregate, walk_forward
from bot.core.config import load_settings
from bot.core.models import Regime, Side, Signal
from bot.data_engine.market_state import INTERVAL_MS, Candle
from bot.filters import strategies as strat_mod
from bot.tests.helpers import make_candles, trend_series

S = load_settings()
START = 1_759_276_800_000  # 2025-10-01 00:00 UTC


def flat(n: int, price: float = 100.0) -> list[Candle]:
    return [Candle(START + i * INTERVAL_MS["15m"], price, price, price, price, 100, 100 * price, 50) for i in range(n)]


def always_long(symbol, f, params, fees, now, regime, quality):
    c = f["ind.15m.close"].as_float()
    return Signal(id=f"{symbol}{now.isoformat()}", symbol=symbol, strategy="always", side=Side.LONG, entry=c,
                  stop=c * 0.98, take_profit=c * 1.04, expected_move_pct=0.04, total_cost_pct=0.001,
                  data_quality=1.0, regime=Regime.RANGE, urgent=True, invalidation_price=None,
                  max_hold_hours=2.0, created_at=now)


def test_aggregate_15m_to_1h():
    cs = make_candles(np.linspace(100, 110, 8), INTERVAL_MS["15m"], start_ms=START)
    h = aggregate(cs, 4, INTERVAL_MS["1h"])
    assert len(h) == 2 and h[0].open == cs[0].open and h[0].close == cs[3].close
    assert h[0].high == max(c.high for c in cs[:4])


def test_no_edge_strategy_loses_exactly_costs(monkeypatch):
    """Precio constante: toda pérdida debe venir de comisiones + deslizamiento + funding."""
    monkeypatch.setitem(strat_mod.STRATEGIES, "always", always_long)
    from bot.core.config import StrategyParams
    params = {"always": StrategyParams(max_hold_hours=2.0)}
    bt = Backtester(S, assumed_spread_bps=2.0, funding_rate_1h=0.0000125, warmup_bars_15m=800)
    res = bt.run({"ETHUSDT": flat(1200)}, strategies=params)
    assert res.trades, res.rejected
    slip = 2.0 / 2 / 1e4 + 1e-4
    for t in res.trades:
        assert t.maker_entry is False
        entry_slip_cost = t.qty * 100.0 * slip
        exit_slip_cost = t.qty * 100.0 - t.qty * t.exit  # vendió por debajo de 100
        assert t.pnl_gross == pytest.approx(-(entry_slip_cost + exit_slip_cost), rel=1e-9)
        expected_fees = t.qty * t.entry * 0.0005 + t.qty * t.exit * 0.0005
        assert t.fees == pytest.approx(expected_fees, rel=1e-9)
        assert t.pnl_net == pytest.approx(-(entry_slip_cost + exit_slip_cost + expected_fees + t.funding), rel=1e-9)
        assert t.pnl_net < 0
    s = res.summary()
    assert s["pnl_net"] == pytest.approx(-sum(t.fees + t.funding for t in res.trades)
                                         + sum(t.pnl_gross for t in res.trades))
    assert s["trades"] <= 5 * 4  # ~4 días de datos post-warmup: techo diario respetado


def test_daily_cap_respected_in_backtest(monkeypatch):
    monkeypatch.setitem(strat_mod.STRATEGIES, "always", always_long)
    from bot.core.config import StrategyParams
    bt = Backtester(S, warmup_bars_15m=800)
    res = bt.run({"ETHUSDT": flat(800 + 96 * 3)}, strategies={"always": StrategyParams(max_hold_hours=0.5)})
    per_day: dict = {}
    for t in res.trades:
        per_day[t.entry_time.date()] = per_day.get(t.entry_time.date(), 0) + 1
    assert per_day and max(per_day.values()) <= 5


def test_real_strategies_run_and_walk_forward_is_fast_enough():
    closes = trend_series(1600, 0.0004, 0.004, seed=7, start=2500)
    data = {"ETHUSDT": make_candles(closes, INTERVAL_MS["15m"], start_ms=START, buy_frac=0.55)}
    bt = Backtester(S, warmup_bars_15m=800)
    t0 = time.time()
    res = bt.run(data)
    assert time.time() - t0 < 60
    s = res.summary()
    assert s["max_drawdown"] < 0.5 and s["candidates"] >= 0
    peak = max(e for _, e in res.equity_curve)
    for t in res.trades:
        assert t.risk_amount <= peak * 0.005 * 1.01  # nunca más de 0.5 % del capital
    folds = walk_forward(bt, data, folds=2)
    assert len(folds) == 2
