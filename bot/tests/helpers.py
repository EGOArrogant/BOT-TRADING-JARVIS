"""Generadores de datos sintéticos reproducibles para tests."""

from __future__ import annotations

import numpy as np

from bot.data_engine.market_state import INTERVAL_MS, Candle, MarketState


def make_candles(closes: np.ndarray, interval_ms: int, start_ms: int = 1_700_000_000_000 // 3_600_000 * 3_600_000,
                 spread: float = 0.002, seed: int = 0, buy_frac: float = 0.5) -> list[Candle]:
    rng = np.random.default_rng(seed)
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) * (1 + abs(rng.normal(0, spread / 2)))
        lo = min(o, c) * (1 - abs(rng.normal(0, spread / 2)))
        vol = float(abs(rng.normal(100, 20)))
        out.append(Candle(start_ms + i * interval_ms, float(o), float(hi), float(lo), float(c), vol, vol * c,
                          vol * buy_frac))
        prev = c
    return out


def trend_series(n: int, drift: float, vol: float = 0.003, seed: int = 0, start: float = 100.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(drift, vol, n)))


def fill_state(state: MarketState, closes_by_tf: dict[str, np.ndarray], end_ms: int | None = None,
               buy_frac: float = 0.5) -> None:
    for tf, closes in closes_by_tf.items():
        iv = INTERVAL_MS[tf]
        n = len(closes)
        end = end_ms if end_ms is not None else 1_760_000_000_000 // iv * iv
        for c in make_candles(closes, iv, start_ms=end - n * iv, buy_frac=buy_frac):
            state.candles[tf].add_closed(c)
