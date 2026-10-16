"""Almacén de series para backtests largos: mismas consultas que FeedStore/AsOfStore, en O(log n).

FeedStore recorre listas completas en cada consulta y guarda como mucho 10 000 puntos por clave:
con un año de open interest cada 5 minutos (~100 000 puntos) el backtest se volvía inviable y
además perdía historia. Aquí cada clave es un par de arreglos ordenados y las consultas usan
búsqueda binaria. `as_of(cutoff)` da la vista sin futuro que usa el Backtester.
"""

from __future__ import annotations

import bisect
from datetime import UTC, datetime

import numpy as np

from bot.core.models import DataPoint


def _ts(dt: datetime) -> float:
    return dt.timestamp()


class SeriesStore:
    def __init__(self) -> None:
        self._t: dict[str, list[float]] = {}
        self._v: dict[str, list[float]] = {}
        self._arr: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------ carga
    def set(self, key: str, value: float | str, source: str, ts: datetime, keep_history: bool = True) -> None:
        if not isinstance(value, int | float):
            return
        t, v = self._t.setdefault(key, []), self._v.setdefault(key, [])
        x = _ts(ts)
        if t and x < t[-1]:
            i = bisect.bisect_left(t, x)
            if i < len(t) and t[i] == x:
                v[i] = float(value)
            else:
                t.insert(i, x)
                v.insert(i, float(value))
        elif t and x == t[-1]:
            v[-1] = float(value)
        else:
            t.append(x)
            v.append(float(value))
        self._arr.pop(key, None)

    def mark_unknown(self, key: str, source: str) -> None:
        pass

    def keys(self) -> list[str]:
        return list(self._t)

    def _values_arr(self, key: str) -> np.ndarray:
        a = self._arr.get(key)
        if a is None:
            a = self._arr[key] = np.asarray(self._v.get(key, ()), dtype=float)
        return a

    # ------------------------------------------------------------ consultas con tope
    def _idx(self, key: str, cutoff: float) -> int:
        """Cantidad de puntos con ts ≤ cutoff."""
        return bisect.bisect_right(self._t.get(key, ()), cutoff)

    def get_upto(self, key: str, cutoff: float) -> DataPoint:
        n = self._idx(key, cutoff)
        if n == 0:
            return DataPoint.unknown("replay")
        return DataPoint.ok(self._v[key][n - 1], "replay", datetime.fromtimestamp(self._t[key][n - 1], UTC))

    def values_upto(self, key: str, cutoff: float, since: datetime | None = None) -> np.ndarray:
        t = self._t.get(key)
        if not t:
            return np.array([], dtype=float)
        hi = self._idx(key, cutoff)
        lo = bisect.bisect_left(t, _ts(since)) if since is not None else 0
        return self._values_arr(key)[lo:hi] if hi > lo else np.array([], dtype=float)

    def value_upto(self, key: str, cutoff: float) -> float | None:
        n = self._idx(key, cutoff)
        return self._v[key][n - 1] if n else None

    # ------------------------------------------------------------ interfaz FeedStore (sin tope)
    def get(self, key: str) -> DataPoint:
        return self.get_upto(key, float("inf"))

    def values(self, key: str, since: datetime | None = None) -> np.ndarray:
        return self.values_upto(key, float("inf"), since)

    def history(self, key: str, since: datetime | None = None) -> list[tuple[datetime, float]]:
        t, v = self._t.get(key, []), self._v.get(key, [])
        lo = bisect.bisect_left(t, _ts(since)) if since is not None else 0
        return [(datetime.fromtimestamp(t[i], UTC), v[i]) for i in range(lo, len(t))]

    def value_at_or_before(self, key: str, ts: datetime) -> float | None:
        return self.value_upto(key, _ts(ts))

    def as_of(self, cutoff: datetime, scratch: SeriesStore | None = None) -> SeriesAsOf:
        return SeriesAsOf(self, cutoff, scratch if scratch is not None else SeriesStore())


class SeriesAsOf:
    """Vista sin futuro: solo ve puntos con ts ≤ cutoff. Lo escrito (calc.*) va al almacén auxiliar."""

    def __init__(self, base: SeriesStore, cutoff: datetime, scratch: SeriesStore):
        self.base, self.scratch, self.cutoff = base, scratch, cutoff
        self._c = _ts(cutoff)

    def _src(self, key: str) -> SeriesStore:
        return self.scratch if key.startswith("calc.") else self.base

    def set(self, key: str, value: float | str, source: str, ts: datetime, keep_history: bool = True) -> None:
        if ts <= self.cutoff:
            self.scratch.set(key, value, source, ts, keep_history)

    def mark_unknown(self, key: str, source: str) -> None:
        pass

    def get(self, key: str) -> DataPoint:
        return self._src(key).get_upto(key, self._c)

    def values(self, key: str, since: datetime | None = None) -> np.ndarray:
        return self._src(key).values_upto(key, self._c, since)

    def history(self, key: str, since: datetime | None = None) -> list[tuple[datetime, float]]:
        return [x for x in self._src(key).history(key, since) if x[0] <= self.cutoff]

    def value_at_or_before(self, key: str, ts: datetime) -> float | None:
        return self._src(key).value_upto(key, min(_ts(ts), self._c))
