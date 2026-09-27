"""Deterministic noise filter, event classification and NEWS_IMPORTANCE.

NEWS_IMPORTANCE (0-100) is a separate diagnostic: never a probability and never added
to the trading confluence score. Direction is UNCLEAR unless a rule states otherwise.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.news.domain import (
    Classification,
    EntityMatch,
    EventType,
    Freshness,
    ImpactLevel,
    Importance,
    MatchType,
    NewsDirection,
    NormalizedNewsItem,
    SourceType,
    Verification,
)

RULE_VERSION = "news-rules-1"

A = NewsDirection.POTENTIALLY_ADVERSE
S = NewsDirection.POTENTIALLY_SUPPORTIVE
U = NewsDirection.UNCLEAR

# Noise is filtered before classification: stored, never alerted.
NOISE: tuple[tuple[str, str], ...] = (
    (
        "price_prediction",
        r"price prediction|could (?:reach|hit)|will (?:reach|hit) \$|price target",
    ),
    ("opinion", r"\bop-ed\b|\bopinion\b|\bwhy i\b|\bcommentary\b"),
    ("listicle", r"\btop \d+\b|best (?:coins|crypto)|coins to (?:buy|watch)"),
    (
        "promotion",
        r"giveaway|promotion|trading competition|carnival|\bbonus\b|earn up to|rewards? pool|campaign|lucky draw|airdrop event",
    ),
    (
        "analyst_chatter",
        r"analysts? (?:say|expect|predict|believe)|according to (?:an )?analyst",
    ),
)

# (rule id, pattern, event, severity, direction); first match wins, most severe first.
RULES: tuple[tuple[str, str, EventType, int, NewsDirection], ...] = (
    ("exploit", r"\bexploit(?:ed|s)?\b|\bdrain(?:ed)?\b", EventType.EXPLOIT, 95, A),
    (
        "hack",
        r"\bhack(?:ed|s)?\b|\bstolen\b|security (?:incident|breach)|\bcompromised\b",
        EventType.HACK,
        95,
        A,
    ),
    (
        "chain_halt",
        r"\bhalt(?:ed|s)?\b|stopped producing blocks|block production (?:halt|stop)",
        EventType.CHAIN_HALT,
        95,
        A,
    ),
    ("depeg", r"\bde-?peg(?:ged|s)?\b|lost (?:its|the) peg", EventType.DEPEG, 90, A),
    ("bankruptcy", r"\bbankrupt|chapter 11|insolven", EventType.BANKRUPTCY, 90, A),
    ("delisting", r"\bdelist", EventType.DELISTING, 85, A),
    (
        "enforcement",
        r"\bcharges?\b|\bcharged\b|enforcement action|cease-and-desist|\bfraud\b|\bsettles?\b",
        EventType.ENFORCEMENT,
        75,
        A,
    ),
    (
        "lawsuit",
        r"\blawsuit\b|\bsues?\b|\bsued\b|class action",
        EventType.LAWSUIT,
        65,
        A,
    ),
    (
        "outage",
        r"\boutage\b|degraded performance|\bdowntime\b|service disruption",
        EventType.OUTAGE,
        75,
        A,
    ),
    (
        "etf_approval",
        r"\betf\b.*\b(?:approv|launch)|\b(?:approv|launch)\w*\b.*\betfs?\b",
        EventType.ETF,
        80,
        S,
    ),
    (
        "etf_denial",
        r"\betf\b.*\b(?:reject|den(?:y|ies|ied)|delay)|\b(?:reject|den(?:y|ies|ied)|delay)\w*\b.*\betfs?\b",
        EventType.ETF,
        75,
        A,
    ),
    ("etf", r"\betf\b", EventType.ETF, 60, U),
    (
        "withdrawal_suspension",
        r"suspen\w* (?:of )?(?:deposits?|withdrawals?)|(?:deposits?|withdrawals?)\b.*\b(?:suspend|pause|halt)",
        EventType.WITHDRAWAL_SUSPENSION,
        60,
        U,
    ),
    ("hard_fork", r"hard ?fork", EventType.HARD_FORK, 65, U),
    (
        "network_upgrade",
        r"\bupgrade\b|mainnet launch|network update",
        EventType.NETWORK_UPGRADE,
        55,
        U,
    ),
    (
        "token_migration",
        r"token (?:migration|swap)|contract (?:migration|swap)|redenominat",
        EventType.TOKENOMICS_CHANGE,
        60,
        U,
    ),
    ("token_unlock", r"\bunlock", EventType.TOKEN_UNLOCK, 60, U),
    ("token_burn", r"\bburn", EventType.TOKEN_BURN, 50, U),
    (
        "tokenomics",
        r"tokenomics|emission|inflation schedule|supply (?:cap|change)",
        EventType.TOKENOMICS_CHANGE,
        60,
        U,
    ),
    (
        "listing",
        r"\bwill list\b|\blists?\b|\blisting\b|\badds?\b.*\b(?:perpetual|futures|spot)",
        EventType.LISTING,
        55,
        U,
    ),
    (
        "regulation",
        r"\bregulat|\bframework\b|\brulemaking\b|\bno-action\b|\bstaff advisory\b|\bguidance\b",
        EventType.REGULATION,
        55,
        U,
    ),
    (
        "governance",
        r"\bgovernance\b|\bproposal\b|\bvote\b|\bdao\b",
        EventType.GOVERNANCE,
        45,
        U,
    ),
    ("integration", r"\bintegrat", EventType.INTEGRATION, 30, U),
    ("partnership", r"\bpartner", EventType.PARTNERSHIP, 25, U),
    (
        "maintenance",
        r"maintenance|margin rate|leverage (?:and|&) margin|funding (?:rate|interval)",
        EventType.OTHER,
        20,
        U,
    ),
)
# Provider categories that are authoritative on their own.
HINTS = {
    "Delisting": ("bingx_delisting", EventType.DELISTING, 85, A),
    "FuturesListing": ("bingx_futures_listing", EventType.LISTING, 55, U),
    "SpotListing": ("bingx_spot_listing", EventType.LISTING, 50, U),
}
CRYPTO_CONTEXT = re.compile(
    r"crypto|digital asset|bitcoin|ether|blockchain|stablecoin|\btoken|defi|\betf\b|"
    r"virtual currenc|\bcoin\b|exchange-traded product",
    re.IGNORECASE,
)
SOURCE_QUALITY = {
    SourceType.OFFICIAL_PRIMARY: 1.0,
    SourceType.NEWSWIRE: 0.85,
    SourceType.PUBLICATION: 0.7,
    SourceType.AGGREGATOR: 0.5,
    SourceType.SOCIAL: 0.3,
}
COVERAGE_WEIGHT = {"PORTFOLIO": 1.0, "TIER_1": 1.0, "TIER_2": 0.85, "UNCOVERED": 0.6}
VERIFICATION_WEIGHT = {
    Verification.PRIMARY_CONFIRMED: 1.0,
    Verification.MULTI_SOURCE_CONFIRMED: 0.9,
    Verification.SINGLE_SOURCE: 0.7,
    Verification.UNVERIFIED: 0.4,
}
FRESHNESS_WEIGHT = {
    Freshness.VERY_FRESH: 1.0,
    Freshness.FRESH: 0.8,
    Freshness.RECENT: 0.5,
    Freshness.HISTORICAL: 0.2,
}
# Regulators publish mostly non-crypto news: without crypto context it is noise here.
REGULATORY_PROVIDERS = {"sec_press", "cftc_press"}


def classify(item: NormalizedNewsItem) -> Classification:
    text = f"{item.title}\n{item.summary}"
    for reason, pattern in NOISE:
        if re.search(pattern, text, re.IGNORECASE):
            return Classification(
                event_type=EventType.OTHER,
                direction=U,
                severity=0,
                noise=True,
                noise_reason=reason,
                rule="noise:" + reason,
                rule_version=RULE_VERSION,
            )
    if item.provider in REGULATORY_PROVIDERS and not CRYPTO_CONTEXT.search(text):
        return Classification(
            event_type=EventType.OTHER,
            direction=U,
            severity=0,
            noise=True,
            noise_reason="not_crypto_related",
            rule="noise:not_crypto_related",
            rule_version=RULE_VERSION,
        )
    if item.event_type_hint in HINTS:
        rule, event, severity, direction = HINTS[item.event_type_hint]
        return Classification(
            event_type=event,
            direction=direction,
            severity=severity,
            rule=rule,
            rule_version=RULE_VERSION,
        )
    for rule, pattern, event, severity, direction in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return Classification(
                event_type=event,
                direction=direction,
                severity=severity,
                rule=rule,
                rule_version=RULE_VERSION,
            )
    return Classification(
        event_type=EventType.OTHER,
        direction=U,
        severity=15,
        rule="default",
        rule_version=RULE_VERSION,
    )


def freshness(published_at: datetime, now: datetime) -> Freshness:
    minutes = (now - published_at).total_seconds() / 60
    if minutes <= 15:
        return Freshness.VERY_FRESH
    if minutes <= 60:
        return Freshness.FRESH
    if minutes <= 240:
        return Freshness.RECENT
    return Freshness.HISTORICAL


def verification(source_types: list[SourceType]) -> Verification:
    """PRIMARY_CONFIRMED only when a primary (official) source is actually present."""
    if SourceType.OFFICIAL_PRIMARY in source_types:
        return Verification.PRIMARY_CONFIRMED
    credible = [t for t in source_types if t != SourceType.SOCIAL]
    if len(credible) >= 2:
        return Verification.MULTI_SOURCE_CONFIRMED
    if credible:
        return Verification.SINGLE_SOURCE
    return Verification.UNVERIFIED


def level(score: int) -> ImpactLevel:
    if score >= 90:
        return ImpactLevel.CRITICAL
    if score >= 75:
        return ImpactLevel.HIGH
    if score >= 55:
        return ImpactLevel.MEDIUM
    return ImpactLevel.LOW


def importance(
    item: NormalizedNewsItem,
    classification: Classification,
    matches: list[EntityMatch],
    coverage: dict[str, str],
    status: Verification,
    fresh: Freshness,
    novel: bool = True,
) -> Importance:
    if classification.noise:
        return Importance(score=0, level=ImpactLevel.LOW, factors={"noise": 1.0})
    best = matches[0] if matches else None
    relevance = best.confidence if best else 0.0
    covered = max(
        (COVERAGE_WEIGHT[coverage.get(m.symbol, "UNCOVERED")] for m in matches),
        default=0.4,
    )
    factors = {
        "entity_relevance": relevance,
        "source_quality": SOURCE_QUALITY[item.source_type],
        "freshness": FRESHNESS_WEIGHT[fresh],
        "coverage": covered,
        "novelty": 1.0 if novel else 0.3,
        "verification": VERIFICATION_WEIGHT[status],
    }
    weights = {
        "entity_relevance": 0.30,
        "source_quality": 0.25,
        "freshness": 0.15,
        "coverage": 0.15,
        "novelty": 0.05,
        "verification": 0.10,
    }
    quality = sum(factors[k] * w for k, w in weights.items())
    # Severity gates the score: excellent sourcing cannot turn a routine notice into
    # a HIGH event; weak sourcing can at most halve a severe one.
    score = round(classification.severity * (0.5 + 0.5 * quality))
    factors["severity"] = classification.severity / 100
    return Importance(score=score, level=level(score), factors=factors)


def is_direct(match: EntityMatch | None) -> bool:
    return match is not None and match.match_type in (
        MatchType.DIRECT_SYMBOL,
        MatchType.PROJECT_NAME,
        MatchType.ORGANIZATION,
    )
