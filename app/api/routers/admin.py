from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from app.admin_import import CSVImportPreviewService
from app.schemas.admin import CSVImportPreviewResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/imports/positions/preview", response_model=CSVImportPreviewResponse)
async def preview_positions_csv(file: UploadFile = File(...)) -> CSVImportPreviewResponse:
    raw_content = await file.read()
    return CSVImportPreviewService().preview(raw_content)
