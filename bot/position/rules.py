"""Reglas de gestión de posición como funciones puras.

Las usan tanto el gestor en vivo/papel como el backtest, para que ambos
apliquen exactamente la misma lógica. El stop solo se mueve a favor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from bot.core.config import PositionConfig
from bot.core.models import Side


class ActionKind(StrEnum):
    MOVE_STOP = "move_stop"
    CLOSE = "close"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    reason: str
    price: float | None = None  # nuevo stop para MOVE_STOP


@dataclass
class PosView:
    side: Side
    entry: float
    initial_stop: float
    stop: float
    opened_at: datetime
    partial_taken: bool
    invalidation_price: float | None
    max_hold_hours: float

    @property
    def risk(self) -> float:
        return abs(self.entry - self.initial_stop)

    def r_at(self, price: float) -> float:
        sign = 1 if self.side is Side.LONG else -1
        return sign * (price - self.entry) / self.risk if self.risk > 0 else 0.0

    def improves(self, new_stop: float) -> bool:
        return new_stop > self.stop if self.side is Side.LONG else new_stop < self.stop


def partial_price(side: Side, entry: float, initial_stop: float, r: float) -> float:
    risk = abs(entry - initial_stop)
    return entry + r * risk if side is Side.LONG else entry - r * risk


def target_qty_split(qty: float, fraction: float, step: float) -> tuple[float, float]:
    """(qty parcial, qty restante) redondeadas al step del exchange."""
    part = int(qty * fraction / step + 1e-9) * step if step > 0 else qty * fraction
    return part, qty - part


def management_actions(pos: PosView, price: float, now: datetime, cfg: PositionConfig, atr: float | None,
                       closed_15m: float | None = None, macro_soon: bool = False) -> list[Action]:
    """Decisiones del gestor dado el precio actual y (opcional) el cierre de 15m recién completado."""
    actions: list[Action] = []
    r_now = pos.r_at(price)
    held = now - pos.opened_at

    # Invalidación de la tesis (evaluada por código sobre el cierre de 15m)
    if closed_15m is not None and pos.invalidation_price is not None:
        beyond = closed_15m < pos.invalidation_price if pos.side is Side.LONG else closed_15m > pos.invalidation_price
        if beyond:
            return [Action(ActionKind.CLOSE, "invalidacion")]

    # Salida antes de evento macro si no hay ganancia; con ≥1R, stop a equilibrio
    if macro_soon:
        if r_now <= 0:
            return [Action(ActionKind.CLOSE, "macro")]
        if r_now >= 1.0 and pos.improves(pos.entry):
            actions.append(Action(ActionKind.MOVE_STOP, "macro_breakeven", pos.entry))

    # Salida por tiempo
    if held >= timedelta(hours=cfg.time_exit_hours) and r_now < cfg.time_exit_min_r:
        return [Action(ActionKind.CLOSE, "tiempo")]
    if held >= timedelta(hours=pos.max_hold_hours):
        return [Action(ActionKind.CLOSE, "tiempo_max")]

    # Tras el parcial: stop a equilibrio y trailing por ATR
    if pos.partial_taken:
        new_stop = pos.entry
        if atr is not None and atr > 0:
            trail = price - cfg.trailing_atr_multiple * atr if pos.side is Side.LONG \
                else price + cfg.trailing_atr_multiple * atr
            new_stop = max(new_stop, trail) if pos.side is Side.LONG else min(new_stop, trail)
        if pos.improves(new_stop) and not any(a.kind is ActionKind.MOVE_STOP and a.price == new_stop for a in actions):
            actions.append(Action(ActionKind.MOVE_STOP, "trailing", new_stop))
    return actions
