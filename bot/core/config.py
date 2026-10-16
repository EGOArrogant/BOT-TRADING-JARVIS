"""Carga y validación de config.yaml + .env.

Los topes duros (HARD_*) viven en código: ningún valor de config.yaml ni
del módulo de aprendizaje puede superarlos (CLAUDE.md §1).
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

HARD_MAX_TRADES_PER_DAY = 5
HARD_MAX_LEVERAGE = 3
HARD_MAX_RISK_PER_TRADE = 0.01
HARD_MAX_DAILY_LOSS = 0.05
HARD_MAX_DRAWDOWN = 0.20

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "bot" / "config.yaml"


class Mode(StrEnum):
    RECORD = "record"
    BACKTEST = "backtest"
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Fees(_Strict):
    maker: float = Field(ge=0, le=0.01)
    taker: float = Field(ge=0, le=0.01)
    bnb_discount: float = Field(default=0.0, ge=0, lt=1)


class ExchangeConfig(_Strict):
    name: str = Field(pattern="^(binance|bybit)$")
    market: str = "usdm_futures"
    symbols: list[str] = Field(min_length=1)
    verify_symbols_on_start: bool = True
    fees: Fees
    recv_window_ms: int = Field(default=5000, gt=0, le=60000)


class RiskConfig(_Strict):
    risk_per_trade: float = Field(gt=0, le=HARD_MAX_RISK_PER_TRADE)
    max_daily_loss: float = Field(gt=0, le=HARD_MAX_DAILY_LOSS)
    max_drawdown: float = Field(gt=0, le=HARD_MAX_DRAWDOWN)
    max_consecutive_losses: int = Field(ge=1)
    consecutive_loss_pause_hours: float = Field(gt=0)
    max_leverage: float = Field(gt=0, le=HARD_MAX_LEVERAGE)
    max_concurrent_positions: int = Field(ge=1, le=6)
    max_net_exposure: float = Field(gt=0, le=HARD_MAX_LEVERAGE)
    max_positions_per_symbol: int = Field(ge=1, le=1)
    min_stop_atr_multiple: float = Field(ge=1.0)
    high_vol_percentile: float = Field(gt=0, lt=100)
    high_vol_size_factor: float = Field(gt=0, le=1)
    correlation_threshold: float = Field(gt=0, le=1)
    correlation_size_factor: float = Field(gt=0, le=1)
    max_fee_share_of_target: float = Field(gt=0, lt=1)


class LimitsConfig(_Strict):
    max_trades_per_day: int = Field(ge=0, le=HARD_MAX_TRADES_PER_DAY)
    base_trades_per_day: int = Field(ge=0)
    extra_trade_min_edge: float = Field(ge=3.0)
    extra_trade_min_confidence: float = Field(ge=0, le=1)
    losing_day_trade_cap: int = Field(ge=0)
    losing_day_loss_count: int = Field(ge=1)
    losing_day_loss_pct: float = Field(gt=0)
    max_trades_per_symbol_per_day: int = Field(ge=1)
    symbol_cooldown_minutes: int = Field(ge=0)
    reversal_cooldown_minutes: int = Field(ge=0)
    candidate_window_minutes: int = Field(ge=1)

    @model_validator(mode="after")
    def _caps_consistent(self) -> LimitsConfig:
        if self.base_trades_per_day > self.max_trades_per_day:
            raise ValueError("base_trades_per_day no puede superar max_trades_per_day")
        if self.losing_day_trade_cap > self.max_trades_per_day:
            raise ValueError("losing_day_trade_cap no puede superar max_trades_per_day")
        return self


class FiltersConfig(_Strict):
    min_edge_cost_multiple: float = Field(ge=3.0)
    macro_block_before_min: int = Field(ge=0)
    macro_block_after_min: int = Field(ge=0)
    dangerous_funding_z: float = Field(gt=0)
    oi_high_lookback_days: int = Field(ge=1)
    extreme_vol_percentile: float = Field(gt=0, le=100)
    max_spread_bps: float = Field(gt=0)
    min_depth_multiple: float = Field(gt=0)


class ExecutionConfig(_Strict):
    post_only_timeout_s: float = Field(gt=0)
    post_only_retries: int = Field(ge=0, le=3)
    market_entry_min_edge: float = Field(ge=4.0)
    stop_confirm_timeout_s: float = Field(gt=0, le=30)
    reconcile_interval_s: float = Field(gt=0, le=300)


class PositionConfig(_Strict):
    partial_take_r: float = Field(gt=0)
    partial_take_fraction: float = Field(gt=0, lt=1)
    trailing_atr_multiple: float = Field(gt=0)
    time_exit_hours: float = Field(gt=0)
    time_exit_min_r: float = Field(ge=0)


class AIConfig(_Strict):
    decision_model: str
    reflection_model: str
    news_model: str
    timeout_s: float = Field(gt=0)
    max_calls_per_hour: int = Field(ge=0)
    max_calls_per_day: int = Field(ge=0)
    max_news_headlines: int = Field(ge=0, le=8)
    max_active_lessons: int = Field(ge=0, le=10)
    schema_retries: int = Field(ge=0, le=1)
    decision_effort: str = Field(default="medium", pattern="^(low|medium|high|xhigh|max)$")
    news_effort: str = Field(default="low", pattern="^(low|medium|high|xhigh|max)$")
    refusal_fallback: bool = True      # fallback server-side ante rechazos de seguridad
    max_news_calls_per_hour: int = Field(default=20, ge=0)


class DataConfig(_Strict):
    ttl_s: dict[str, float]
    poll_s: dict[str, float]
    news_max_age_min: int = Field(gt=0)
    dir: Path
    raw_retention_days: int = Field(default=14, ge=2)   # aggTrade/book20 crudos; lo demás no se borra
    min_free_gb: float = Field(default=5.0, ge=0.5)      # bajo esto se borran los crudos más viejos


class LearningConfig(_Strict):
    require_human_approval: bool = True
    challenger_min_cases: int = Field(ge=50)
    challenger_min_trades: int = Field(ge=20)
    lesson_expiry_days: int = Field(gt=0)
    max_hypotheses_per_week: int = Field(ge=1, le=3)


class AlertsConfig(_Strict):
    telegram_enabled: bool = False


class LoggingConfig(_Strict):
    dir: Path
    level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
    backup_days: int = Field(default=30, ge=1)


class Secrets(BaseModel):
    """Valores de .env. Nunca se registran en logs (repr oculto)."""

    model_config = ConfigDict(frozen=True)

    binance_api_key: str | None = Field(default=None, repr=False)
    binance_api_secret: str | None = Field(default=None, repr=False)
    binance_testnet_api_key: str | None = Field(default=None, repr=False)
    binance_testnet_api_secret: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    cryptopanic_api_key: str | None = Field(default=None, repr=False)
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)
    confirm_live_trading: bool = False

    @classmethod
    def from_env(cls) -> Secrets:
        def get(name: str) -> str | None:
            value = os.getenv(name, "").strip()
            return value or None

        return cls(
            binance_api_key=get("BINANCE_API_KEY"),
            binance_api_secret=get("BINANCE_API_SECRET"),
            binance_testnet_api_key=get("BINANCE_TESTNET_API_KEY"),
            binance_testnet_api_secret=get("BINANCE_TESTNET_API_SECRET"),
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            cryptopanic_api_key=get("CRYPTOPANIC_API_KEY"),
            telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=get("TELEGRAM_CHAT_ID"),
            confirm_live_trading=os.getenv("CONFIRM_LIVE_TRADING", "").strip().lower() == "true",
        )


class EndpointSet(_Strict):
    rest: str
    ws_public: str
    ws_market: str
    ws_private: str


class EndpointsConfig(_Strict):
    """Rutas verificadas contra la doc oficial (ver CLAUDE.md, notas de implementación)."""

    binance: EndpointSet
    binance_testnet: EndpointSet
    deribit: str
    hyperliquid: str
    alternative_me: str
    defillama_stablecoins: str
    stellar_horizon: str
    forexfactory_week: str
    cryptopanic: str


class NewsConfig(_Strict):
    rss_feeds: list[str]
    trusted_sources: list[str]
    high_impact_keywords: list[str]
    symbol_keywords: dict[str, list[str]]
    dedupe_similarity: float = Field(default=0.8, gt=0, le=1)


class StrategyParams(BaseModel):
    """Parámetros de estrategia: lo ÚNICO que el aprendizaje puede proponer cambiar."""

    model_config = ConfigDict(extra="allow", frozen=True)

    enabled: bool = True
    target_atr_multiple: float = Field(default=2.0, gt=0)
    stop_atr_multiple: float = Field(default=1.2, ge=1.0)
    max_hold_hours: float = Field(default=6.0, gt=0)


class PaperConfig(_Strict):
    initial_capital: float = Field(gt=0)
    latency_ms: int = Field(default=150, ge=0)


class StorageConfig(_Strict):
    db_path: Path
    reports_dir: Path
    state_path: Path


class TopTradersConfig(_Strict):
    """Observación de traders destacados de Hyperliquid (solo datos de contexto; nunca se copian)."""

    enabled: bool = True
    leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    count: int = Field(default=30, ge=1, le=60)
    refresh_hours: float = Field(default=6.0, gt=0)
    poll_s: float = Field(default=120.0, ge=30)
    min_account_usd: float = Field(default=100_000, ge=0)
    max_account_usd: float = Field(default=50_000_000, gt=0)   # más grande suele ser bóveda o protocolo
    max_monthly_roi: float = Field(default=3.0, gt=0)          # +300 % al mes suele ser suerte o cuenta nueva
    min_monthly_turnover: float = Field(default=1.0, ge=0)     # volumen/capital: descarta cuentas inactivas
    max_monthly_turnover: float = Field(default=300.0, gt=0)   # ...y creadores de mercado


class Settings(_Strict):
    mode: Mode = Mode.PAPER
    exchange: ExchangeConfig
    endpoints: EndpointsConfig
    news: NewsConfig
    strategies: dict[str, StrategyParams]
    paper: PaperConfig
    storage: StorageConfig
    risk: RiskConfig
    limits: LimitsConfig
    filters: FiltersConfig
    execution: ExecutionConfig
    position: PositionConfig
    ai: AIConfig
    data: DataConfig
    learning: LearningConfig
    alerts: AlertsConfig
    logging: LoggingConfig
    top_traders: TopTradersConfig = Field(default_factory=TopTradersConfig)


def load_settings(path: Path | str = DEFAULT_CONFIG_PATH, mode: Mode | str | None = None) -> Settings:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if mode is not None:
        raw["mode"] = str(mode)
    # Rutas relativas se resuelven contra la raíz del proyecto, no contra el cwd.
    for section, key in (("data", "dir"), ("logging", "dir"), ("storage", "db_path"),
                         ("storage", "reports_dir"), ("storage", "state_path")):
        d = raw.get(section, {}).get(key)
        if d is not None and not Path(d).is_absolute():
            raw[section][key] = PROJECT_ROOT / d
    return Settings.model_validate(raw)


def load_secrets(env_file: Path | str | None = None) -> Secrets:
    load_dotenv(env_file, override=False)
    return Secrets.from_env()
