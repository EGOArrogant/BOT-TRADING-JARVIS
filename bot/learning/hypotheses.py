"""Hipótesis del aprendizaje: lista blanca de cambios permitidos.

PROHIBIDO (CLAUDE.md §1.7 y §12.6): límites de riesgo, apalancamiento, lógica
de stops, ejecución. Por eso solo existen estos tipos, todos de nivel
estrategia/filtro, y validados aquí:

- min_edge:        subir (nunca bajar de 3×) la ventaja mínima para una moneda/estrategia.
- block_condition: no operar cuando un indicador cumple una condición.
- disable:         desactivar una estrategia en una moneda/dirección.
- strategy_param:  cambiar un parámetro de ENTRADA/OBJETIVO de una estrategia.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from bot.core.config import StrategyParams
from bot.core.models import DataPoint, Signal

# Parámetros de estrategia que el aprendizaje puede tocar (NO stop_atr_multiple: es lógica de stop)
TUNABLE_PARAMS = {
    "trend_pullback": {"enabled", "target_atr_multiple", "max_hold_hours", "min_adx", "max_ema_distance_atr"},
    "range_breakout": {"enabled", "target_atr_multiple", "max_hold_hours", "range_bars", "max_range_atr",
                       "min_rel_volume"},
    "funding_reversion": {"enabled", "target_atr_multiple", "max_hold_hours", "min_funding_z", "min_fng_extreme"},
    "trend_breakout": {"enabled", "target_atr_multiple", "max_hold_hours", "min_adx", "min_rel_volume"},
}
FIELD_PREFIXES = ("ind.", "sent.", "deriv.", "dex.", "vol.", "onchain.")
MIN_EDGE_FLOOR = 3.0
MIN_EDGE_CEIL = 8.0
OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "strategy_overrides.yaml"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["min_edge", "block_condition", "disable", "strategy_param"]
    description: str = Field(min_length=5, max_length=300)
    symbol: str | None = None
    strategy: str | None = None
    side: Literal["LONG", "SHORT"] | None = None
    # min_edge
    min_edge: float | None = None
    # block_condition
    field: str | None = None
    op: Literal[">", "<"] | None = None
    threshold: float | None = None
    # strategy_param
    param: str | None = None
    value: float | bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Hypothesis:
        if self.strategy is not None and self.strategy not in TUNABLE_PARAMS:
            raise ValueError(f"estrategia desconocida: {self.strategy}")
        if self.kind == "min_edge":
            if self.min_edge is None or not MIN_EDGE_FLOOR <= self.min_edge <= MIN_EDGE_CEIL:
                raise ValueError("min_edge debe estar en [3, 8]: nunca se baja del mínimo duro")
        elif self.kind == "block_condition":
            if not self.field or not self.field.startswith(FIELD_PREFIXES) or self.op is None or self.threshold is None:
                raise ValueError("block_condition requiere field (ind./sent./deriv./...), op y threshold")
        elif self.kind == "disable":
            if self.strategy is None:
                raise ValueError("disable requiere strategy")
        elif self.kind == "strategy_param":
            if self.strategy is None or self.param not in TUNABLE_PARAMS[self.strategy] or self.value is None:
                raise ValueError("strategy_param: parámetro no permitido para el aprendizaje")
            if self.symbol is not None or self.side is not None:
                raise ValueError("strategy_param es global por estrategia")
        return self

    @property
    def id(self) -> str:
        return hashlib.sha1(self.model_dump_json().encode()).hexdigest()[:12]

    # ------------------------------------------------------------ aplicación

    def _matches(self, sig: Signal) -> bool:
        return ((self.symbol is None or sig.symbol == self.symbol)
                and (self.strategy is None or sig.strategy == self.strategy)
                and (self.side is None or sig.side.value == self.side))

    def allows(self, sig: Signal, fields: dict[str, DataPoint]) -> bool:
        """¿El retador dejaría pasar este candidato?"""
        if self.kind == "strategy_param" or not self._matches(sig):
            return True
        if self.kind == "disable":
            return False
        if self.kind == "min_edge":
            return sig.edge_ratio >= float(self.min_edge or MIN_EDGE_FLOOR)
        dp = fields.get(self.field or "")
        v = dp.as_float() if dp is not None else None
        if v is None:
            return True  # sin dato la condición no se activa (el filtro de datos ya decide)
        hit = v > float(self.threshold) if self.op == ">" else v < float(self.threshold)  # type: ignore[arg-type]
        return not hit

    def apply_params(self, strategies: dict[str, StrategyParams]) -> dict[str, StrategyParams]:
        if self.kind != "strategy_param" or self.strategy not in strategies:
            return strategies
        out = dict(strategies)
        out[self.strategy] = strategies[self.strategy].model_copy(update={self.param: self.value})
        return out


class AppliedOverride(BaseModel):
    hypothesis: Hypothesis
    approved_at: AwareDatetime
    evidence: dict


def load_overrides(path: Path = OVERRIDES_PATH) -> list[Hypothesis]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [AppliedOverride.model_validate(o).hypothesis for o in raw.get("overrides", [])]


def save_override(ov: AppliedOverride, path: Path = OVERRIDES_PATH) -> None:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    raw.setdefault("overrides", []).append(ov.model_dump(mode="json"))
    path.write_text("# Cambios aprendidos y APROBADOS. No editar límites de riesgo aquí (no se aceptan).\n"
                    + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def effective_strategies(base: dict[str, StrategyParams], overrides: list[Hypothesis]) -> dict[str, StrategyParams]:
    out = base
    for h in overrides:
        out = h.apply_params(out)
    return out


def overrides_allow(overrides: list[Hypothesis], sig: Signal, fields: dict[str, DataPoint]) -> tuple[bool, str]:
    for h in overrides:
        if not h.allows(sig, fields):
            return False, f"aprendido: {h.description}"
    return True, ""
