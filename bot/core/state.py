"""Estado global persistente del bot (sobrevive reinicios).

Guarda lo necesario para las guardias: equity y pico, entradas y cierres
recientes, detención por drawdown (requiere reinicio manual), pausas y modo seguro.
Las posiciones abiertas NO se confían a este archivo: se reconcilian con el exchange.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, Field

from bot.core.models import Side


class EntryRecord(BaseModel):
    symbol: str
    side: Side
    ts: AwareDatetime
    edge_ratio: float = 0.0
    signal_id: str = ""


class CloseRecord(BaseModel):
    symbol: str
    side: Side
    closed_at: AwareDatetime
    pnl_net: float


class BotState(BaseModel):
    equity: float
    peak_equity: float
    day_start_equity: float
    day: date
    entries: list[EntryRecord] = Field(default_factory=list)
    closes: list[CloseRecord] = Field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""
    pause_until: AwareDatetime | None = None
    safe_mode: bool = False
    safe_mode_reason: str = ""

    # ------------------------------------------------------------ persistencia

    @classmethod
    def fresh(cls, capital: float, today: date) -> BotState:
        return cls(equity=capital, peak_equity=capital, day_start_equity=capital, day=today)

    @classmethod
    def load_or_new(cls, path: Path, capital: float, today: date) -> BotState:
        if path.exists():
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        return cls.fresh(capital, today)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(self.model_dump_json(indent=2))
        os.replace(tmp, path)

    # ------------------------------------------------------------ eventos

    def roll_day(self, today: date) -> None:
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity

    def record_entry(self, rec: EntryRecord) -> None:
        self.entries.append(rec)
        self._trim(rec.ts)

    def record_close(self, rec: CloseRecord, update_equity: bool = True) -> None:
        """En vivo/papel la equity se toma del exchange (update_equity=False); en backtest se suma aquí."""
        self.closes.append(rec)
        if update_equity:
            self.update_equity(self.equity + rec.pnl_net)
        self._trim(rec.closed_at)

    def update_equity(self, equity: float) -> None:
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(days=3)
        self.entries = [e for e in self.entries if e.ts >= cutoff]
        self.closes = self.closes[-200:]

    # ------------------------------------------------------------ consultas

    def entries_on(self, day: date) -> list[EntryRecord]:
        return [e for e in self.entries if e.ts.date() == day]

    def closes_on(self, day: date) -> list[CloseRecord]:
        return [c for c in self.closes if c.closed_at.date() == day]

    def consecutive_losses(self) -> int:
        n = 0
        for c in reversed(self.closes):
            if c.pnl_net < 0:
                n += 1
            else:
                break
        return n

    @property
    def drawdown(self) -> float:
        return 0.0 if self.peak_equity <= 0 else 1 - self.equity / self.peak_equity

    @property
    def day_pnl_pct(self) -> float:
        return 0.0 if self.day_start_equity <= 0 else self.equity / self.day_start_equity - 1

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"))
