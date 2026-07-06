import json

from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.schema_registry import SchemaRegistry
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

_DIR = "scenarios/outpost"


def _schema():
    return FormatSchema.model_validate(json.load(open(f"{_DIR}/format_schema.json")))


def test_outpost_validates_clean_against_schema():
    schema = _schema()
    wb = read_workbook(_DIR, schema)
    assert SchemaRegistry().validate(schema, wb) == []  # no type/FK/enum errors


def test_outpost_roundtrips_lossless():
    schema = _schema()
    wb = read_workbook(_DIR, schema)
    adapter = AureusCsvAdapter()
    snapA = adapter.to_ir(wb, file_ref=_DIR)
    wb2 = adapter.from_ir(snapA)
    assert wb2 == wb
    assert adapter.to_ir(wb2, file_ref=_DIR).to_graph().diff(snapA.to_graph()).is_empty()


def test_outpost_has_all_four_systems():
    wb = read_workbook(_DIR, _schema())
    assert wb["monsters"] and wb["shops"] and wb["gacha_pools"]
    assert any(s["kind"] == "fight" for s in wb["quest_steps"])
