"""Almacén en memoria de valores por clave con historial (para z-scores)."""

from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np

from bot.core.models import DataPoint


class FeedStore:
    def __init__(self, history_len: int = 10_000):
        self._latest: dict[str, DataPoint] = {}
        self._hist: dict[str, deque[tuple[datetime, float]]] = {}
        self._history_len = history_len

    def set(self, key: str, value: float | str, source: str, ts: datetime, keep_history: bool = True) -> None:
        self._latest[key] = DataPoint.ok(value, source, ts)
        if keep_history and isinstance(value, int | float):
            q = self._hist.setdefault(key, deque(maxlen=self._history_len))
            if not q or ts > q[-1][0]:
                q.append((ts, float(value)))
            elif ts == q[-1][0]:
                q[-1] = (ts, float(value))

    def mark_unknown(self, key: str, source: str) -> None:
        self._latest[key] = DataPoint.unknown(source)

    def get(self, key: str) -> DataPoint:
        return self._latest.get(key) or DataPoint.unknown("missing")

    def keys(self) -> list[str]:
        return list(self._latest)

    def history(self, key: str, since: datetime | None = None) -> list[tuple[datetime, float]]:
        q = self._hist.get(key, ())
        return [x for x in q if since is None or x[0] >= since]

    def values(self, key: str, since: datetime | None = None) -> np.ndarray:
        return np.array([v for _, v in self.history(key, since)], dtype=float)

    def value_at_or_before(self, key: str, ts: datetime) -> float | None:
        best = None
        for t, v in self._hist.get(key, ()):
            if t <= ts:
                best = v
            else:
                break
        return best
