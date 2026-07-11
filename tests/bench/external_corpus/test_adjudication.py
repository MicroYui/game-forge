from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.bench.external_corpus.adjudication import (
    AdjudicationError,
    adjudicate,
    build_review_package,
)
from gameforge.bench.external_corpus.contracts import (
    AdjudicationEvidence,
    AdjudicationPayload,
    CandidateDisposition,
    LineageResolution,
    ReviewAttestation,
    SelectedParentEdge,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.taxonomy import DefectClass
from tests.bench.external_corpus.adjudication_fixture import (
    discovery_ledger,
    discovery_with_external_lineage_siblings,
    discovery_with_lineage,
    discovery_with_recursive_external_revert,
    oid,
    reviewed_evidence,
)


def _reattest(evidence, **updates):
    changed = evidence.model_copy(update=updates)
    payload = AdjudicationPayload.model_validate(
        changed.model_dump(mode="json", exclude={"review_attestation"})
    )
    attestation = changed.review_attestation.model_copy(
        update={"reviewed_payload_sha256": sha256_hex(canonical_bytes(payload))}
    )
    return AdjudicationEvidence.model_validate(
        {
            **payload.model_dump(mode="json"),
            "review_attestation": attestation.model_dump(mode="json"),
        }
    )


@pytest.mark.parametrize(
    ("groups", "classes", "expected"),
    [(7, 4, "insufficient_evidence"), (8, 3, "insufficient_evidence"), (8, 4, "pass")],
)
def test_gate_counts_independent_groups_and_applicable_classes_only(groups, classes, expected):
    discovery = discovery_ledger()
    ledger, decision = adjudicate(
        discovery,
        reviewed_evidence(discovery, group_count=groups, class_count=classes),
    )

    assert decision.gate.status == expected
    assert decision.gate.independent_proposed_groups == groups
    assert decision.gate.domain_applicable_proposed_classes == classes
    assert decision.gate.next_action == (
        "proceed_to_b0b" if expected == "pass" else "stop_source_and_use_fallback"
    )
    assert ledger.gate_summary == decision.gate


def test_unattested_payload_cannot_produce_a_decision():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    payload = AdjudicationPayload.model_validate(
        evidence.model_dump(mode="json", exclude={"review_attestation"})
    )

    with pytest.raises(AdjudicationError, match="human review attestation"):
        adjudicate(discovery, payload)


def test_every_selected_candidate_has_exactly_one_assignment():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    incomplete = _reattest(
        evidence,
        candidate_decisions=evidence.candidate_decisions[:-1],
    )

    with pytest.raises(AdjudicationError, match="every discovered candidate"):
        adjudicate(discovery, incomplete)


def test_attestation_binds_payload_and_reviewer_differs_from_adjudicator():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    changed_group = evidence.group_decisions[0].model_copy(update={"rationale": "Changed later"})
    changed = evidence.model_copy(
        update={"group_decisions": [changed_group, *evidence.group_decisions[1:]]}
    )
    with pytest.raises(AdjudicationError, match="attestation|payload"):
        adjudicate(discovery, changed)

    payload = evidence.model_dump(mode="json", exclude={"review_attestation"})
    payload_hash = sha256_hex(canonical_bytes(AdjudicationPayload.model_validate(payload)))
    invalid_attestation = ReviewAttestation(
        reviewer_kind="human",
        reviewer_id="adjudicator.fixture",
        reviewed_at="2026-07-11T00:00:00Z",
        written_statement="I reviewed the complete candidate assignment table.",
        candidate_universe_sha256=discovery.candidate_universe_sha256,
        reviewed_payload_sha256=payload_hash,
    )
    invalid = evidence.model_copy(update={"review_attestation": invalid_attestation})
    with pytest.raises(AdjudicationError, match="reviewer.*adjudicator"):
        adjudicate(discovery, invalid)


def test_non_config_candidate_cannot_enter_a_proposed_group():
    discovery = discovery_ledger(non_config_indices={0})
    evidence = reviewed_evidence(discovery)

    with pytest.raises(AdjudicationError, match="non-config"):
        adjudicate(discovery, evidence)


def test_evidence_reference_must_belong_to_its_owner():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    first = evidence.group_decisions[0]
    foreign_ref = evidence.group_decisions[1].root_cause_evidence_refs[0]
    changed_first = first.model_copy(update={"root_cause_evidence_refs": [foreign_ref]})
    payload = AdjudicationPayload.model_validate(
        {
            **evidence.model_dump(mode="json", exclude={"review_attestation"}),
            "group_decisions": [
                changed_first.model_dump(mode="json"),
                *[item.model_dump(mode="json") for item in evidence.group_decisions[1:]],
            ],
        }
    )
    rebound = AdjudicationEvidence.model_validate(
        {
            **payload.model_dump(mode="json"),
            "review_attestation": evidence.review_attestation.model_copy(
                update={"reviewed_payload_sha256": sha256_hex(canonical_bytes(payload))}
            ).model_dump(mode="json"),
        }
    )

    with pytest.raises(AdjudicationError, match="does not belong"):
        adjudicate(discovery, rebound)


def test_multi_commit_group_must_be_contiguous_on_first_parent():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    first = evidence.group_decisions[0]
    second = evidence.group_decisions[1]
    combined = first.model_copy(
        update={
            "commits": [first.commits[0], second.commits[0]],
            "selected_parent_edges": [
                first.selected_parent_edges[0],
                second.selected_parent_edges[0],
            ],
        }
    )
    changed = _reattest(
        evidence,
        group_decisions=[combined, *evidence.group_decisions[2:]],
    )

    with pytest.raises(AdjudicationError, match="first-parent"):
        adjudicate(discovery, changed)


def test_unknown_candidate_assignment_is_a_domain_error():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    first = evidence.group_decisions[0]
    unknown_oid = oid(99999)
    changed_first = first.model_copy(
        update={
            "commits": [unknown_oid],
            "selected_parent_edges": [
                SelectedParentEdge(commit_oid=unknown_oid, parent_oid=oid(88888))
            ],
        }
    )
    reviewed = _reattest(
        evidence,
        group_decisions=[changed_first, *evidence.group_decisions[1:]],
    )

    with pytest.raises(AdjudicationError, match="unknown commit"):
        adjudicate(discovery, reviewed)


def _replace_disposition_reason(evidence, commit_oid, reason_code):
    decisions = []
    for decision in evidence.candidate_decisions:
        if decision.commit_oid != commit_oid:
            decisions.append(decision)
            continue
        decisions.append(
            CandidateDisposition(
                commit_oid=decision.commit_oid,
                disposition="rejected",
                reason_code=reason_code,
                rationale=f"Reviewed exclusion as {reason_code}",
                evidence_refs=decision.evidence_refs,
                adjudicator_id=decision.adjudicator_id,
            )
        )
    return decisions


def test_lineage_siblings_cannot_both_count_as_independent_groups():
    discovery = discovery_with_lineage("patch_id")
    evidence = reviewed_evidence(discovery)
    link = discovery.objective_lineage_links[0]
    reviewed = _reattest(
        evidence,
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="separate",
                affected_group_ids=["group.0", "group.1"],
                rationale="Reviewed as separate candidates with identical patch evidence.",
            )
        ],
    )

    with pytest.raises(AdjudicationError, match="lineage siblings"):
        adjudicate(discovery, reviewed)


