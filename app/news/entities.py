"""Coverage metadata and NewsEntityResolver (deterministic, no guessing).

Coverage tier = news MONITORING priority only. Portfolio priority is read from the
portfolio database at runtime; holdings are never written into code or config.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.news.domain import EntityMatch, MatchType, NormalizedNewsItem

DEFAULT_CONFIG = PROJECT_ROOT / "config" / "news_projects.toml"
QUOTE = "USDT"
CONFIDENCE = {
    MatchType.DIRECT_SYMBOL: 0.95,
    MatchType.PROJECT_NAME: 0.9,
    MatchType.ORGANIZATION: 0.85,
    MatchType.ECOSYSTEM: 0.6,
    MatchType.INDIRECT: 0.4,
}


@dataclass(frozen=True)
class Project:
    symbol: str  # base asset, e.g. "ARB"
    canonical_name: str
    coverage_tier: int
    aliases: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    ecosystem_terms: tuple[str, ...] = ()
    indirect_terms: tuple[str, ...] = ()
    backer_tags: tuple[str, ...] = ()
    theme_tags: tuple[str, ...] = ()
    enabled: bool = True

    @property
    def pair(self) -> str:
        return self.symbol + QUOTE


@dataclass
class Coverage:
    projects: dict[str, Project]  # by canonical pair, e.g. ARBUSDT
    ambiguous: frozenset[str]
    portfolio: frozenset[str] = frozenset()  # canonical pairs held in the portfolio
    version: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def level(self, pair: str) -> str:
        if pair in self.portfolio:
            return "PORTFOLIO"
        project = self.projects.get(pair)
        if project is None or not project.enabled:
            return "UNCOVERED"
        return f"TIER_{project.coverage_tier}"


def load_coverage(path: Path = DEFAULT_CONFIG) -> Coverage:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    projects = {}
    for row in data.get("project", []):
        tier = int(row["coverage_tier"])
        if tier not in (1, 2):
            raise ValueError(f"{row['symbol']}: coverage_tier must be 1 or 2")
        project = Project(
            symbol=row["symbol"].upper(),
            canonical_name=row["canonical_name"],
            coverage_tier=tier,
            **{
                key: tuple(row.get(key, ()))
                for key in (
                    "aliases",
                    "organizations",
                    "ecosystem_terms",
                    "indirect_terms",
                    "backer_tags",
                    "theme_tags",
                )
            },
            enabled=bool(row.get("enabled", True)),
        )
        projects[project.pair] = project
    return Coverage(
        projects=projects,
        ambiguous=frozenset(t.upper() for t in data.get("ambiguous_tickers", [])),
        version=str(data.get("version", "")),
    )


def portfolio_pairs(session: Session) -> frozenset[str]:
    """Symbols with an open position in the application's own portfolio database."""
    from app.db.models import Asset, Position

    rows = session.scalars(
        select(Asset.symbol)
        .join(Position, Position.asset_id == Asset.id)
        .where(Position.quantity > 0)
    )
    return frozenset(symbol.upper() + QUOTE for symbol in rows if symbol)


def _phrase(text: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w$])" + re.escape(text) + r"(?![\w])", re.IGNORECASE)


class NewsEntityResolver:
    """Map a news item to canonical symbols with an explicit match type/confidence."""

    def __init__(self, coverage: Coverage) -> None:
        self.coverage = coverage
        self._phrases: list[tuple[re.Pattern[str], str, MatchType, str]] = []
        for pair, project in coverage.projects.items():
            if not project.enabled:
                continue
            for kind, texts in (
                (MatchType.PROJECT_NAME, (project.canonical_name, *project.aliases)),
                (MatchType.ORGANIZATION, project.organizations),
                (MatchType.ECOSYSTEM, project.ecosystem_terms),
                (MatchType.INDIRECT, project.indirect_terms),
            ):
                for text in texts:
                    # A bare ambiguous word ("Near") is never a project-name match.
                    if text.upper() in coverage.ambiguous:
                        continue
                    self._phrases.append((_phrase(text), pair, kind, text))

    def _tickers(self, text: str) -> list[tuple[str, bool]]:
        """(ticker, explicit). Explicit forms may name uncovered symbols."""
        found: list[tuple[str, bool]] = []
        found += [(t, True) for t in re.findall(r"\$([A-Z][A-Z0-9]{1,14})\b", text)]
        found += [
            (t, True)
            for t in re.findall(r"\b([A-Z0-9]{2,15})-?USDT\b", text)
            if re.search(r"[A-Z]", t)
        ]
        # "(OP)" disambiguates a covered ticker; it never introduces new symbols.
        found += [
            (t, False)
            for t in re.findall(r"\(([A-Z][A-Z0-9]{1,14})\)", text)
            if t + QUOTE in self.coverage.projects
        ]
        # A bare upper-case covered ticker counts unless it is an ordinary word.
        found += [
            (t, False)
            for t in re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", text)
            if t not in self.coverage.ambiguous
        ]
        return found

    def resolve(self, item: NormalizedNewsItem) -> list[EntityMatch]:
        text = f"{item.title}\n{item.summary}"
        best: dict[str, EntityMatch] = {}

        def add(pair: str, kind: MatchType, matched: str, confidence: float) -> None:
            current = best.get(pair)
            if current is None or confidence > current.confidence:
                best[pair] = EntityMatch(
                    symbol=pair,
                    match_type=kind,
                    confidence=confidence,
                    matched_text=matched,
                )

        # Provider tags are the strongest evidence and may name uncovered symbols.
        for tag in item.symbols:
            add(tag, MatchType.DIRECT_SYMBOL, tag, 1.0)
        for ticker, explicit in self._tickers(text):
            pair = ticker + QUOTE
            if explicit or pair in self.coverage.projects:
                add(
                    pair,
                    MatchType.DIRECT_SYMBOL,
                    ticker,
                    CONFIDENCE[MatchType.DIRECT_SYMBOL],
                )
        for pattern, pair, kind, phrase in self._phrases:
            if pattern.search(text):
                add(pair, kind, phrase, CONFIDENCE[kind])
        return sorted(best.values(), key=lambda m: (-m.confidence, m.symbol))
