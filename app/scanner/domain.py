"""Exchange-neutral, validated research data. Scores are confluence, never probability."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalState(StrEnum):
    IGNORE = "IGNORE"
    WATCH = "WATCH"
    SETUP_FORMING = "SETUP_FORMING"
    HIGH_CONFLUENCE = "HIGH_CONFLUENCE"
    EXTREME_CONFLUENCE = "EXTREME_CONFLUENCE"


class Readiness(StrEnum):
    WAIT_LOCATION = "WAIT_LOCATION"
    WAIT_STRUCTURE = "WAIT_STRUCTURE"
    WAIT_RETEST = "WAIT_RETEST"
    WAIT_VOLUME = "WAIT_VOLUME"
    WAIT_RISK = "WAIT_RISK"
    READY = "READY"


PivotKind = Literal["HIGH", "LOW"]
BreakKind = Literal["BOS", "CHoCH", "INITIAL_BREAK"]
FVGStatus = Literal[
    "FRESH", "PARTIALLY_MITIGATED", "CE_TOUCHED", "FILLED", "INVALIDATED"
]


Timeframe = Literal["5m", "15m", "1h", "4h", "1d"]
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    open_time: AwareDatetime
    close_time: AwareDatetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def valid_bar(self) -> Candle:
        if self.close_time <= self.open_time or not (
            self.low
            <= min(self.open, self.close)
            <= max(self.open, self.close)
            <= self.high
        ):
            raise ValueError("Invalid OHLC or candle timestamps")
        return self


class Contract(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z0-9_]{2,40}$")
    base_asset: str
    quote_asset: str
    contract_type: str
    status: str
    underlying_type: str = "COIN"
    underlying_subtypes: list[str] = Field(default_factory=list)


class Ticker(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    symbol: str
    price: Decimal = Field(gt=0)
    quote_volume: Decimal = Field(ge=0)
    timestamp: AwareDatetime


class OIPoint(BaseModel):
    timestamp: AwareDatetime
    contracts: Decimal = Field(ge=0)


class Derivatives(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, validate_assignment=True)
    funding_rate: Decimal | None = None  # Fraction per funding interval, not percent.
    funding_timestamp: AwareDatetime | None = None
    open_interest: Decimal | None = Field(
        default=None, ge=0
    )  # Base-asset quantity, not USD notional.
    oi_timestamp: AwareDatetime | None = None
    history: list[OIPoint] = Field(default_factory=list)
    oi_change_pct: float | None = None
    price_change_pct: float | None = None
    interpretation: str = "unavailable"
    funding_state: str = "unavailable"
    errors: list[str] = Field(default_factory=list)


class Pivot(BaseModel):
    index: int
    confirmed_index: int
    price: Decimal
    kind: PivotKind
    label: str = ""
    timestamp: datetime | None = None
    confirmed_at: datetime | None = None


class StructureBreak(BaseModel):
    index: int
    timestamp: datetime
    level: Decimal
    direction: Direction
    kind: BreakKind


class Structure(BaseModel):
    trend: str = "MIXED"
    pivots: list[Pivot] = Field(default_factory=list)
    breaks: list[StructureBreak] = Field(default_factory=list)
    retest: Direction | None = None
    sweep: Direction | None = None
    equal_highs: bool = False
    equal_lows: bool = False
    last_processed_at: datetime | None = None
    active_pivots: dict[str, Pivot] = Field(default_factory=dict)
    consumed_kinds: list[str] = Field(default_factory=list)


class Zone(BaseModel):
    lower: Decimal
    upper: Decimal
    created_at: datetime
    age_bars: int
    interactions: int
    distance_atr: float


class FVG(BaseModel):
    lower: Decimal
    upper: Decimal
    midpoint: Decimal
    created_at: datetime
    timeframe: Timeframe
    direction: Direction
    status: FVGStatus = "FRESH"


class Technical(BaseModel):
    ema: dict[str, float]
    price_above_ema: dict[str, bool]
    ema_slope_atr: dict[str, float]
    alignment: str
    expansion: str
    atr: float
    atr_pct: float
    rvol: float | None
    rsi: float
    adx: float
    plus_di: float
    minus_di: float
    extension_atr: float
    momentum_pct: float


class FrameAnalysis(BaseModel):
    timeframe: Timeframe
    candle: Candle
    technical: Technical
    macro: Structure
    micro: Structure
    supports: list[Zone]
    resistances: list[Zone]
    fvgs: list[FVG]


class MarketContext(BaseModel):
    state: str = "unavailable"
    assets: dict[str, dict[str, str | float]] = Field(default_factory=dict)
    explanation: str = "BTC/ETH data unavailable"


class RiskPlan(BaseModel):
    invalidation: Decimal | None = None
    stop_distance: Decimal | None = None
    stop_atr: float | None = None
    nearest_obstacle: Decimal | None = None
    targets: list[Zone] = Field(default_factory=list)
    rr: float | None = None
    valid: bool = False
    reason: str = "NO_STRUCTURAL_STOP"


class Setup(BaseModel):
    symbol: str
    direction: Direction
    score: int = Field(ge=0, le=100)
    state: SignalState
    readiness: Readiness
    next_condition: str
    blocks: dict[str, int]
    risk: RiskPlan


class ScannerResult(BaseModel):
    symbol: str
    market_type: str = "USDT_PERPETUAL"
    price: Decimal
    observed_price: Decimal
    observed_at: datetime
    candle_closed_at: datetime
    created_at: datetime
    expires_at: datetime
    long_score: int
    short_score: int
    setups: list[Setup]
    frames: dict[str, FrameAnalysis]
    derivatives: Derivatives
    context: MarketContext
    unavailable: list[str] = Field(
        default_factory=lambda: ["on-chain", "macro", "news/fundamental"]
    )
    model_version: str = "scanner-v2"
    setup_lifecycles: dict[str, str] = Field(default_factory=dict)


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    started_at: datetime
    completed_at: datetime | None
    status: str
    universe_size: int
    analyzed: int
    failed: int
    high_confluence: int
    duration_seconds: float
    errors: dict[str, str]


class ScannerStatus(BaseModel):
    enabled: bool
    running: bool
    last_run: RunRead | None
    score_meaning: str = "Model confluence points, not a probability of success"


class SetupRead(Setup):
    id: UUID
    price: Decimal
    created_at: datetime
    expires_at: datetime
    lifecycle: str
    episode_invalidation: Decimal | None = None
