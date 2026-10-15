"""Escritura atómica de archivos que otro proceso (el panel) puede estar leyendo.

En Windows, reemplazar un archivo que otro proceso tiene abierto falla con
PermissionError durante unos milisegundos: se reintenta antes de rendirse.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, attempts: int = 20, wait_s: float = 0.05) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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
