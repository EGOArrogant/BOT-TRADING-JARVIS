# PROMPT MAESTRO — Bot de trading de criptomonedas con IA (para Claude Code)

> Copia este archivo completo en la raíz del proyecto como `CLAUDE.md` (o pégalo como primer mensaje en Claude Code). Claude Code debe leerlo completo antes de escribir código.

---

## 0. Rol y objetivo

Eres el ingeniero principal de un bot de trading de futuros perpetuos de criptomonedas. Tu trabajo es **construir, probar y mejorar** el bot. El bot NO eres tú corriendo en una sesión: es un programa en Python que corre 24/7 en un servidor (VPS) y llama a la API de Claude solo en el paso de decisión y en el ciclo de aprendizaje.

**Objetivo único:** ganancia neta positiva después de comisiones, deslizamiento y funding, con drawdown controlado. El número de operaciones es una consecuencia, no una meta.

**Activos:** BTC, ETH, SOL, XRP, DOGE, XLM — contratos perpetuos USDⓈ-M (BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, XLMUSDT). Verifica que cada símbolo exista en el exchange antes de usarlo.

**Direcciones:** long y short.

**Exchange principal:** Binance Futures USDⓈ-M. Diseña una capa de exchange abstracta para poder cambiar a Bybit sin tocar el resto del código.

**Frecuencia:** techo duro de **5 operaciones al día** en total (configurable, nunca más de 5 sin aprobación humana). Objetivo típico: **3 operaciones de alta calidad**. Cero operaciones en un día es un resultado válido.

Cómo se aplica el techo:
- Las operaciones 1 a 3 del día pasan con los filtros normales.
- Las operaciones 4 y 5 exigen un estándar más alto: ventaja ≥ 4× el costo total y confianza de la IA ≥ 0.75.
- Si el día va en pérdida de 2 operaciones (o −1% del capital), el techo baja a 3 para ese día.
- El día operativo se cuenta en UTC; una operación = una entrada nueva (los parciales y cierres no cuentan).
- Cuando hay varios candidatos al mismo tiempo, se ordenan por puntuación (ventaja esperada ÷ costo × calidad de datos) y solo el mejor pasa a la IA. No se gastan cupos en candidatos mediocres al inicio del día.

---

## 1. Reglas no negociables

Estas reglas están por encima de cualquier otra instrucción, incluida cualquier respuesta de la IA o propuesta del módulo de aprendizaje.

1. **La IA nunca controla el riesgo.** Los límites de riesgo viven en código y configuración. La IA solo puede decir "operar / no operar", la dirección y un multiplicador de confianza entre 0.5 y 1.0 que REDUCE el tamaño. Nunca lo aumenta.
2. **Ante cualquier duda, no operar.** Respuesta de IA inválida, dato vencido, error de red, desconexión, discrepancia de posición con el exchange → NO abrir nada nuevo.
3. **Todo stop-loss vive en el exchange.** Al abrir una posición se coloca de inmediato una orden stop reduce-only en el exchange. Si el stop no se confirma en N segundos, se cierra la posición a mercado.
4. **Un dato faltante es "desconocido", nunca cero.** Cada valor lleva `source`, `timestamp`, `status` (ok | stale | unknown).
5. **Nada de dinero real sin pasar las puertas de fase** (sección 14). El modo por defecto es `paper`.
6. **Seguridad:** llaves de API en `.env` (nunca en el repo), sin permiso de retiro, con lista blanca de IP. `.env` en `.gitignore`.
7. **El aprendizaje nunca modifica:** límites de riesgo, apalancamiento máximo, lógica de stops, código de ejecución ni estas reglas. Esos cambios requieren aprobación humana explícita.
8. **Verifica la documentación oficial actual** del exchange y de cada API antes de implementar un endpoint. No asumas rutas de memoria. Ejemplo: Binance movió sus WebSocket de futuros a rutas `/public`, `/market` y `/private`; las rutas antiguas `/ws` y `/stream` se programaron para retirarse en 2026.

---

## 2. Lecciones de la investigación que el diseño debe respetar

