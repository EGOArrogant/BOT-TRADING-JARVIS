"""Punto de arranque. Uso: python -m bot.main --mode paper"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from bot.core.config import Mode, Secrets, Settings, load_secrets, load_settings
from bot.core.logging_setup import get_logger, setup_logging

log = get_logger("bot.main")

LIVE_CONFIRM_PHRASE = "OPERAR CON DINERO REAL"


def check_live_allowed(secrets: Secrets, ask: Callable[[str], str] = input) -> bool:
    """`live` exige CONFIRM_LIVE_TRADING=true en .env y confirmación en consola (§14)."""
    if not secrets.confirm_live_trading:
        log.error("live bloqueado: CONFIRM_LIVE_TRADING no es 'true' en .env")
        return False
    if not (secrets.binance_api_key and secrets.binance_api_secret):
        log.error("live bloqueado: faltan llaves de API")
        return False
    answer = ask(f"Modo LIVE con dinero real. Escribe '{LIVE_CONFIRM_PHRASE}' para continuar: ")
    if answer.strip() != LIVE_CONFIRM_PHRASE:
        log.error("live bloqueado: confirmación en consola incorrecta")
        return False
    return True


def run(settings: Settings, secrets: Secrets) -> int:
    log.info("arranque", extra={"mode": settings.mode.value, "symbols": settings.exchange.symbols})
    if settings.mode is Mode.LIVE and not check_live_allowed(secrets):
        return 2
    # Los modos se conectan en los hitos siguientes (CLAUDE.md §16).
    log.warning("modo aún no implementado", extra={"mode": settings.mode.value})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot de trading de perpetuos")
    parser.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--env-file", default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config, args.mode) if args.config else load_settings(mode=args.mode)
    secrets = load_secrets(args.env_file)
    setup_logging(settings.logging.dir, settings.logging.level, settings.logging.backup_days)
    return run(settings, secrets)


if __name__ == "__main__":
    sys.exit(main())
