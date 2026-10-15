"""Reloj del bot: siempre UTC, con desfase opcional respecto al exchange.

El desfase se medirá contra la hora del servidor del exchange (hito 2/10);
el día operativo (techo diario, pérdida diaria) se cuenta en UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


class Clock:
    def __init__(self) -> None:
        self._offset = timedelta(0)

    def set_exchange_offset(self, server_time: datetime, local_time: datetime | None = None) -> None:
        local_time = local_time or datetime.now(UTC)
        self._offset = server_time - local_time

    @property
    def offset(self) -> timedelta:
        return self._offset

    def now(self) -> datetime:
        return datetime.now(UTC) + self._offset

    def trading_day(self, at: datetime | None = None) -> date:
        return (at or self.now()).astimezone(UTC).date()

    def next_day_start(self, at: datetime | None = None) -> datetime:
        day = self.trading_day(at)
        return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)


def utc_now() -> datetime:
    return datetime.now(UTC)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
