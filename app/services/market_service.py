from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Asset, Portfolio, Position, PriceSnapshot
from app.providers.interfaces import AbstractMarketDataProvider, AssetMarketQuery, MarketQuote


class MarketService:
    def __init__(self, session: Session, market_provider: AbstractMarketDataProvider) -> None:
        self.session = session
        self.market_provider = market_provider

    def build_queries(self, portfolio: Portfolio) -> list[AssetMarketQuery]:
        queries: list[AssetMarketQuery] = []
        for position in portfolio.positions:
            asset = position.asset
            queries.append(
                AssetMarketQuery(
                    asset_id=asset.id,
                    symbol=asset.symbol,
                    name=asset.name,
                    coingecko_id=asset.coingecko_id,
                    binance_symbol=asset.binance_symbol,
                    bybit_symbol=asset.bybit_symbol,
                )
            )
        return queries

    async def fetch_quotes_for_portfolio(self, portfolio: Portfolio) -> dict[UUID, MarketQuote]:
        return await self.market_provider.fetch_quotes(self.build_queries(portfolio))

    async def fetch_quotes_for_queries(self, queries: list[AssetMarketQuery]) -> dict[UUID, MarketQuote]:
        if not queries:
            return {}
        return await self.market_provider.fetch_quotes(queries)

    def persist_quotes(self, quotes: dict[UUID, MarketQuote]) -> list[PriceSnapshot]:
        snapshots: list[PriceSnapshot] = []
        for asset_id, quote in quotes.items():
            snapshot = PriceSnapshot(
                asset_id=asset_id,
                provider=quote.provider,
                price_usd=quote.price_usd,
                market_cap_usd=quote.market_cap_usd,
                volume_24h_usd=quote.volume_24h_usd,
                change_1h_pct=quote.change_1h_pct,
                change_24h_pct=quote.change_24h_pct,
                raw_payload=quote.raw,
                captured_at=quote.source_timestamp or datetime.now(timezone.utc),
            )
            self.session.add(snapshot)
            snapshots.append(snapshot)
        self.session.flush()
        return snapshots

    def latest_snapshot_before(self, asset_id: UUID, target_time: datetime) -> PriceSnapshot | None:
        statement = (
            select(PriceSnapshot)
            .where(PriceSnapshot.asset_id == asset_id, PriceSnapshot.captured_at <= target_time)
            .order_by(PriceSnapshot.captured_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def historical_price(self, asset_id: UUID, hours_ago: int) -> Decimal | None:
        target_time = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        snapshot = self.latest_snapshot_before(asset_id, target_time)
        return Decimal(snapshot.price_usd) if snapshot is not None else None

    def historical_price_minutes(self, asset_id: UUID, minutes_ago: int) -> Decimal | None:
        target_time = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        snapshot = self.latest_snapshot_before(asset_id, target_time)
        return Decimal(snapshot.price_usd) if snapshot is not None else None

    def peak_price(self, asset_id: UUID, hours_lookback: int) -> Decimal | None:
        since = datetime.now(timezone.utc) - timedelta(hours=hours_lookback)
        statement = (
            select(PriceSnapshot.price_usd)
            .where(PriceSnapshot.asset_id == asset_id, PriceSnapshot.captured_at >= since)
            .order_by(PriceSnapshot.price_usd.desc())
            .limit(1)
        )
        result = self.session.scalar(statement)
        return Decimal(result) if result is not None else None
