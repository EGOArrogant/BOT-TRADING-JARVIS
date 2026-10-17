"""Pruebas múltiples: la suerte de probar muchas variantes no debe pasar por ventaja."""
import numpy as np

from bot.metrics.multiple_testing import bh_fdr, deflated_sharpe, expected_max_sharpe, welch_p


def test_deflated_sharpe_penalizes_many_trials():
    rng = np.random.default_rng(1)
    edge = rng.normal(0.4, 1.0, 300)        # ventaja real y clara
    luck = rng.normal(0.05, 1.0, 120)       # casi nada: el tipo de "ganador" que sale de probar 60 variantes
    assert deflated_sharpe(edge, 60)["dsr"] > 0.95
    assert deflated_sharpe(luck, 60)["dsr"] < 0.95
    assert deflated_sharpe(luck, 1)["dsr"] > deflated_sharpe(luck, 60)["dsr"]  # más intentos, más exigencia
    assert expected_max_sharpe(1, 0.01) == 0 and expected_max_sharpe(100, 0.01) > expected_max_sharpe(10, 0.01)
    assert deflated_sharpe([0.1] * 5, 60)["dsr"] == 0.0  # sin muestra no hay aprobación


def test_bh_fdr_and_welch():
    assert bh_fdr([0.001, 0.2, 0.03, 0.9], q=0.10) == [True, False, True, False]
    assert bh_fdr([0.04, 0.05, 0.06], q=0.10) == [True, True, True]
    assert bh_fdr([0.2, 0.3], q=0.10) == [False, False]
    rng = np.random.default_rng(2)
    a, b = rng.normal(0.3, 1, 200), rng.normal(0.0, 1, 200)
    assert welch_p(list(a), list(b)) < 0.01 and welch_p(list(b), list(a)) > 0.9
    assert welch_p([1, 2], [0, 0, 0, 0, 0]) == 1.0
