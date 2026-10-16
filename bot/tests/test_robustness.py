"""Estadística de robustez: casos con respuesta conocida."""
import pytest

from bot.metrics import robustness as rb


def rows(rs, mae=None, mfe=None):
    return [{"r": r, "mae_r": (mae[i] if mae else None), "mfe_r": (mfe[i] if mfe else None)} for i, r in enumerate(rs)]


def test_wilson_matches_textbook():
    lo, hi = rb.wilson(30, 50)
    assert lo == pytest.approx(0.462, abs=0.005) and hi == pytest.approx(0.724, abs=0.005)
    lo2, hi2 = rb.wilson(300, 500)
    assert hi2 - lo2 < (hi - lo) / 2.5  # 10 veces más muestra: intervalo mucho más estrecho


def test_expectancy_examples_from_video():
    assert rb.expectancy(0.45, 2, 1) == pytest.approx(0.35)
    assert rb.expectancy(0.60, 0.8, 1) == pytest.approx(0.08)
    assert rb.expectancy(0.35, 3, 1) == pytest.approx(0.40)


def test_real_edge_is_confirmed_and_kelly():
    a = rb.analyze(rows([2.0, -1.0] * 100), 0.005, 0.10)
    assert a["edge"] == "confirmada" and a["expectancy_r"] == pytest.approx(0.5)
    assert a["expectancy_ci"][0] > 0 and not a["fragile"]
    assert a["kelly"] == pytest.approx(0.25) and a["kelly_fraction_used"] == pytest.approx(0.02)
    assert a["mc_prob_halt"] < 0.05


def test_coin_flip_is_not_an_edge_and_small_sample_is_uncertain():
    a = rb.analyze(rows([1.0, -1.0] * 50), 0.005, 0.10)
    assert a["edge"] == "incierta" and a["expectancy_ci"][0] < 0 < a["expectancy_ci"][1]
    lucky = rb.analyze(rows([1, 1, -1, 1, 1, -1, 1, -1, 1, 1]), 0.005, 0.10)  # 70 % en 10: suerte posible
    assert lucky["edge"] == "incierta"
    assert rb.analyze(rows([-1.0, -1.0, 0.5] * 40), 0.005, 0.10)["edge"] == "negativa"


def test_fragile_edge_and_dependence_on_few_trades():
    a = rb.analyze(rows([1.0, -1.0] * 50 + [1.05]), 0.005, 0.10)
    assert a["expectancy_r"] > 0 and a["fragile"]
    big = rb.analyze(rows([-1.0] * 40 + [9.0] * 5), 0.005, 0.10)
    assert big["expectancy_r"] > 0 and big["tails"]["sin_5_mejores"] < 0 and big["skew"] > 0.5


def test_streaks_and_monte_carlo_depend_on_risk():
    rs = [1.5, -1.0] * 60
    calm = rb.analyze(rows(rs), 0.005, 0.10)
    wild = rb.analyze(rows(rs), 0.05, 0.10)
    assert calm["streak_reasonable"] >= calm["streak_worst_seen"] == 1  # lo visto no es el límite
    assert wild["mc_dd_p95"] > calm["mc_dd_p95"] * 5 and wild["mc_prob_halt"] > 0.3 > calm["mc_prob_halt"]
    assert wild["streak_cost_pct"] == pytest.approx(1 - 0.95 ** wild["streak_reasonable"])
    # la volatilidad "roba" crecimiento: con más riesgo, geométrico < aritmético
    assert wild["growth_geom_pct"] < wild["growth_arith_pct"]


def test_mae_mfe_summaries():
    rs = [1.0] * 10 + [-1.0] * 10
    mae = [-0.1] * 8 + [-0.9] * 2 + [-1.0] * 10
    mfe = [2.1] * 5 + [1.0] * 5 + [1.2] * 4 + [0.2] * 6
    a = rb.analyze(rows(rs, mae, mfe), 0.005, 0.10)
    assert a["mae_winners"]["p80"] == pytest.approx(-0.1, abs=0.2)
    assert a["mfe_reach"]["1R"] == pytest.approx(14 / 20) and a["mfe_reach"]["2R"] == pytest.approx(5 / 20)
    assert a["losers_were_up_1r"] == pytest.approx(0.4)


def test_deterministic_and_needs_minimum():
    assert rb.analyze(rows([1, -1, 1]), 0.005, 0.1) == {"n": 3, "enough": False}
    r = rows([1.2, -1, 0.4, -1, 2.0, -0.5, 1.1] * 5)
    assert rb.analyze(r, 0.005, 0.1) == rb.analyze(r, 0.005, 0.1)


def test_counterfactual_rows_skip_traded():
    c = [{"ts": "2", "status": "rechazado_ia", "counterfactual": {"r_net": 1.0, "mae_r": -0.2, "mfe_r": 1.5}},
         {"ts": "1", "status": "operado", "counterfactual": {"r_net": 5.0}},
         {"ts": "3", "status": "rechazado_filtro", "counterfactual": None}]
    assert rb.from_counterfactuals(c) == [{"r": 1.0, "mae_r": -0.2, "mfe_r": 1.5}]
