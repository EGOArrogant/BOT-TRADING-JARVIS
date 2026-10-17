from datetime import UTC, datetime, timedelta

import pytest

from bot.core.config import load_settings
from bot.core.models import DataPoint, Regime, Side, Signal
from bot.core.state import BotState, CloseRecord, EntryRecord
from bot.data_engine.macro import MacroEvent
from bot.filters.checks import (
    check_dangerous_leverage,
    check_data,
    check_extreme_vol,
    check_liquidity,
    check_macro,
    run_filters,
    select_best,
)
from bot.core.config import StrategyParams
from bot.filters.strategies import funding_reversion, generate, range_breakout, trend_breakout, trend_pullback

# Parámetros fijos: las pruebas validan la lógica, no los valores que elija config.yaml
TP_PARAMS = StrategyParams(enabled=True, target_atr_multiple=2.5, stop_atr_multiple=1.2, max_hold_hours=8, min_adx=20,
                           max_ema_distance_atr=0.5)
from bot.risk.costs import estimate_costs
from bot.risk.engine import OpenExposure, RiskEngine, SymbolRules, round_step, round_tick

S = load_settings()
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
RULES = SymbolRules(tick_size=0.01, step_size=0.001, min_qty=0.001, min_notional=5.0)


def eng() -> RiskEngine:
    return RiskEngine(S.risk, S.limits, S.exchange)


def sig(side=Side.LONG, entry=100.0, stop=99.0, move=0.02, cost=0.002, symbol="ETHUSDT", q=1.0, urgent=False):
    return Signal(id=f"{symbol}{side}{entry}{move}", symbol=symbol, strategy="t", side=side, entry=entry, stop=stop,
                  take_profit=entry * (1 + move) if side is Side.LONG else entry * (1 - move),
                  expected_move_pct=move, total_cost_pct=cost, data_quality=q, regime=Regime.TREND_UP,
                  urgent=urgent, created_at=NOW)


def state(equity=10_000.0) -> BotState:
    return BotState.fresh(equity, NOW.date())


def add_entries(st: BotState, n: int, when=NOW):
    for i in range(n):
        st.record_entry(EntryRecord(symbol=f"S{i}", side=Side.LONG, ts=when - timedelta(minutes=i + 1)))


# ------------------------------------------------------------ costos


def test_costs_maker_vs_taker_and_funding():
    c = estimate_costs(S.exchange.fees, Side.LONG, True, 2.0, 0.0001, 4)
    assert c.fees_pct == pytest.approx(0.0007)
    assert c.slippage_pct == pytest.approx(0.0002)
    assert c.funding_pct == pytest.approx(0.0004)
    short = estimate_costs(S.exchange.fees, Side.SHORT, False, None, 0.0001, 4)
    assert short.funding_pct == 0 and short.fees_pct == pytest.approx(0.001)
    assert short.slippage_pct == pytest.approx(2 * 4e-4)


# ------------------------------------------------------------ techo diario


def test_daily_cap_sixth_always_rejected():
    st = state()
    add_entries(st, 5)
    assert not eng().slot_check(st, NOW, edge_ratio=100, ai_confidence=1.0).ok


def test_fourth_and_fifth_need_edge_and_confidence():
    st = state()
    add_entries(st, 3)
    e = eng()
    assert not e.slot_check(st, NOW, edge_ratio=3.5).ok
    assert e.slot_check(st, NOW, edge_ratio=4.0).ok
    assert not e.slot_check(st, NOW, edge_ratio=4.0, ai_confidence=0.74).ok
    assert e.slot_check(st, NOW, edge_ratio=4.0, ai_confidence=0.75).ok
    add_entries(st, 1, when=NOW - timedelta(minutes=10))
    assert e.slot_check(st, NOW, edge_ratio=5, ai_confidence=0.9).ok  # 5.ª


def test_first_three_normal_filters():
    st = state()
    add_entries(st, 2)
    assert eng().slot_check(st, NOW, edge_ratio=3.0, ai_confidence=0.3).ok


