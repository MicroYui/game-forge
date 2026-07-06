"""FormatSchema — typed schema description for spreadsheet-like ingestion formats.

Deterministic data-only contract: no DB, no LLM, no runtime imports (spine
boundary, contract §1). Consumed by SchemaRegistry (schema_registry.py) and
by format-specific readers (e.g. csv_format, Task 7) that coerce raw string
cells into these declared types before validation runs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ColumnType = Literal["str", "int", "float", "bool", "int_list", "str_list", "json"]


class ColumnSchema(BaseModel):
    name: str
    type: ColumnType = "str"
    required: bool = True
    enum: list[str] | None = None
    foreign_key: str | None = None  # "sheet.column"


class SheetSchema(BaseModel):
    name: str
    primary_key: str | None = None
    columns: list[ColumnSchema] = Field(default_factory=list)


class FormatSchema(BaseModel):
    format_id: str
    version: str
    sheets: list[SheetSchema] = Field(default_factory=list)

    def sheet(self, name: str) -> SheetSchema | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None
