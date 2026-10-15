"""Modelos de datos comunes (pydantic v2).

Regla §1.4: un dato faltante es "desconocido", nunca cero. Todo valor de
mercado viaja en un DataPoint con source, ts y status.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- datos


class DataStatus(StrEnum):
    OK = "ok"
    STALE = "stale"
    UNKNOWN = "unknown"


class DataPoint(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | str | None = None
    source: str
    ts: AwareDatetime | None = None
    status: DataStatus = DataStatus.UNKNOWN

    @model_validator(mode="after")
    def _consistent(self) -> DataPoint:
        if self.status is DataStatus.UNKNOWN:
            if self.value is not None:
                raise ValueError("un DataPoint 'unknown' no puede llevar valor")
        else:
            if self.value is None or self.ts is None:
                raise ValueError(f"un DataPoint '{self.status}' requiere value y ts")
        return self

    @classmethod
    def unknown(cls, source: str) -> DataPoint:
        return cls(source=source, status=DataStatus.UNKNOWN)

    @classmethod
    def ok(cls, value: float | str, source: str, ts: datetime) -> DataPoint:
        return cls(value=value, source=source, ts=ts, status=DataStatus.OK)

    def age(self, now: datetime) -> timedelta | None:
        return None if self.ts is None else now - self.ts

    def with_ttl(self, now: datetime, ttl_s: float) -> DataPoint:
        """Devuelve una copia marcada 'stale' si superó su tiempo de vida."""
        if self.status is DataStatus.OK and self.ts is not None:
            if (now - self.ts).total_seconds() > ttl_s:
                return self.model_copy(update={"status": DataStatus.STALE})
        return self

    @property
    def is_ok(self) -> bool:
        return self.status is DataStatus.OK

    def as_float(self) -> float | None:
        """Valor numérico solo si está 'ok'; nunca convierte faltantes en 0."""
        if not self.is_ok or isinstance(self.value, str):
            return None
        return float(self.value)  # type: ignore[arg-type]


class Snapshot(_Model):
    """Estado de una moneda en un instante. `fields` agrupa por categoría."""

    symbol: str
    ts: AwareDatetime
    fields: dict[str, DataPoint] = Field(default_factory=dict)

    def get(self, name: str) -> DataPoint:
        return self.fields.get(name) or DataPoint.unknown(source="missing")

    def refreshed(self, now: datetime, ttl_by_prefix: dict[str, float]) -> Snapshot:
        """Aplica TTL por prefijo de campo (p. ej. 'market.' -> ttl market)."""
        out: dict[str, DataPoint] = {}
        for name, dp in self.fields.items():
            prefix = name.split(".", 1)[0]
            ttl = ttl_by_prefix.get(prefix)
            out[name] = dp if ttl is None else dp.with_ttl(now, ttl)
        return self.model_copy(update={"fields": out})

    def missing(self, required: list[str]) -> list[str]:
        return [n for n in required if not self.get(n).is_ok]


# ---------------------------------------------------------------- señales


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Regime(StrEnum):
    TREND_UP = "tendencia_alcista"
    TREND_DOWN = "tendencia_bajista"
    RANGE = "rango"
    HIGH_VOL = "alta_volatilidad"
    INSUFFICIENT = "datos_insuficientes"


class Signal(_Model):
    """Candidato generado por reglas deterministas."""

    id: str
    symbol: str
    strategy: str
    side: Side
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    expected_move_pct: float = Field(gt=0)
    total_cost_pct: float = Field(gt=0)
    data_quality: float = Field(ge=0, le=1)
    regime: Regime
    urgent: bool = False
    invalidation: str = ""
    # Invalidación estructurada evaluada por código: cierre de 15m más allá de este nivel.
    invalidation_price: float | None = Field(default=None, gt=0)
    max_hold_hours: float = Field(default=6.0, gt=0)
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _stop_side(self) -> Signal:
        if self.side is Side.LONG and self.stop >= self.entry:
            raise ValueError("stop de un LONG debe estar bajo la entrada")
        if self.side is Side.SHORT and self.stop <= self.entry:
            raise ValueError("stop de un SHORT debe estar sobre la entrada")
        return self

    @property
    def edge_ratio(self) -> float:
        return self.expected_move_pct / self.total_cost_pct

    @property
    def score(self) -> float:
        """Ventaja esperada ÷ costo × calidad de datos (CLAUDE.md §0)."""
        return self.edge_ratio * self.data_quality

    @property
    def stop_distance_pct(self) -> float:
        return abs(self.entry - self.stop) / self.entry


# ---------------------------------------------------------------- IA


class AIAction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    NO_TRADE = "NO_TRADE"


class NewsImpact(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"
    NONE = "NONE"


class Decision(_Model):
    """Salida del analista IA. Cualquier desviación del esquema -> inválida."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: AIAction
    confidence: float = Field(ge=0.0, le=1.0)
    size_multiplier: float = Field(ge=0.5, le=1.0)
    thesis: str = Field(min_length=1, max_length=400)
    key_evidence: list[str] = Field(default_factory=list, max_length=8)
    invalidation: str = Field(default="", max_length=400)
    news_impact: NewsImpact
    risk_flags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("confidence", "size_multiplier", mode="before")
    @classmethod
    def _no_bool(cls, v: Any) -> Any:
        if isinstance(v, bool):
            raise ValueError("se esperaba número")
        return v

    @model_validator(mode="after")
    def _trade_needs_invalidation(self) -> Decision:
        if self.action is not AIAction.NO_TRADE and not self.invalidation.strip():
            raise ValueError("una operación requiere condición de invalidación")
        return self

    @classmethod
    def no_trade(cls, reason: str) -> Decision:
        return cls(
            action=AIAction.NO_TRADE,
            confidence=0.0,
            size_multiplier=0.5,
            thesis=reason[:400] or "no_trade",
            news_impact=NewsImpact.NONE,
            risk_flags=[reason[:100]] if reason else [],
        )

    def agrees_with(self, side: Side) -> bool:
        return self.action.value == side.value


