"""Controles manuales desde el panel: "Operar / Solo cierre" y agresividad 1–10.

Viven en `reports/<modo>/control.json`; el panel los escribe y el motor los lee
en cada ventana de candidatos (y otra vez justo antes de ejecutar).

La agresividad SOLO puede hacer al bot más prudente que `config.yaml`:
el nivel 10 es exactamente la configuración (el máximo que permiten las reglas)
y los niveles menores bajan el riesgo por operación, suben la ventaja mínima y
la confianza exigida a la IA, y reducen el techo diario. Nunca sube nada por
encima de la configuración ni de los topes duros.

Ante la duda, no operar: un archivo de control ilegible equivale a "solo cierre".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from bot.core.config import Settings
from bot.core.logging_setup import get_logger

log = get_logger("bot.control")

FILENAME = "control.json"
MAX_LEVEL = 10


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["operar", "solo_cierre"] = "operar"
    aggressiveness: int = Field(default=MAX_LEVEL, ge=1, le=MAX_LEVEL)
    updated_at: AwareDatetime | None = None

    @property
    def close_only(self) -> bool:
        return self.mode == "solo_cierre"


@dataclass(frozen=True)
class Profile:
    """Ajustes que resultan de un nivel, relativos a config.yaml."""

    level: int
    risk_factor: float        # × riesgo por operación (≤ 1)
    min_edge: float           # ventaja mínima (≥ la de config)
    max_trades: int           # techo diario (≤ el de config)
    min_ai_confidence: float  # confianza mínima de la IA para cualquier entrada


def profile(level: int, settings: Settings) -> Profile:
    level = max(1, min(MAX_LEVEL, int(level)))
    f = (level - 1) / (MAX_LEVEL - 1)          # 0 en el nivel 1, 1 en el nivel 10
    cap = settings.limits.max_trades_per_day
    return Profile(
        level=level,
        risk_factor=round(0.4 + 0.6 * f, 3),
        min_edge=round(settings.filters.min_edge_cost_multiple + 2.0 * (1 - f), 1),
        max_trades=min(cap, max(1, round(2 + 3 * f))) if cap else 0,
        min_ai_confidence=round(0.7 * (1 - f), 2),
    )


def apply(settings: Settings, ctl: Control) -> tuple[Settings, Profile]:
    """Configuración efectiva para el nivel elegido (re-validada con los mismos topes)."""
    p = profile(ctl.aggressiveness, settings)
    lim = settings.limits
    limits = lim.model_validate({**lim.model_dump(),
                                 "max_trades_per_day": p.max_trades,
                                 "base_trades_per_day": min(lim.base_trades_per_day, p.max_trades),
                                 "losing_day_trade_cap": min(lim.losing_day_trade_cap, p.max_trades),
                                 "extra_trade_min_edge": max(lim.extra_trade_min_edge, p.min_edge)})
    filters = settings.filters.model_validate({**settings.filters.model_dump(), "min_edge_cost_multiple": p.min_edge})
    risk = settings.risk.model_validate({**settings.risk.model_dump(),
                                         "risk_per_trade": settings.risk.risk_per_trade * p.risk_factor})
    assert risk.risk_per_trade <= settings.risk.risk_per_trade and limits.max_trades_per_day <= lim.max_trades_per_day
    return settings.model_copy(update={"limits": limits, "filters": filters, "risk": risk}), p


def path_for(settings: Settings) -> Path:
    return settings.storage.reports_dir / FILENAME


def load(path: Path) -> Control:
    if not path.exists():
        return Control()
    try:
        return Control.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        log.error("control.json ilegible: solo cierre hasta corregirlo", extra={"err": repr(exc)})
        return Control(mode="solo_cierre")


def save(path: Path, ctl: Control) -> Control:
    ctl = ctl.model_copy(update={"updated_at": datetime.now(UTC)})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".control-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ctl.model_dump(mode="json"), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return ctl