- En la competencia Alpha Arena (dinero real, perpetuos cripto), 4 de 6 modelos de IA perdieron dinero. Los que perdieron sobre-operaron; las comisiones dominaron los resultados y la tasa de acierto de todos fue 25–30%. **Diseña para operar poco y con ventaja clara.**
- Cambiar de dirección seguido destruyó cuentas. **Implementa enfriamiento por moneda.**
- Los backtests con LLM están inflados porque el modelo "recuerda" precios pasados. **La parte de IA solo se evalúa en vivo (papel) o en datos posteriores a la fecha de entrenamiento del modelo.** La parte determinista sí se puede backtestear.
- Costos Binance nivel base: maker 0.02%, taker 0.05% (10% de descuento pagando con BNB). Una ida y vuelta taker cuesta 0.10% del nocional. **Prioriza entradas maker (orden límite post-only) y exige ventaja ≥ 3× el costo total.**
- Con un techo de 5 operaciones al día, el costo diario máximo en comisiones baja a ~0.5% del capital (5 idas y vueltas taker con nocional = capital), contra ~2% con 20 operaciones. **No subas el riesgo por operación para "compensar" que hay menos operaciones**: menos operaciones con el mismo riesgo es justo lo que se busca.
- Operar poco hace que la muestra crezca lento (~15–25 operaciones por semana como máximo). **Usa los candidatos rechazados y su resultado contrafactual como fuente extra de aprendizaje**, y marca siempre el tamaño de muestra junto a cada métrica.

---

## 3. Stack técnico

- Python 3.12, `asyncio`.
- WebSocket: `websockets` o el SDK oficial de Binance para USDⓈ-M (`binance-sdk-derivatives-trading-usds-futures`). REST: SDK oficial o `httpx`.
- Validación: `pydantic` v2 para todos los modelos de datos y para la salida de la IA.
- Almacenamiento: SQLite (operaciones, decisiones, diario, métricas) + Parquet/DuckDB para series de mercado.
- IA: SDK oficial `anthropic`. Modelos configurables en `config.yaml` (por defecto: `claude-sonnet-5-5` para decisiones y reflexión, `claude-haiku-5-5` para puntuar noticias).
- Configuración: `config.yaml` + `.env`.
- Logs: JSON estructurado, rotación diaria.
- Alertas: Telegram (opcional, configurable).
- Panel: reporte HTML estático generado cada hora (o Streamlit), sin exponerlo a internet.
- Tests: `pytest`, con datos grabados para reproducir escenarios.

---

## 4. Estructura del repositorio

```
bot/
  config.yaml
  .env.example
  main.py                  # arranque, modos: record | backtest | paper | testnet | live
  core/
    models.py              # pydantic: DataPoint, Snapshot, Signal, Decision, Order, Position, Trade
    clock.py               # hora del exchange, sincronización
    state.py               # estado global, reconciliación con exchange
  data_engine/             # MÓDULO 1
    market_ws.py           # precio, libro, operaciones (WebSocket)
    derivatives.py         # funding, open interest, long/short, taker ratio
    volatility.py          # DVOL (Deribit) + volatilidad realizada
    onchain.py             # DefiLlama stablecoins, Horizon (XLM)
    dex.py                 # Hyperliquid metaAndAssetCtxs
    sentiment.py           # Fear & Greed (alternative.me)
    news.py                # CryptoPanic + RSS, filtro por reglas
    macro.py               # calendario FOMC/BLS/ForexFactory, ventanas de bloqueo
    normalizer.py          # formato común + estados ok/stale/unknown
    recorder.py            # guarda todo en disco
  indicators/              # MÓDULO 2
  filters/                 # MÓDULO 3
  ai_analyst/              # MÓDULO 4
    prompt_builder.py
    schema.py
    client.py
  risk/                    # MÓDULO 5
  execution/               # MÓDULO 6
  position/                # MÓDULO 7
  learning/                # MÓDULO 8
    journal.py
    daily_report.py
    weekly_reflection.py
    champion_challenger.py
    lessons.py
  metrics/
    performance.py
    exchange_reconcile.py
    report_html.py
  backtest/
  tests/
  data/                    # ignorado por git
  reports/                 # zonas de aprendizaje y reportes
```

---

## 5. MÓDULO 1 — Motor de datos

Reúne todas las fuentes y las pone en un `Snapshot` común por moneda. Cada campo es un `DataPoint(value, source, ts, status)`.