def test_cap_drops_to_three_after_two_losses_or_minus_one_pct():
    st = state()
    add_entries(st, 3)
    for i in range(2):
        st.record_close(CloseRecord(symbol="X", side=Side.LONG, closed_at=NOW - timedelta(minutes=i), pnl_net=-1))
    assert eng().daily_cap(st, NOW) == 3
    assert not eng().slot_check(st, NOW, edge_ratio=10, ai_confidence=1).ok
    st2 = state()
    st2.update_equity(9_890)  # −1.1 % en el día
    assert eng().daily_cap(st2, NOW) == 3


def test_counter_resets_at_utc_midnight():
    st = state()
    late = datetime(2026, 10, 15, 23, 59, tzinfo=UTC)
    nxt = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    st.entries = [EntryRecord(symbol="A", side=Side.LONG, ts=late - timedelta(minutes=i)) for i in range(5)]
    assert not eng().slot_check(st, late, edge_ratio=10).ok
    assert eng().slot_check(st, nxt, edge_ratio=3).ok


# ------------------------------------------------------------ guardias


def test_guards_daily_loss_drawdown_streak():
    e = eng()
    st = state()
    st.update_equity(9_790)
    assert not e.global_guard(st, NOW).ok  # −2.1 % en el día
    st = state()
    st.peak_equity = 12_000
    st.update_equity(10_700)  # dd 10.8 %
    e.update_guards(st, NOW)
    assert st.halted and not e.global_guard(st, NOW + timedelta(days=3)).ok
    st = state()
    for i in range(4):
        st.record_close(CloseRecord(symbol="X", side=Side.LONG, closed_at=NOW - timedelta(minutes=10 - i), pnl_net=-1))
    e.update_guards(st, NOW)
    assert not e.global_guard(st, NOW + timedelta(hours=3)).ok
    st.day_start_equity = st.equity
    assert e.global_guard(st, NOW + timedelta(hours=4, minutes=1)).ok
    st.safe_mode, st.safe_mode_reason = True, "discrepancia"
    assert not e.global_guard(st, NOW + timedelta(hours=5)).ok


def test_symbol_limits_and_cooldowns():
    e = eng()
    st = state()
    assert not e.symbol_limits(st, "ETHUSDT", Side.LONG, NOW, [OpenExposure("ETHUSDT", Side.LONG, 1000)]).ok
    two = [OpenExposure("A", Side.LONG, 1), OpenExposure("B", Side.LONG, 1)]
    assert not e.symbol_limits(st, "ETHUSDT", Side.LONG, NOW, two).ok
    st.closes.append(CloseRecord(symbol="ETHUSDT", side=Side.LONG, closed_at=NOW - timedelta(minutes=30), pnl_net=1))
    assert not e.symbol_limits(st, "ETHUSDT", Side.LONG, NOW, []).ok
    assert e.symbol_limits(st, "ETHUSDT", Side.LONG, NOW + timedelta(minutes=31), []).ok
    assert not e.symbol_limits(st, "ETHUSDT", Side.SHORT, NOW + timedelta(minutes=31), []).ok  # reversa: 4 h
    assert e.symbol_limits(st, "ETHUSDT", Side.SHORT, NOW + timedelta(hours=3, minutes=31), []).ok
    st2 = state()
    st2.entries = [EntryRecord(symbol="ETHUSDT", side=Side.LONG, ts=NOW - timedelta(hours=h)) for h in (5, 6)]
    assert not e.symbol_limits(st2, "ETHUSDT", Side.LONG, NOW, []).ok


# ------------------------------------------------------------ tamaño


def _size(s, equity=10_000.0, mult=1.0, volp=50.0, pos=(), depth=1e9, corr=None, rules=RULES):
    return eng().size(s, equity, mult, volp, list(pos), corr or {}, rules, depth, True, 5)


def test_size_formula_and_ai_only_reduces():
    r = _size(sig(entry=100, stop=99))  # 1 % stop -> 0.5 % riesgo = 5 000 nocional
    assert r.ok and r.notional == pytest.approx(5_000, rel=1e-3)
    assert _size(sig(), mult=0.5).notional == pytest.approx(2_500, rel=1e-3)
    assert not _size(sig(), mult=1.5).ok
    assert not _size(sig(), mult=0.4).ok


