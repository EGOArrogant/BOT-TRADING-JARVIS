"""Monitor de salud: frescura de cada fuente, latido a disco y alertas en transiciones."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class HealthMonitor:
    path: Path
    max_age_s: dict[str, float]
    last_seen: dict[str, datetime] = field(default_factory=dict)
    unhealthy: set[str] = field(default_factory=set)

    def beat(self, source: str, ts: datetime) -> None:
        self.last_seen[source] = ts

    def check(self, now: datetime) -> tuple[list[str], list[str]]:
        """Devuelve (fuentes que acaban de caer, fuentes que se recuperaron)."""
        down, up = [], []
        for src, max_age in self.max_age_s.items():
            seen = self.last_seen.get(src)
            bad = seen is None or (now - seen).total_seconds() > max_age
            if bad and src not in self.unhealthy:
                self.unhealthy.add(src)
                down.append(src)
            elif not bad and src in self.unhealthy:
                self.unhealthy.discard(src)
                up.append(src)
        return down, up

    def write(self, now: datetime, extra: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": now.isoformat(), "pid": os.getpid(), "unhealthy": sorted(self.unhealthy),
                   "last_seen": {k: v.isoformat() for k, v in self.last_seen.items()}, **extra}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