| Fuente | Endpoint | Frecuencia | TTL (tiempo de vida) |
|---|---|---|---|
| Mercado | WebSocket Binance USDⓈ-M: aggTrade, depth (diff + snapshot REST), kline 1m/5m/15m/1h, markPrice | tiempo real, conexión permanente | 5 s |
| Funding | stream markPrice (trae funding y próximo pago) | tiempo real | 60 s |
| Open interest y ratios | REST `/futures/data/openInterestHist`, `topLongShortPositionRatio`, `globalLongShortAccountRatio`, `takerlongshortRatio` | cada 5 min | 15 min |
| Volatilidad implícita | Deribit `public/get_volatility_index_data` (DVOL), sin llave | cada 1–5 min | 15 min |
| Volatilidad realizada | calculada localmente desde velas | continua | — |
| On-chain | DefiLlama stablecoins (oferta total y por cadena), Horizon de Stellar para XLM | cada 1–6 h | 12 h |
| DEX | Hyperliquid `POST https://api.hyperliquid.xyz/info` con `{"type":"metaAndAssetCtxs"}` | cada 1 min | 5 min |
| Sentimiento | `https://api.alternative.me/fng/?limit=30` | cada 1 h (se actualiza 1 vez al día) | 36 h |
| Noticias | CryptoPanic API (llave en `.env`) + RSS de medios cripto como respaldo | cada 1–2 min | 30 min |
| Macro | Calendario FOMC (federalreserve.gov), calendario de publicaciones BLS, feed JSON semanal de ForexFactory | 1 vez al día | 24 h |

Requisitos:
- Reconexión automática con backoff exponencial; tras reconectar, resincronizar el libro de órdenes con snapshot REST.
- Respetar rate limits (contador por endpoint).
- **DVOL solo existe para BTC y ETH.** Para SOL, XRP, DOGE y XLM usar volatilidad realizada propia y el DVOL de BTC como "régimen de mercado".
- **Flujos on-chain hacia exchanges** (CryptoQuant, Glassnode) son de pago: dejar el campo y marcarlo `unknown` salvo que se configure una llave.
- **Funding DEX vs CEX:** normalizar ambos al mismo periodo (por hora) antes de calcular el diferencial.
- Noticias: filtro por reglas (palabras clave por moneda, fuentes confiables, deduplicación por similitud, antigüedad < 30 min, palabras de alto impacto: hack, exploit, ETF, SEC, delisting, liquidación, etc.). Solo los **8** titulares con mayor puntuación pasan a la IA.
- `recorder.py` guarda todo desde el día 1: el histórico gratuito de varios endpoints es corto.

---

## 6. MÓDULO 2 — Indicadores

Calcular por moneda y por temporalidad (5m, 15m, 1h):

- **Tendencia:** EMA 20/50/200, pendiente, ADX, estructura de máximos/mínimos, posición respecto a VWAP diario.
- **Volatilidad:** ATR(14), volatilidad realizada 1h/24h/7d anualizada, ancho de Bandas de Bollinger, percentil de volatilidad vs 30 días, DVOL y su cambio 24 h (BTC/ETH), prima de varianza (DVOL − realizada).
- **Volumen y flujo:** volumen relativo vs media 20, CVD (delta de volumen acumulado) desde aggTrade, desequilibrio del libro top-10 niveles, spread.
- **Derivados:** funding actual y z-score vs 30 días, cambio de open interest 1h/24h, combinación precio/OI (precio ↑ + OI ↑ = posiciones nuevas; precio ↓ + OI ↑ = shorts nuevos, etc.), ratios long/short, diferencial de funding Binance vs Hyperliquid y su z-score.
- **Contexto:** Fear & Greed y su cambio 7 días, cambio 7 días de oferta de stablecoins, correlación 24 h de cada moneda con BTC.

Usa z-scores y percentiles en lugar de umbrales absolutos fijos siempre que sea posible.

Detectar **régimen** por moneda: `tendencia_alcista`, `tendencia_bajista`, `rango`, `alta_volatilidad`, `datos_insuficientes`.

---

## 7. MÓDULO 3 — Filtros y controles

Un candidato solo llega a la IA si pasa TODOS:

