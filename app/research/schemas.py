from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

OBSERVED_RATE_NOTE = (
    "Historical observed rates of scanner setup outcomes. Not probabilities, not "
    "trades, and not a forecast of future results."
)
METRIC_DEFINITION = (
    "Rate = setups where the target was touched before -1R invalidation, divided by "
    "setups where either happened first. Same-bar ambiguous and expired-unresolved "
    "setups are excluded from the rate and reported as counts."
)


class OutcomeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    market_setup_id: UUID
    symbol: str
    exchange: str
    direction: str
    setup_created_at: datetime
    timeframe: str
    ready_at: datetime
    entry_reference_price: Decimal
    invalidation_price: Decimal
    initial_risk_distance: Decimal
    score_at_entry: int
    score_breakdown: dict[str, int]
    market_regime: str
    structure_regime: str
    target_0_5r_price: Decimal
    target_1r_price: Decimal
    target_1_5r_price: Decimal
    target_2r_price: Decimal
    hit_0_5r: bool
    hit_1r: bool
    hit_1_5r: bool
    hit_2r: bool
    hit_minus_1r: bool
    first_0_5r: str
    first_1r: str
    first_1_5r: str
    first_2r: str
    first_event: str | None
    first_event_at: datetime | None
    max_favorable_price: Decimal
    max_adverse_price: Decimal
    max_favorable_excursion_r: float
    max_adverse_excursion_r: float
    bars_to_0_5r: int | None
    bars_to_1r: int | None
    bars_to_1_5r: int | None
    bars_to_2r: int | None
    bars_to_invalidation: int | None
    bars_processed: int
    last_processed_at: datetime
    ambiguity_resolution: str | None
    expired_at: datetime | None
    completed_at: datetime | None
    outcome_status: str


class TargetRate(BaseModel):
    target: str
    target_first: int
    invalidation_first: int
    resolved: int
    ambiguous: int
    unresolved: int
    observed_rate: float | None = Field(
        description="Historical observed rate among resolved setups; not a probability"
    )


class BandStats(BaseModel):
    band: str
    score_min: int
    score_max: int
    sample_count: int
    low_sample: bool
    targets: list[TargetRate]
    avg_mfe_r: float | None
    median_mfe_r: float | None
    avg_mae_r: float | None
    median_mae_r: float | None
    ambiguous_count: int
    expired_count: int
    data_gap_count: int


class CalibrationGroup(BaseModel):
    key: dict[str, str]
    sample_count: int
    bands: list[BandStats]


class CalibrationReport(BaseModel):
    note: str = OBSERVED_RATE_NOTE
    metric_definition: str = METRIC_DEFINITION
    min_samples: int
    group_by: list[str]
    filters: dict[str, str | int]
    tracking_count: int
    groups: list[CalibrationGroup]
