"""News context domain. News is CONTEXT ONLY: nothing here feeds the trading score."""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import AwareDatetime, BaseModel, Field


class SourceType(IntEnum):
    """Lower value = higher priority when choosing a cluster's canonical source."""

    OFFICIAL_PRIMARY = 1
    NEWSWIRE = 2
    PUBLICATION = 3
    AGGREGATOR = 4
    SOCIAL = 5


class Transport(StrEnum):
    WEBSOCKET = "WEBSOCKET"
    WEBHOOK = "WEBHOOK"
    STREAM = "STREAM"
    REST_POLL = "REST_POLL"
    RSS_POLL = "RSS_POLL"


class EventType(StrEnum):
    EXPLOIT = "EXPLOIT"
    HACK = "HACK"
    CHAIN_HALT = "CHAIN_HALT"
    OUTAGE = "OUTAGE"
    DEPEG = "DEPEG"
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    WITHDRAWAL_SUSPENSION = "WITHDRAWAL_SUSPENSION"
    NETWORK_UPGRADE = "NETWORK_UPGRADE"
    HARD_FORK = "HARD_FORK"
    TOKEN_UNLOCK = "TOKEN_UNLOCK"
    TOKEN_BURN = "TOKEN_BURN"
    TOKENOMICS_CHANGE = "TOKENOMICS_CHANGE"
    ETF = "ETF"
    REGULATION = "REGULATION"
    ENFORCEMENT = "ENFORCEMENT"
    LAWSUIT = "LAWSUIT"
    BANKRUPTCY = "BANKRUPTCY"
    GOVERNANCE = "GOVERNANCE"
    PARTNERSHIP = "PARTNERSHIP"
    INTEGRATION = "INTEGRATION"
    OTHER = "OTHER"


class NewsDirection(StrEnum):
    POTENTIALLY_SUPPORTIVE = "POTENTIALLY_SUPPORTIVE"
    POTENTIALLY_ADVERSE = "POTENTIALLY_ADVERSE"
    MIXED = "MIXED"
    UNCLEAR = "UNCLEAR"


class MatchType(StrEnum):
    DIRECT_SYMBOL = "DIRECT_SYMBOL"
    PROJECT_NAME = "PROJECT_NAME"
    ORGANIZATION = "ORGANIZATION"
    ECOSYSTEM = "ECOSYSTEM"
    INDIRECT = "INDIRECT"


class Verification(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    PRIMARY_CONFIRMED = "PRIMARY_CONFIRMED"
    MULTI_SOURCE_CONFIRMED = "MULTI_SOURCE_CONFIRMED"


class ImpactLevel(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Freshness(StrEnum):
    VERY_FRESH = "VERY_FRESH"  # 0-15 min
    FRESH = "FRESH"  # 15-60 min
    RECENT = "RECENT"  # 1-4 h
    HISTORICAL = "HISTORICAL"


class NormalizedNewsItem(BaseModel):
    """One provider item, exactly as received. Timestamps are never rewritten."""

    id: str  # f"{provider}:{provider_item_id}"
    provider: str
    provider_item_id: str
    source: str  # human-readable publisher, e.g. "BingX (official)"
    source_type: SourceType
    title: str
    summary: str = ""
    url: str | None = None
    published_at: AwareDatetime  # the source's own timestamp
    received_at: AwareDatetime  # when CPDA received it (causal availability)
    symbols: list[str] = Field(default_factory=list)  # provider tags, canonical form
    entities: list[str] = Field(default_factory=list)
    event_type_hint: str | None = None  # provider category, e.g. BingX "Delisting"
    raw_payload_hash: str


class EntityMatch(BaseModel):
    symbol: str  # canonical internal symbol, e.g. ARBUSDT
    match_type: MatchType
    confidence: float = Field(ge=0, le=1)
    matched_text: str


class Classification(BaseModel):
    event_type: EventType
    direction: NewsDirection
    severity: int = Field(ge=0, le=100)
    noise: bool = False
    noise_reason: str | None = None
    rule: str  # which rule fired, for audit
    rule_version: str


class Importance(BaseModel):
    """Diagnostic NEWS_IMPORTANCE. Not a probability; never part of trading score."""

    score: int = Field(ge=0, le=100)
    level: ImpactLevel
    factors: dict[str, float]


class ProcessedNews(BaseModel):
    item: NormalizedNewsItem
    matches: list[EntityMatch]
    classification: Classification
    importance: Importance
    verification: Verification
    freshness: Freshness
    coverage: dict[str, str]  # symbol -> PORTFOLIO | TIER_1 | TIER_2 | UNCOVERED
    stages: dict[str, datetime] = Field(default_factory=dict)  # latency timestamps
