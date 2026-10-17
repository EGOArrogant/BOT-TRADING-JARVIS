from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from bot.core.models import DataStatus, Regime
from bot.data_engine.market_state import MarketState
from bot.data_engine.store import FeedStore
from bot.indicators import ta
from bot.indicators.features import compute_features, detect_regime
from bot.tests.helpers import fill_state, trend_series

NOW = datetime(2025, 10, 9, 8, 0, tzinfo=UTC)


def test_ema_matches_definition():
    x = np.arange(1, 11, dtype=float)
    e = ta.ema(x, 3)
    assert np.isnan(e[1]) and e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)


def test_atr_constant_range():
    n = 50
    c = np.full(n, 100.0)
    a = ta.atr(c + 1, c - 1, c, 14)
    assert a[-1] == pytest.approx(2.0)


def test_adx_high_in_trend_low_in_noise():
    up = np.linspace(100, 200, 200)
    assert ta.adx(up + 0.5, up - 0.5, up, 14)[-1] > 50
    rng = np.random.default_rng(1)
    noise = 100 + rng.normal(0, 0.5, 200)
    assert ta.adx(noise + 0.5, noise - 0.5, noise, 14)[-1] < 30


def test_zscore_percentile_structure():
    x = np.concatenate([np.random.default_rng(0).normal(0, 1, 100), [5.0]])
    assert ta.zscore_last(x) > 4
    assert ta.percentile_rank_last(x) == 100.0
    assert ta.zscore_last(np.ones(5)) is None
    h = np.array([1, 2, 3, 2, 1, 2, 4, 5, 4, 3, 4, 6, 7, 6, 5, 6, 8.0])
    assert ta.structure(h + 0.1, h - 0.1, swing=1) == 1
    assert ta.structure(-h + 0.1, -h - 0.1, swing=1) == -1


def test_vwap_and_correlation():
    t = np.array([86_400_000 * 10 + i * 60_000 for i in range(3)])
    v = ta.daily_vwap(t, np.array([2.0, 3, 4]), np.array([0.0, 1, 2]), np.array([1.0, 2, 3]), np.array([1.0, 1, 2]))
    assert v == pytest.approx((1 + 2 + 2 * 3) / 4)
    a = trend_series(200, 0, seed=3)
    assert ta.correlation(a, a) == pytest.approx(1.0)


def _store_with_derivs(sym: str) -> FeedStore:
    s = FeedStore()
    for i in range(100):
        s.set(f"deriv.funding_hist.{sym}", 0.0001 + 0.00001 * np.sin(i), "t", NOW - timedelta(hours=8 * (100 - i)))
    for i in range(48):
        s.set(f"deriv.oi.{sym}", 1e9 * (1 + i / 100), "t", NOW - timedelta(hours=48 - i))
    return s


def _trend_state(sym: str, drift: float) -> MarketState:
    st = MarketState(sym)
    end = int(NOW.timestamp() * 1000)
    fill_state(st, {
        "1m": trend_series(300, drift / 60, 0.0005, seed=1),
        "5m": trend_series(400, drift / 12, 0.001, seed=2),
        "15m": trend_series(400, drift / 4, 0.002, seed=3),
        "1h": trend_series(800, drift, 0.003, seed=4),
    }, end_ms=end)
    st.funding_rate = 0.0005
    st.mark_ms = end
    return st


def test_compute_features_uptrend_regime():
    st = _trend_state("ETHUSDT", 0.003)
    f = compute_features(st, _store_with_derivs("ETHUSDT"), NOW, btc_state=None)
    assert f["ind.1h.ema20"].is_ok and f["ind.15m.atr"].is_ok
    assert f["ind.funding_z"].is_ok and f["ind.funding_z"].value > 2       # 0.0005 vs ~0.0001
    assert f["ind.oi_chg_1h"].is_ok and f["ind.oi_at_30d_high"].value == 1.0
    assert f["ind.regime"].value == Regime.TREND_UP.value
    assert f["ind.dvol_chg_24h"].status is DataStatus.UNKNOWN  # sin DVOL cargado
    assert f["ind.corr_btc_24h"].status is DataStatus.UNKNOWN


def test_compute_features_downtrend_and_insufficient():
    f = compute_features(_trend_state("SOLUSDT", -0.003), FeedStore(), NOW)
    assert f["ind.regime"].value == Regime.TREND_DOWN.value
    assert f["ind.funding_z"].status is DataStatus.UNKNOWN  # sin historial
    empty = compute_features(MarketState("XLMUSDT"), FeedStore(), NOW)
    assert empty["ind.1h.ema20"].status is DataStatus.UNKNOWN
    assert detect_regime(empty) is Regime.INSUFFICIENT


def test_pivots_fibonacci_and_room_to_levels_without_lookahead():
    up1, dn, up2 = np.linspace(95, 110, 16), np.linspace(110, 100, 11)[1:], np.linspace(100, 106, 7)[1:]
    c = np.concatenate([up1, dn, up2])
    h, l = c + 0.5, c - 0.5
    highs, lows = ta.pivots(h, l, swing=3)
    assert highs[-1] == (15, pytest.approx(110.5)) and lows[-1] == (25, pytest.approx(99.5))
    assert len(c) - 1 - lows[-1][0] >= 3  # un giro necesita 3 velas cerradas después
    for cut in range(10, len(c)):  # cortar el futuro no cambia los giros ya confirmados
        hc, lc = ta.pivots(h[:cut], l[:cut], swing=3)
        assert hc == [x for x in highs if x[0] <= cut - 4] and lc == [x for x in lows if x[0] <= cut - 4]
    lv = ta.structure_levels(h, l, c, atr_last=2.0, swing=3)
    assert lv["fib_pos"] == pytest.approx((106 - 99.5) / (110.5 - 99.5))
    assert lv["room_up_atr"] == pytest.approx((110.5 - 106) / 2) and lv["room_down_atr"] == pytest.approx((106 - 99.5) / 2)
    c2 = np.concatenate([c, np.linspace(107, 118, 6)])  # máximos nuevos: ninguna resistencia arriba
    assert ta.structure_levels(c2 + 0.5, c2 - 0.5, c2, 2.0)["room_up_atr"] == ta.ROOM_CAP_ATR
    assert ta.structure_levels(h[:5], l[:5], c[:5], 2.0)["fib_pos"] is None  # pocas velas: desconocido


def test_structure_fields_reach_the_snapshot():
    f = compute_features(_trend_state("ETHUSDT", 0.003), FeedStore(), NOW)
    for k in ("swing_high", "swing_low", "fib_pos", "room_up_atr", "room_down_atr"):
        assert f"ind.1h.{k}" in f
    empty = compute_features(MarketState("XLMUSDT"), FeedStore(), NOW)
    assert empty["ind.1h.fib_pos"].status is DataStatus.UNKNOWN


def test_panel_levels_match_what_the_strategy_watches():
    from bot.dashboard import snapshot
    st = _trend_state("ETHUSDT", 0.003)
    lv = snapshot.levels(st)
    a15 = st.candles["15m"].arrays()
    assert lv["range_15m"]["high"] == a15["h"][-24:].max() and lv["range_15m"]["low"] == a15["l"][-24:].min()
    close = st.candles["1h"].arrays()["c"][-1]
    assert all(p > close for p in lv["res"]) and all(p < close for p in lv["sup"]) and len(lv["res"]) <= 3
    assert snapshot.levels(MarketState("XLMUSDT")) is None
