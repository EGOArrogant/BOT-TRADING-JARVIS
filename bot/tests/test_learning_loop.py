import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from bot.core.config import load_settings
from bot.core.models import DataPoint, Regime, Side, Signal
from bot.learning.champion_challenger import PENDING_APPROVAL, PROMOTED, ChampionChallenger, r_drawdown
from bot.learning.hypotheses import Hypothesis, effective_strategies, load_overrides, overrides_allow
from bot.learning.journal import NOT_SELECTED, TRADED, Journal
from bot.learning.lessons import LessonBook
from bot.learning.weekly_reflection import run_reflection, validate_hypotheses, write_reflection_md
from bot.tests.test_learning_metrics import mk_trade

S = load_settings()
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def sig(i, sym="SOLUSDT", side=Side.LONG, edge=4.0, strategy="trend_pullback"):
    return Signal(id=f"c{i}", symbol=sym, strategy=strategy, side=side, entry=100,
                  stop=99 if side is Side.LONG else 101, take_profit=103 if side is Side.LONG else 97,
                  expected_move_pct=0.001 * edge, total_cost_pct=0.001, data_quality=1, regime=Regime.RANGE,
                  created_at=NOW + timedelta(minutes=15 * i))


# ------------------------------------------------------------ lista blanca


@pytest.mark.parametrize("bad", [
    {"kind": "min_edge", "description": "bajar ventaja", "min_edge": 2.0},
    {"kind": "strategy_param", "description": "stop más corto", "strategy": "trend_pullback",
     "param": "stop_atr_multiple", "value": 0.5},
    {"kind": "strategy_param", "description": "subir riesgo", "strategy": "trend_pullback",
     "param": "risk_per_trade", "value": 0.02},
    {"kind": "block_condition", "description": "campo raro", "field": "risk.leverage", "op": ">", "threshold": 1},
    {"kind": "leverage", "description": "apalancar más"},
])
def test_whitelist_rejects_risk_and_stop_changes(bad):
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(bad)


def test_hypothesis_semantics():
    f = {"ind.fund_diff_z": DataPoint.ok(2.5, "t", NOW)}
    block = Hypothesis(kind="block_condition", description="no SOL long con diferencial >2σ", symbol="SOLUSDT",
                       side="LONG", field="ind.fund_diff_z", op=">", threshold=2.0)
    assert not block.allows(sig(1), f)
    assert block.allows(sig(1, side=Side.SHORT), f) and block.allows(sig(1, sym="BTCUSDT"), f)
    assert block.allows(sig(1), {})  # sin dato no bloquea
    edge = Hypothesis(kind="min_edge", description="DOGE más exigente", symbol="DOGEUSDT", min_edge=3.5)
    assert not edge.allows(sig(1, sym="DOGEUSDT", edge=3.2), {}) and edge.allows(sig(1, sym="DOGEUSDT", edge=3.6), {})
    p = Hypothesis(kind="strategy_param", description="objetivo más largo", strategy="trend_pullback",
                   param="target_atr_multiple", value=3.0)
    eff = effective_strategies(S.strategies, [p])
    base = S.strategies["trend_pullback"].target_atr_multiple
    assert eff["trend_pullback"].target_atr_multiple == 3.0 and S.strategies["trend_pullback"].target_atr_multiple == base
    ok, why = overrides_allow([block], sig(1), f)
    assert not ok and "aprendido" in why


# ------------------------------------------------------------ campeón / retador


