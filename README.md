# Bot de perpetuos cripto con IA

Bot de futuros perpetuos USDⓈ-M (BTC, ETH, SOL, XRP, DOGE, XLM) que opera poco y con ventaja clara:
reglas deterministas generan candidatos, filtros duros los depuran, Claude actúa como analista
escéptico y el código controla todo el riesgo. La especificación completa está en `CLAUDE.md`.

> **Aviso.** Operar futuros apalancados puede hacer perder todo el capital. Este software no
> garantiza ganancias. Usa siempre `paper` y `testnet` antes de `live`, y respeta las puertas de fase.

## Windows en 3 pasos (modo demo: precios reales, dinero ficticio)

1. Descarga el ZIP desde GitHub (botón verde **Code → Download ZIP**) y descomprímelo, por ejemplo en `C:\bot`.
2. Doble clic en **`instalar.bat`**. Instala solo lo necesario (incluido Python 3.12, sin tocar otros Python),
   ofrece evitar que la PC se suspenda y al final ejecuta la **revisión**: comprueba que Binance responda desde
   tu país, que Hyperliquid responda y te pide la llave de Anthropic (se guarda en `.env`, que nunca se sube).
3. Doble clic en **`iniciar_demo.bat`**. Arranca el bot en modo `paper` (se reinicia solo si se cae) y abre el
   panel en el navegador. Para apagarlo, cierra su ventana. El panel también se abre con `abrir_panel.bat`.

`python -m bot.revisar` repite la revisión cuando quieras.

**Notificaciones al teléfono:** doble clic en `configurar_telegram.bat` (te guía con @BotFather). Cada entrada,
salida y resumen diario llega a Telegram y a la campana del panel.

**Ver el panel en el celular:** doble clic en `activar_celular.bat`, reinicia `iniciar_demo.bat` y en el panel de la
PC, sección *Ver en tu celular*, escanea el QR con la cámara (mismo Wi-Fi; fuera de casa, con Tailscale). Sin la
clave del QR nadie entra. `desactivar_celular.bat` lo apaga y cambia la clave.

**Copia dañada:** antes de cada arranque `iniciar_demo.bat` ejecuta `python -m bot.integridad`, que importa cada
módulo y comprueba los archivos de datos. Si la carpeta mezcla versiones (un ZIP nuevo extraído encima de uno
viejo), no entra en un ciclo de reinicios: se detiene y explica cómo reinstalar en una carpeta nueva.

**Apagones y reinicios:** el modo demo guarda cada minuto (y al abrir o cerrar una operación) su saldo
ficticio, sus posiciones y sus stops en `bot/data/state_paper_exchange.json`, y al volver a arrancar continúa
desde ahí; si el precio tocó un stop mientras estaba apagado, se cierra al precio actual, como un hueco real.
`activar_inicio_automatico.bat` lo arranca solo al entrar a Windows (`quitar_inicio_automatico.bat` lo quita).

**Pensado para quedarse solo semanas:**
- *Disco:* lo crudo de alto volumen (cada operación del mercado y el libro) ocupa ~1 GB/día. Cada día se comprime
  lo anterior, se borra lo de alto volumen con más de `data.raw_retention_days` (14) y, si quedan menos de
  `data.min_free_gb` (5 GB), se borra lo más viejo. Velas, funding y series REST nunca se borran.
- *Moneda en mantenimiento o retirada:* el bot sigue con las demás y bloquea sus entradas; lo revisa cada hora.
- *Una moneda deja de mandar velas:* la ventana de 15 min se evalúa igual a los 2 min (esa moneda queda
  bloqueada por datos vencidos) en vez de esperar para siempre.
- *Reloj de la PC desfasado:* se mide contra Binance al arrancar y cada hora, y se compensa.
- *IA sin saldo o llave inválida:* el panel lo avisa arriba con el motivo (mientras tanto no opera).
- *Bitácora:* toda alerta (entradas, cierres, fuentes caídas, modo seguro) queda en el diario y se ve en el panel.