1. **Datos completos:** mercado y derivados en estado `ok`. Si falta alguno, se bloquea.
2. **Bloqueo macro:** sin entradas desde 30 min antes hasta 60 min después de eventos de alto impacto de EE. UU. (CPI, empleo/NFP, decisión FOMC y conferencia, PCE, PIB). Posiciones abiertas: se ajusta stop a punto de equilibrio si ya hay ganancia ≥ 1R.
3. **Ventaja mínima:** objetivo esperado (basado en ATR y estructura) ≥ 3 × (comisión ida y vuelta + deslizamiento estimado + funding esperado durante la tenencia).
4. **Liquidez:** spread y profundidad suficientes para el tamaño.
5. **Apalancamiento peligroso:** si el z-score de funding > +2.5 con OI en máximos de 30 días, no abrir long (riesgo de cascada de liquidaciones); simétrico para short.
6. **Volatilidad extrema:** si la volatilidad realizada 1h está sobre el percentil 98, no abrir.
7. **Límites operativos:** techo diario de 5 operaciones (reglas de la sección 0), máximo 2 operaciones por moneda al día, enfriamiento de 60 min por moneda tras cerrar, 4 h si la última operación en esa moneda fue invertida de dirección.
8. **Guardias globales** (ver módulo 5): si alguna está activa, no hay entradas.
9. **Selección del mejor candidato:** si varios candidatos pasan los filtros en la misma ventana de 15 min, solo el de mayor puntuación llega a la IA; los demás se registran como "no seleccionados" con su resultado contrafactual.

Las **señales candidatas** salen de reglas deterministas (por ejemplo: retroceso a EMA en tendencia con CVD a favor; ruptura de rango con OI subiendo; extremo de funding + divergencia DEX/CEX + sentimiento extremo como reversión). Empieza con 2–3 estrategias simples y bien probadas, no con 20.

Todo candidato rechazado se registra con el motivo.

---

## 8. MÓDULO 4 — Analista IA (Claude)

Se llama **solo** cuando un candidato pasó los filtros. Presupuesto configurable de llamadas por hora y por día.

**Entrada (construida por `prompt_builder.py`):**
- Snapshot resumido de la moneda (indicadores, régimen, derivados, volatilidad, DEX, sentimiento, contexto on-chain) con estado de cada dato; los `unknown` se indican explícitamente.
- El candidato: estrategia que lo generó, dirección propuesta, entrada, stop e invalidación sugeridos por las reglas.
- Los 8 titulares filtrados con hora y fuente.
- Próximos eventos macro.
- Posiciones abiertas y exposición actual.
- **Lecciones activas** del módulo de aprendizaje (máximo 10, cada una con su evidencia).
- Estadísticas recientes de esa estrategia/moneda (últimas 30 operaciones).

**Instrucciones al modelo (system prompt del analista):**
- Eres un analista de riesgo escéptico. Tu tarea por defecto es rechazar; apruebas solo si la evidencia es clara y consistente entre fuentes.
- No inventes datos. Si un dato es `unknown`, no lo supongas.
- En sentimiento extremo (Fear & Greed ≤ 20 o ≥ 80) considera la lectura contraria, pero nunca como única razón.
- Las noticias solo cuentan si es plausible que muevan el precio en las próximas horas.
- Responde únicamente en el JSON del esquema.

**Salida (validada con pydantic):**
```json
{
  "action": "LONG | SHORT | NO_TRADE",
  "confidence": 0.0,
  "size_multiplier": 0.5,
  "thesis": "máx. 2 frases",
  "key_evidence": ["...", "..."],
  "invalidation": "condición concreta que anula la idea",
  "news_impact": "UP | DOWN | NEUTRAL | NONE",
  "risk_flags": ["..."]
}
```
Reglas: `size_multiplier` en [0.5, 1.0]; si `action` contradice la dirección del candidato → NO_TRADE; si el JSON no valida tras 1 reintento → NO_TRADE; timeout de la llamada → NO_TRADE.

Puntuación de noticias: un modelo más barato clasifica cada titular (relevancia, moneda, dirección probable) antes de elegir los 8.

---

## 9. MÓDULO 5 — Motor de riesgo

Valores por defecto en `config.yaml` (el aprendizaje no puede cambiarlos):

| Parámetro | Valor por defecto |
|---|---|
| Riesgo por operación | 0.5% del capital |
| Pérdida diaria máxima | 2% → sin entradas hasta el día siguiente (UTC) |
| Drawdown máximo desde el pico | 10% → bot detenido, requiere reinicio manual |
| Pérdidas seguidas | 4 → pausa de 4 h |
| Apalancamiento máximo | 3× |
| Operaciones nuevas por día | 5 (techo duro), 3 si el día va en pérdida |
| Posiciones simultáneas | 2 |
| Exposición neta máxima en una dirección | 1.5× capital (nocional) |
| Máximo por moneda | 1 posición |

