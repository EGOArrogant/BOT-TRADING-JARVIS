# Bot de perpetuos cripto con IA

Bot de futuros perpetuos USDⓈ-M (BTC, ETH, SOL, XRP, DOGE, XLM) que opera poco y con ventaja clara:
reglas deterministas generan candidatos, filtros duros los depuran, Claude actúa como analista
escéptico y el código controla todo el riesgo. La especificación completa está en `CLAUDE.md`.

> **Aviso.** Operar futuros apalancados puede hacer perder todo el capital. Este software no
> garantiza ganancias. Usa siempre `paper` y `testnet` antes de `live`, y respeta las puertas de fase.

## Instalación

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]'
cp .env.example .env    # rellena las llaves (sin permiso de retiro, con lista blanca de IP)
.venv/bin/python -m pytest    # 150 tests
```

## Modos

| Comando | Qué hace |
|---|---|
| `python -m bot.main --mode record` | Solo graba datos (JSONL por fuente y día en `bot/data/raw/`, incluidas las series REST: OI, funding, DEX, sentimiento). Puerta: 7 días seguidos con < 1 % de velas perdidas y una reconexión registrada. |
| `python -m bot.main --mode backtest` | Descarga 120 días de velas de 15m y usa lo grabado (sin mirar al futuro) para OI/funding/DEX. Walk-forward en 4 tramos; la puerta pasa si todos los tramos tienen expectativa positiva. |
| `python -m bot.main --mode paper` | Todo en vivo con IA real; órdenes simuladas sobre el libro real. |
| `python -m bot.main --mode testnet` | Órdenes reales en el testnet de Binance (datos de mercado de producción). |
| `python -m bot.main --mode live` | Dinero real. Exige puertas aprobadas, `CONFIRM_LIVE_TRADING=true` y escribir la frase de confirmación. |

Cada modo tiene su propia base de datos (`bot/data/bot_<modo>.sqlite`), su estado (`state_<modo>.json`)
y sus reportes (`bot/reports/<modo>/`): papel, testnet y live nunca se mezclan.

### Otros comandos

```bash
python -m bot.main gates               # estado de las 4 puertas: record, backtest, paper, testnet
python -m bot.main gates --approve     # registra la aprobación SOLO si las métricas pasan
python -m bot.main approve <id>        # aprueba un cambio aprendido pendiente (ver reports/<modo>/cambios.md)
python -m bot.main reset-halt --mode live   # reinicio manual tras detención por drawdown del 10 %
python -m bot.main report --mode paper # regenera el reporte HTML
```

## Qué produce

- `bot/reports/<modo>/reporte.html`: cada hora. Muestra KPIs, curva de capital, drawdown, tablas por moneda, dirección, estrategia, régimen, hora y salida, calibración de la IA, candidatos y contrafactuales. No se expone a internet.
- `bot/reports/<modo>/zona_YYYY-MM-DD.md`: la zona de aprendizaje diaria, generada a las 00:05 UTC.
- `bot/reports/<modo>/reflexion_YYYY-Www.md`: la reflexión semanal (lunes 00:30 UTC), con un máximo de 3 hipótesis.
- `bot/reports/<modo>/cambios.md`: cada promoción del retador, con su evidencia.
- `bot/data/logs/bot.jsonl`: logs JSON con rotación diaria (las llaves se ocultan).

## Despliegue en un VPS

**Docker** (reinicio automático y healthcheck sobre el latido del bot):

```bash
BOT_MODE=paper docker compose up -d --build
docker compose logs -f
```

**systemd**: ver `deploy/crypto-bot.service`. Para `live` arranca a mano dentro de `tmux` porque
la confirmación en consola necesita una terminal. `deploy/healthcheck.sh` sirve para cron.

Configura Telegram (`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` en `.env`, `alerts.telegram_enabled: true`)
para recibir alertas de entradas, cierres, modo seguro, detenciones y fuentes caídas.

## Seguridad operativa (resumen)

- Los stops viven en el exchange (`STOP_MARKET` con close-position vía `/fapi/v1/algoOrder`). Si un stop no se confirma, la posición se cierra a mercado.
- La conciliación cada 30 s detecta posiciones sin stop, huérfanas o con cantidad distinta. Ante una discrepancia: alerta, modo seguro y reposición del stop (o cierre).
- Al reiniciar, las posiciones abiertas en el exchange se adoptan junto con su stop.
- Guardias globales:
  - pérdida diaria del 2 %;
  - drawdown del 10 %, que detiene el bot y requiere reinicio manual;
  - 4 pérdidas seguidas, que pausan 4 h;
  - techo de 5 entradas por día (3 si el día va en pérdida).
- El aprendizaje solo puede proponer cambios de una lista blanca (umbral de ventaja, bloqueos por condición, desactivar estrategia, parámetros de entrada u objetivo). Nunca toca riesgo, stops ni ejecución, y por defecto requiere tu aprobación.
