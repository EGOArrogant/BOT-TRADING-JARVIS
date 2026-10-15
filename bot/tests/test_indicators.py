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
