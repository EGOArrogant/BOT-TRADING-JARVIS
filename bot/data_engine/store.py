"""Almacén en memoria de valores por clave con historial (para z-scores)."""

from __future__ import annotations

import bisect
import json
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np

from bot.core.models import DataPoint


OnSet = Callable[[str, float | str, str, datetime], None]


class FeedStore:
    def __init__(self, history_len: int = 10_000, on_set: OnSet | None = None):
        self._latest: dict[str, DataPoint] = {}
        self._hist: dict[str, deque[tuple[datetime, float]]] = {}
        self._history_len = history_len
        self.on_set = on_set  # p. ej. grabar a disco cada valor nuevo (recorder)

    def set(self, key: str, value: float | str, source: str, ts: datetime, keep_history: bool = True) -> None:
        cur = self._latest.get(key)
        if cur is not None and cur.ts is not None and ts < cur.ts:
            # dato más antiguo que el vigente (relleno): solo historial, no reemplaza al último
            self._insert_history(key, ts, value)
            return
        if self.on_set is not None and not key.startswith("calc.") and (cur is None or cur.ts != ts):
            self.on_set(key, value, source, ts)
        self._latest[key] = DataPoint.ok(value, source, ts)
        if keep_history and isinstance(value, int | float):
            q = self._hist.setdefault(key, deque(maxlen=self._history_len))
            if not q or ts > q[-1][0]:
                q.append((ts, float(value)))
            elif ts == q[-1][0]:
                q[-1] = (ts, float(value))

    def _insert_history(self, key: str, ts: datetime, value: float | str) -> None:
        if not isinstance(value, int | float):
            return
        q = self._hist.setdefault(key, deque(maxlen=self._history_len))
        items = list(q)
        i = bisect.bisect_left([t for t, _ in items], ts)
        if i < len(items) and items[i][0] == ts:
            items[i] = (ts, float(value))
        else:
            items.insert(i, (ts, float(value)))
        self._hist[key] = deque(items[-self._history_len:], maxlen=self._history_len)

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


class AsOfStore:
    """Vista de un FeedStore congelada en `cutoff`: solo ve valores con ts ≤ cutoff.

    Evita sesgo de mirar al futuro en el backtest. Lo que se escribe (campos calc.*) va a un
    almacén aparte y también se filtra por fecha.
    """

    def __init__(self, base: FeedStore, cutoff: datetime, scratch: FeedStore | None = None):
        self.base = base
        self.cutoff = cutoff
        self.scratch = scratch if scratch is not None else FeedStore()

    def _src(self, key: str) -> FeedStore:
        return self.scratch if key.startswith("calc.") else self.base

    def set(self, key: str, value: float | str, source: str, ts: datetime, keep_history: bool = True) -> None:
        if ts <= self.cutoff:
            self.scratch.set(key, value, source, ts, keep_history)

    def mark_unknown(self, key: str, source: str) -> None:
        pass

    def get(self, key: str) -> DataPoint:
        hist = self.history(key)
        if not hist:
            dp = self._src(key).get(key)
            if dp.is_ok and dp.ts is not None and dp.ts <= self.cutoff:
                return dp
            return DataPoint.unknown("replay")
        ts, v = hist[-1]
        return DataPoint.ok(v, "replay", ts)

    def history(self, key: str, since: datetime | None = None) -> list[tuple[datetime, float]]:
        return [x for x in self._src(key).history(key, since) if x[0] <= self.cutoff]

    def values(self, key: str, since: datetime | None = None) -> np.ndarray:
        return np.array([v for _, v in self.history(key, since)], dtype=float)

    def value_at_or_before(self, key: str, ts: datetime) -> float | None:
        return self._src(key).value_at_or_before(key, min(ts, self.cutoff))


def load_recorded_store(raw_dir: Path, days: list[str]) -> FeedStore:
    """Reconstruye un FeedStore a partir de lo grabado por el recorder (fuente "store")."""
    store = FeedStore(history_len=200_000)
    rows = []
    for day in days:
        path = raw_dir / "store" / f"{day}.jsonl"
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                rows += [json.loads(line)["data"] for line in fh if line.strip()]
    rows.sort(key=lambda r: r["ts"])
    for r in rows:
        store.set(r["k"], r["v"], r.get("src", "replay"), datetime.fromisoformat(r["ts"]))
    return store
