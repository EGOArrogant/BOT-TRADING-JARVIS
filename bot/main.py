"""Punto de arranque.

    python -m bot.main --mode record|backtest|paper|testnet|live
    python -m bot.main gates [--approve]       # estado de las puertas de fase
    python -m bot.main approve <id_hipótesis>  # aprobar un cambio aprendido
    python -m bot.main reset-halt --mode <m>   # reinicio manual tras detención por drawdown
    python -m bot.main report --mode <m>       # regenerar reporte HTML
    python -m bot.main panel --mode <m>        # panel en el navegador (http://127.0.0.1:8765)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.core.alerts import Alerter
from bot.core.config import Mode, Secrets, Settings, StorageConfig, load_secrets, load_settings
from bot.core.logging_setup import get_logger, setup_logging

log = get_logger("bot.main")

LIVE_CONFIRM_PHRASE = "OPERAR CON DINERO REAL"


def check_live_allowed(secrets: Secrets, ask: Callable[[str], str] = input, gates_ok: bool = True) -> bool:
    """`live` exige puertas de fase aprobadas, CONFIRM_LIVE_TRADING=true y confirmación en consola (§14)."""
    if not gates_ok:
        log.error("live bloqueado: las puertas de fase (paper + testnet) no están aprobadas; ver `gates`")
        return False
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


def per_mode_storage(settings: Settings) -> Settings:
    """Base de datos y estado separados por modo: papel, testnet y live nunca se mezclan."""
    st = settings.storage
    mode = settings.mode.value
    return settings.model_copy(update={"storage": StorageConfig(
        db_path=st.db_path.with_name(f"bot_{mode}.sqlite"), reports_dir=st.reports_dir / mode,
        state_path=st.state_path.with_name(f"state_{mode}.json"))})


def run(settings: Settings, secrets: Secrets, ask: Callable[[str], str] = input) -> int:
    from bot import gates

    settings = per_mode_storage(settings)
    log.info("arranque", extra={"mode": settings.mode.value, "symbols": settings.exchange.symbols})
    alert = Alerter(settings.alerts.telegram_enabled, secrets.telegram_bot_token, secrets.telegram_chat_id)
    if settings.mode is Mode.LIVE and not check_live_allowed(secrets, ask, gates.live_allowed(settings.data.dir)):
        return 2
    if settings.mode is Mode.TESTNET and not (secrets.binance_testnet_api_key and secrets.binance_testnet_api_secret):
        log.error("testnet: faltan BINANCE_TESTNET_API_KEY/SECRET en .env")
        return 2
    if settings.mode in (Mode.PAPER, Mode.TESTNET, Mode.LIVE) and not secrets.anthropic_api_key:
        log.warning("sin ANTHROPIC_API_KEY: el SDK intentará otras credenciales; si fallan, toda decisión será NO_TRADE")
    if settings.mode is Mode.BACKTEST:
        return asyncio.run(run_backtest(settings))
    from bot.engine import run_live_like, run_record
    try:
        if settings.mode is Mode.RECORD:
            asyncio.run(run_record(settings, secrets, alert))
        else:
            asyncio.run(run_live_like(settings, secrets, alert))
    except KeyboardInterrupt:
        log.info("detenido por el usuario")
    except Exception as exc:  # noqa: BLE001
        log.error("no se pudo arrancar o el bot se detuvo por un error", extra={"err": repr(exc)}, exc_info=True)
        return 1
    return 0


async def run_backtest(settings: Settings, days: int = 120) -> int:
    from bot.backtest.data import cached_klines
    from bot.backtest.engine import Backtester, walk_forward
    from bot.data_engine.http import HttpClient

    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    http = HttpClient()
    data = {}
    for sym in settings.exchange.symbols:
        data[sym] = await cached_klines(http, settings.endpoints.binance.rest, settings.data.dir / "klines", sym,
                                        "15m", start, end)
    await http.close()
    from bot.data_engine.store import load_recorded_store
    day_list = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days + 1)]
    store = load_recorded_store(settings.data.dir / "raw", day_list)
    bt = Backtester(settings, store=store)
    total = bt.run(data).summary()
    folds = walk_forward(bt, data, folds=4)
    out = settings.storage.reports_dir / f"backtest_{end:%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Backtest {start:%Y-%m-%d} → {end:%Y-%m-%d} (parte determinista, sin IA)", "",
             "Supuestos: entrada maker solo si el precio atraviesa el límite; stop gana si coincide con objetivo;"
             " salidas taker con deslizamiento; funding grabado si existe (si no, 0.01%/8h). Las estrategias de"
             " ruptura y de funding solo generan señales donde hay OI/funding/DEX/sentimiento grabados"
             " (modo record).", "",
             "## Total", "```", json.dumps(total, indent=1, default=str), "```", "", "## Walk-forward (tramos)"]
    for i, f in enumerate(folds):
        lines.append(f"- tramo {i + 1}: n={f['trades']} pnl={f['pnl_net']:+.2f} expectativa={f['expectancy_r']:+.3f}R "
                     f"PF={f['profit_factor']:.2f} DD={f['max_drawdown']:.2%}")
    passed = all(f["expectancy_r"] > 0 for f in folds if f["trades"] > 0) and any(f["trades"] for f in folds)
    (settings.data.dir / "backtest_gate.json").write_text(json.dumps(
        {"ok": passed, "fecha": end.isoformat(), "dias": days, "tramos": folds, "total": total}, default=str, indent=1),
        encoding="utf-8")
    lines += ["", f"**Puerta de fase (expectativa positiva fuera de muestra en todos los tramos): "
                  f"{'PASA' if passed else 'NO PASA'}**"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out.read_text(encoding="utf-8"))
    return 0


def cmd_gates(settings: Settings, do_approve: bool) -> int:
    from bot import gates

    data_dir = settings.data.dir
    symbols = settings.exchange.symbols
    res = gates.approve(data_dir, settings.paper.initial_capital, symbols) if do_approve else \
        gates.all_gates(data_dir, settings.paper.initial_capital, symbols)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "resumen"} for k, v in res.items()},
                     indent=1, ensure_ascii=False, default=str))
    print("live permitido:", gates.live_allowed(data_dir))
    return 0


def cmd_approve(settings: Settings, hyp_id: str) -> int:
    from bot.learning.champion_challenger import PENDING_APPROVAL, ChampionChallenger
    from bot.learning.journal import Journal
    from bot.learning.lessons import LessonBook

    settings = per_mode_storage(settings)
    j = Journal(settings.storage.db_path)
    cc = ChampionChallenger(j, reports_dir=settings.storage.reports_dir)
    if cc.status(hyp_id) != PENDING_APPROVAL:
        print(f"la hipótesis {hyp_id} no está pendiente de aprobación (estado: {cc.status(hyp_id)})")
        return 1
    now = datetime.now(UTC)
    h = cc.promote(hyp_id, now)
    ev = json.loads(j.db.execute("SELECT evaluation FROM hypotheses WHERE id=?", (hyp_id,)).fetchone()[0] or "{}")
    LessonBook(j).add_from_hypothesis(h, ev, now)
    print(f"promovida: {h.description}. Reinicia el bot para aplicarla.")
    return 0


def cmd_reset_halt(settings: Settings, ask: Callable[[str], str] = input) -> int:
    from bot.core.state import BotState

    settings = per_mode_storage(settings)
    path = settings.storage.state_path
    if not path.exists():
        print("no hay estado guardado")
        return 1
    st = BotState.model_validate_json(path.read_text(encoding="utf-8"))
    if not st.halted:
        print("el bot no está detenido")
        return 0
    print(f"Detenido por: {st.halt_reason}. Equity {st.equity:.2f}, pico {st.peak_equity:.2f}.")
    if ask("Escribe REINICIAR para levantar la detención (el pico se reinicia a la equity actual): ").strip() != "REINICIAR":
        return 1
    st.halted, st.halt_reason, st.peak_equity = False, "", st.equity
    st.save(path)
    print("detención levantada")
    return 0


def cmd_report(settings: Settings) -> int:
    from bot.learning.journal import Journal
    from bot.metrics.report_html import write_report

    settings = per_mode_storage(settings)
    j = Journal(settings.storage.db_path)
    now = datetime.now(UTC)
    p = write_report(settings.storage.reports_dir / "reporte.html", j.trades(datetime(2000, 1, 1, tzinfo=UTC)),
                     j.candidates(now - timedelta(days=30)), settings.paper.initial_capital, now, settings.mode.value)
    print(p)
    return 0


def cmd_panel(settings: Settings, port: int) -> int:
    import webbrowser

    from bot.dashboard.server import serve

    settings = per_mode_storage(settings)
    try:
        server = serve(settings, port)
    except OSError as exc:
        print(f"no se pudo abrir el panel en el puerto {port} ({exc}). Prueba con otro: --port 8766")
        return 1
    url = f"http://127.0.0.1:{port}"
    print(f"Panel del modo '{settings.mode.value}' en {url}  (deja esta ventana abierta; Ctrl+C para cerrarlo)")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bot de trading de perpetuos")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "gates", "approve", "reset-halt", "report", "panel"])
    parser.add_argument("arg", nargs="?", default=None)
    parser.add_argument("--mode", choices=[m.value for m in Mode], default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--approve", action="store_true", help="con `gates`: registrar aprobación si pasan")
    parser.add_argument("--port", type=int, default=8765, help="con `panel`: puerto local")
    args = parser.parse_args(argv)

    settings = load_settings(Path(args.config), args.mode) if args.config else load_settings(mode=args.mode)
    secrets = load_secrets(args.env_file)
    # El panel corre en otro proceso: su propio archivo de log (en Windows dos procesos no pueden rotar el mismo)
    log_dir = settings.logging.dir / "panel" if args.command == "panel" else settings.logging.dir
    setup_logging(log_dir, settings.logging.level, settings.logging.backup_days)
    if args.command == "gates":
        return cmd_gates(settings, args.approve)
    if args.command == "approve":
        if not args.arg:
            parser.error("approve requiere el id de la hipótesis")
        return cmd_approve(settings, args.arg)
    if args.command == "reset-halt":
        return cmd_reset_halt(settings)
    if args.command == "report":
        return cmd_report(settings)
    if args.command == "panel":
        return cmd_panel(settings, args.port)
    return run(settings, secrets)


if __name__ == "__main__":
    sys.exit(main())
