from __future__ import annotations

import json
from pathlib import Path

from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.contracts.ir import EdgeType
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ingestion.endless_sky_adapter import (
    EndlessSkyContext,
    EndlessSkyResource,
    EndlessSkyTarget,
    EndlessSkyTxtAdapter,
)
from gameforge.spine.ingestion.endless_sky_reader import read_source_tree


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
REGISTRATION = load_case_specs(CORPUS / "case-specs.json")
EMPTY_CONTEXT = EndlessSkyContext(resources=(), restricted_destinations=())


def _adapt_dialogue(raw: bytes):
    path = "data/dialogue.txt"
    return EndlessSkyTxtAdapter().to_ir(
        read_source_tree({path: raw}),
        targets=(
            EndlessSkyTarget(
                path=path,
                record_kind="mission",
                record_name="Branching Exercise",
            ),
        ),
        context=EMPTY_CONTEXT,
    )


def _findings(snapshot, defect_class: str):
    return [
        finding
        for finding in GraphChecker().check(snapshot)
        if finding.defect_class == defect_class
    ]


def _case_snapshot(case_id: str, side: str):
    spec = next(spec for spec in REGISTRATION.cases if spec.case_id == case_id)
    case_root = CORPUS / "cases" / case_id
    side_root = case_root / side
    tree = read_source_tree(
        {path: (side_root / path).read_bytes() for path in spec.changed_paths}
    )
    payload = json.loads((case_root / "context.json").read_bytes())
    context = EndlessSkyContext(
        resources=tuple(
            EndlessSkyResource(kind=item["kind"], name=item["name"])
            for item in payload["resources"]
        ),
        restricted_destinations=tuple(payload["restricted_destinations"]),
    )
    targets = tuple(
        EndlessSkyTarget(
            path=target.path,
            record_kind=target.record_kind,
            record_name=target.record_name,
        )
        for target in spec.target_locators
    )
    return EndlessSkyTxtAdapter().to_ir(tree, targets=targets, context=context)


def test_missing_choice_merge_becomes_an_ordinary_dangling_relation() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\tlanding\n"
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\toption-one\n"
        b"\t\t\t\toption-two\n"
        b"\t\t\t\toption-three\n"
        b"\t\t\t\t\tgoto shared\n"
        b"\t\t\tfirst-path\n"
        b"\t\t\tlabel shared\n"
        b"\t\t\tsecond-path\n"
    )

    snapshot = _adapt_dialogue(raw)
    findings = _findings(snapshot, "dangling_reference")

    assert len(findings) == 1
    assert findings[0].evidence["edge_type"] == EdgeType.REFERENCES.value


def test_single_fallthrough_branch_can_legitimately_join_an_explicit_branch() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\tlanding\n"
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tlong-answer\n"
        b"\t\t\t\tshort-answer\n"
        b"\t\t\t\t\tgoto shared\n"
        b"\t\t\tlong-answer-detail\n"
        b"\t\t\tlabel shared\n"
        b"\t\t\tcommon-continuation\n"
    )

    snapshot = _adapt_dialogue(raw)

    assert _findings(snapshot, "dangling_reference") == []


def test_explicit_merge_label_clears_the_dangling_relation() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\tlanding\n"
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\toption-one\n"
        b"\t\t\t\toption-two\n"
        b"\t\t\t\t\tgoto shared\n"
        b"\t\t\tfirst-path\n"
        b"\t\t\t\tgoto merge\n"
        b"\t\t\tlabel shared\n"
        b"\t\t\tsecond-path\n"
        b"\t\t\tlabel merge\n"
        b"\t\t\tmerged-path\n"
    )

    snapshot = _adapt_dialogue(raw)

    assert any(
        relation.type is EdgeType.PRECEDES
        for relation in snapshot.relations.values()
    )
    assert _findings(snapshot, "dangling_reference") == []


