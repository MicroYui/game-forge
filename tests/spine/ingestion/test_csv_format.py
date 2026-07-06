import pytest

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

def test_json_cell_nested_float_roundtrips(tmp_path):
    # drop_tables.entries-shaped payload: a json cell whose nested value is a
    # float must come back as a float, not a canonical_json "f:0.5" string.
    wb = {"monsters": [{
        "monster_id": "m", "hp": 1, "skills": [],
        "stats": {"entries": [{"item": "i", "probability": 0.5}]},
        "rate": 1.0,
    }]}
    write_workbook(str(tmp_path), _schema(), wb)
    back = read_workbook(str(tmp_path), _schema())
    assert back == wb
    probability = back["monsters"][0]["stats"]["entries"][0]["probability"]
    assert isinstance(probability, float)
    assert probability == 0.5

def test_json_cell_nested_none_roundtrips(tmp_path):
    # canonical_json drops None-valued dict keys (it's a hashing helper, not
    # a lossless serializer); a json cell must preserve the key with None.
    wb = {"monsters": [{
        "monster_id": "m", "hp": 1, "skills": [],
        "stats": {"entries": [{"item": "i", "note": None}]},
        "rate": 1.0,
    }]}
    write_workbook(str(tmp_path), _schema(), wb)
    back = read_workbook(str(tmp_path), _schema())
    assert back == wb
    entry = back["monsters"][0]["stats"]["entries"][0]
    assert "note" in entry
    assert entry["note"] is None

def test_json_cell_multi_key_dict_deterministic_across_writes(tmp_path):
    wb = {"monsters": [{
        "monster_id": "m", "hp": 1, "skills": [],
        "stats": {"b": 1, "a": 2, "c": 3},
        "rate": 1.0,
    }]}
    write_workbook(str(tmp_path), _schema(), wb)
    first = (tmp_path / "monsters.csv").read_bytes()
    write_workbook(str(tmp_path), _schema(), wb)
    second = (tmp_path / "monsters.csv").read_bytes()
    assert first == second


def _bool_schema():
    return FormatSchema(format_id="t", version="1", sheets=[
        SheetSchema(name="flags", columns=[
            ColumnSchema(name="key"),
            ColumnSchema(name="req_flag", type="bool"),
            ColumnSchema(name="opt_flag", type="bool", required=False),
        ]),
    ])

def test_required_bool_empty_cell_raises(tmp_path):
    schema = _bool_schema()
    write_workbook(str(tmp_path), schema, {"flags": [{"key": "k", "req_flag": None, "opt_flag": True}]})
    with pytest.raises(ValueError):
        read_workbook(str(tmp_path), schema)

def test_optional_bool_empty_cell_is_none(tmp_path):
    schema = _bool_schema()
    write_workbook(str(tmp_path), schema, {"flags": [{"key": "k", "req_flag": True, "opt_flag": None}]})
    back = read_workbook(str(tmp_path), schema)
    assert back["flags"][0]["opt_flag"] is None