def test_size_extremes():
    tiny_stop = _size(sig(entry=100, stop=99.99))  # stop 0.01 % -> nocional 500 000 -> topes
    assert tiny_stop.ok and tiny_stop.notional <= 3 * 10_000 + 1e-6
    assert tiny_stop.notional <= 1.5 * 10_000 + 1e-6  # exposición neta
    assert _size(sig(), volp=95).factors["vol"] == 0.5
    assert _size(sig(), volp=None).factors["vol"] == 0.5
    small = _size(sig(entry=100, stop=99), equity=5.0)  # capital pequeño -> bajo mínimo
    assert not small.ok and "mínimo" in small.reason
    assert not _size(sig(), depth=None).ok
    assert _size(sig(), depth=10_000).notional <= 2_000 + 1e-6
    corr = _size(sig(), pos=[OpenExposure("BTCUSDT", Side.LONG, 1000)], corr={"BTCUSDT": 0.9})
    assert corr.factors["corr"] == 0.5
    lowfee = _size(sig(move=0.0005))  # objetivo minúsculo: comisión > 25 %
    assert not lowfee.ok and "comisión" in lowfee.reason


def test_rounding():
    assert round_step(1.23456, 0.001) == pytest.approx(1.234)
    assert round_tick(100.004, 0.01) == 100.0
    assert round_tick(100.001, 0.01, side_up=True) == 100.01


# ------------------------------------------------------------ filtros


def ok(v):
    return DataPoint.ok(v, "t", NOW)


def fields(**over) -> dict:
    base = {
        "market.price": ok(100.0), "market.mark": ok(100.0), "market.spread_bps": ok(1.0),
        "market.depth_notional": ok(1e7), "funding.rate_1h": ok(1e-5), "deriv.oi": ok(1e9),
        "ind.funding_z": ok(0.5), "ind.oi_at_30d_high": ok(0.0), "ind.vol_pct_30d": ok(50.0),
    }
    base.update({k: (ok(v) if v is not None else DataPoint.unknown("t")) for k, v in over.items()})
    return base


def test_data_filter_blocks_unknown_and_stale():
    assert check_data(fields()).ok
    assert not check_data(fields(**{"deriv.oi": None})).ok
    f = fields()
    f["market.price"] = f["market.price"].with_ttl(NOW + timedelta(seconds=10), 5)
    assert not check_data(f).ok


def test_macro_filter_edges_and_unknown_calendar():
    ev = [MacroEvent(title="CPI", ts=NOW, source="t")]
    assert not check_macro(NOW - timedelta(minutes=30), ev, S.filters, True, NOW).ok
    assert check_macro(NOW - timedelta(minutes=31), ev, S.filters, True, NOW).ok
    assert not check_macro(NOW + timedelta(minutes=60), ev, S.filters, True, NOW).ok
    assert check_macro(NOW + timedelta(minutes=61), ev, S.filters, True, NOW).ok
    assert not check_macro(NOW, [], S.filters, False, None).ok


def test_leverage_vol_liquidity_filters():
    f = fields(**{"ind.funding_z": 3.0, "ind.oi_at_30d_high": 1.0})
    assert not check_dangerous_leverage(sig(Side.LONG), f, S.filters).ok
    assert check_dangerous_leverage(sig(Side.SHORT, stop=101), f, S.filters).ok
    assert not check_dangerous_leverage(sig(), fields(**{"ind.funding_z": None}), S.filters).ok
    assert not check_extreme_vol(fields(**{"ind.vol_pct_30d": 99.0}), S.filters).ok
    assert check_extreme_vol(fields(**{"ind.vol_pct_30d": 98.0}), S.filters).ok
    assert not check_liquidity(fields(**{"market.spread_bps": 10.0}), 1000, S.filters).ok
    assert not check_liquidity(fields(**{"market.depth_notional": 4000.0}), 1000, S.filters).ok


