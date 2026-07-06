from gameforge.spine.ingestion.format_schema import FormatSchema, SheetSchema, ColumnSchema
from gameforge.spine.ingestion.csv_format import read_workbook, write_workbook

def _schema():
    return FormatSchema(format_id="t", version="1", sheets=[
        SheetSchema(name="monsters", columns=[
            ColumnSchema(name="monster_id"),
            ColumnSchema(name="hp", type="int"),
            ColumnSchema(name="skills", type="str_list", required=False),
            ColumnSchema(name="stats", type="json", required=False),
            ColumnSchema(name="rate", type="float"),
        ]),
    ])

def test_write_then_read_roundtrips_typed_values(tmp_path):
    wb = {"monsters": [
        {"monster_id": "m:1", "hp": 20, "skills": ["a", "b"], "stats": {"atk": 3}, "rate": 0.5},
        {"monster_id": "m:2", "hp": 5, "skills": [], "stats": None, "rate": 1.0},
    ]}
    write_workbook(str(tmp_path), _schema(), wb)
    back = read_workbook(str(tmp_path), _schema())
    assert back == wb  # field-level equality after a full write→read cycle

def test_float_canonical_no_drift(tmp_path):
    wb = {"monsters": [{"monster_id": "m", "hp": 1, "skills": [], "stats": None, "rate": 1.10}]}
    write_workbook(str(tmp_path), _schema(), wb)
    assert read_workbook(str(tmp_path), _schema())["monsters"][0]["rate"] == 1.1
