"""Graba todo a disco desde el día 1: JSONL por fuente y día (UTC), compactable a Parquet.

También lleva estadísticas de completitud para la puerta de fase de `record`
(< 1 % de datos perdidos durante 7 días).
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from bot.core.logging_setup import get_logger

log = get_logger("bot.recorder")


class Recorder:
    def __init__(self, base_dir: Path, flush_every_s: float = 5.0, max_buffer: int = 5000):
        self.base = Path(base_dir) / "raw"
        self.base.mkdir(parents=True, exist_ok=True)
        self.flush_every_s = flush_every_s
        self.max_buffer = max_buffer
        self._buf: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._last_flush = time.monotonic()
        self.counts: dict[str, int] = defaultdict(int)

    @staticmethod
    def _safe(name: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-_@." else "_" for ch in name)

    def record(self, source: str, payload: Any, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(UTC)
        day = ts.strftime("%Y-%m-%d")
        line = json.dumps({"ts": ts.isoformat(), "data": payload}, default=str, separators=(",", ":"))
        self._buf[(self._safe(source), day)].append(line)
        self.counts[source] += 1
        total = sum(len(v) for v in self._buf.values())
        if total >= self.max_buffer or time.monotonic() - self._last_flush >= self.flush_every_s:
            self.flush()

    def flush(self) -> None:
        for (source, day), lines in self._buf.items():
            if not lines:
                continue
            path = self.base / source / f"{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        self._buf.clear()
        self._last_flush = time.monotonic()

    def path_for(self, source: str, day: str) -> Path:
        return self.base / self._safe(source) / f"{day}.jsonl"

    def read(self, source: str, day: str) -> list[dict]:
        path = self.path_for(source, day)
        if not path.exists():
            gz = path.with_suffix(".jsonl.gz")
            if not gz.exists():
                return []
            with gzip.open(gz, "rt", encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def compact_to_parquet(self, source: str, day: str) -> Path | None:
        """Convierte un día cerrado a Parquet (columnas planas) y comprime el JSONL."""
        rows = self.read(source, day)
        if not rows:
            return None
        flat = [{"ts": r["ts"], **(r["data"] if isinstance(r["data"], dict) else {"value": r["data"]})} for r in rows]
        cols: dict[str, list] = defaultdict(list)
        keys = sorted({k for f in flat for k in f})
        for f in flat:
            for k in keys:
                v = f.get(k)
                cols[k].append(json.dumps(v) if isinstance(v, dict | list) else (None if v is None else str(v)))
        out = self.path_for(source, day).with_suffix(".parquet")
        pq.write_table(pa.table(cols), out, compression="zstd")
        src = self.path_for(source, day)
        if src.exists():
            with open(src, "rb") as fi, gzip.open(src.with_suffix(".jsonl.gz"), "wb") as fo:
                fo.write(fi.read())
            src.unlink()
        return out


def completeness(expected: int, received: int) -> float:
    """Fracción de datos perdidos (0 = completo)."""
    if expected <= 0:
        return 0.0
    return max(0.0, 1 - received / expected)
