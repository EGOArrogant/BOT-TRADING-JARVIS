"""Indicadores técnicos puros sobre arrays numpy (sin estado, testeables)."""

from __future__ import annotations

import math

import numpy as np


def ema(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < period:
        return out
    alpha = 2 / (period + 1)
    out[period - 1] = np.mean(x[:period])
    for i in range(period, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def _wilder(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < period:
        return out
    out[period - 1] = np.mean(x[:period])
    for i in range(period, len(x)):
        out[i] = (out[i - 1] * (period - 1) + x[i]) / period
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev = np.concatenate([[c[0]], c[:-1]])
    return np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    return _wilder(true_range(h, l, c), period)


def adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(c)
    out = np.full(n, np.nan)
    if n < 2 * period + 1:
        return out
    up = np.diff(h, prepend=h[0])
    down = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(h, l, c)
    atr_ = _wilder(tr[1:], period)
    with np.errstate(invalid="ignore", divide="ignore"):
        pdi = 100 * _wilder(plus_dm[1:], period) / atr_
        mdi = 100 * _wilder(minus_dm[1:], period) / atr_
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    dx = np.nan_to_num(dx, nan=0.0)
    valid = dx[period - 1:]
    adx_ = _wilder(valid, period)
    out[period:] = adx_
    return out


def bollinger_width(c: np.ndarray, period: int = 20, k: float = 2.0) -> float | None:
    if len(c) < period:
        return None
    w = c[-period:]
    mid = w.mean()
    return float(2 * k * w.std(ddof=0) / mid) if mid else None


def zscore_last(x: np.ndarray, min_n: int = 20) -> float | None:
    x = x[~np.isnan(x)]
    if len(x) < min_n:
        return None
    sd = x[:-1].std(ddof=1)
    if sd == 0 or math.isnan(sd):
        return None
    return float((x[-1] - x[:-1].mean()) / sd)


def percentile_rank_last(x: np.ndarray, min_n: int = 50) -> float | None:
    """Percentil (0-100) del último valor dentro de la serie (empates a la mitad)."""
    x = x[~np.isnan(x)]
    if len(x) < min_n:
        return None
    prev, last = x[:-1], x[-1]
    return float(100 * (np.mean(prev < last) + 0.5 * np.mean(prev == last)))


def parkinson_vol(h: np.ndarray, l: np.ndarray) -> np.ndarray:
    """Volatilidad por barra (Parkinson) a partir de máximo/mínimo."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.sqrt(np.log(h / l) ** 2 / (4 * math.log(2)))


def relative_volume(v: np.ndarray, period: int = 20) -> float | None:
    if len(v) < period + 1:
        return None
    base = v[-period - 1:-1].mean()
    return float(v[-1] / base) if base > 0 else None


def slope_per_bar(x: np.ndarray, n: int = 10) -> float | None:
    y = x[-n:]
    if len(y) < n or np.any(np.isnan(y)):
        return None
    return float(np.polyfit(np.arange(n), y, 1)[0])


def structure(h: np.ndarray, l: np.ndarray, swing: int = 3, lookback: int = 60) -> int:
    """+1 máximos y mínimos crecientes, −1 decrecientes, 0 mixto."""
    h, l = h[-lookback:], l[-lookback:]
    highs, lows = [], []
    for i in range(swing, len(h) - swing):
        if h[i] == h[i - swing:i + swing + 1].max():
            highs.append(h[i])
        if l[i] == l[i - swing:i + swing + 1].min():
            lows.append(l[i])
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return 1
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return -1
    return 0


def daily_vwap(t_ms: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray) -> float | None:
    if len(t_ms) == 0:
        return None
    day_start = (t_ms[-1] // 86_400_000) * 86_400_000
    m = t_ms >= day_start
    vol = v[m].sum()
    if vol <= 0:
        return None
    return float(((h[m] + l[m] + c[m]) / 3 * v[m]).sum() / vol)


def correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    n = min(len(a), len(b))
    if n < 30:
        return None
    ra, rb = np.diff(np.log(a[-n:])), np.diff(np.log(b[-n:]))
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])