def test_monotonic_display_guard_marks_only_the_guarded_transition_once() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\tlanding\n"
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tlabel alpha\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tvisit-beta\n"
        b"\t\t\t\t\tto display\n"
        b'\t\t\t\t\t\tnot "visited beta"\n'
        b"\t\t\tlabel beta\n"
        b"\t\t\taction\n"
        b'\t\t\t\tset "visited beta"\n'
        b"\t\t\tchoice\n"
        b"\t\t\t\treturn-alpha\n"
        b"\t\t\t\t\tgoto alpha\n"
    )

    snapshot = _adapt_dialogue(raw)
    transitions = [
        relation
        for relation in snapshot.relations.values()
        if relation.type is EdgeType.PRECEDES
    ]

    assert sum(
        relation.attrs == {"repeatability": "once"}
        for relation in transitions
    ) == 1
    assert _findings(snapshot, "cyclic_dependency") == []


def test_unmatched_guard_remains_repeatable_and_cycle_is_detected() -> None:
    raw = (
        b'mission "Branching Exercise"\n'
        b"\tlanding\n"
        b"\ton offer\n"
        b"\t\tconversation\n"
        b"\t\t\tlabel alpha\n"
        b"\t\t\tchoice\n"
        b"\t\t\t\tvisit-beta\n"
        b"\t\t\t\t\tto display\n"
        b'\t\t\t\t\t\tnot "guard flag"\n'
        b"\t\t\tlabel beta\n"
        b"\t\t\taction\n"
        b'\t\t\t\tset "different flag"\n'
        b"\t\t\tchoice\n"
        b"\t\t\t\treturn-alpha\n"
        b"\t\t\t\t\tgoto alpha\n"
    )

    snapshot = _adapt_dialogue(raw)

    assert all(
        relation.attrs != {"repeatability": "once"}
        for relation in snapshot.relations.values()
        if relation.type is EdgeType.PRECEDES
    )
    assert len(_findings(snapshot, "cyclic_dependency")) == 1


def test_named_resource_reference_is_dangling_until_context_declares_it() -> None:
    path = "data/effects.txt"
    raw = b'effect "Pulse"\n\tsound "pulse impact"\n'
    tree = read_source_tree({path: raw})
    target = EndlessSkyTarget(path=path, record_kind="effect", record_name="Pulse")

    missing = EndlessSkyTxtAdapter().to_ir(
        tree,
        targets=(target,),
        context=EMPTY_CONTEXT,
    )
    present = EndlessSkyTxtAdapter().to_ir(
        tree,
        targets=(target,),
        context=EndlessSkyContext(
            resources=(EndlessSkyResource(kind="sound", name="pulse impact"),),
            restricted_destinations=(),
        ),
    )

    assert len(_findings(missing, "dangling_reference")) == 1
    assert _findings(present, "dangling_reference") == []


def test_development_conversation_loop_is_fixed_by_monotonic_guards() -> None:
    before = _case_snapshot("endless-sky.cyclic-dependency.development", "before")
    after = _case_snapshot("endless-sky.cyclic-dependency.development", "after")

    assert _findings(before, "cyclic_dependency")
    assert _findings(after, "cyclic_dependency") == []


def test_verification_missing_merge_is_cleared_by_explicit_label() -> None:
    before = _case_snapshot("endless-sky.dangling-reference.verification", "before")
    after = _case_snapshot("endless-sky.dangling-reference.verification", "after")

    assert any(
        finding.evidence["edge_type"] == EdgeType.REFERENCES.value
        for finding in _findings(before, "dangling_reference")
    )
    assert _findings(after, "dangling_reference") == []


def test_development_sound_reference_is_cleared_by_existing_resource() -> None:
    before = _case_snapshot("endless-sky.dangling-reference.development", "before")
    after = _case_snapshot("endless-sky.dangling-reference.development", "after")

    assert len(_findings(before, "dangling_reference")) == 1
    assert _findings(after, "dangling_reference") == []