def test_champion_challenger_promotion_requires_sample_and_approval(tmp_path):
    j = Journal(":memory:")
    ov_path = tmp_path / "ov.yaml"
    cc = ChampionChallenger(j, min_cases=50, min_trades=20, require_approval=True, reports_dir=tmp_path,
                            overrides_path=ov_path)
    h = Hypothesis(kind="block_condition", description="no operar con funding z alto", field="ind.funding_z",
                   op=">", threshold=2.0)
    hid = cc.add(h, NOW - timedelta(hours=1))
    for i in range(80):
        bad = i % 3 == 0  # 1/3 de los casos tienen funding alto y pierden
        s = sig(i)
        f = {"ind.funding_z": DataPoint.ok(3.0 if bad else 0.0, "t", NOW)}
        r = -1.0 if bad else 0.6
        traded = i % 2 == 0
        j.record_candidate(s, TRADED if traded else NOT_SELECTED, [], {"ind.funding_z": 3.0 if bad else 0.0})
        if traded:
            t = mk_trade(i, r * 1.0 + 0.08 if r > 0 else r + 0.08)
            j.record_trade(t.model_copy(update={"signal_id": s.id}))
        else:
            j.set_counterfactual(s.id, {"r_net": r})
        cc.record_shadow(h, s, f)
    for case in cc.pending_shadow():
        c = [x for x in j.candidates(NOW - timedelta(days=1)) if x["id"] == case["signal_id"]][0]
        cc.resolve_shadow(case["hyp_id"], case["signal_id"], (c.get("counterfactual") or {}).get("r_net", 0.0))
    ev = cc.evaluate(hid)
    assert ev.n_cases >= 50 and ev.n_trades >= 20 and ev.better
    assert ev.challenger_exp > ev.champion_exp and ev.challenger_dd <= ev.champion_dd
    cc.review(NOW)
    assert cc.status(hid) == PENDING_APPROVAL and not ov_path.exists()
    assert "PENDIENTE" in (tmp_path / "cambios.md").read_text()
    cc.promote(hid, NOW)
    assert cc.status(hid) == PROMOTED and load_overrides(ov_path)[0].id == hid


def test_small_sample_never_promotes():
    j = Journal(":memory:")
    cc = ChampionChallenger(j, require_approval=False)
    h = Hypothesis(kind="disable", description="sin breakout en XLM", strategy="range_breakout", symbol="XLMUSDT")
    hid = cc.add(h, NOW)
    cc.record_shadow(h, sig(1), {})
    cc.resolve_shadow(hid, "c1", 5.0)
    assert not cc.evaluate(hid).eligible and cc.review(NOW) == []
    assert r_drawdown([1, -2, 1, -1]) == pytest.approx(2.0)


# ------------------------------------------------------------ lecciones


def test_lessons_expire_and_cap():
    j = Journal(":memory:")
    lb = LessonBook(j, expiry_days=30, max_active=10)
    for i in range(12):
        h = Hypothesis(kind="min_edge", description=f"lección número {i}", symbol=f"S{i}", min_edge=3.5)
        lb.add_from_hypothesis(h, {"n_cases": 60, "challenger_exp": 0.2, "champion_exp": 0.1},
                               NOW - timedelta(days=40 if i < 3 else 1))
    assert len(lb.active()) == 10
    assert len(lb.expire_stale(NOW)) == 3
    assert len(lb.active()) == 9 and "n=60" in lb.active()[0]["leccion"]


# ------------------------------------------------------------ reflexión semanal


def test_reflection_filters_forbidden_proposals(tmp_path):
    out = {"observations": ["DOGE pierde por costos"], "hypotheses": [
        {"kind": "min_edge", "description": "subir ventaja en DOGE", "symbol": "DOGEUSDT", "min_edge": 3.5},
        {"kind": "strategy_param", "description": "stop más corto", "strategy": "trend_pullback",
         "param": "stop_atr_multiple", "value": 0.8},
        {"kind": "disable", "description": "sin breakout XLM", "strategy": "range_breakout", "symbol": "XLMUSDT"},
        {"kind": "min_edge", "description": "cuarta", "min_edge": 4}, {"kind": "min_edge", "description": "quinta", "min_edge": 4}]}

    class M:
        async def create(self, **kw):
            assert kw["model"] == "claude-sonnet-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out))], stop_reason="end_turn")

    j = Journal(":memory:")
    res = asyncio.run(run_reflection(j, S.ai, NOW - timedelta(days=7), NOW, 10_000,
                                     client=SimpleNamespace(messages=M())))
    assert len(res["hypotheses"]) == 3
    assert all(h.param != "stop_atr_multiple" for h in res["hypotheses"])
    assert any("stop más corto" in r for r in res["rejected"])
    md = write_reflection_md(res, tmp_path, NOW).read_text()
    assert "descartadas" in md and "DOGE" in md
    hyps, rej = validate_hypotheses([{"kind": "min_edge", "description": "x", "min_edge": None}])
    assert hyps == [] and rej
