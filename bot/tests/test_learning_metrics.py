from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot.core.models import ExitReason, Regime, Side, Signal, Trade
from bot.data_engine.market_state import Candle
from bot.learning.counterfactual import due, simulate
from bot.learning.daily_report import build_zone
from bot.learning.journal import AI_REJECTED, REJECTED, TRADED, Journal
from bot.metrics.performance import ai_calibration, by_dimension, rejection_stats, summarize
from bot.metrics.report_html import render

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def mk_trade(i: int, pnl_gross: float, conf: float | None = 0.7, sym="ETHUSDT") -> Trade:
    return Trade(id=f"t{i}", signal_id=f"s{i}", symbol=sym, side=Side.LONG, strategy="trend_pullback",
                 regime=Regime.TREND_UP, qty=Decimal(1), entry_price=Decimal(100),
                 exit_price=Decimal(str(100 + pnl_gross)), expected_entry_price=Decimal(100),
                 initial_stop_price=Decimal(99), entry_maker=True, fees=Decimal("0.07"), funding=Decimal("-0.01"),
                 pnl_gross=Decimal(str(pnl_gross)), mae_r=-0.5, mfe_r=1.5, opened_at=NOW + timedelta(hours=i),
                 closed_at=NOW + timedelta(hours=i, minutes=90), exit_reason=ExitReason.TAKE_PROFIT,
                 ai_confidence=conf)


def sig(i: int) -> Signal:
    return Signal(id=f"c{i}", symbol="SOLUSDT", strategy="range_breakout", side=Side.LONG, entry=100, stop=99,
                  take_profit=103, expected_move_pct=0.03, total_cost_pct=0.002, data_quality=1,
                  regime=Regime.RANGE, created_at=NOW + timedelta(minutes=i))


def test_journal_roundtrip_and_counterfactual_queue():
    j = Journal(":memory:")
    j.record_candidate(sig(1), REJECTED, ["datos: datos no ok: ['deriv.oi']"], {"market.price": 100})
    j.record_candidate(sig(2), AI_REJECTED, [], {})
    j.update_candidate("c2", ai={"action": "NO_TRADE"})
    j.record_trade(mk_trade(1, 2.0))
    assert len(j.candidates(NOW)) == 2 and j.candidates(NOW)[1]["ai"]["action"] == "NO_TRADE"
    assert len(j.pending_counterfactuals(NOW + timedelta(hours=1))) == 2
    j.set_counterfactual("c1", {"r_net": -1.0})
    assert len(j.pending_counterfactuals(NOW + timedelta(hours=1))) == 1
    t = j.trades(NOW)[0]
    assert t["pnl_net"] == pytest.approx(2.0 - 0.07 - 0.01) and t["r"] == pytest.approx(1.92)


def candles(prices: list[tuple[float, float, float]], start: datetime) -> list[Candle]:
    t0 = int(start.timestamp() * 1000)
    return [Candle(t0 + i * 900_000, c, h, l, c, 1, 1, 0.5) for i, (h, l, c) in enumerate(prices)]


def test_counterfactual_simulation():
    cs = candles([(101, 99.5, 100.5), (103.5, 100, 103)], NOW)
    r = simulate("LONG", 100, 99, 103, NOW, 0.5, cs, cost_pct=0.002)
    assert r["exit"] == "take_profit" and r["r_gross"] == 3.0 and r["r_net"] == pytest.approx(2.8)
    both = candles([(104, 98, 100), (100, 100, 100)], NOW)
    assert simulate("LONG", 100, 99, 103, NOW, 0.5, both)["exit"] == "stop"  # misma vela: gana el stop
    assert simulate("SHORT", 100, 101, 97, NOW, 0.5, candles([(100.5, 99, 99.5)], NOW)) is None  # faltan datos
    assert due(NOW, 1, NOW + timedelta(hours=1, minutes=16)) and not due(NOW, 1, NOW + timedelta(hours=1))


def test_performance_and_calibration():
    j = Journal(":memory:")
    for i, (p, c) in enumerate([(2.0, 0.8), (-1.0, 0.55), (3.0, 0.9), (-1.0, 0.6)]):
        j.record_trade(mk_trade(i, p, c, sym="ETHUSDT" if i % 2 else "BTCUSDT"))
    tr = j.trades(NOW)
    s = summarize(tr, 10_000)
    assert s["n"] == 4 and s["win_rate"] == 0.5
    assert s["profit_factor"] == pytest.approx((1.92 + 2.92) / (1.08 * 2))
    assert s["max_drawdown"] > 0 and s["avg_mfe_r"] == 1.5
    dims = by_dimension(tr, "symbol", 10_000)
    assert set(dims) == {"BTCUSDT", "ETHUSDT"} and dims["BTCUSDT"]["n"] == 2
    cal = ai_calibration(tr)
    assert {c["tramo"] for c in cal} == {"0.5-0.6", "0.6-0.7", "0.8-0.9", "0.9-1.0"}
    assert summarize([], 1)["n"] == 0


def test_rejection_stats_and_zone_and_report(tmp_path):
    j = Journal(":memory:")
    j.record_candidate(sig(1), REJECTED, ["datos: datos no ok: ['deriv.oi']"], {})
    j.record_candidate(sig(2), AI_REJECTED, [], {})
    j.set_counterfactual("c2", {"r_net": 1.5})
    j.record_candidate(sig(3), TRADED, [], {})
    j.record_trade(mk_trade(1, -1.3))
    rej = rejection_stats(j.candidates(NOW))
    assert rej["n"] == 3 and rej["rejection_rate"] == pytest.approx(2 / 3)
    assert rej["counterfactual"][AI_REJECTED]["expectativa_r"] == 1.5
    zone = build_zone(j, NOW.date(), 10_000)
    assert "Zona de aprendizaje" in zone and "datos" in zone and "**Lección:**" in zone
    assert len(zone.splitlines()) < 45  # máximo 1 página
    html = render(j.trades(NOW), j.candidates(NOW), 10_000, NOW, "paper")
    assert "<svg" in html and "Por moneda" in html and "prefers-color-scheme: dark" in html
    empty = render([], [], 10_000, NOW, "paper")
    assert "Sin datos suficientes" in empty
