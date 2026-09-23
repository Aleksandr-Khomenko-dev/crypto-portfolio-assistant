from __future__ import annotations

import csv
import io

from app.schemas.admin import CSVImportPreviewResponse, CSVImportPreviewRow


class CSVImportPreviewService:
    required_columns = {"portfolio_name", "symbol", "quantity", "average_entry_price"}

    def preview(self, raw_content: bytes) -> CSVImportPreviewResponse:
        text = raw_content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        columns = reader.fieldnames or []
        rows: list[CSVImportPreviewRow] = []

        missing_columns = sorted(self.required_columns - set(columns))
        if missing_columns:
            return CSVImportPreviewResponse(
                columns=columns,
                rows=[],
                valid_row_count=0,
                invalid_row_count=0,
            )

        valid_row_count = 0
        invalid_row_count = 0
        for index, row in enumerate(reader, start=2):
            errors: list[str] = []
            if not row.get("portfolio_name"):
                errors.append("portfolio_name is required")
            if not row.get("symbol"):
                errors.append("symbol is required")
            if not row.get("quantity"):
                errors.append("quantity is required")
            if not row.get("average_entry_price"):
                errors.append("average_entry_price is required")

            preview_row = CSVImportPreviewRow(
                row_number=index,
                portfolio_name=row.get("portfolio_name"),
                symbol=row.get("symbol"),
                name=row.get("name"),
                quantity=row.get("quantity"),
                average_entry_price=row.get("average_entry_price"),
                notes=row.get("notes"),
                valid=not errors,
                errors=errors,
            )
            rows.append(preview_row)
            if errors:
                invalid_row_count += 1
            else:
                valid_row_count += 1

        return CSVImportPreviewResponse(
            columns=columns,
            rows=rows,
            valid_row_count=valid_row_count,
            invalid_row_count=invalid_row_count,
        )