def test_run_filters_all_pass_and_selection():
    st = state()
    out = run_filters(sig(move=0.02, cost=0.002), fields(), NOW, st, eng(), S.risk, S.filters, [], True, NOW, [])
    assert out.passed, out.reasons
    low_edge = run_filters(sig(move=0.004, cost=0.002), fields(), NOW, st, eng(), S.risk, S.filters, [], True, NOW, [])
    assert not low_edge.passed and any("ventaja" in r for r in low_edge.reasons)
    a, b = sig(move=0.02, q=0.7, symbol="A"), sig(move=0.02, q=0.9, symbol="B")
    best, rest = select_best([a, b])
    assert best is b and rest == [a]


# ------------------------------------------------------------ estrategias


def strat_fields(**over):
    base = {"ind.15m.close": 100.0, "ind.15m.ema20": 99.9, "ind.15m.ema50": 99.0, "ind.15m.atr": 1.0,
            "ind.1h.adx": 30.0, "ind.15m.cvd_ratio": 0.1, "ind.1h.structure": 1.0, "ind.15m.high_n": 99.5,
            "ind.15m.low_n": 97.0, "ind.15m.rel_volume": 2.0, "ind.oi_chg_1h": 0.01, "ind.funding_z": 0.0,
            "ind.fund_diff_z": 0.0, "sent.fng": 50.0, "ind.15m.structure": 0.0, "market.spread_bps": 1.0,
            "funding.rate_1h": 1e-5}
    base.update(over)
    return {k: (ok(v) if v is not None else DataPoint.unknown("t")) for k, v in base.items()}


def test_trend_pullback_long_and_short():
    p = TP_PARAMS
    s = trend_pullback("ETHUSDT", strat_fields(), p, S.exchange.fees, NOW, Regime.TREND_UP, 1.0)
    assert s and s.side is Side.LONG and s.stop == pytest.approx(100 - 1.2) and s.invalidation_price == 99.0
    assert s.stop_distance_pct >= 0.01  # ≥ 1 ATR
    f = strat_fields(**{"ind.15m.close": 100.0, "ind.15m.ema20": 100.1, "ind.15m.ema50": 101.0,
                        "ind.15m.cvd_ratio": -0.1, "ind.1h.structure": -1.0})
    s2 = trend_pullback("ETHUSDT", f, p, S.exchange.fees, NOW, Regime.TREND_DOWN, 1.0)
    assert s2 and s2.side is Side.SHORT
    assert trend_pullback("ETHUSDT", strat_fields(), p, S.exchange.fees, NOW, Regime.RANGE, 1.0) is None
    assert trend_pullback("ETHUSDT", strat_fields(**{"ind.15m.cvd_ratio": None}), p, S.exchange.fees, NOW,
                          Regime.TREND_UP, 1.0) is None


def test_range_breakout_requires_oi_rising():
    p = S.strategies["range_breakout"]
    s = range_breakout("BTCUSDT", strat_fields(), p, S.exchange.fees, NOW, Regime.RANGE, 1.0)
    assert s and s.side is Side.LONG and s.urgent and s.invalidation_price == 99.5
    assert range_breakout("BTCUSDT", strat_fields(**{"ind.oi_chg_1h": -0.01}), p, S.exchange.fees, NOW,
                          Regime.RANGE, 1.0) is None
    assert range_breakout("BTCUSDT", strat_fields(**{"ind.oi_chg_1h": None}), p, S.exchange.fees, NOW,
                          Regime.RANGE, 1.0) is None


def test_funding_reversion_short_on_greed():
    p = S.strategies["funding_reversion"]
    f = strat_fields(**{"ind.funding_z": 2.5, "ind.fund_diff_z": 1.5, "sent.fng": 85.0, "ind.15m.structure": -1.0,
                        "ind.15m.cvd_ratio": -0.2, "ind.15m.high_n": 101.0})
    s = funding_reversion("DOGEUSDT", f, p, S.exchange.fees, NOW, Regime.RANGE, 1.0)
    assert s and s.side is Side.SHORT
    f2 = strat_fields(**{"ind.funding_z": 2.5, "ind.fund_diff_z": 1.5, "sent.fng": 60.0})
    assert funding_reversion("DOGEUSDT", f2, p, S.exchange.fees, NOW, Regime.RANGE, 1.0) is None


