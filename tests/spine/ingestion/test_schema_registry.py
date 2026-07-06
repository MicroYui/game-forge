from gameforge.spine.ingestion.format_schema import FormatSchema, SheetSchema, ColumnSchema
from gameforge.spine.ingestion.schema_registry import SchemaRegistry


def _schema():
    return FormatSchema(format_id="aureus", version="1", sheets=[
        SheetSchema(name="items", primary_key="item_id",
                    columns=[ColumnSchema(name="item_id"), ColumnSchema(name="name", required=False)]),
        SheetSchema(name="quest_steps", primary_key="step_id", columns=[
            ColumnSchema(name="step_id"),
            ColumnSchema(name="kind", enum=["talk", "collect", "turn_in", "fight"]),
            ColumnSchema(name="item", required=False, foreign_key="items.item_id"),
        ]),
    ])


def test_register_and_get_roundtrips():
    reg = SchemaRegistry()
    reg.register(_schema())
    assert reg.get("aureus", "1").format_id == "aureus"


def test_validate_flags_bad_enum_and_dangling_fk():
    reg = SchemaRegistry()
    wb = {
        "items": [{"item_id": "item:x", "name": "X"}],
        "quest_steps": [
            {"step_id": "s1", "kind": "BOGUS", "item": None},                 # bad enum
            {"step_id": "s2", "kind": "collect", "item": "item:ghost"},        # dangling FK
            {"step_id": "s3", "kind": "collect", "item": "item:x"},            # ok
        ],
    }
    errs = reg.validate(_schema(), wb)
    kinds = {(e.sheet, e.column) for e in errs}
    assert ("quest_steps", "kind") in kinds and ("quest_steps", "item") in kinds
    assert len(errs) == 2


def test_validate_clean_workbook_no_errors():
    reg = SchemaRegistry()
    wb = {"items": [{"item_id": "item:x", "name": "X"}], "quest_steps": []}
    assert reg.validate(_schema(), wb) == []
