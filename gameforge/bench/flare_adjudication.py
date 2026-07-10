"""Pure offline adjudication for the Flare B0A evidence ledger."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from gameforge.bench.flare_evidence import (
    B0A_DEFECT_CLASSES,
    AdjudicationEvidence,
    ApplicabilityDeclaration,
    ApplicabilityRow,
    B0ADecision,
    CandidateDisposition,
    CandidateFixGroup,
    CandidateGroupDecision,
    CandidateLedger,
    DiscoveryLedger,
    EvidenceCounts,
    EvidenceRef,
    GateSummary,
    LineageResolution,
    canonical_bytes,
    sha256_hex,
)


class AdjudicationError(ValueError):
    """Raised when reviewed evidence cannot be replayed from discovery facts."""


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _revalidate(model_type: type[_ModelT], value: _ModelT, label: str) -> _ModelT:
    try:
        return model_type.model_validate(value.model_dump(mode="json", exclude_none=True))
    except (TypeError, ValueError) as exc:
        raise AdjudicationError(f"invalid {label}: {exc}") from exc


def _ensure_unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise AdjudicationError(f"{label} must be unique")


def _canonical_sequence(values: Sequence[BaseModel]) -> list[bytes]:
    return [canonical_bytes(value) for value in values]


def _validate_evidence_refs(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    commit_messages = {item.commit.commit_oid for item in discovered.discovered_candidates}
    patch_blobs = {item.diff_evidence.patch_sha256 for item in discovered.discovered_candidates}
    lineage_links = {item.link_id for item in discovered.objective_lineage_links}
    source_artifacts = {item.artifact_id for item in evidence.source_artifacts}
    valid_targets = {
        "commit_message": commit_messages,
        "patch_blob": patch_blobs,
        "lineage_link": lineage_links,
        "source_artifact": source_artifacts,
    }

    refs: list[EvidenceRef] = []
    for group in evidence.group_decisions:
        refs.extend(group.root_cause_evidence_refs)
        for case in group.case_decisions:
            refs.extend(case.evidence_refs)
    for disposition in evidence.candidate_decisions:
        refs.extend(disposition.evidence_refs)
    for ref in refs:
        if ref.target_id not in valid_targets[ref.kind]:
            raise AdjudicationError(f"evidence ref does not resolve: {ref.kind}:{ref.target_id}")


def _validate_assignments(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    group_ids = [item.fix_group_id for item in evidence.group_decisions]
    _ensure_unique(group_ids, "fix_group_id values")

    universe = {item.commit.commit_oid for item in discovered.discovered_candidates}
    grouped = [oid for group in evidence.group_decisions for oid in group.commits]
    excluded = [item.commit_oid for item in evidence.candidate_decisions]
    _ensure_unique(grouped, "grouped candidate assignments")
    _ensure_unique(excluded, "candidate decisions")

    assigned = set(grouped) | set(excluded)
    unknown = assigned - universe
    if unknown:
        raise AdjudicationError(
            f"candidate assignment refers to an unknown commit: {sorted(unknown)[0]}"
        )
    overlap = set(grouped) & set(excluded)
    if overlap:
        raise AdjudicationError("grouped and candidate-level decisions assign the same commit")
    candidates = {item.commit.commit_oid: item for item in discovered.discovered_candidates}
    for disposition in evidence.candidate_decisions:
        candidate = candidates[disposition.commit_oid]
        if not candidate.config_only and disposition.reason_code != "non_config_only":
            raise AdjudicationError(
                f"non-config candidate {disposition.commit_oid} requires non_config_only"
            )
        if candidate.config_only and disposition.reason_code == "non_config_only":
            raise AdjudicationError(
                f"config_only candidate {disposition.commit_oid} cannot use non_config_only"
            )
    for group in evidence.group_decisions:
        selected = [candidates[oid] for oid in group.commits]
        for previous, current in zip(selected, selected[1:], strict=False):
            if current.commit.selected_parent_oid != previous.commit.commit_oid:
                raise AdjudicationError(
                    f"group {group.fix_group_id} is not a complete first-parent range"
                )
    if assigned != universe:
        missing = sorted(universe - assigned)
        raise AdjudicationError(
            f"every discovered candidate requires exactly one disposition: {missing[0]}"
        )


def _derive_group(
    decision: CandidateGroupDecision,
    candidates: dict[str, object],
    resolutions: Sequence[LineageResolution],
) -> CandidateFixGroup:
    selected = [candidates[oid] for oid in decision.commits]
    for edge, candidate in zip(decision.selected_parent_edges, selected, strict=True):
        expected_parent = candidate.commit.diff_base_oid
        if edge.parent_oid != expected_parent:
            detail = (
                "selected parent is not the discovered first parent"
                if len(candidate.commit.parent_oids) > 1
                else "selected parent differs from discovery"
            )
            raise AdjudicationError(f"{detail} for commit {candidate.commit.commit_oid}")

    for previous, current in zip(selected, selected[1:], strict=False):
        if current.commit.selected_parent_oid != previous.commit.commit_oid:
            raise AdjudicationError(
                f"group {decision.fix_group_id} is not a complete first-parent range"
            )

    if any(not candidate.config_only for candidate in selected):
        raise AdjudicationError(f"group {decision.fix_group_id} contains a non-config-only commit")

    dispositions = {case.disposition for case in decision.case_decisions}
    disposition_summary = (
        "ambiguous"
        if "ambiguous" in dispositions
        else "proposed"
        if "proposed" in dispositions
        else "rejected"
    )
    return CandidateFixGroup(
        fix_group_id=decision.fix_group_id,
        group_decision_sha256=sha256_hex(canonical_bytes(decision)),
        commits=list(decision.commits),
        before_commit=selected[0].commit.diff_base_oid,
        after_commit=selected[-1].commit.commit_oid,
        after_committed_at=selected[-1].commit.committed_at,
        changed_paths=sorted({path for candidate in selected for path in candidate.changed_paths}),
        config_only=all(candidate.config_only for candidate in selected),
        diff_evidence=[candidate.diff_evidence for candidate in selected],
        cases=list(decision.case_decisions),
        disposition_summary=disposition_summary,
        rationale=decision.rationale,
        lineage_links=[
            resolution.link_id
            for resolution in resolutions
            if decision.fix_group_id in resolution.affected_group_ids
        ],
    )


def derive_applicability_matrix(
    groups: Sequence[CandidateFixGroup],
    declared: Sequence[ApplicabilityDeclaration],
) -> tuple[ApplicabilityRow, ...]:
    """Derive evidence counts while preserving the declared taxonomy order."""

    try:
        validated_groups = [
            CandidateFixGroup.model_validate(group.model_dump(mode="json", exclude_none=True))
            for group in groups
        ]
        declarations = [
            ApplicabilityDeclaration.model_validate(
                declaration.model_dump(mode="json", exclude_none=True)
            )
            for declaration in declared
        ]
    except (TypeError, ValueError) as exc:
        raise AdjudicationError(f"invalid applicability input: {exc}") from exc

    declaration_classes = [item.defect_class for item in declarations]
    if len(declaration_classes) != len(B0A_DEFECT_CLASSES) or set(declaration_classes) != set(
        B0A_DEFECT_CLASSES
    ):
        raise AdjudicationError("applicability declarations must contain the exact B0A matrix")

    cases = [case for group in validated_groups for case in group.cases]
    rows: list[ApplicabilityRow] = []
    for declaration in declarations:
        class_cases = [case for case in cases if case.defect_class == declaration.defect_class]
        if declaration.domain_applicability == "not_applicable" and any(
            case.disposition == "proposed" for case in class_cases
        ):
            raise AdjudicationError(
                f"not_applicable class {declaration.defect_class.value} cannot be proposed"
            )
        rows.append(
            ApplicabilityRow(
                defect_class=declaration.defect_class,
                domain_applicability=declaration.domain_applicability,
                evidence_counts=EvidenceCounts(
                    proposed=sum(case.disposition == "proposed" for case in class_cases),
                    rejected=sum(case.disposition == "rejected" for case in class_cases),
                    ambiguous=sum(case.disposition == "ambiguous" for case in class_cases),
                ),
                implementation_support=declaration.implementation_support,
            )
        )
    return tuple(rows)


def _failure_reasons(proposed_groups: int, proposed_classes: int) -> list[str]:
    reasons: list[str] = []
    if proposed_groups < 8:
        reasons.append("fewer than eight independent proposed groups")
    if proposed_classes < 4:
        reasons.append("fewer than four proposed defect classes")
    return reasons


def evaluate_provisional_gate(
    groups: Sequence[CandidateFixGroup],
    matrix: Sequence[ApplicabilityRow],
    search_round: str,
) -> GateSummary:
    """Evaluate the fixed 8-group/4-class provisional gate."""

    if search_round not in {"initial", "expanded"}:
        raise AdjudicationError(f"unknown search round: {search_round}")
    try:
        validated_groups = [
            CandidateFixGroup.model_validate(group.model_dump(mode="json", exclude_none=True))
            for group in groups
        ]
        validated_matrix = [
            ApplicabilityRow.model_validate(row.model_dump(mode="json", exclude_none=True))
            for row in matrix
        ]
    except (TypeError, ValueError) as exc:
        raise AdjudicationError(f"invalid gate input: {exc}") from exc

    matrix_classes = [row.defect_class for row in validated_matrix]
    if len(matrix_classes) != len(B0A_DEFECT_CLASSES) or set(matrix_classes) != set(
        B0A_DEFECT_CLASSES
    ):
        raise AdjudicationError("gate requires the exact B0A applicability matrix")
    rows = {row.defect_class: row for row in validated_matrix}
    for group in validated_groups:
        for case in group.cases:
            if (
                case.disposition == "proposed"
                and rows[case.defect_class].domain_applicability == "not_applicable"
            ):
                raise AdjudicationError(
                    f"not_applicable class {case.defect_class.value} cannot be proposed"
                )

    # A repeated ID is one reviewed fix, even if supplied more than once.
    unique_groups: dict[str, CandidateFixGroup] = {}
    for group in validated_groups:
        unique_groups.setdefault(group.fix_group_id, group)
    counted = [group for group in unique_groups.values() if group.counts_toward_gate]
    proposed_classes = {
        case.defect_class
        for group in counted
        for case in group.cases
        if case.disposition == "proposed"
        and rows[case.defect_class].domain_applicability == "applicable"
    }
    proposed_group_count = len(counted)
    proposed_class_count = len(proposed_classes)
    passed = proposed_group_count >= 8 and proposed_class_count >= 4
    if passed:
        status = "provisional_pass"
        next_action = "proceed_to_b0b"
    elif search_round == "initial":
        status = "expanded_round_required"
        next_action = "run_expanded_round"
    else:
        status = "insufficient_evidence"
        next_action = "stop_flare_heavy_investment"
    return GateSummary(
        status=status,
        proposed_groups=proposed_group_count,
        proposed_classes=proposed_class_count,
        failure_reasons=(
            [] if passed else _failure_reasons(proposed_group_count, proposed_class_count)
        ),
        next_action=next_action,
    )


def _validate_lineage(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    groups: Sequence[CandidateFixGroup],
) -> None:
    links = {item.link_id: item for item in discovered.objective_lineage_links}
    resolution_ids = [item.link_id for item in evidence.lineage_resolutions]
    _ensure_unique(resolution_ids, "lineage resolution link IDs")
    if set(resolution_ids) != set(links):
        raise AdjudicationError(
            "lineage resolutions must cover every discovered objective link exactly once"
        )

    group_by_id = {group.fix_group_id: group for group in groups}
    group_by_commit = {oid: group for group in groups for oid in group.commits}
    decisions = {item.commit_oid: item for item in evidence.candidate_decisions}
    decision_by_group = {item.fix_group_id: item for item in evidence.group_decisions}
    counted_ids = {group.fix_group_id for group in groups if group.counts_toward_gate}
    same_fix_parent: dict[str, str] = {}

    def find_same_fix(oid: str) -> str:
        same_fix_parent.setdefault(oid, oid)
        root = oid
        while same_fix_parent[root] != root:
            root = same_fix_parent[root]
        while same_fix_parent[oid] != oid:
            parent = same_fix_parent[oid]
            same_fix_parent[oid] = root
            oid = parent
        return root

    def join_same_fix(source_oid: str, target_oid: str) -> None:
        source_root = find_same_fix(source_oid)
        target_root = find_same_fix(target_oid)
        if source_root != target_root:
            same_fix_parent[max(source_root, target_root)] = min(source_root, target_root)

    for resolution in evidence.lineage_resolutions:
        link = links[resolution.link_id]
        if resolution.resolution == "same_group":
            source_node = f"commit:{link.source_oid}"
            target_node = f"commit:{link.target_oid}"
            join_same_fix(source_node, target_node)
            for group_id in resolution.affected_group_ids:
                join_same_fix(source_node, f"group:{group_id}")
        unknown_group_ids = set(resolution.affected_group_ids) - set(group_by_id)
        if unknown_group_ids:
            raise AdjudicationError("lineage resolution refers to an unknown affected group")
        endpoint_group_ids = {
            group_by_commit[oid].fix_group_id
            for oid in (link.source_oid, link.target_oid)
            if oid in group_by_commit
        }
        if not endpoint_group_ids <= set(resolution.affected_group_ids):
            raise AdjudicationError("lineage resolution omits a grouped endpoint")

        counted_endpoint_ids = endpoint_group_ids & counted_ids
        if link.link_type != "patch_id" and resolution.resolution != "same_group":
            raise AdjudicationError(f"{link.link_type} lineage must resolve as the same fix")

        if link.link_type in {"cherry_pick", "backport"}:
            source_group = group_by_commit.get(link.source_oid)
            target_group = group_by_commit.get(link.target_oid)
            continuous_same_group = (
                source_group is not None
                and target_group is not None
                and source_group.fix_group_id == target_group.fix_group_id
            )
            if not continuous_same_group:
                target_decision = decisions.get(link.target_oid)
                if target_decision is None or target_decision.reason_code != "revert_or_duplicate":
                    raise AdjudicationError(
                        f"{link.link_type} non-primary endpoint must be excluded as "
                        "revert_or_duplicate"
                    )
            if len(counted_endpoint_ids) > 1:
                raise AdjudicationError(f"{link.link_type} endpoints are not independent groups")

        if link.link_type == "revert":
            target_group = group_by_commit.get(link.target_oid)
            if target_group is not None:
                raise AdjudicationError(
                    "a revert endpoint must use a revert_or_duplicate candidate disposition"
                )
            target_decision = decisions.get(link.target_oid)
            if target_decision is None or target_decision.reason_code != "revert_or_duplicate":
                raise AdjudicationError("a revert endpoint must be excluded as revert_or_duplicate")

        if resolution.resolution == "same_group" and len(counted_endpoint_ids) > 1:
            raise AdjudicationError("objective lineage cannot inflate independent proposed groups")

        if link.link_type == "patch_id" and resolution.resolution == "separate":
            if len(endpoint_group_ids) != 2:
                raise AdjudicationError("separate patch_id lineage requires two affected groups")
            root_cause_projections = {
                frozenset(
                    canonical_bytes(ref)
                    for ref in decision_by_group[group_id].root_cause_evidence_refs
                )
                for group_id in endpoint_group_ids
            }
            if len(root_cause_projections) != 2:
                raise AdjudicationError(
                    "separate patch_id fixes require distinct root-cause evidence"
                )

    counted_by_component: dict[str, set[str]] = {}
    for group_id in counted_ids:
        group_node = f"group:{group_id}"
        if group_node not in same_fix_parent:
            continue
        root = find_same_fix(group_node)
        counted_by_component.setdefault(root, set()).add(group_id)
    if any(len(group_ids) > 1 for group_ids in counted_by_component.values()):
        raise AdjudicationError("same-fix lineage cannot inflate independent proposed groups")


def _validate_prior_pair(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    prior_ledger: CandidateLedger | None,
    prior_decision: B0ADecision | None,
) -> tuple[CandidateLedger, B0ADecision] | None:
    if discovered.search_round == "initial":
        if prior_ledger is not None or prior_decision is not None:
            raise AdjudicationError("initial adjudication forbids a prior decision pair")
        return None
    if prior_ledger is None or prior_decision is None:
        raise AdjudicationError("expanded adjudication requires the exact prior pair")

    validated_ledger = _revalidate(CandidateLedger, prior_ledger, "prior candidate ledger")
    validated_decision = _revalidate(B0ADecision, prior_decision, "prior B0A decision")

    same_search = (
        validated_ledger.schema_version == discovered.schema_version
        and canonical_bytes(validated_ledger.search_frame)
        == canonical_bytes(discovered.search_frame)
        and validated_ledger.search_spec_sha256 == discovered.search_spec_sha256
        and validated_ledger.search_registration == discovered.search_registration
        and validated_ledger.observed_revision_count == discovered.observed_revision_count
        and validated_ledger.discovery_tool == discovered.discovery_tool
    )
    if not same_search:
        raise AdjudicationError("expanded prior pair is not from the same registered search")
    if validated_ledger.search_round != "initial":
        raise AdjudicationError("expanded prior ledger must be the initial round")
    if (
        validated_decision.candidate_ledger_sha256 != sha256_hex(canonical_bytes(validated_ledger))
        or canonical_bytes(validated_decision.gate)
        != canonical_bytes(validated_ledger.gate_summary)
        or validated_decision.gate.status != "expanded_round_required"
    ):
        raise AdjudicationError("prior decision must bind the initial ledger and require expansion")
    if evidence.prior_candidate_ledger_sha256 != sha256_hex(
        canonical_bytes(validated_ledger)
    ) or evidence.prior_decision_sha256 != sha256_hex(canonical_bytes(validated_decision)):
        raise AdjudicationError("expanded evidence does not bind the exact prior pair")
    return validated_ledger, validated_decision


def _validate_expanded_prefixes(
    evidence: AdjudicationEvidence,
    prior_ledger: CandidateLedger,
) -> None:
    prior_group_ids = [item.fix_group_id for item in prior_ledger.groups]
    replay_group_ids = [item.fix_group_id for item in evidence.group_decisions]
    if (
        len(replay_group_ids) < len(prior_group_ids)
        or replay_group_ids[: len(prior_group_ids)] != prior_group_ids
    ):
        raise AdjudicationError("expanded groups must retain the initial decision ordered prefix")
    for prior_group, replayed in zip(
        prior_ledger.groups,
        evidence.group_decisions[: len(prior_group_ids)],
        strict=True,
    ):
        if prior_group.group_decision_sha256 != sha256_hex(canonical_bytes(replayed)):
            raise AdjudicationError("expanded group differs from its complete initial decision")

    prior_candidates = _canonical_sequence(prior_ledger.candidate_decisions)
    replayed_candidates = _canonical_sequence(evidence.candidate_decisions)
    if (
        len(replayed_candidates) < len(prior_candidates)
        or replayed_candidates[: len(prior_candidates)] != prior_candidates
    ):
        raise AdjudicationError(
            "expanded candidate decisions must retain the initial decision ordered prefix"
        )

    prior_lineage = _canonical_sequence(prior_ledger.lineage_resolutions)
    replayed_lineage = _canonical_sequence(evidence.lineage_resolutions)
    if (
        len(replayed_lineage) < len(prior_lineage)
        or replayed_lineage[: len(prior_lineage)] != prior_lineage
    ):
        raise AdjudicationError("expanded lineage resolutions must retain the ordered prefix")


def _with_reason_counts(
    gate: GateSummary,
    decisions: Iterable[CandidateDisposition],
) -> GateSummary:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.reason_code] = counts.get(decision.reason_code, 0) + 1
    return GateSummary.model_validate(
        {
            **gate.model_dump(mode="json"),
            "reason_code_counts": dict(sorted(counts.items())),
        }
    )


def adjudicate(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    prior_ledger: CandidateLedger | None = None,
    prior_decision: B0ADecision | None = None,
) -> tuple[CandidateLedger, B0ADecision]:
    """Replay reviewed evidence against immutable discovery facts, without Git I/O."""

    discovered = _revalidate(DiscoveryLedger, discovered, "discovery ledger")
    evidence = _revalidate(AdjudicationEvidence, evidence, "adjudication evidence")

    if evidence.search_round != discovered.search_round:
        raise AdjudicationError("evidence search round differs from discovery")
    discovery_hash = sha256_hex(canonical_bytes(discovered))
    if evidence.discovery_ledger_sha256 != discovery_hash:
        raise AdjudicationError("discovery ledger hash does not match evidence")
    if evidence.candidate_universe_sha256 != discovered.candidate_universe_sha256:
        raise AdjudicationError("candidate-universe hash does not match discovery")

    prior_pair = _validate_prior_pair(
        discovered,
        evidence,
        prior_ledger,
        prior_decision,
    )
    if prior_pair is not None:
        _validate_expanded_prefixes(evidence, prior_pair[0])

    _validate_evidence_refs(discovered, evidence)
    _validate_assignments(discovered, evidence)
    candidates = {item.commit.commit_oid: item for item in discovered.discovered_candidates}
    groups = [
        _derive_group(decision, candidates, evidence.lineage_resolutions)
        for decision in evidence.group_decisions
    ]
    _validate_lineage(discovered, evidence, groups)

    matrix = derive_applicability_matrix(
        groups,
        evidence.applicability_declarations,
    )
    gate = _with_reason_counts(
        evaluate_provisional_gate(groups, matrix, discovered.search_round),
        evidence.candidate_decisions,
    )
    adjudicator_ids = sorted(
        {
            *[item.adjudicator_id for item in evidence.group_decisions],
            *[item.adjudicator_id for item in evidence.candidate_decisions],
        }
    )
    reviewer_ids = sorted(
        {
            evidence.review_attestation.reviewer_id,
            *[item.reviewer_id for item in evidence.group_decisions],
            *[item.reviewer_id for item in evidence.candidate_decisions],
        }
    )
    ledger = CandidateLedger(
        search_frame=discovered.search_frame,
        search_spec_sha256=discovered.search_spec_sha256,
        search_registration=discovered.search_registration,
        search_round=discovered.search_round,
        observed_revision_count=discovered.observed_revision_count,
        discovery_tool=discovered.discovery_tool,
        discovery_ledger_sha256=discovery_hash,
        candidate_universe_sha256=discovered.candidate_universe_sha256,
        adjudication_evidence_sha256=sha256_hex(canonical_bytes(evidence)),
        evidence_revision=evidence.evidence_revision,
        prior_candidate_ledger_sha256=evidence.prior_candidate_ledger_sha256,
        prior_decision_sha256=evidence.prior_decision_sha256,
        adjudicator_ids=adjudicator_ids,
        reviewer_ids=reviewer_ids,
        groups=groups,
        candidate_decisions=list(evidence.candidate_decisions),
        applicability_matrix=list(matrix),
        gate_summary=gate,
        lineage_resolutions=list(evidence.lineage_resolutions),
    )

    decision = B0ADecision(
        candidate_ledger_sha256=sha256_hex(canonical_bytes(ledger)),
        gate=ledger.gate_summary,
    )
    return ledger, decision
