"""Retención de datos crudos: que semanas de grabación no llenen el disco.

Lo crudo de alto volumen (cada operación de mercado `@aggTrade` y el libro muestreado `@book20`) ocupa
del orden de 1 GB al día entre las 6 monedas. Política:

1. Cada día cerrado (UTC) se comprime con gzip (~10× menos; `Recorder.read` y las puertas lo leen igual).
2. Lo de alto volumen se borra pasados `raw_retention_days`.
3. Si quedan menos de `min_free_gb` libres, se borran los días más antiguos de alto volumen hasta
   recuperar espacio. Lo pequeño y valioso (velas, funding, series REST `store`, traders destacados)
   nunca se borra: es lo que reproduce el backtest.
"""

from __future__ import annotations

import gzip
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from bot.core.logging_setup import get_logger

log = get_logger("bot.retention")

HIGH_VOLUME = ("@aggtrade", "@book20")


@dataclass
class RetentionReport:
    compressed: int = 0
    deleted: list[str] = field(default_factory=list)
    freed_mb: float = 0.0
    free_gb: float | None = None
    low_disk: bool = False


def _day_of(p: Path) -> date | None:
    try:
        return date.fromisoformat(p.name[:10])
    except ValueError:
        return None


def is_high_volume(source_dir: Path) -> bool:
    return source_dir.name.lower().endswith(HIGH_VOLUME)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def compress_day(path: Path) -> int:
    """JSONL -> JSONL.GZ; devuelve bytes ahorrados."""
    gz = path.with_suffix(".jsonl.gz")
    before = path.stat().st_size
    with open(path, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    path.unlink()
    return before - gz.stat().st_size


def run(raw_dir: Path, today: date, retention_days: int, min_free_gb: float,
        free_fn=free_gb) -> RetentionReport:
    rep = RetentionReport()
    if not raw_dir.exists():
        return rep
    sources = [d for d in raw_dir.iterdir() if d.is_dir()]
    for src in sources:
        for f in sorted(src.glob("*.jsonl")):
            d = _day_of(f)
            if d is not None and d < today:
                try:
                    rep.freed_mb += compress_day(f) / 1e6
                    rep.compressed += 1
                except OSError as exc:
                    log.warning("no se pudo comprimir", extra={"file": str(f), "err": repr(exc)})

    def deletable() -> list[tuple[date, Path]]:
        out = []
        for src in sources:
            if is_high_volume(src):
                for f in src.iterdir():
                    d = _day_of(f)
                    if d is not None and d < today:
                        out.append((d, f))
        return sorted(out)

    for d, f in deletable():
        if (today - d).days > retention_days:
            rep.freed_mb += f.stat().st_size / 1e6
            f.unlink()
            rep.deleted.append(f"{f.parent.name}/{f.name}")
    rep.free_gb = free_fn(raw_dir)
    if rep.free_gb < min_free_gb:
        for d, f in deletable():
            if rep.free_gb >= min_free_gb:
                break
            size = f.stat().st_size
            f.unlink()
            rep.deleted.append(f"{f.parent.name}/{f.name}")
            rep.freed_mb += size / 1e6
            rep.free_gb = free_fn(raw_dir)
        rep.low_disk = rep.free_gb < min_free_gb
    if rep.compressed or rep.deleted:
        log.info("retención de datos", extra={"comprimidos": rep.compressed, "borrados": len(rep.deleted),
                                              "liberado_mb": round(rep.freed_mb), "libre_gb": round(rep.free_gb, 1)})
    return rep
