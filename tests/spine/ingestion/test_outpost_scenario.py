import json
from collections import Counter

from gameforge.contracts.ir import EdgeType
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


def test_outpost_to_ir_builds_derived_relations():
    # `to_ir`'s pass-1 (row -> Entity) is what `from_ir` reads back, so the
    # round-trip test above can pass even if pass-2 (derived-relation
    # building) is completely broken -- to_ir is pure, so a broken pass-2
    # produces the SAME (wrong) graph on both sides of the round trip.
    # This test asserts the pass-2 output directly: every relation type the
    # adapter is documented to derive from the outpost config must actually
    # show up as a graph edge, and HAS_STEP must match the quest's step count
    # so a regression that silently drops step/edge-building is caught.
    wb = read_workbook(_DIR, _schema())
    snap = AureusCsvAdapter().to_ir(wb, file_ref=_DIR)
    relations = list(snap.to_graph().all_relations())
    counts = Counter(r.type for r in relations)

    expected_types = {
        EdgeType.HAS_STEP,
        EdgeType.PRECEDES,
        EdgeType.TALKS_TO,
        EdgeType.REQUIRES,
        EdgeType.TRIGGERED_BY,
        EdgeType.DROPS_FROM,
        EdgeType.SELLS,
        EdgeType.USES_SKILL,
        EdgeType.APPLIES_EFFECT,
    }
    assert expected_types <= set(counts)

    # quest:outpost has 4 steps (talk, collect, fight, turn_in) -> 4 HAS_STEP
    # edges; a regression that drops step edges (or only emits one) fails here.
    assert counts[EdgeType.HAS_STEP] == 4
