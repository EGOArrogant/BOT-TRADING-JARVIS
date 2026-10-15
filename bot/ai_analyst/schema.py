"""Esquema JSON de la salida del analista (para `output_config.format`).

La API garantiza JSON con esta forma; los rangos numéricos y las reglas de
negocio (size_multiplier en [0.5, 1.0], invalidación obligatoria al operar)
los valida después pydantic (`bot.core.models.Decision`).
"""

from __future__ import annotations

from bot.core.models import Decision, parse_decision

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["LONG", "SHORT", "NO_TRADE"]},
        "confidence": {"type": "number"},
        "size_multiplier": {"type": "number"},
        "thesis": {"type": "string"},
        "key_evidence": {"type": "array", "items": {"type": "string"}},
        "invalidation": {"type": "string"},
        "news_impact": {"type": "string", "enum": ["UP", "DOWN", "NEUTRAL", "NONE"]},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "confidence", "size_multiplier", "thesis", "key_evidence", "invalidation",
                 "news_impact", "risk_flags"],
    "additionalProperties": False,
}

NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevance": {"type": "number"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "direction": {"type": "string", "enum": ["UP", "DOWN", "NEUTRAL"]},
                },
                "required": ["index", "relevance", "symbols", "direction"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

__all__ = ["DECISION_SCHEMA", "NEWS_SCHEMA", "Decision", "parse_decision"]
