"""Reflexión semanal: Claude analiza la semana y propone ≤ 3 hipótesis.

Las hipótesis se validan contra la lista blanca (learning/hypotheses.py); las
que intentan tocar riesgo, stops o ejecución se descartan y se registran.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import numpy as np
from pydantic import ValidationError

from bot.core.config import AIConfig
from bot.core.logging_setup import get_logger
from bot.learning import midas
from bot.learning.hypotheses import TUNABLE_PARAMS, Hypothesis
from bot.learning.journal import Journal
from bot.metrics import discipline, robustness
from bot.metrics.performance import ai_brier, ai_calibration, by_dimension, rejection_stats, summarize

log = get_logger("bot.reflection")

REFLECTION_SYSTEM = """Eres el analista cuantitativo que revisa una semana de un bot de trading de \
perpetuos cripto. Tu objetivo es mejorar la ganancia neta después de costos.

Propón como máximo 3 hipótesis concretas, medibles y reversibles. Cada una se probará en paralelo \
en papel (campeón vs retador) antes de aplicarse, así que no tienen que ser seguras: tienen que \
estar respaldadas por los datos que ves. Señala el tamaño de muestra: con n pequeño, sé prudente.
Revisa también "disciplina": si la IA rechaza oportunidades que habrían ganado, o decide distinto tras ganar que tras perder, es un sesgo a corregir con reglas medibles, no con más o menos cautela.
Usa "robustez_*": una esperanza cuyo intervalo de confianza incluye 0 todavía no es ventaja; si es frágil (se vuelve negativa con resultados un poco peores) o depende de 5 operaciones, dilo. El MAE/MFE indica si el objetivo está lejos o cerca del recorrido real (mfe_reach) y si las perdedoras llegaron a +1R antes de perder (gestión de salida): puedes proponer ajustar target_atr_multiple, nunca el stop.

Solo puedes proponer estos tipos (cualquier otro se descarta):
- min_edge: subir la ventaja mínima (entre 3 y 8) para una moneda y/o estrategia.
- block_condition: no operar cuando un campo (ind.*, sent.*, deriv.*, dex.*) es > o < un umbral.
- disable: desactivar una estrategia en una moneda y/o dirección.
- strategy_param: ajustar un parámetro de entrada/objetivo de una estrategia.