**Actualizar a una versión nueva:** cierra las ventanas del bot, descarga el ZIP nuevo y copia su contenido
encima de `C:\bot` aceptando reemplazar archivos. `.env` y `bot/data/` (lo aprendido) no vienen en el ZIP,
así que se conservan. Solo hace falta volver a ejecutar `instalar.bat` si se agregan librerías nuevas.

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
python -m bot.main panel --mode paper  # abre el panel en el navegador (http://127.0.0.1:8765)
```

## Qué produce

- **Panel** (`python -m bot.main panel --mode <modo>`, en otra ventana mientras el bot corre): estado del bot
  (operando / en pausa / detenido / apagado), capital y ganancia del día, operaciones de hoy contra el techo,
  gráfico de precio de las últimas 48 h con entradas y cierres, posiciones abiertas con su stop, operaciones
  cerradas, decisiones recientes con el motivo, y las zonas "Aprendiendo" y "Aprendido". Solo lectura, solo
  en `127.0.0.1`, se actualiza cada 15 s. El bot le deja una foto cada minuto en `reports/<modo>/panel_vivo.json`.
- **Traders destacados** (`data_engine/top_traders.py`, sección `top_traders` de config.yaml): cada 6 h elige
  ~30 cuentas públicas de Hyperliquid que ganan de forma sostenida y operan de forma direccional (descarta
  bóvedas y creadores de mercado); cada 2 min lee sus posiciones. Publica por moneda `dex.smart_ratio`
  (−1 todos en short … +1 todos en long, por dinero), `dex.smart_long_n`, `dex.smart_short_n` y
  `dex.smart_net_usd`, que la IA ve como contexto y que el aprendizaje puede usar en sus hipótesis. Sus
  movimientos (abrió, cerró, aumentó, redujo, giró) se graban en `data/raw/smart_events/` y se ven en el panel.
  **No se copian operaciones.**
- **Disciplina** (`metrics/discipline.py`, en el panel, la zona diaria y la reflexión semanal): busca en los
  datos los sesgos que en una persona serían emociones: rechazar oportunidades que habrían ganado (miedo),
  aprobar perdedoras (exceso de confianza), decidir distinto tras ganar que tras perder (miedo o revancha),
  arriesgar distinto según la racha y cerrar antes de tiempo (nerviosismo). Solo opina con ≥ 10 casos por grupo.
  La IA tiene además una regla explícita de "mismo estándar siempre" y nunca recibe el resultado del día.
  **Operaciones de práctica**: cada candidato no operado se simula con los precios reales posteriores
  (contrafactual); son decenas al día, gratis y sin consultar a la IA, y alimentan el aprendizaje.
- **¿Ventaja real o suerte?** (`metrics/robustness.py`, panel y reflexión semanal): esperanza con intervalo de
  confianza 95 % (bootstrap) y acierto con intervalo de Wilson, prueba de estrés, dispersión y asimetría,
  dependencia de las 5 mejores/peores, rachas razonables (Monte Carlo) y su costo, Monte Carlo de 2.000 órdenes
  (caída típica y del 5 % peor, probabilidad de tocar la detención, rango de resultados, crecimiento compuesto),
  Kelly como referencia y MAE/MFE. Para operaciones del demo y para las de práctica. La puerta de `paper`
  exige además que el IC 95 % de la esperanza sea > 0 y que la probabilidad de tocar la detención sea < 5 %.
- **Panel, además:** modo claro/oscuro (se recuerda), avisos importantes arriba, salud del sistema (cada fuente
  de datos, IA, reloj, disco), bitácora, rendimiento por estrategia/moneda/dirección, filtro por moneda y
  descarga CSV de las operaciones, y el último reporte diario.
- **Controles del panel** (lo único que el panel puede cambiar, guardado en `reports/<modo>/control.json`):
  - *Operar / Solo cierre*: con "Solo cierre" no abre nada nuevo (los candidatos se siguen registrando para
    aprender) y sigue gestionando y cerrando lo abierto. Se revisa también justo antes de ejecutar.
  - *Agresividad 1–10*: el 10 es exactamente `config.yaml` (el máximo que permiten las reglas). Bajar el nivel
    reduce el riesgo por operación (hasta 40 %), sube la ventaja mínima (hasta +2×), exige más confianza a la IA
    (hasta 0,70) y baja el techo diario (hasta 2). Nunca supera la configuración ni los topes duros.
  - Se aplican en la siguiente ventana de 15 min, sin reiniciar. Un `control.json` ilegible = solo cierre.
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