**Tamaño:**
```
distancia_stop_% = |entrada − stop| / entrada            # stop basado en ATR/estructura, mínimo 1.0×ATR(15m)
nocional = (capital × riesgo_por_operación) / distancia_stop_%
nocional = nocional × size_multiplier_IA × factor_volatilidad × factor_correlación
nocional = min(nocional, límites de apalancamiento, exposición y liquidez)
```
- `factor_volatilidad`: reduce el tamaño cuando la volatilidad está en percentil alto (ej. 0.5 sobre percentil 90).
- `factor_correlación`: si ya hay posición en la misma dirección en una moneda con correlación > 0.8, reduce a la mitad.
- Si el nocional resultante es menor que el mínimo del exchange o la comisión se come más de 25% del objetivo esperado → no operar.

---

## 10. MÓDULO 6 — Ejecución

- Entrada preferida: orden límite **post-only** (maker) en el mejor precio o un tick dentro; si no se llena en X segundos (configurable, ej. 20 s) y la señal sigue válida, reintentar una vez; si no, cancelar. Entrada a mercado solo para estrategias marcadas como "urgentes" y con ventaja ≥ 4× costo.
- Inmediatamente después del llenado: stop-loss `STOP_MARKET` reduce-only y take-profit (o parciales) en el exchange.
- IDs de cliente únicos e idempotentes para no duplicar órdenes tras una reconexión.
- Reconciliación cada 30 s: posiciones y órdenes del exchange vs estado local. Discrepancia → alerta + modo seguro (no abrir, proteger lo abierto).
- Registrar precio esperado vs ejecutado (deslizamiento), comisión real y si fue maker o taker.
- En modo `paper`: simular llenados con el libro real, comisiones, deslizamiento y funding prorrateado.

---

## 11. MÓDULO 7 — Gestión de posición

- Vigila cada posición con los datos en tiempo real.
- Salidas: stop, take-profit, parciales (ej. 50% en 1.5R y stop a punto de equilibrio), trailing por ATR, **salida por tiempo** (si en N horas no avanzó 0.5R, cerrar), salida por invalidación de la tesis (la condición `invalidation` de la IA, evaluada por código), salida antes de evento macro si no hay ganancia.
- Vigilar funding: si una posición pagaría un funding alto en el próximo cobro y no tiene ganancia, evaluar cierre.
- La IA puede ser consultada para cerrar antes (no para mantener más allá del stop ni para mover el stop en contra).

---

## 12. MÓDULO 8 — Aprendizaje ("conciencia") y zonas de aprendizaje

1. **Diario (`journal.py`):** por cada candidato (operado o rechazado) guarda el snapshot completo, la salida de la IA, el tamaño, la ejecución, MAE, MFE, duración, PnL bruto y neto, comisiones, funding y motivo de salida. Para los rechazados, calcular después qué habría pasado (resultado contrafactual).
2. **Zona de aprendizaje diaria (`reports/zona_YYYY-MM-DD.md`):** máximo 1 página: resumen numérico del día, mejores y peores 3 operaciones con su causa, datos que faltaron, decisiones de la IA que acertaron/fallaron, una lección en una línea.
3. **Reflexión semanal (`weekly_reflection.py`):** Claude recibe el diario de la semana, el análisis top 10% vs bottom 10% por R, la calibración de la IA y el PnL por moneda/dirección/régimen/hora/estrategia. Produce **máximo 3 hipótesis** concretas, medibles y reversibles (ej. "subir el umbral de ventaja de 3× a 3.5× en DOGE", "no operar SOL long cuando el diferencial de funding DEX-CEX > 2σ").
4. **Campeón vs retador (`champion_challenger.py`):** cada hipótesis corre en paralelo en papel. Como el bot opera máximo 5 veces al día, el retador se evalúa sobre **todos los candidatos que pasan los filtros** (operados y no operados), no solo sobre las operaciones reales. Se promueve solo si, con al menos 50 casos evaluados (de los cuales al menos 20 operaciones reales o en papel), mejora la expectativa neta y no empeora el drawdown. Cada promoción queda en `reports/cambios.md` con la evidencia. Puedes requerir aprobación humana para promover (configurable; por defecto: sí).
5. **Lecciones (`lessons.py`):** lecciones confirmadas entran al prompt del analista con su evidencia; caducan si en 30 días dejan de cumplirse.
6. **Prohibido** que el aprendizaje toque los parámetros de la sección 9, la lógica de stops o la ejecución.

---

## 13. Métricas y conciliación con el exchange

