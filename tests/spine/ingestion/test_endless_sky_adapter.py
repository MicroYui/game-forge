from __future__ import annotations

from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ingestion.endless_sky_adapter import (
    EndlessSkyContext,
    EndlessSkyTarget,
    EndlessSkyTxtAdapter,
    quest_id,
    region_id,
)
from gameforge.spine.ingestion.endless_sky_reader import read_source_tree


EMPTY_CONTEXT = EndlessSkyContext(resources=(), restricted_destinations=())


def _target(name: str, *, path: str = "data/missions.txt") -> EndlessSkyTarget:
    return EndlessSkyTarget(path=path, record_kind="mission", record_name=name)


def _adapt(
    raw: bytes,
    target: EndlessSkyTarget,
    *,
    context: EndlessSkyContext = EMPTY_CONTEXT,
):
    tree = read_source_tree({target.path: raw})
    return EndlessSkyTxtAdapter().to_ir(tree, targets=(target,), context=context)


def _relations(snapshot, edge_type: EdgeType):
    return [relation for relation in snapshot.relations.values() if relation.type is edge_type]


def _one_relation(snapshot, edge_type: EdgeType):
    relations = _relations(snapshot, edge_type)
    assert len(relations) == 1
    return relations[0]


def test_adapter_round_trips_unknown_records_and_exact_file_bytes() -> None:
    raw = (
        b"# preamble\r\n"
        b"color custom\r\n"
        b"\tunknown `opaque value`\r\n"
        b"\r\n"
        b'mission "Unselected"\r\n'
        b"\tlanding\r\n"
    )
    tree = read_source_tree({"data/mixed.txt": raw, "data/empty.txt": b""})

    snapshot = EndlessSkyTxtAdapter().to_ir(
        tree,
        targets=(),
        context=EMPTY_CONTEXT,
    )

    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {
        "data/empty.txt": b"",
        "data/mixed.txt": raw,
    }
    assert all(entity.type is NodeType.EVENT for entity in snapshot.entities.values())


def test_adapter_preserves_duplicate_named_unknown_records_by_source_order() -> None:
    raw = (
        b'phrase "shared name"\n'
        b"\tword first\n"
        b"\n"
        b'phrase "shared name"\n'
        b"\tword second\n"
    )
    tree = read_source_tree({"data/phrases.txt": raw})

    snapshot = EndlessSkyTxtAdapter().to_ir(
        tree,
        targets=(),
        context=EMPTY_CONTEXT,
    )

    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {"data/phrases.txt": raw}


def test_mission_maps_to_generic_quest_start_step_destination_and_gate() -> None:
    raw = (
        b'mission "Deliver"\n'
        b"\tlanding\n"
        b'\tdestination "Mars"\n'
        b"\tclearance\n"
    )
    context = EndlessSkyContext(resources=(), restricted_destinations=("Mars",))

    snapshot = _adapt(raw, _target("Deliver"), context=context)
    graph = snapshot.to_graph()

    assert graph.get_node(quest_id("Deliver")).type is NodeType.QUEST
    assert _one_relation(snapshot, EdgeType.STARTS_AT).src_id == quest_id("Deliver")
    assert _one_relation(snapshot, EdgeType.HAS_STEP).src_id == quest_id("Deliver")
    assert _one_relation(snapshot, EdgeType.LOCATED_IN).dst_id == region_id("Mars")
    gate = _one_relation(snapshot, EdgeType.GATED_BY)
    assert gate.src_id == region_id("Mars")
    unlock = _one_relation(snapshot, EdgeType.UNLOCKS)
    assert unlock.src_id == quest_id("Deliver")
    assert unlock.dst_id == gate.dst_id
    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {"data/missions.txt": raw}


def test_landing_access_condition_is_a_generic_access_proof() -> None:
    without_proof = (
        b'mission "Deliver"\n'
        b'\tsource "Earth"\n'
        b'\tdestination "Mars"\n'
    )
    with_proof = without_proof + b'\tto offer\n\t\thas "landing access: Mars"\n'
    context = EndlessSkyContext(resources=(), restricted_destinations=("Mars",))

    blocked = _adapt(without_proof, _target("Deliver"), context=context)
    accessible = _adapt(with_proof, _target("Deliver"), context=context)

    blocked_findings = [
        finding
        for finding in GraphChecker().check(blocked)
        if finding.defect_class == "unreachable_target"
    ]
    accessible_findings = [
        finding
        for finding in GraphChecker().check(accessible)
        if finding.defect_class == "unreachable_target"
    ]
    assert len(blocked_findings) == 1
    assert accessible_findings == []
    proof = _one_relation(accessible, EdgeType.REQUIRES)
    assert proof.src_id == quest_id("Deliver")
    assert proof.dst_id == _one_relation(accessible, EdgeType.GATED_BY).dst_id


def test_direct_mission_state_dependency_is_mapped_as_a_real_quest() -> None:
    raw = (
        b'mission "Prior"\n'
        b"\tlanding\n"
        b"\n"
        b'mission "Current"\n'
        b"\tspaceport\n"
        b"\tto offer\n"
        b'\t\thas "Prior: done"\n'
    )

    snapshot = _adapt(raw, _target("Current"))
    graph = snapshot.to_graph()

    assert graph.get_node(quest_id("Current")).type is NodeType.QUEST
    assert graph.get_node(quest_id("Prior")).type is NodeType.QUEST
    dependency = _one_relation(snapshot, EdgeType.REQUIRES)
    assert (dependency.src_id, dependency.dst_id) == (
        quest_id("Current"),
        quest_id("Prior"),
    )
    for name in ("Current", "Prior"):
        assert graph.neighbors(quest_id(name), EdgeType.HAS_STEP, direction="out")
        assert graph.neighbors(quest_id(name), EdgeType.STARTS_AT, direction="out")
    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {"data/missions.txt": raw}


def test_mission_dependency_outside_loaded_tree_remains_raw_only() -> None:
    raw = (
        b'mission "Current"\n'
        b"\tlanding\n"
        b"\tto offer\n"
        b'\t\thas "External Story: done"\n'
    )

    snapshot = _adapt(raw, _target("Current"))

    assert _relations(snapshot, EdgeType.REQUIRES) == []
    assert [
        finding
        for finding in GraphChecker().check(snapshot)
        if finding.defect_class == "dangling_reference"
    ] == []
    assert EndlessSkyTxtAdapter().from_ir(snapshot) == {"data/missions.txt": raw}
