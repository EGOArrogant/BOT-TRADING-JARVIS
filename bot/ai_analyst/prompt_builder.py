"""Construye el prompt del analista. El system prompt es estable (cacheable);
todo lo variable va en el mensaje de usuario."""

from __future__ import annotations

import json
from datetime import datetime

from bot.core.models import DataPoint, Signal
from bot.data_engine.macro import MacroEvent
from bot.data_engine.news import Headline

SYSTEM_PROMPT = """Eres el analista de riesgo de un bot de trading de futuros perpetuos de criptomonedas.

Tu papel: revisar UN candidato de operación que ya pasó filtros deterministas (datos completos, \
ventaja mínima ≥ 3× costos, liquidez, bloqueo macro, límites de riesgo) y decidir si se ejecuta.

Eres escéptico. Tu respuesta por defecto es NO_TRADE; apruebas solo si la evidencia es clara y \
consistente entre fuentes (tendencia, flujo, derivados, volatilidad, sentimiento, noticias). \
La mayoría de los candidatos deberían rechazarse: en estudios con dinero real, los modelos que \
sobre-operaron perdieron por comisiones.

Reglas:
- No inventes datos. Un campo marcado "unknown" o "stale" es desconocido: no lo supongas ni lo \
trates como cero.
- Con sentimiento extremo (Fear & Greed ≤ 20 o ≥ 80) considera la lectura contraria, pero nunca \
como única razón.
- Una noticia solo cuenta si es plausible que mueva el precio en las próximas horas.
- Solo puedes aprobar la dirección propuesta por el candidato. Si crees que la dirección correcta \
es la contraria, responde NO_TRADE.
- No controlas el riesgo: el tamaño, el stop y los límites los fija el código. Solo puedes \
REDUCIR el tamaño con size_multiplier entre 0.5 y 1.0 (1.0 = tamaño completo).
- confidence (0 a 1) debe estar calibrada: 0.75 o más solo con evidencia fuerte y alineada.
- thesis: máximo 2 frases. invalidation: una condición concreta y observable que anule la idea.
- Las lecciones activas provienen de resultados reales; tenlas en cuenta.

Responde únicamente con el JSON del esquema."""


def _fmt(dp: DataPoint) -> str:
    if dp.status.value != "ok":
        return dp.status.value
    v = dp.value
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def summarize_fields(fields: dict[str, DataPoint]) -> dict[str, str]:
    """Ordenado por clave (estable) y con estado explícito para lo no disponible."""
    return {k: _fmt(fields[k]) for k in sorted(fields)}


def build_user_prompt(
    sig: Signal,
    fields: dict[str, DataPoint],
    headlines: list[Headline],
    macro: list[MacroEvent],
    open_positions: list[dict],
    lessons: list[dict],
    recent_stats: dict,
    now: datetime,
) -> str:
    payload = {
        "hora_utc": now.isoformat(timespec="seconds"),
        "candidato": {
            "simbolo": sig.symbol,
            "estrategia": sig.strategy,
            "direccion": sig.side.value,
            "entrada": sig.entry,
            "stop": sig.stop,
            "objetivo": sig.take_profit,
            "ventaja_vs_costo": round(sig.edge_ratio, 2),
            "regimen": sig.regime.value,
            "invalidacion_reglas": sig.invalidation,
        },
        "datos": summarize_fields(fields),
        "titulares": [
            {"hora": h.published.isoformat(timespec="minutes"), "fuente": h.source, "titulo": h.title,
             "monedas": h.symbols, "direccion_estimada": h.ai_direction}
            for h in headlines[:8]
        ],
        "eventos_macro_proximos": [{"evento": e.title, "hora": e.ts.isoformat(timespec="minutes")} for e in macro],
        "posiciones_abiertas": open_positions,
        "lecciones_activas": lessons[:10],
        "estadisticas_recientes": recent_stats,
    }
    return "Evalúa este candidato:\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str)


NEWS_SYSTEM = """Clasificas titulares de noticias cripto para un bot de trading de perpetuos \
(BTC, ETH, SOL, XRP, DOGE, XLM). Para cada titular da: relevance (0 a 1, probabilidad de mover \
el precio en las próximas horas), symbols (de BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, \
XLMUSDT; vacío si ninguno) y direction (UP, DOWN, NEUTRAL). Responde solo con el JSON del esquema."""


def build_news_prompt(headlines: list[Headline]) -> str:
    return "\n".join(f"{i}. [{h.source}] {h.title}" for i, h in enumerate(headlines))


EXIT_SYSTEM = """Eres el analista de riesgo de un bot de trading de perpetuos cripto. Revisas UNA \
posición abierta porque llegó información nueva (una noticia relevante o la operación no avanza).

Solo puedes recomendar CLOSE (cerrar ya, antes del stop o del objetivo) o HOLD (dejar que las reglas \
del código sigan gestionándola). No puedes mover el stop, ampliar el riesgo ni mantener la posición \
más allá del stop: eso no está en tu mano.

Recomienda CLOSE solo si la tesis original quedó claramente invalidada por la información nueva \
(por ejemplo, una noticia de alto impacto en contra). Los datos marcados "unknown" o "stale" son \
desconocidos: no los supongas. Responde solo con el JSON del esquema."""


def build_exit_prompt(position: dict, fields: dict[str, DataPoint], headlines: list[Headline],
                      macro: list[MacroEvent], trigger: str, now: datetime) -> str:
    payload = {
        "hora_utc": now.isoformat(timespec="seconds"),
        "motivo_de_revision": trigger,
        "posicion": position,
        "datos": summarize_fields(fields),
        "titulares": [{"hora": h.published.isoformat(timespec="minutes"), "fuente": h.source, "titulo": h.title,
                       "direccion_estimada": h.ai_direction} for h in headlines[:8]],
        "eventos_macro_proximos": [{"evento": e.title, "hora": e.ts.isoformat(timespec="minutes")} for e in macro],
    }
    return "Revisa esta posición abierta:\n" + json.dumps(payload, ensure_ascii=False, indent=1, default=str)