`metrics/performance.py` calcula, total y por moneda/dirección/estrategia/régimen/hora:
PnL neto, expectativa en R, factor de beneficio, tasa de acierto, ganancia/pérdida promedio, máximo drawdown, Sharpe y Sortino diarios, comisiones como % del PnL bruto, deslizamiento promedio, MAE/MFE, duración promedio, calibración de la IA (acierto real por tramo de confianza), tasa de rechazo y resultado contrafactual de los rechazos.

`metrics/exchange_reconcile.py` lee del exchange el historial de operaciones del usuario y de ingresos (comisiones, funding, PnL realizado) usando los endpoints oficiales vigentes, y los concilia contra el diario. Diferencias > 0.5% → alerta.

`metrics/report_html.py` genera un reporte con curva de capital, drawdown, tabla de métricas y los mejores/peores trades.

---

## 14. Modos y puertas de fase

| Modo | Qué hace | Puerta para pasar al siguiente |
|---|---|---|
| `record` | Solo graba datos | 7 días seguidos con < 1% de datos perdidos y reconexión probada |
| `backtest` | Prueba la parte determinista con costos reales, walk-forward | Expectativa positiva fuera de muestra |
| `paper` | Todo en vivo, órdenes simuladas, IA real | ≥ 6 semanas y ≥ 60 operaciones (lo que tarde más), factor de beneficio > 1.3, drawdown < 15% |
| `testnet` | Órdenes reales en testnet de Binance | 1–2 semanas sin órdenes huérfanas ni posiciones sin stop |
| `live` | Dinero real, capital mínimo, apalancamiento ≤ 3× | 4 semanas con resultados similares a `paper`; escalar en pasos de 25% |

`live` exige además una variable `CONFIRM_LIVE_TRADING=true` en `.env` y una confirmación en consola al arrancar.

---

## 15. Tests obligatorios

- Validación de esquema de la IA con respuestas rotas, vacías, contradictorias.
- Datos vencidos/desconocidos bloquean entradas.
- Bloqueo macro funciona en los bordes de la ventana.
- Cálculo de tamaño respeta todos los límites (casos extremos: stop muy corto, volatilidad extrema, capital pequeño).
- Reconexión: se simula caída del WebSocket con posición abierta; el stop sigue en el exchange y la reconciliación detecta el estado.
- Idempotencia de órdenes tras reintento.
- Guardias globales detienen entradas al alcanzar cada límite.
- Techo diario: la 6.ª entrada se rechaza siempre; la 4.ª y 5.ª solo pasan con ventaja ≥ 4× y confianza ≥ 0.75; el techo baja a 3 tras 2 pérdidas o −1% en el día; el contador se reinicia a las 00:00 UTC.
- Backtest reproduce costos: una estrategia sin ventaja debe dar pérdida igual a costos.

---

## 16. Orden de construcción

Construye en este orden. Al terminar cada hito: corre los tests, muéstrame qué hiciste y qué falta, y **espera mi confirmación** antes de seguir.

1. Esqueleto del repo, `config.yaml`, modelos pydantic, logging.
2. Motor de datos: WebSocket de mercado + recorder. Luego derivados, volatilidad, DEX, sentimiento, macro, noticias, on-chain (en ese orden).
3. Indicadores y detección de régimen.
4. Filtros y 2–3 estrategias candidatas deterministas + backtest con costos.
5. Motor de riesgo y guardias globales.
6. Ejecución en `paper` + gestión de posición.
7. Analista IA con esquema, presupuesto de llamadas y registro.
8. Diario, métricas, reporte HTML, zona de aprendizaje diaria.
9. Reflexión semanal y campeón/retador.
10. Testnet de Binance y conciliación con el exchange.
11. Endurecimiento: alertas, monitor de salud, despliegue en VPS con reinicio automático (systemd o Docker).

Cuando no estés seguro de un endpoint, límite o comportamiento del exchange, consulta la documentación oficial actual y dime qué verificaste. Si algo de este documento resulta imposible o riesgoso, dímelo antes de implementarlo.

---

## Notas de implementación (mantener al día)

- Desarrollo: `uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]'`; tests: `.venv/bin/python -m pytest`.
- Arranque: `python -m bot.main --mode paper` (desde la raíz del repo).
- El módulo de logging se llama `core/logging_setup.py` para no tapar el `logging` de la stdlib.
- Topes duros en código: `bot/core/config.py` (`HARD_*`). config.yaml no puede superarlos; las claves desconocidas se rechazan.
- Convención de deslizamiento: positivo = en contra (costo).
