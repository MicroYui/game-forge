"""SchemaRegistry — register/lookup FormatSchemas and validate typed workbooks.

Deterministic constraint checking only (contract §1: spine is LLM-free).
`validate` assumes rows are already typed (str/int/float/bool/... per
ColumnSchema.type) — coercion from raw strings happens in format-specific
readers (e.g. csv_format, Task 7), not here. This checks: required-non-None,
enum membership, and foreign-key existence against the referenced sheet's
referenced column value-set.
"""

from __future__ import annotations

from pydantic import BaseModel

from gameforge.spine.ingestion.format_schema import FormatSchema


class SchemaError(BaseModel):
    sheet: str
    row: int
    column: str | None = None
    message: str


class SchemaRegistry:
    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], FormatSchema] = {}

    def register(self, schema: FormatSchema) -> None:
        self._schemas[(schema.format_id, schema.version)] = schema

    def get(self, format_id: str, version: str) -> FormatSchema:
        key = (format_id, version)
        if key not in self._schemas:
            raise KeyError(f"no FormatSchema registered for {format_id!r} version {version!r}")
        return self._schemas[key]

    def validate(self, schema: FormatSchema, workbook: dict[str, list[dict]]) -> list[SchemaError]:
        errors: list[SchemaError] = []
        # Precompute each sheet's column -> set-of-values, for foreign-key checks.
        value_sets: dict[str, dict[str, set]] = {}
        for sheet in schema.sheets:
            rows = workbook.get(sheet.name, [])
            cols: dict[str, set] = {}
            for col in sheet.columns:
                cols[col.name] = {row.get(col.name) for row in rows if row.get(col.name) is not None}
            value_sets[sheet.name] = cols

        for sheet in schema.sheets:
            rows = workbook.get(sheet.name, [])
            for row_idx, row in enumerate(rows):
                for col in sheet.columns:
                    value = row.get(col.name)

                    if value is None:
                        if col.required:
                            errors.append(SchemaError(
                                sheet=sheet.name, row=row_idx, column=col.name,
                                message=f"required column {col.name!r} is missing/None",
                            ))
                        continue

                    if col.enum is not None and value not in col.enum:
                        errors.append(SchemaError(
                            sheet=sheet.name, row=row_idx, column=col.name,
                            message=f"value {value!r} not in enum {col.enum}",
                        ))

                    if col.foreign_key is not None:
                        ref_sheet, ref_column = col.foreign_key.split(".", 1)
                        ref_values = value_sets.get(ref_sheet, {}).get(ref_column, set())
                        if value not in ref_values:
                            errors.append(SchemaError(
                                sheet=sheet.name, row=row_idx, column=col.name,
                                message=(
                                    f"foreign key {col.name}={value!r} not found in "
                                    f"{col.foreign_key!r}"
                                ),
                            ))

        errors.sort(key=lambda e: (e.sheet, e.row))
        return errors
