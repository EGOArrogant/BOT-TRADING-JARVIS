import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from bot.ai_analyst.client import Analyst, CallBudget
from bot.ai_analyst.prompt_builder import SYSTEM_PROMPT, build_user_prompt
from bot.core.config import load_settings
from bot.core.models import AIAction, DataPoint, Regime, Side, Signal
from bot.data_engine.news import Headline

S = load_settings()
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
SIG = Signal(id="s", symbol="ETHUSDT", strategy="trend_pullback", side=Side.LONG, entry=100, stop=99,
             take_profit=103, expected_move_pct=0.03, total_cost_pct=0.002, data_quality=1,
             regime=Regime.TREND_UP, created_at=NOW)
GOOD = {"action": "LONG", "confidence": 0.8, "size_multiplier": 0.9, "thesis": "Tendencia limpia.",
        "key_evidence": ["a"], "invalidation": "cierre bajo 99", "news_impact": "NONE", "risk_flags": []}


def resp(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5), model="m")


class Fake:
    def __init__(self, outputs, delay=0.0):
        self.outputs = list(outputs)
        self.calls = []
        self.delay = delay
        outer = self

        class M:
            async def create(self, **kw):
                outer.calls.append(kw)
                await asyncio.sleep(outer.delay)
                o = outer.outputs.pop(0)
                if isinstance(o, Exception):
                    raise o
                return o
        self.messages = M()
        self.beta = SimpleNamespace(messages=M())


def run(outputs, delay=0.0, cfg=None):
    fake = Fake(outputs, delay)
    a = Analyst(cfg or S.ai, client=fake)
    return asyncio.run(a.decide(SIG, "prompt")), fake


def test_valid_decision_uses_schema_cache_and_fallback():
    r, fake = run([resp(json.dumps(GOOD))])
    assert r.valid and r.decision.action is AIAction.LONG and r.decision.size_multiplier == 0.9
    kw = fake.calls[0]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]


def test_invalid_then_valid_retry():
    r, fake = run([resp("no json"), resp(json.dumps(GOOD))])
    assert r.valid and len(fake.calls) == 2


def test_invalid_twice_is_no_trade():
    r, _ = run([resp("{}"), resp(json.dumps({**GOOD, "size_multiplier": 2}))])
    assert not r.valid and r.decision.action is AIAction.NO_TRADE and r.reason == "schema"


def test_contradiction_refusal_timeout_errors():
    r, _ = run([resp(json.dumps({**GOOD, "action": "SHORT"}))])
    assert r.decision.action is AIAction.NO_TRADE and r.reason == "contradiccion"
    r, _ = run([resp("", stop="refusal")])
    assert r.decision.action is AIAction.NO_TRADE and r.reason == "refusal"
    cfg = S.ai.model_copy(update={"timeout_s": 0.05})
    r, _ = run([resp(json.dumps(GOOD))], delay=0.2, cfg=cfg)
    assert r.decision.action is AIAction.NO_TRADE and r.reason == "timeout"


def test_budget():
    b = CallBudget(per_hour=2, per_day=3)
    b.consume(0), b.consume(10)
    assert not b.available(20)
    assert b.available(3700)
    b.consume(3700)
    assert not b.available(3800)  # 3 en el día
    cfg = S.ai.model_copy(update={"max_calls_per_hour": 0})
    r, fake = run([], cfg=cfg)
    assert r.reason == "budget" and fake.calls == []


def test_prompt_marks_unknown_and_has_no_volatile_system():
    f = {"market.price": DataPoint.ok(100.0, "t", NOW), "vol.dvol": DataPoint.unknown("deribit")}
    p = build_user_prompt(SIG, f, [], [], [], [], {}, NOW)
    assert '"vol.dvol": "unknown"' in p and "ETHUSDT" in p
    assert "{" not in SYSTEM_PROMPT  # system estable, sin datos variables


def test_news_scoring_reorders_and_fails_safe():
    hs = [Headline(title="a", source="x", published=NOW, score=1.0),
          Headline(title="b", source="x", published=NOW, score=1.0)]
    out = {"items": [{"index": 1, "relevance": 1.0, "symbols": ["BTCUSDT"], "direction": "UP"},
                     {"index": 9, "relevance": 1.0, "symbols": [], "direction": "UP"}]}
    fake = Fake([resp(json.dumps(out))])
    a = Analyst(S.ai, client=fake)
    scored = asyncio.run(a.score_news(hs))
    assert scored[0].title == "b" and scored[0].ai_direction == "UP"
    assert fake.calls[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in fake.calls[0]
    broken = Analyst(S.ai, client=Fake([resp("xx")]))
    assert asyncio.run(broken.score_news(hs)) == hs


def test_review_exit_fails_safe_to_hold():
    a = Analyst(S.ai, client=Fake([resp(json.dumps({"action": "CLOSE", "reason": "noticia"}))]))
    assert asyncio.run(a.review_exit("p")) == (True, "noticia")
    for bad in (resp("roto"), resp("", stop="refusal"), RuntimeError("red")):
        a = Analyst(S.ai, client=Fake([bad]))
        close, _ = asyncio.run(a.review_exit("p"))
        assert close is False