def test_generate_uses_regime_field():
    f = strat_fields()
    f["ind.regime"] = DataPoint.ok(Regime.TREND_UP.value, "calc", NOW)
    names = {s.strategy for s in generate("ETHUSDT", f, {**S.strategies, "trend_pullback": TP_PARAMS}, S.exchange.fees,
                                          NOW, 1.0)}
    assert "trend_pullback" in names


def test_trend_breakout_follows_the_trend_only():
    p = S.strategies["trend_breakout"]
    up = strat_fields(**{"ind.15m.close": 100.0, "ind.15m.high_n": 99.5})
    s = trend_breakout("SOLUSDT", up, p, S.exchange.fees, NOW, Regime.TREND_UP, 1.0)
    assert s and s.side is Side.LONG and s.stop == pytest.approx(100 - p.stop_atr_multiple)
    assert s.take_profit == pytest.approx(100 + p.target_atr_multiple)
    assert trend_breakout("SOLUSDT", up, p, S.exchange.fees, NOW, Regime.TREND_DOWN, 1.0) is None  # contra tendencia
    assert trend_breakout("SOLUSDT", up, p, S.exchange.fees, NOW, Regime.RANGE, 1.0) is None
    assert trend_breakout("SOLUSDT", strat_fields(**{"ind.15m.rel_volume": 0.8}), p, S.exchange.fees, NOW,
                          Regime.TREND_UP, 1.0) is None  # sin volumen no hay ruptura
    dn = strat_fields(**{"ind.15m.close": 96.0, "ind.15m.low_n": 97.0})
    s2 = trend_breakout("SOLUSDT", dn, p, S.exchange.fees, NOW, Regime.TREND_DOWN, 1.0)
    assert s2 and s2.side is Side.SHORT and s2.edge_ratio >= 3
    assert trend_breakout("SOLUSDT", strat_fields(**{"ind.1h.adx": None}), p, S.exchange.fees, NOW,
                          Regime.TREND_UP, 1.0) is None  # dato desconocido: no hay señal


def test_underperformance_lock_pauses_a_losing_coin_side():
    risk = RiskEngine(S.risk, S.limits.model_copy(update={"underperf_enabled": True}), S.exchange)
    st = BotState.fresh(1000.0, NOW.date())
    for h in (6, 4, 2):  # tres pérdidas seguidas de SOL long que suman 1,5 % del capital
        st.closes.append(CloseRecord(symbol="SOLUSDT", side=Side.LONG, closed_at=NOW - timedelta(hours=h), pnl_net=-5.0))
    chk = risk.symbol_limits(st, "SOLUSDT", Side.LONG, NOW, [])
    assert not chk.ok and "bajo rendimiento" in chk.reason
    assert risk.symbol_limits(st, "SOLUSDT", Side.SHORT, NOW + timedelta(hours=5), []).ok  # la otra dirección sigue
    assert risk.symbol_limits(st, "SOLUSDT", Side.LONG, NOW + timedelta(hours=21), []).ok is False
    assert risk.symbol_limits(st, "SOLUSDT", Side.LONG, NOW + timedelta(hours=27), []).ok  # se levanta solo
    st.closes[-1] = CloseRecord(symbol="SOLUSDT", side=Side.LONG, closed_at=NOW - timedelta(hours=2), pnl_net=+8.0)
    assert risk.symbol_limits(st, "SOLUSDT", Side.LONG, NOW + timedelta(hours=2), []).ok  # ya no viene perdiendo
    st.closes[-1] = CloseRecord(symbol="SOLUSDT", side=Side.LONG, closed_at=NOW - timedelta(hours=2), pnl_net=-5.0)
    assert RiskEngine(S.risk, S.limits, S.exchange).symbol_limits(st, "SOLUSDT", Side.LONG, NOW, []).ok  # apagada
