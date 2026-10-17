"""Pruebas múltiples: que una variante "gane" por pura suerte después de probar muchas (Jansen, López de Prado).

- `deflated_sharpe`: probabilidad de que el Sharpe observado supere al mejor que daría el azar tras `n_trials`
  intentos (Bailey y López de Prado, 2014). Usa asimetría y curtosis de los resultados.
- `bh_fdr`: Benjamini-Hochberg; controla la proporción de falsos descubrimientos al comparar varias ideas a la vez.
- `welch_p`: p-valor unilateral de que la media de `a` supere a la de `b`.

Solo informan y endurecen decisiones (puertas de fase, promoción de retadores); nunca cambian el riesgo.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

EULER = 0.5772156649
_N = NormalDist()


def sharpe(rs: np.ndarray) -> float:
    sd = float(np.std(rs, ddof=1)) if len(rs) > 1 else 0.0
    return float(np.mean(rs)) / sd if sd > 0 else 0.0


def expected_max_sharpe(n_trials: int, sr_var: float) -> float:
    """Sharpe máximo esperado por azar entre `n_trials` estrategias sin ventaja."""
    if n_trials <= 1:
        return 0.0
    n = float(n_trials)
    return math.sqrt(max(sr_var, 0.0)) * ((1 - EULER) * _N.inv_cdf(1 - 1 / n) + EULER * _N.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(rs: list[float] | np.ndarray, n_trials: int, sr_var: float | None = None) -> dict[str, float]:
    a = np.asarray(rs, dtype=float)
    t = len(a)
    if t < 10:
        return {"n": t, "dsr": 0.0, "sr": 0.0, "sr0": 0.0}
    sr = sharpe(a)
    sd = float(np.std(a))
    skew = float(np.mean(((a - a.mean()) / sd) ** 3)) if sd > 0 else 0.0
    kurt = float(np.mean(((a - a.mean()) / sd) ** 4)) if sd > 0 else 3.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    est_var = max(denom, 1e-9) / (t - 1)  # varianza del estimador del Sharpe
    sr0 = expected_max_sharpe(n_trials, sr_var if sr_var is not None else est_var)
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(max(denom, 1e-9))
    return {"n": t, "dsr": float(_N.cdf(z)), "sr": sr, "sr0": sr0}


def bh_fdr(pvals: list[float], q: float = 0.10) -> list[bool]:
    """True = descubrimiento que sobrevive al control de falsos positivos."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    k_max = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            k_max = rank
    keep = set(order[:k_max])
    return [i in keep for i in range(m)]


def welch_p(a: list[float], b: list[float]) -> float:
    """p unilateral de media(a) > media(b) (aproximación normal; con pocos datos, p = 1)."""
    if len(a) < 5 or len(b) < 5:
        return 1.0
    va, vb = np.var(a, ddof=1) / len(a), np.var(b, ddof=1) / len(b)
    se = math.sqrt(va + vb)
    if se == 0:
        return 0.0 if np.mean(a) > np.mean(b) else 1.0
    return float(1 - _N.cdf((np.mean(a) - np.mean(b)) / se))