def test_external_lineage_context_connects_selected_siblings_without_assignment():
    discovery = discovery_with_external_lineage_siblings()
    package = build_review_package(discovery)
    assert package.rows[0].lineage_links == [
        discovery.objective_lineage_links[0].link_id
    ]
    assert package.rows[1].lineage_links == [
        discovery.objective_lineage_links[1].link_id
    ]

    evidence = reviewed_evidence(discovery)
    reviewed = _reattest(
        evidence,
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=[f"group.{index}"],
                rationale="Both selected backports reference one external source commit.",
            )
            for index, link in enumerate(discovery.objective_lineage_links)
        ],
    )

    with pytest.raises(AdjudicationError, match="lineage siblings"):
        adjudicate(discovery, reviewed)


def test_recursive_external_revert_context_needs_no_candidate_disposition():
    discovery = discovery_with_recursive_external_revert()
    evidence = reviewed_evidence(discovery)
    reviewed = _reattest(
        evidence,
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=(
                    ["group.0"] if link.link_type == "backport" else []
                ),
                rationale="Reviewed recursive external lineage context.",
            )
            for link in discovery.objective_lineage_links
        ],
    )

    ledger, decision = adjudicate(discovery, reviewed)

    assert decision.gate.status == "pass"
    assert ledger.lineage_resolutions == reviewed.lineage_resolutions