def parse_decision(raw: str | dict[str, Any] | None) -> Decision | None:
    """Intenta validar la respuesta de la IA. None si es inválida."""
    if raw is None:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return Decision.model_validate(data)
    except (ValueError, TypeError, ValidationError):
        return None


# ---------------------------------------------------------------- órdenes


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class OrderStatus(StrEnum):
    PENDING = "PENDING"        # creada localmente, aún no enviada/confirmada
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Order(_Model):
    client_order_id: str = Field(min_length=1, max_length=36)
    exchange_order_id: str | None = None
    symbol: str
    side: OrderSide
    type: OrderType
    qty: Decimal = Field(ge=0)          # 0 solo con close_position
    price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False
    post_only: bool = False
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    close_position: bool = False       # stop que cierra toda la posición (sin qty)
    is_algo: bool = False              # condicional: Binance lo enruta por /fapi/v1/algoOrder
    fee: Decimal = Decimal(0)
    maker: bool | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @model_validator(mode="after")
    def _type_fields(self) -> Order:
        if self.type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT requiere price")
        if self.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET):
            if self.stop_price is None:
                raise ValueError(f"{self.type} requiere stop_price")
            if not (self.reduce_only or self.close_position):
                raise ValueError("stops y take-profits deben ser reduce-only")
        if self.post_only and self.type is not OrderType.LIMIT:
            raise ValueError("post_only solo aplica a LIMIT")
        if self.qty == 0 and not self.close_position:
            raise ValueError("qty debe ser > 0")
        return self


class Position(_Model):
    symbol: str
    side: Side
    qty: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    stop_price: Decimal = Field(gt=0)
    initial_stop_price: Decimal = Field(gt=0)
    take_profit_price: Decimal | None = None
    stop_order_id: str | None = None
    strategy: str
    signal_id: str
    opened_at: AwareDatetime
    invalidation: str = ""
    invalidation_price: Decimal | None = None
    max_hold_hours: float = 6.0
    regime: Regime = Regime.INSUFFICIENT
    expected_entry_price: Decimal | None = None
    entry_maker: bool = True
    entry_fee: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)     # de cierres parciales
    exit_fees: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    initial_qty: Decimal | None = None
    partial_taken: bool = False
    mae_r: float = 0.0
    mfe_r: float = 0.0
    tp_order_id: str | None = None
    ai_confidence: float | None = None
    ai_size_multiplier: float | None = None

    @property
    def notional(self) -> Decimal:
        return self.qty * self.entry_price

    @property
    def risk_per_unit(self) -> Decimal:
        return abs(self.entry_price - self.initial_stop_price)

    def r_multiple(self, price: Decimal) -> Decimal:
        sign = 1 if self.side is Side.LONG else -1
        return sign * (price - self.entry_price) / self.risk_per_unit


class ExitReason(StrEnum):
    STOP = "stop"
    TAKE_PROFIT = "take_profit"
    TRAILING = "trailing"
    TIME = "time"
    INVALIDATION = "invalidation"
    MACRO = "macro"
    FUNDING = "funding"
    AI_EARLY_EXIT = "ai_early_exit"
    MANUAL = "manual"
    SAFE_MODE = "safe_mode"


class Trade(_Model):
    """Operación cerrada: lo que entra al diario."""

    id: str
    signal_id: str
    symbol: str
    side: Side
    strategy: str
    regime: Regime
    qty: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)
    expected_entry_price: Decimal = Field(gt=0)
    initial_stop_price: Decimal = Field(gt=0)
    entry_maker: bool
    fees: Decimal = Field(ge=0)
    funding: Decimal = Decimal(0)          # positivo = cobrado, negativo = pagado
    pnl_gross: Decimal
    mae_r: float
    mfe_r: float
    opened_at: AwareDatetime
    closed_at: AwareDatetime
    exit_reason: ExitReason
    ai_confidence: float | None = Field(default=None, ge=0, le=1)
    ai_size_multiplier: float | None = Field(default=None, ge=0.5, le=1)

    @property
    def pnl_net(self) -> Decimal:
        return self.pnl_gross - self.fees + self.funding

    @property
    def r_multiple(self) -> Decimal:
        risk = abs(self.entry_price - self.initial_stop_price) * self.qty
        return self.pnl_net / risk

    @property
    def slippage_pct(self) -> Decimal:
        """Positivo = llenado peor que lo esperado (costo); negativo = mejora."""
        sign = 1 if self.side is Side.LONG else -1
        return sign * (self.entry_price - self.expected_entry_price) / self.expected_entry_price

    @property
    def duration(self) -> timedelta:
        return self.closed_at - self.opened_at

