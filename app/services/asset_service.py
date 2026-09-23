from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Asset
from app.schemas.portfolio import AssetCreate, AssetUpdate


class AssetService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_assets(self) -> list[Asset]:
        statement = select(Asset).order_by(Asset.symbol.asc())
        return list(self.session.scalars(statement))

    def get_asset(self, asset_id) -> Asset | None:
        return self.session.get(Asset, asset_id)

    def get_by_symbol(self, symbol: str) -> Asset | None:
        statement = select(Asset).where(func.upper(Asset.symbol) == symbol.upper())
        return self.session.scalar(statement)

    def get_or_create_asset(self, payload: AssetCreate) -> Asset:
        existing = self.get_by_symbol(payload.symbol)
        if existing:
            updated = False
            if payload.name and not existing.name:
                existing.name = payload.name
                updated = True
            if payload.coingecko_id and not existing.coingecko_id:
                existing.coingecko_id = payload.coingecko_id
                updated = True
            if payload.binance_symbol and not existing.binance_symbol:
                existing.binance_symbol = payload.binance_symbol
                updated = True
            if payload.bybit_symbol and not existing.bybit_symbol:
                existing.bybit_symbol = payload.bybit_symbol
                updated = True
            if payload.metadata_json and not existing.metadata_json:
                existing.metadata_json = payload.metadata_json
                updated = True
            if updated:
                self.session.add(existing)
                self.session.flush()
            return existing

        asset = Asset(
            symbol=payload.symbol.upper(),
            name=payload.name,
            coingecko_id=payload.coingecko_id,
            binance_symbol=(payload.binance_symbol or "").upper() or None,
            bybit_symbol=(payload.bybit_symbol or "").upper() or None,
            metadata_json=payload.metadata_json,
        )
        self.session.add(asset)
        self.session.flush()
        return asset

    def update_asset(self, asset: Asset, payload: AssetUpdate) -> Asset:
        for field, value in payload.model_dump(exclude_unset=True).items():
            if field in {"symbol", "binance_symbol", "bybit_symbol"} and isinstance(value, str):
                value = value.upper()
            setattr(asset, field, value)
        self.session.add(asset)
        self.session.commit()
        self.session.refresh(asset)
        return asset
