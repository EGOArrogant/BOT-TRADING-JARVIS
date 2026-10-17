"""Escritura atómica de archivos que otro proceso (el panel) puede estar leyendo, a prueba de apagones.

- En Windows, reemplazar un archivo que otro proceso tiene abierto falla con PermissionError durante unos
  milisegundos: se reintenta antes de rendirse.
- Un apagón en mal momento puede dejar un archivo lleno de ceros: antes de reemplazarlo se fuerza la escritura
  al disco (fsync) y, con `backup=True`, la versión anterior buena queda en `<archivo>.bak` para recuperarla
  (`read_with_backup`).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def _is_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def atomic_write_text(path: Path, text: str, attempts: int = 20, wait_s: float = 0.05, backup: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())  # que llegue al disco antes de reemplazar (apagones)
        if backup and path.exists() and _is_json(path):
            try:
                shutil.copyfile(path, backup_path(path))
            except OSError:
                pass  # el respaldo nunca impide guardar
        for i in range(attempts):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == attempts - 1:
                    raise
                time.sleep(wait_s)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_text_retry(path: Path, attempts: int = 20, wait_s: float = 0.05) -> str:
    for i in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait_s)
    raise AssertionError("inalcanzable")


def read_with_backup(path: Path, parse: Callable[[str], T], on_recover: Callable[[Exception], None] | None = None) -> T:
    """Lee y valida `path`; si está dañado (p. ej. ceros tras un apagón) usa `<archivo>.bak`.

    Lanza la excepción original si ninguno se puede leer. No escribe nada (lo puede usar el panel)."""
    try:
        return parse(read_text_retry(path))
    except (OSError, ValueError) as exc:
        bak = backup_path(path)
        if not bak.exists():
            raise
        obj = parse(read_text_retry(bak))
        if on_recover is not None:
            on_recover(exc)
        return obj
