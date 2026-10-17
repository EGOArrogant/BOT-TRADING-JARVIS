"""Pantalla de IC: encuentra un indicador que sí predice y descarta el ruido."""
import numpy as np

from bot.research.ic import screen, spearman


def test_screen_finds_signal_and_rejects_noise():
    rng = np.random.default_rng(0)
    n = 3000
    good = rng.normal(size=n)
    noise = rng.normal(size=n)
    fwd = 0.003 * good + rng.normal(0, 0.01, n)          # el indicador "good" explica parte del futuro
    rows = [{"ind.good": g, "ind.noise": z} for g, z in zip(good, noise)]
    res = {r["feature"]: r for r in screen(rows, list(fwd), ["A" if i % 2 else "B" for i in range(n)])}
    assert res["ind.good"]["significant"] and res["ind.good"]["ic"] > 0.2 and res["ind.good"]["consistent"]
    assert not res["ind.noise"]["significant"] and abs(res["ind.noise"]["ic"]) < 0.05
    assert res["ind.good"]["covers_costs"]
    assert abs(spearman(np.arange(10.0), np.arange(10.0) ** 3) - 1.0) < 1e-12
