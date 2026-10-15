"""Cliente del analista IA (Claude) con presupuesto, timeout y validación.

Cualquier falla -> NO_TRADE: timeout, error de red, rechazo de seguridad,
JSON inválido tras 1 reintento, o acción que contradice al candidato.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import anthropic

from bot.ai_analyst.prompt_builder import NEWS_SYSTEM, SYSTEM_PROMPT, build_news_prompt
from bot.ai_analyst.schema import DECISION_SCHEMA, NEWS_SCHEMA
from bot.core.config import AIConfig
from bot.core.logging_setup import get_logger
from bot.core.models import AIAction, Decision, Signal, parse_decision
from bot.data_engine.news import Headline

log = get_logger("bot.ai")

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class CallBudget:
    """Límite de llamadas por hora y por día (ventanas deslizantes)."""

    def __init__(self, per_hour: int, per_day: int):
        self.per_hour = per_hour
        self.per_day = per_day
        self._calls: deque[float] = deque()

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] > 86_400:
            self._calls.popleft()

    def available(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._trim(now)
        last_hour = sum(1 for t in self._calls if now - t <= 3600)
        return last_hour < self.per_hour and len(self._calls) < self.per_day

    def consume(self, now: float | None = None) -> None:
        self._calls.append(time.time() if now is None else now)


@dataclass
class AnalystResult:
    decision: Decision
    valid: bool                 # False = NO_TRADE forzado por falla
    reason: str
    raw: list[str] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


def _text_of(resp: Any) -> str:
    return next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")


class Analyst:
    def __init__(self, cfg: AIConfig, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.AsyncAnthropic(timeout=cfg.timeout_s, max_retries=1)
        self.budget = CallBudget(cfg.max_calls_per_hour, cfg.max_calls_per_day)
        self.news_budget = CallBudget(cfg.max_news_calls_per_hour, cfg.max_news_calls_per_hour * 24)

    async def _call(self, model: str, system: str, user: str, schema: dict, effort: str, max_tokens: int) -> Any:
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if self.cfg.refusal_fallback and model.startswith(("claude-sonnet-5-5", "claude-opus-5")):
            return await self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return await self.client.messages.create(**kwargs)

    async def decide(self, sig: Signal, user_prompt: str) -> AnalystResult:
        if not self.budget.available():
            return AnalystResult(Decision.no_trade("presupuesto de IA agotado"), False, "budget")
        raws: list[str] = []
        t0 = time.monotonic()
        tokens_in = tokens_out = 0
        model_used = None
        for attempt in range(1 + self.cfg.schema_retries):
            self.budget.consume()
            try:
                resp = await asyncio.wait_for(
                    self._call(self.cfg.decision_model, SYSTEM_PROMPT, user_prompt, DECISION_SCHEMA,
                               self.cfg.decision_effort, 4000),
                    timeout=self.cfg.timeout_s)
            except (TimeoutError, asyncio.TimeoutError):
                return AnalystResult(Decision.no_trade("timeout IA"), False, "timeout", raws,
                                     latency_s=time.monotonic() - t0)
            except anthropic.APIConnectionError as exc:
                return AnalystResult(Decision.no_trade("error de red IA"), False, f"conexion: {exc}", raws)
            except anthropic.APIStatusError as exc:
                return AnalystResult(Decision.no_trade("error API IA"), False, f"api {exc.status_code}", raws)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                tokens_in += getattr(usage, "input_tokens", 0) or 0
                tokens_out += getattr(usage, "output_tokens", 0) or 0
            model_used = getattr(resp, "model", None)
            if getattr(resp, "stop_reason", None) == "refusal":
                return AnalystResult(Decision.no_trade("rechazo del modelo"), False, "refusal", raws, model_used,
                                     tokens_in, tokens_out, time.monotonic() - t0)
            text = _text_of(resp)
            raws.append(text)
            decision = parse_decision(text)
            if decision is None:
                log.warning("salida IA inválida", extra={"attempt": attempt, "raw": text[:300]})
                continue
            res = AnalystResult(decision, True, "ok", raws, model_used, tokens_in, tokens_out, time.monotonic() - t0)
            if decision.action is not AIAction.NO_TRADE and not decision.agrees_with(sig.side):
                res.decision = Decision.no_trade("IA contradice la dirección del candidato")
                res.reason = "contradiccion"
            return res
        return AnalystResult(Decision.no_trade("JSON inválido tras reintento"), False, "schema", raws, model_used,
                             tokens_in, tokens_out, time.monotonic() - t0)

    async def score_news(self, headlines: list[Headline]) -> list[Headline]:
        """Clasificación barata (Haiku). Si falla, se devuelven los titulares sin puntuación IA."""
        if not headlines or not self.news_budget.available():
            return headlines
        self.news_budget.consume()
        try:
            resp = await asyncio.wait_for(
                self._call(self.cfg.news_model, NEWS_SYSTEM, build_news_prompt(headlines), NEWS_SCHEMA,
                           self.cfg.news_effort, 2000),
                timeout=self.cfg.timeout_s)
            data = json.loads(_text_of(resp))
        except Exception as exc:  # noqa: BLE001
            log.warning("puntuación de noticias falló", extra={"err": repr(exc)})
            return headlines
        out = list(headlines)
        for item in data.get("items", []):
            i = item.get("index")
            if not isinstance(i, int) or not 0 <= i < len(out):
                continue
            rel = max(0.0, min(1.0, float(item.get("relevance", 0))))
            syms = [s for s in item.get("symbols", []) if isinstance(s, str)]
            out[i] = out[i].model_copy(update={
                "ai_relevance": rel, "ai_direction": item.get("direction"),
                "symbols": sorted(set(out[i].symbols) | set(syms)),
                "score": out[i].score * (0.5 + rel),
            })
        return sorted(out, key=lambda h: -h.score)
