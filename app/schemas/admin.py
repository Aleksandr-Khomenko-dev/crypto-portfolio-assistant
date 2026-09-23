from __future__ import annotations

from pydantic import BaseModel, Field


class CSVImportPreviewRow(BaseModel):
    row_number: int
    portfolio_name: str | None = None
    symbol: str | None = None
    name: str | None = None
    quantity: str | None = None
    average_entry_price: str | None = None
    notes: str | None = None
    valid: bool
    errors: list[str] = Field(default_factory=list)


class CSVImportPreviewResponse(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[CSVImportPreviewRow] = Field(default_factory=list)
    valid_row_count: int
    invalid_row_count: int
