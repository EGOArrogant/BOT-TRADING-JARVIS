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

**Llave de la IA:** si el panel dice "Falta la llave de la IA", doble clic en `configurar_ia.bat`: pide la llave de
Anthropic, la valida y la guarda en `.env`. Sin llave el bot nunca abre operaciones (por seguridad).

**Notificaciones al teléfono:** doble clic en `configurar_telegram.bat` (te guía con @BotFather). Cada entrada,
salida y resumen diario llega a Telegram y a la campana del panel.

**Ver el panel en el celular:** doble clic en `activar_celular.bat`, reinicia `iniciar_demo.bat` y en el panel de la
PC, sección *Ver en tu celular*, escanea el QR con la cámara (mismo Wi-Fi; fuera de casa, con Tailscale). Sin la
clave del QR nadie entra. `desactivar_celular.bat` lo apaga y cambia la clave.

**Servidor en la nube (24/7, sin depender de la PC):** crea un servidor Ubuntu 24.04 (por ejemplo Oracle Cloud
"Always Free" con región de casa en México: Querétaro o Monterrey) y en su consola ejecuta
`curl -fsSL https://raw.githubusercontent.com/EGOArrogant/BOT-TRADING-JARVIS/main/deploy/instalar_servidor.sh | sudo bash`.
Instala todo, revisa que Binance responda desde ahí, crea los servicios `jarvis-bot` y `jarvis-panel` (arrancan solos)
y Tailscale: el panel solo se abre desde tus dispositivos de Tailscale y con la clave del QR que muestra al final.
Con el bot en el servidor, apaga el de la PC (dos copias con el mismo Telegram chocan).

**Comandos de Telegram:** `/estado`, `/posiciones`, `/hoy`, `/solo_cierre`, `/operar`, `/clave` (enlace y clave del panel para el celular, si el modo celular está activo), `/actualizar` (instala la versión nueva; el bot y el panel se reinician solos), `/ayuda`. Solo responde a tu chat;
ningún comando abre operaciones ni cambia límites.

**Investigación de estrategias (2026-10):** 13 meses de datos de Binance (velas 15 min, open interest, funding, F&G,
funding de Hyperliquid), backtest walk-forward (ajuste sep-25..abr-26, comprobación may-26..oct-26) con costos reales
y las guardias de riesgo activas. Solo `trend_breakout` aguantó el periodo sin tocar la detención (~−0.04R/op, PF 0.93,
~1 op/día, antes de la IA); las otras tres perdían de forma consistente y quedan apagadas en `config.yaml`. Ninguna
estrategia tiene todavía ventaja confirmada: por eso todo sigue en demo.
Pantalla de IC (`python -m bot.research.ic`, 13 meses, dos mitades): solo el cambio de open interest en 1 h predice
de forma estable la hora siguiente (IC ≈ +0.04, diferencia que paga costos), pero como filtro de `trend_breakout` no
mejoró el resultado. La pausa por bajo rendimiento lo empeoró (queda apagada). Ideas de un trader humano (vídeo): "dejar correr
las ganancias" (objetivo 4:1, 48 h, parcial en 2R) fue lo único que mejoró: positivo en ajuste y comprobación (PF 1.05);
mover el stop a "sin pérdida" en 1R, el sesgo diario de BTC y filtrar por sesiones lo empeoraron. El chequeo de mirar al futuro
(`python -m bot.backtest.lookahead`) no encontró fugas en los datos reales.
**Método de Chris Kmer** (campeón del Robbins World Cup, julio 2026; transcripción de su entrevista) en reglas como
`discount_absorption`: entorno (impulso de 1 h), ubicación (retroceso al 70,5–88,6 % de Fibonacci sin pasar del 88,6 %),
confirmación (en la vela anterior los agresores contrarios no lograron mover el precio y la vela actual gira a favor con
su extremo más adentro), stop detrás de esos agresores y objetivo en el extremo del impulso (≥ 1,5 a 1). Con los 13
meses: +0,41R por operación sola (ajuste +0,44 / comprobación +0,36, PF ≈ 1,5, 104 operaciones); **sin la confirmación
pierde** (−0,16R en 1018 entradas), igual que con la zona del 61,8 % (−0,29R): exactamente su punto de "no anticipar".
Junto a `trend_breakout` y con las guardias reales: +0,066R por operación (+0,041 / +0,103) contra +0,036R de
`trend_breakout` sola, sin tocar la detención. Su regla de parar tras 2 pérdidas seguidas (pensada para el tilt humano)
empeoró los resultados y llevó a la detención: no se adoptó. Implementación idéntica a la del laboratorio (10/10
operaciones iguales) y sin fugas de futuro.
Niveles de estructura (giros de 1 h, Fibonacci, espacio hasta soporte/resistencia; ~200 días de velas de 1 h de
Hyperliquid, las 6 monedas): la posición dentro del último tramo (`ind.1h.fib_pos`) predice débil pero con el mismo
signo en todas (IC ≈ −0.05 a 4 h: arriba del tramo, retroceso leve). El espacio hasta el siguiente nivel no hizo
peores las rupturas, al contrario de la regla típica. Por eso solo son contexto para la IA y el diario; no filtran.

