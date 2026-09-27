from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env and relative SQLite paths resolve from the project root, not the shell's cwd;
# otherwise running from another directory silently falls back to the PostgreSQL default.
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class TakeProfitBand:
    gain_trigger_pct: Decimal
    sell_min_pct: Decimal
    sell_max_pct: Decimal


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="CPDA_",
    )

    app_name: str = "Portfolio Signal Agent"
    environment: str = "development"
    api_prefix: str = "/api"
    debug: bool = False
    timezone: str = "Europe/Brussels"
    default_quote_currency: str = "USD"

    database_url: str = (
        "postgresql+psycopg://portfolio_user:portfolio_pass@localhost:5432/portfolio_db"
    )

    min_position_weight_pct: Decimal = Decimal("3")

    market_provider_order: str = "binance,coingecko"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_demo_api_key: str | None = None
    binance_base_url: str = "https://api.binance.com"
    http_timeout_seconds: float = 20.0

    telegram_bot_token: str | None = None
    telegram_polling_enabled: bool = True
    # Scanner message language (catalog in app/telegram/i18n.py); English can be added.
    telegram_language: Literal["ru"] = "ru"

    scheduler_enabled: bool = True
    monitoring_interval_minutes: int = 5
    morning_digest_hour: int = 8
    morning_digest_minute: int = 0
    price_snapshot_retention_days: int = 30
    quote_cache_ttl_seconds: int = 60

    abnormal_rise_15m_pct: Decimal = Decimal("5")

    extreme_pump_1h_pct: Decimal = Decimal("15")
    extreme_pump_4h_pct: Decimal = Decimal("30")
    extreme_pump_24h_pct: Decimal = Decimal("40")

    pullback_healthy_pct: Decimal = Decimal("8")
    pullback_caution_pct: Decimal = Decimal("12")
    pullback_weak_pct: Decimal = Decimal("20")
    pullback_lookback_hours: int = 48

    abnormal_rise_1h_low_pct: Decimal = Decimal("6")
    abnormal_rise_1h_medium_pct: Decimal = Decimal("10")
    abnormal_rise_1h_high_pct: Decimal = Decimal("15")
    abnormal_rise_4h_low_pct: Decimal = Decimal("10")
    abnormal_rise_4h_medium_pct: Decimal = Decimal("16")
    abnormal_rise_4h_high_pct: Decimal = Decimal("24")
    abnormal_rise_24h_low_pct: Decimal = Decimal("12")
    abnormal_rise_24h_medium_pct: Decimal = Decimal("20")
    abnormal_rise_24h_high_pct: Decimal = Decimal("35")

    abnormal_drop_1h_low_pct: Decimal = Decimal("5")
    abnormal_drop_1h_medium_pct: Decimal = Decimal("8")
    abnormal_drop_1h_high_pct: Decimal = Decimal("12")
    abnormal_drop_4h_low_pct: Decimal = Decimal("8")
    abnormal_drop_4h_medium_pct: Decimal = Decimal("14")
    abnormal_drop_4h_high_pct: Decimal = Decimal("20")
    abnormal_drop_24h_low_pct: Decimal = Decimal("10")
    abnormal_drop_24h_medium_pct: Decimal = Decimal("18")
    abnormal_drop_24h_high_pct: Decimal = Decimal("28")

    main_drawdown_alert_pct: Decimal = Decimal("14")
    long_term_drawdown_alert_pct: Decimal = Decimal("10")

    main_concentration_alert_pct: Decimal = Decimal("45")
    long_term_concentration_alert_pct: Decimal = Decimal("35")

    high_volatility_24h_pct: Decimal = Decimal("18")
    high_volatility_4h_pct: Decimal = Decimal("12")

    signal_cooldown_minutes: int = 240
    risk_cooldown_minutes: int = 720
    take_profit_cooldown_days: int = 14

    main_take_profit_stages: str = Field(
        default="30:10-15,50:15-20,100:20-25",
        description="Format: gain_pct:min_sell_pct-max_sell_pct comma separated.",
    )
    long_term_take_profit_stages: str = Field(
        default="75:5-10,125:10-15,200:15-20",
        description="Format: gain_pct:min_sell_pct-max_sell_pct comma separated.",
    )

    # Public futures research only; follows the existing CPDA_ environment prefix.
    scanner_enabled: bool = True
    scanner_interval_minutes: int = Field(default=5, ge=1)
    scanner_top_n: int = Field(default=100, ge=0)
    scanner_max_markets: int = Field(default=100, ge=1, le=1000)
    scanner_min_quote_volume_usd: Decimal = Field(default=Decimal("10000000"), ge=0)
    scanner_blacklist: str = ""
    scanner_whitelist: str = ""
    scanner_exclude_leveraged: bool = True
    scanner_concurrency: int = Field(default=5, ge=1, le=20)
    scanner_requests_per_second: float = Field(default=4.0, gt=0, le=10)
    scanner_weight_per_minute: int = Field(default=1200, ge=40, le=2400)
    scanner_derivatives_cache_seconds: int = Field(default=300, ge=1, le=900)
    scanner_http_retries: int = Field(default=3, ge=0, le=5)
    scanner_candle_limit: int = Field(default=300, ge=210, le=499)
    scanner_universe_cache_seconds: int = Field(default=300, ge=1)
    scanner_data_grace_seconds: int = Field(default=5, ge=0, le=60)
    binance_futures_base_url: str = "https://fapi.binance.com"
    # Primary scanner exchange. One run uses exactly one exchange for every metric.
    scanner_provider: Literal["bingx", "binance"] = "bingx"
    bingx_base_url: str = "https://open-api.bingx.com"
    bingx_mark_price_ws_url: str = "wss://open-api-swap.bingx.com/swap-market"
    # Observed BingX headers: x-ratelimit-requests-remain/expire (500 per 10 s window).
    # Budget half of it; the scanner shares the IP with anything else you run.
    bingx_requests_per_window: int = Field(default=250, ge=10, le=500)
    bingx_rate_window_seconds: float = Field(default=10.0, gt=0, le=60)
    # Self-recorded OI (exchanges without public OI history, e.g. BingX): a live
    # observation is stored for a 15m boundary only if taken within this many seconds
    # of it. Collection runs independently on 15m clock boundaries.
    oi_snapshot_tolerance_seconds: float = Field(default=150, ge=0, le=450)
    # Future clock-skew allowance for OI timestamps (exchange clock slightly ahead
    # of this host). Clock-skew validation only; keep the host clock NTP-synced.
    oi_clock_skew_tolerance_seconds: float = Field(default=2.0, ge=0, le=5)
    oi_snapshot_retention_days: int = Field(default=14, ge=1)
    # News context (CONTEXT ONLY: never changes the trading score or alert rules).
    news_enabled: bool = True
    news_send_telegram: bool = True
    news_queue_size: int = Field(default=1000, ge=10, le=100_000)
    news_workers: int = Field(default=2, ge=1, le=16)
    # Polling cadence for sources without push delivery (fair use; never 15 min).
    news_poll_bingx_seconds: float = Field(default=30, ge=5, le=600)
    news_poll_rss_seconds: float = Field(default=60, ge=30, le=3600)
    # SEC fair access requires a descriptive User-Agent with a contact you choose;
    # the SEC feed stays disabled until this is set. Never defaulted to any address.
    news_sec_user_agent: str | None = None
    # Items older than this at receipt are stored but never alerted (no backlog spam).
    news_alert_max_age_minutes: int = Field(default=60, ge=1)
    news_critical_max_age_minutes: int = Field(default=240, ge=1)
    news_urgent_cooldown_minutes: int = Field(default=15, ge=0)
    news_min_link_confidence: float = Field(default=0.8, ge=0, le=1)
    news_latency_slo_ms: int = Field(default=3000, ge=100)
    # Fast market watcher: EARLY WARNING only (never a trading score / alert rule).
    fast_enabled: bool = True
    fast_send_telegram: bool = False
    fast_symbols_per_connection: int = Field(default=100, ge=10, le=200)
    fast_min_quote_volume_usd: float = Field(default=10_000_000, ge=0)
    fast_sigma_k: float = Field(default=4.0, gt=0)
    fast_sigma_k_extreme: float = Field(default=8.0, gt=0)
    fast_min_pct_1m: float = Field(default=1.0, gt=0)
    fast_min_pct_3m: float = Field(default=1.8, gt=0)
    fast_min_pct_5m: float = Field(default=2.5, gt=0)
    fast_volume_expansion: float = Field(default=3.0, gt=1)
    fast_cooldown_minutes: int = Field(default=30, ge=1)
    fast_queue_size: int = Field(default=200, ge=10)
    # Liquidity: markets wider than this bid/ask spread (REST ticker) are excluded,
    # and every exclusion is recorded with its reason (never silently dropped).
    fast_max_spread_pct: float = Field(default=0.15, gt=0)
    # Early structural layer (EARLY WARNING; never a score, threshold or scanner
    # alert rule). Distances are in ATR of the zone's reference timeframe (1H ATR
    # for 1H/4H zones, 15m ATR for 15m zones/patterns, 5m ATR for 5m patterns).
    early_enabled: bool = True  # early trend ignition + breakout/retest episodes
    early_patterns_enabled: bool = True  # FORMATION_WATCH candidates
    early_zone_watch_enabled: bool = True
    early_send_telegram: bool = False
    early_zone_min_touches: int = Field(default=2, ge=1)
    early_approach_atr: float = Field(default=0.35, gt=0, le=2)
    early_break_atr: float = Field(default=0.10, gt=0, le=2)
    early_break_hold_seconds: float = Field(default=10, ge=1, le=300)
    early_retest_atr: float = Field(default=0.25, gt=0, le=2)
    early_fail_atr: float = Field(default=0.25, gt=0, le=2)
    early_late_origin_atr: float = Field(default=3.0, gt=0)
    early_ignition_min_strength: int = Field(default=55, ge=0, le=100)
    early_formation_min_strength: int = Field(default=50, ge=0, le=100)
    early_cooldown_minutes: float = Field(default=120, ge=1)
    # Notification policy (Telegram only; every valid event is still persisted and
    # enters outcome research). Zone/formation/late events are research by default.
    early_notify_zone_watch: bool = False
    early_notify_formation: bool = False
    early_notify_approach_atr: float = Field(default=0.20, gt=0, le=1)
    early_notify_budget_per_5min: int = Field(default=3, ge=0)
    early_notify_budget_per_hour: int = Field(default=12, ge=0)
    early_context_concurrency: int = Field(default=3, ge=1, le=10)
    early_priority_analyses_per_minute: int = Field(default=6, ge=0, le=60)
    early_outcome_horizon_minutes: int = Field(default=240, ge=30, le=1440)
    # How often setup expiry is checked between scans (lifecycle alerts).
    lifecycle_check_seconds: float = Field(default=15, ge=1, le=300)
    oi_collection_enabled: bool = True
    oi_collection_concurrency: int = Field(default=5, ge=1, le=20)
    scanner_watch_score: int = Field(default=60, ge=0, le=100)
    scanner_setup_score: int = Field(default=70, ge=0, le=100)
    scanner_high_score: int = Field(default=80, ge=0, le=100)
    scanner_extreme_score: int = Field(default=90, ge=0, le=100)
    scanner_alert_score: int = Field(default=80, ge=0, le=100)
    scanner_cooldown_minutes: int = Field(default=60, ge=0)
    scanner_score_delta: int = Field(default=5, ge=1, le=100)
    scanner_setup_expiry_minutes: int = Field(default=240, ge=15)
    scanner_alerts_per_run: int = Field(default=10, ge=1, le=100)
    scanner_telegram_interval_seconds: float = Field(default=1.1, ge=0)
    scanner_telegram_chat_id: str | None = None
    scanner_min_rr: Decimal = Field(default=Decimal("1.5"), gt=0)
    scanner_max_extension_atr: float = Field(default=3.0, gt=0)
    scanner_stop_buffer_atr: Decimal = Field(default=Decimal("0.25"), gt=0)
    scanner_zone_atr: float = Field(default=0.3, gt=0)
    scanner_location_atr: float = Field(default=1.0, gt=0)
    scanner_macro_pivot_length: int = Field(default=5, ge=1, le=30)
    scanner_micro_pivot_length: int = Field(default=2, ge=1, le=10)
    scanner_break_requires_close: bool = True
    scanner_event_max_bars: int = Field(default=12, ge=1, le=100)
    scanner_rvol_baseline: int = Field(default=20, ge=2, le=100)
    scanner_rvol_min: float = Field(default=1.2, gt=0)
    scanner_adx_min: float = Field(default=20.0, ge=0)
    scanner_high_volatility_atr_pct: float = Field(default=4.0, gt=0)
    scanner_funding_extreme: Decimal = Field(default=Decimal("0.001"), gt=0)
    scanner_use_btc_context: bool = True
    scanner_use_derivatives: bool = True
    fvg_min_atr: float = Field(default=0.15, ge=0)
    # Dry run labels 24/7 research collection; the scanner never trades in any mode.
    scanner_dry_run: bool = False
    scanner_send_telegram: bool = True
    # Setup-outcome horizon in closed bars of the setup's trigger timeframe
    # (scanner setups trigger on 1h; 4h provides trend confirmation).
    outcome_max_bars_15m: int = Field(default=96, ge=1, le=280)
    outcome_max_bars_1h: int = Field(default=72, ge=1, le=280)
    outcome_max_bars_4h: int = Field(default=42, ge=1, le=280)
    outcome_ltf_resolution: bool = True
    calibration_bands: str = "60,70,80,90"
    calibration_min_samples: int = Field(default=30, ge=1)
    tradingview_webhook_enabled: bool = False
    tradingview_webhook_secret: str | None = None

    @model_validator(mode="after")
    def anchor_sqlite_path(self) -> Settings:
        prefix = "sqlite:///"
        path = self.database_url.removeprefix(prefix)
        if (
            self.database_url.startswith(prefix)
            and path
            and path != ":memory:"
            and not path.startswith("/")
        ):
            self.database_url = prefix + str((PROJECT_ROOT / path).resolve())
        return self

    @model_validator(mode="after")
    def validate_scanner(self) -> "Settings":
        if not (
            self.scanner_watch_score
            < self.scanner_setup_score
            < self.scanner_high_score
            < self.scanner_extreme_score
        ):
            raise ValueError("Scanner score thresholds must strictly increase")
        if self.tradingview_webhook_enabled and (
            not self.tradingview_webhook_secret
            or len(self.tradingview_webhook_secret) < 24
        ):
            raise ValueError(
                "Enabled TradingView webhook requires a secret of at least 24 characters"
            )
        if not self.calibration_band_edges:  # The property validates the band list.
            raise ValueError("Calibration bands must not be empty")
        return self

    @property
    def calibration_band_edges(self) -> tuple[int, ...]:
        edges = tuple(int(v) for v in self.calibration_bands.split(",") if v.strip())
        if (
            not edges
            or list(edges) != sorted(set(edges))
            or not 0 <= edges[0] <= edges[-1] <= 100
        ):
            raise ValueError("Calibration bands must be increasing scores within 0-100")
        return edges

    def outcome_max_bars(self, timeframe: str) -> int:
        return {
            "15m": self.outcome_max_bars_15m,
            "1h": self.outcome_max_bars_1h,
            "4h": self.outcome_max_bars_4h,
        }[timeframe]

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def provider_order(self) -> tuple[str, ...]:
        return tuple(
            provider.strip().lower()
            for provider in self.market_provider_order.split(",")
            if provider.strip()
        )

    def parse_take_profit_stages(self, raw_value: str) -> tuple[TakeProfitBand, ...]:
        stages: list[TakeProfitBand] = []
        for chunk in raw_value.split(","):
            trigger_raw, sell_range_raw = chunk.strip().split(":")
            if "-" in sell_range_raw:
                sell_min_raw, sell_max_raw = sell_range_raw.split("-")
            else:
                # Backward-compatible parsing for older env formats like 25:0.15
                legacy_value = Decimal(sell_range_raw)
                legacy_pct = (
                    legacy_value * Decimal("100")
                    if legacy_value <= Decimal("1")
                    else legacy_value
                )
                sell_min_raw = str(legacy_pct)
                sell_max_raw = str(legacy_pct)
            stages.append(
                TakeProfitBand(
                    gain_trigger_pct=Decimal(trigger_raw),
                    sell_min_pct=Decimal(sell_min_raw),
                    sell_max_pct=Decimal(sell_max_raw),
                )
            )
        return tuple(sorted(stages, key=lambda stage: stage.gain_trigger_pct))

    @property
    def main_strategy_take_profit_plan(self) -> tuple[TakeProfitBand, ...]:
        return self.parse_take_profit_stages(self.main_take_profit_stages)

    @property
    def long_term_strategy_take_profit_plan(self) -> tuple[TakeProfitBand, ...]:
        return self.parse_take_profit_stages(self.long_term_take_profit_stages)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