def test_one_reviewed_lineage_representative_can_count():
    discovery = discovery_with_lineage("patch_id")
    evidence = reviewed_evidence(
        discovery,
        group_indices=[0, 2, 3, 4, 5, 6, 7, 8],
    )
    target_oid = discovery.discovered_candidates[1].commit.commit_oid
    link = discovery.objective_lineage_links[0]
    reviewed = _reattest(
        evidence,
        candidate_decisions=_replace_disposition_reason(evidence, target_oid, "duplicate_lineage"),
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=["group.0"],
                rationale="The first endpoint is the reviewed representative.",
            )
        ],
    )

    ledger, decision = adjudicate(discovery, reviewed)

    assert decision.gate.status == "pass"
    assert ledger.groups[0].lineage_links == [link.link_id]


def test_same_group_resolution_cannot_name_two_distinct_fix_groups():
    discovery = discovery_with_lineage("patch_id")
    evidence = reviewed_evidence(discovery)
    second = evidence.group_decisions[1]
    rejected_case = second.case_decisions[0].model_copy(update={"disposition": "rejected"})
    rejected_second = second.model_copy(update={"case_decisions": [rejected_case]})
    link = discovery.objective_lineage_links[0]
    reviewed = _reattest(
        evidence,
        group_decisions=[
            evidence.group_decisions[0],
            rejected_second,
            *evidence.group_decisions[2:],
        ],
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=["group.0", "group.1"],
                rationale="Reviewed as one fix despite separate group assignments.",
            )
        ],
    )

    with pytest.raises(AdjudicationError, match="same_group.*one fix group"):
        adjudicate(discovery, reviewed)


def test_revert_endpoint_never_counts_or_enters_a_group():
    discovery = discovery_with_lineage("revert")
    link = discovery.objective_lineage_links[0]
    invalid = reviewed_evidence(discovery)
    invalid_reviewed = _reattest(
        invalid,
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=["group.0", "group.1"],
                rationale="Reviewed revert relationship.",
            )
        ],
    )
    with pytest.raises(AdjudicationError, match="revert endpoint"):
        adjudicate(discovery, invalid_reviewed)

    valid = reviewed_evidence(
        discovery,
        group_indices=[0, 2, 3, 4, 5, 6, 7, 8],
    )
    target_oid = discovery.discovered_candidates[1].commit.commit_oid
    valid_reviewed = _reattest(
        valid,
        candidate_decisions=_replace_disposition_reason(valid, target_oid, "revert"),
        lineage_resolutions=[
            LineageResolution(
                link_id=link.link_id,
                resolution="same_group",
                affected_group_ids=["group.0"],
                rationale="The target commit reverts the represented fix.",
            )
        ],
    )
    _ledger, decision = adjudicate(discovery, valid_reviewed)
    assert decision.gate.status == "pass"


def test_not_applicable_class_cannot_be_proposed():
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery)
    first_group = evidence.group_decisions[0]
    first_case = first_group.case_decisions[0].model_copy(
        update={"defect_class": list(DefectClass)[4]}
    )
    changed_group = first_group.model_copy(update={"case_decisions": [first_case]})
    reviewed = _reattest(
        evidence,
        group_decisions=[changed_group, *evidence.group_decisions[1:]],
    )

    with pytest.raises(AdjudicationError, match="not_applicable"):
        adjudicate(discovery, reviewed)


def test_review_package_is_complete_but_non_approving():
    discovery = discovery_ledger()

    package = build_review_package(discovery)

    assert package.review_status == "awaiting_human"
    assert [row.commit.commit_oid for row in package.rows] == [
        item.commit.commit_oid for item in discovery.discovered_candidates
    ]
    assert package.candidate_universe_sha256 == discovery.candidate_universe_sha256
    assert "reviewer" not in package.model_dump_json()
    assert "disposition" not in package.model_dump_json()
    with pytest.raises(ValidationError):
        AdjudicationEvidence.model_validate(package.model_dump(mode="json"))
