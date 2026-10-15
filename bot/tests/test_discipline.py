"""Disciplina: cada sesgo "emocional" se detecta con datos y solo con muestra suficiente."""
from datetime import UTC, datetime, timedelta

from bot.metrics import discipline as d

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def trade(i, pnl, r, exit_reason="stop"):
    ts = (T0 + timedelta(hours=2 * i)).isoformat()
    return {"closed_at": ts, "pnl_net": pnl, "r": r, "exit_reason": exit_reason}


def cand(i, status, cf=None, action=None, minutes=30):
    return {"ts": (T0 + timedelta(hours=2 * i, minutes=minutes)).isoformat(), "status": status,
            "counterfactual": {"r_net": cf} if cf is not None else None,
            "ai": {"action": action} if action else None}


def by_key(res):
    return {c["key"]: c for c in res["checks"]}


def test_no_data_means_no_opinion():
    res = d.evaluate([], [], [], 2000)
    assert res["judged"] == 0 and all(c["state"] == "sin_datos" for c in res["checks"])
    assert res["practice"] == {"n": 0}


def test_fear_rejecting_winners():
    trades = [trade(i, -2, -0.1) for i in range(12)]
    cands = [cand(i, "rechazado_ia", cf=0.6) for i in range(12)]
    c = by_key(d.evaluate(trades, cands, [], 2000))
    assert c["miedo"]["state"] == "alerta" and c["confianza"]["state"] == "ok"


def test_overconfidence_approving_losers():
    trades = [trade(i, -10, -0.6) for i in range(12)]
    cands = [cand(i, "rechazado_ia", cf=0.0) for i in range(12)]
    c = by_key(d.evaluate(trades, cands, [], 2000))
    assert c["confianza"]["state"] == "alerta" and c["miedo"]["state"] == "ok"


def test_decisions_change_after_losses():
    trades = [trade(i, -10 if i % 2 == 0 else 15, -1 if i % 2 == 0 else 1.5) for i in range(24)]
    # tras cada pérdida (i par) la IA rechaza; tras cada ganancia aprueba
    cands = [cand(i, "rechazado_ia" if i % 2 == 0 else "operado", action="NO_TRADE" if i % 2 == 0 else "LONG")
             for i in range(24)]
    assert by_key(d.evaluate(trades, cands, [], 2000))["tras_perder"]["state"] == "alerta"
    same = [cand(i, "operado", action="LONG") for i in range(24)]
    assert by_key(d.evaluate(trades, same, [], 2000))["tras_perder"]["state"] == "ok"


def test_size_grows_after_wins():
    rows = []
    for i in range(30):
        prev_win = i > 0 and rows[-1]["pnl_net"] > 0
        risk = 20.0 if prev_win else 10.0          # 1R en $: el doble tras ganar
        r = 1.5 if i % 2 else -1.0
        rows.append(trade(i, r * risk, r))
    assert by_key(d.evaluate(rows, [], [], 2000))["tamano"]["state"] == "alerta"
    steady = [trade(i, (1.5 if i % 2 else -1.0) * 10, 1.5 if i % 2 else -1.0) for i in range(30)]
    assert by_key(d.evaluate(steady, [], [], 2000))["tamano"]["state"] == "ok"


def test_early_exits_and_rules():
    trades = [trade(i, 1, 0.1, "ai_early_exit" if i < 5 else "take_profit") for i in range(12)]
    c = by_key(d.evaluate(trades, [], [{"kind": "entrada_sin_stop"}], 2000))
    assert c["paciencia"]["state"] == "alerta" and c["reglas"]["state"] == "alerta"


def test_practice_counts_simulated_non_traded():
    cands = [cand(0, "rechazado_filtro", cf=1.0), cand(1, "rechazado_ia", cf=-1.0),
             cand(2, "no_seleccionado", cf=0.5), cand(3, "operado", cf=2.0), cand(4, "rechazado_filtro")]
    p = d.evaluate([], cands, [], 2000)["practice"]
    assert p["n"] == 3 and abs(p["acierto"] - 2 / 3) < 1e-9 and abs(p["expectativa_r"] - 0.5 / 3) < 1e-9


def test_ai_prompt_has_discipline_rule():
    from bot.ai_analyst.prompt_builder import SYSTEM_PROMPT, build_user_prompt
    assert "mismo estándar" in SYSTEM_PROMPT and "recuperar" in SYSTEM_PROMPT
    import inspect
    # la IA no recibe el resultado del día ni el capital: no puede "sentir presión"
    assert "equity" not in inspect.signature(build_user_prompt).parameters
