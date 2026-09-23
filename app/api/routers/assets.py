from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routers._helpers import raise_integrity_http_error
from app.db.session import get_db
from app.schemas.portfolio import AssetCreate, AssetRead, AssetUpdate
from app.services.asset_service import AssetService

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("", response_model=list[AssetRead])
def list_assets(db: Session = Depends(get_db)) -> list[AssetRead]:
    return [AssetRead.model_validate(asset) for asset in AssetService(db).list_assets()]


@router.post("", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
def create_asset(payload: AssetCreate, db: Session = Depends(get_db)) -> AssetRead:
    try:
        asset = AssetService(db).get_or_create_asset(payload)
        db.commit()
        db.refresh(asset)
    except IntegrityError as exc:
        db.rollback()
        raise_integrity_http_error(exc)
    return AssetRead.model_validate(asset)


@router.patch("/{asset_id}", response_model=AssetRead)
def update_asset(asset_id: UUID, payload: AssetUpdate, db: Session = Depends(get_db)) -> AssetRead:
    service = AssetService(db)
    asset = service.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
    return AssetRead.model_validate(service.update_asset(asset, payload))
