"""Logs JSON estructurados con rotación diaria (UTC)."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

# Defensa extra: nunca escribir algo con forma de llave/secret en los logs.
_SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|signature|password)", re.IGNORECASE)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEYS.search(str(k)) else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        payload.update(_redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(log_dir: Path | str, level: str = "INFO", backup_days: int = 30, console: bool = True) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    formatter = JsonFormatter()
    file_handler = TimedRotatingFileHandler(
        log_dir / "bot.jsonl", when="midnight", backupCount=backup_days, utc=True, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