Nunca propongas cambios al riesgo por operación, apalancamiento, pérdidas máximas, stops ni \
ejecución: están fuera de tu alcance. Responde solo con el JSON del esquema."""

HYP_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "array", "items": {"type": "string"}},
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["min_edge", "block_condition", "disable", "strategy_param"]},
                    "description": {"type": "string"},
                    "symbol": {"type": "string"},
                    "strategy": {"type": "string"},
                    "side": {"type": "string", "enum": ["LONG", "SHORT"]},
                    "min_edge": {"type": "number"},
                    "field": {"type": "string"},
                    "op": {"type": "string", "enum": [">", "<"]},
                    "threshold": {"type": "number"},
                    "param": {"type": "string"},
                    "value": {"type": "number"},
                },
                "required": ["kind", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["observations", "hypotheses"],
    "additionalProperties": False,
}


def _feature_contrast(trades: list[dict], cands: dict[str, dict]) -> dict:
    """Top 10 % vs bottom 10 % por R: medias de los indicadores al entrar."""
    if len(trades) < 10:
        return {"nota": f"muestra insuficiente (n={len(trades)})"}
    ranked = sorted(trades, key=lambda t: float(t["r"]))
    k = max(1, len(ranked) // 10)
    out: dict[str, Any] = {"n_por_grupo": k}
    for name, group in (("peores", ranked[:k]), ("mejores", ranked[-k:])):
        feats: dict[str, list[float]] = {}
        for t in group:
            snap = (cands.get(t["signal_id"]) or {}).get("snapshot") or {}
            for f, v in snap.items():
                if isinstance(v, int | float) and f.startswith(("ind.", "sent.", "deriv.", "dex.")):
                    feats.setdefault(f, []).append(float(v))
        out[name] = {f: round(float(np.mean(v)), 5) for f, v in sorted(feats.items())}
    return out


def build_week_payload(journal: Journal, start: datetime, end: datetime, capital: float) -> dict:
    trades = journal.trades(start, end)
    cands_list = journal.candidates(start, end)
    cands = {c["id"]: c for c in cands_list}
    return {
        "periodo": [start.isoformat(), end.isoformat()],
        "resumen": summarize(trades, capital),
        "por_moneda": by_dimension(trades, "symbol", capital) if trades else {},
        "por_direccion": by_dimension(trades, "side", capital) if trades else {},
        "por_estrategia": by_dimension(trades, "strategy", capital) if trades else {},
        "por_regimen": by_dimension(trades, "regime", capital) if trades else {},
        "por_hora": by_dimension(trades, "hour", capital) if trades else {},
        "calibracion_ia": ai_calibration(trades),
        "brier_ia": ai_brier(trades),
        "candidatos": rejection_stats(cands_list),
        "contraste_top_bottom": _feature_contrast(trades, cands),
        "disciplina": discipline.evaluate(trades, cands_list, journal.events(start), capital)["checks"],
        # Estadística de robustez (operaciones y práctica): MAE/MFE para estudiar objetivos, rachas, Monte Carlo
        "robustez_operaciones": robustness.analyze(robustness.from_trades(trades), 0.005, 0.10),
        "robustez_practica": robustness.analyze(robustness.from_counterfactuals(cands_list), 0.005, 0.10),
        "parametros_ajustables": {k: sorted(v) for k, v in TUNABLE_PARAMS.items()},
        # Laboratorio MIDAS (solo simulación): las mismas señales con 20 US$ a x5–x25. Informa qué señales aguantan
        # apalancamiento (stops cortos, poca excursión en contra); nunca autoriza cambiar riesgo ni apalancamiento.
        "laboratorio_midas": _midas_brief(cands_list),
    }


def _midas_brief(cands: list[dict]) -> dict:
    sim = midas.simulate(cands, 20.0, 0.0007)
    return {scope: [{k: s[k] for k in ("leverage", "n", "wins", "liquidated", "total", "max_drawdown")}
                    for s in sim[scope]["by_leverage"]] for scope in midas.SCOPES} | {"lecciones": sim["lessons"]}


def validate_hypotheses(raw: list[dict], max_n: int = 3) -> tuple[list[Hypothesis], list[str]]:
    ok, rejected = [], []
    for item in raw[: max_n * 2]:
        clean = {k: v for k, v in item.items() if v is not None}
        try:
            ok.append(Hypothesis.model_validate(clean))
        except ValidationError as exc:
            rejected.append(f"{item.get('description', '?')[:80]}: {exc.errors()[0]['msg']}")
    return ok[:max_n], rejected


async def run_reflection(journal: Journal, cfg: AIConfig, start: datetime, end: datetime, capital: float,
                         client: anthropic.AsyncAnthropic | None = None, max_hypotheses: int = 3) -> dict:
    payload = build_week_payload(journal, start, end, capital)
    client = client or anthropic.AsyncAnthropic(timeout=120, max_retries=2)
    resp = await asyncio.wait_for(client.messages.create(
        model=cfg.reflection_model, max_tokens=8000,
        system=REFLECTION_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": HYP_SCHEMA}},
    ), timeout=300)
    if getattr(resp, "stop_reason", None) == "refusal":
        return {"payload": payload, "observations": [], "hypotheses": [], "rejected": ["rechazo del modelo"]}
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "{}")
    data = json.loads(text)
    hyps, rejected = validate_hypotheses(data.get("hypotheses", []), max_hypotheses)
    return {"payload": payload, "observations": data.get("observations", []), "hypotheses": hyps,
            "rejected": rejected}


def write_reflection_md(result: dict, reports_dir: Path, end: datetime) -> Path:
    iso = end.isocalendar()
    path = reports_dir / f"reflexion_{iso.year}-W{iso.week:02d}.md"
    res = result["payload"]["resumen"]
    lines = [f"# Reflexión semanal {iso.year}-W{iso.week:02d}", "",
             f"Operaciones: n={res.get('n', 0)} · PnL neto {res.get('pnl_net', 0):+.2f} · "
             f"expectativa {res.get('expectancy_r', 0):+.3f}R", "", "## Observaciones"]
    lines += [f"- {o}" for o in result["observations"]] or ["- (ninguna)"]
    lines += ["", "## Hipótesis (a prueba en campeón/retador)"]
    lines += [f"- `{h.id}` {h.description} — `{h.model_dump_json(exclude_none=True)}`" for h in result["hypotheses"]] \
        or ["- (ninguna)"]
    if result["rejected"]:
        lines += ["", "## Propuestas descartadas por la lista blanca"] + [f"- {r}" for r in result["rejected"]]
    reports_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def last_week(now: datetime) -> tuple[datetime, datetime]:
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return end - timedelta(days=7), end
