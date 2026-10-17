"""Demo (papel) con capital nuevo: si cambia `paper.initial_capital`, se archiva la demo anterior y se empieza de cero.

Mezclar el historial de una demo de 2 000 $ con una de 27 $ dejaría mal todas las métricas (drawdown, %
de ganancia, puertas de fase). Nada se borra: la base de datos, el estado y los reportes anteriores se mueven
a `bot/data/archivo/<fecha>_capital_<anterior>/`. Solo aplica al modo paper; testnet y live nunca se tocan.
"""

from __future__ import annotations

import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from bot.core.config import Mode, Settings
from bot.core.logging_setup import get_logger

log = get_logger("bot.demo_reset")
KEEP_IN_REPORTS = {"panel_clave.txt", "control.json"}  # la clave del celular y los controles siguen igual


def _move(src: Path, dst: Path, tries: int = 10, wait_s: float = 1.0) -> None:
    """En Windows un archivo abierto por otro proceso (el panel) no se puede mover: reintentar unos segundos."""
    for i in range(tries):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait_s)


def marker_path(settings: Settings) -> Path:
    return settings.storage.state_path.with_name(f"capital_{settings.mode.value}.txt")


def check(settings: Settings, now: datetime | None = None) -> Path | None:
    """`settings` con rutas por modo. Devuelve la carpeta de archivo si archivó algo."""
    if settings.mode is not Mode.PAPER:
        return None
    now = now or datetime.now(UTC)
    st = settings.storage
    capital = float(settings.paper.initial_capital)
    marker = marker_path(settings)
    try:
        old = float(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        old = None
    if old is not None and abs(old - capital) < 1e-9:
        return None
    exchange = st.state_path.with_name(f"{st.state_path.stem}_exchange.json")
    files = [p for p in (st.db_path, st.state_path, exchange) if p.exists()]
    for extra in (st.db_path.with_name(st.db_path.name + "-wal"), st.db_path.with_name(st.db_path.name + "-shm")):
        if extra.exists():
            files.append(extra)
    reports = [p for p in st.reports_dir.iterdir() if p.name not in KEEP_IN_REPORTS] if st.reports_dir.exists() else []
    dest = None
    if files or reports:
        label = f"{old:g}" if old is not None else "anterior"
        dest = st.state_path.parent / "archivo" / f"{now.strftime('%Y%m%d-%H%M%S')}_capital_{label}"
        (dest / "reports").mkdir(parents=True, exist_ok=True)
        try:
            for p in files:
                _move(p, dest / p.name)
            for p in reports:
                _move(p, dest / "reports" / p.name)
        except OSError as exc:
            # No tumbar el arranque: se sigue con lo que haya y se reintenta en el próximo arranque
            log.error("no se pudo archivar la demo anterior (¿archivo abierto?); se reintenta al próximo arranque",
                      extra={"err": repr(exc), "archivo": str(dest)})
            return None
        log.warning("capital de la demo cambiado: demo anterior archivada, se empieza de cero",
                    extra={"capital_anterior": old, "capital_nuevo": capital, "archivo": str(dest)})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{capital:g}\n", encoding="utf-8")
    return dest