**Mercado como terminal:** a todo lo ancho, como Hyperliquid: arriba la moneda (menú con las 6), precio de marca,
cambio y rango de 24 h, volumen, interés abierto y funding con cuenta regresiva; al centro el gráfico grande; a la
derecha **Análisis JARVIS** (qué condiciones de la estrategia se cumplen y cuáles faltan, la operación abierta y los
mismos datos que recibe la IA) o el **Libro** de órdenes en vivo; abajo posiciones, señales, operaciones (clic = ir a
ese momento del gráfico) y traders destacados.

**Cuenta MIDAS (opera con dinero ficticio):** una cuenta aparte de la demo (500 US$ ficticios) que entra con las
señales de las estrategias poniendo un margen fijo (20 US$) a x5–x25. Con "todas las señales" deja de lado la
disciplina; con "solo las que aprueba la IA" entra cuando la IA dice que sí. Mantiene el stop y el objetivo del bot y
simula la liquidación. Se enciende, apaga y ajusta en la pestaña MIDAS; avisa cada entrada y salida (campana y
Telegram). Solo existe en modo demo: con dinero real el bot no la carga.

**MIDAS (laboratorio de apalancamiento):** pestaña del panel que toma cada señal del bot (operadas y rechazadas)
con su misma entrada, stop, objetivo y tiempo, y calcula qué habría pasado poniendo un margen fijo (20 US$ por
defecto) a x5, x10, x15, x20 y x25, incluidas las liquidaciones. Compara "solo las aprobadas" contra "todas las
señales (sin disciplina)" y saca lecciones en palabras simples, que también recibe la reflexión semanal. Solo
simula: nunca envía órdenes ni cambia el riesgo del bot.

**Gráfico del panel (Mercado):** herramientas de dibujo como TradingView en la barra de la izquierda (arriba en el
celular): línea de tendencia, rayo, horizontal, vertical, canal, tridente, Fibonacci, pincel, texto, emoji, posición
larga/corta (con R:R), regla, zoom, imán, bloquear, ocultar y borrar. Se guardan por moneda en ese navegador. El botón
**JARVIS** muestra lo que hace el bot: cada operación como caja (entrada, stop, objetivo, salida), la posición abierta
con su stop actual e invalidación, las señales que descartó (rombos) y el rango de 6 h cuya ruptura vigila
`trend_breakout`; opcional, soportes/resistencias y Fibonacci de 1 h (los mismos niveles que recibe la IA). Toca una
caja o un rombo para ver por qué entró o no y qué dijo la IA.

**Actualizar:** cierra el bot y el panel y haz doble clic en `actualizar.bat`: baja la versión más reciente de GitHub
y reemplaza solo el código (no toca `.env`, `bot/data` ni `bot/reports`).

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
