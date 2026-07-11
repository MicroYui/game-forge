"""Pure offline adjudication for source-neutral external-corpus evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from gameforge.bench.external_corpus.contracts import (
    AdjudicationEvidence,
    ApplicabilityRow,
    B0AProtocol,
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
    ReviewPackage,
    ReviewPackageRow,
    canonical_bytes,
    sha256_hex,
)


class AdjudicationError(ValueError):
    """Raised when reviewed evidence cannot be replayed from discovery facts."""


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _revalidate(model_type: type[_ModelT], value: BaseModel, label: str) -> _ModelT:
    try:
        return model_type.model_validate(value.model_dump(mode="json", exclude_none=True))
    except (TypeError, ValueError) as exc:
        if model_type is AdjudicationEvidence:
            raise AdjudicationError(
                f"invalid {label}; a valid human review attestation is required: {exc}"
            ) from exc
        raise AdjudicationError(f"invalid {label}: {exc}") from exc


def _ensure_unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise AdjudicationError(f"{label} must be unique")


def _validate_evidence_refs(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    candidates = {item.commit.commit_oid: item for item in discovered.discovered_candidates}
    lineage_links = {item.link_id: item for item in discovered.objective_lineage_links}
    valid_targets = {
        "commit_message": set(candidates),
        "patch_blob": {
            item.diff_evidence.patch_sha256 for item in discovered.discovered_candidates
        },
        "lineage_link": set(lineage_links),
        "source_artifact": {artifact.artifact_id for artifact in evidence.source_artifacts},
    }

    def validate_resolved(ref: EvidenceRef) -> None:
        if ref.target_id not in valid_targets[ref.kind]:
            raise AdjudicationError(f"evidence ref does not resolve: {ref.kind}:{ref.target_id}")

    def belongs_to(ref: EvidenceRef, owner_commits: set[str]) -> bool:
        if ref.kind == "commit_message":
            return ref.target_id in owner_commits
        if ref.kind == "patch_blob":
            return any(
                candidates[commit_oid].diff_evidence.patch_sha256 == ref.target_id
                for commit_oid in owner_commits
            )
        if ref.kind == "lineage_link":
            link = lineage_links[ref.target_id]
            return not owner_commits.isdisjoint({link.source_oid, link.target_oid})
        return ref.kind == "source_artifact"

    for disposition in evidence.candidate_decisions:
        owner_commits = {disposition.commit_oid}
        for ref in disposition.evidence_refs:
            validate_resolved(ref)
            if not belongs_to(ref, owner_commits):
                raise AdjudicationError(
                    "candidate evidence ref does not belong to its candidate: "
                    f"{ref.kind}:{ref.target_id}"
                )

    for group in evidence.group_decisions:
        owner_commits = set(group.commits)
        for ref in group.root_cause_evidence_refs:
            validate_resolved(ref)
            if not belongs_to(ref, owner_commits):
                raise AdjudicationError(
                    f"group evidence ref does not belong to its commits: {ref.kind}:{ref.target_id}"
                )
        for case in group.case_decisions:
            for ref in case.evidence_refs:
                validate_resolved(ref)
                if not belongs_to(ref, owner_commits):
                    raise AdjudicationError(
                        "case evidence ref does not belong to its fix group: "
                        f"{ref.kind}:{ref.target_id}"
                    )


def _validate_assignments(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    universe = {item.commit.commit_oid for item in discovered.discovered_candidates}
    candidates = {item.commit.commit_oid: item for item in discovered.discovered_candidates}
    grouped = [oid for group in evidence.group_decisions for oid in group.commits]
    decided = [item.commit_oid for item in evidence.candidate_decisions]
    _ensure_unique(grouped, "grouped candidate assignments")
    _ensure_unique(decided, "candidate decisions")
    assigned = set(grouped) | set(decided)
    unknown = assigned - universe
    if unknown:
        raise AdjudicationError(
            f"candidate assignment refers to an unknown commit: {sorted(unknown)[0]}"
        )
    if set(grouped) & set(decided):
        raise AdjudicationError("grouped and candidate-level decisions assign the same commit")
    if assigned != universe:
        missing = sorted(universe - assigned)
        raise AdjudicationError(
            f"every discovered candidate requires exactly one assignment: {missing[0]}"
        )

    for disposition in evidence.candidate_decisions:
        candidate = candidates[disposition.commit_oid]
        if not candidate.config_only and disposition.reason_code != "non_config_only":
            raise AdjudicationError(
                f"non-config candidate {disposition.commit_oid} requires non_config_only"
            )
        if candidate.config_only and disposition.reason_code == "non_config_only":
            raise AdjudicationError(
                f"config-only candidate {disposition.commit_oid} cannot use non_config_only"
            )
    for group in evidence.group_decisions:
        selected = [candidates[oid] for oid in group.commits]
        if any(not candidate.config_only for candidate in selected):
            raise AdjudicationError(f"group {group.fix_group_id} contains a non-config-only commit")
        for previous, current in zip(selected, selected[1:], strict=False):
            if current.commit.selected_parent_oid != previous.commit.commit_oid:
                raise AdjudicationError(
                    f"group {group.fix_group_id} is not a complete first-parent range"
                )


def _derive_group(
    decision: CandidateGroupDecision,
    candidates: dict[str, Any],
    resolutions: Sequence[LineageResolution],
) -> CandidateFixGroup:
    selected = [candidates[oid] for oid in decision.commits]
    for edge, candidate in zip(decision.selected_parent_edges, selected, strict=True):
        if edge.parent_oid != candidate.commit.diff_base_oid:
            raise AdjudicationError(
                f"selected parent differs from discovery for commit {candidate.commit.commit_oid}"
            )
    dispositions = {case.disposition for case in decision.case_decisions}
    summary = (
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
        disposition_summary=summary,
        rationale=decision.rationale,
        lineage_links=sorted(
            resolution.link_id
            for resolution in resolutions
            if decision.fix_group_id in resolution.affected_group_ids
        ),
        counts_toward_gate=(
            all(candidate.config_only for candidate in selected) and "proposed" in dispositions
        ),
    )


def derive_applicability_matrix(
    groups: Sequence[CandidateFixGroup],
    discovered: DiscoveryLedger,
) -> tuple[ApplicabilityRow, ...]:
    cases = [case for group in groups for case in group.cases]
    rows: list[ApplicabilityRow] = []
    for profile_row in discovered.source_profile.taxonomy_applicability:
        class_cases = [case for case in cases if case.defect_class == profile_row.defect_class]
        if profile_row.domain_applicability == "not_applicable" and any(
            case.disposition == "proposed" for case in class_cases
        ):
            raise AdjudicationError(
                f"not_applicable class {profile_row.defect_class.value} cannot be proposed"
            )
        rows.append(
            ApplicabilityRow(
                defect_class=profile_row.defect_class,
                domain_applicability=profile_row.domain_applicability,
                implementation_support=profile_row.implementation_support,
                evidence_counts=EvidenceCounts(
                    proposed=sum(case.disposition == "proposed" for case in class_cases),
                    rejected=sum(case.disposition == "rejected" for case in class_cases),
                    ambiguous=sum(case.disposition == "ambiguous" for case in class_cases),
                ),
            )
        )
    return tuple(rows)


def count_supply(
    groups: Sequence[Any],
    matrix: Sequence[Any],
) -> tuple[int, int]:
    """Count independent proposed groups and applicable proposed classes."""

    unique_groups: dict[str, Any] = {}
    for group in groups:
        unique_groups.setdefault(group.fix_group_id, group)
    counted = [group for group in unique_groups.values() if group.counts_toward_gate]
    applicability = {row.defect_class: row.domain_applicability for row in matrix}
    proposed_classes = {
        case.defect_class
        for group in counted
        for case in group.cases
        if case.disposition == "proposed" and applicability.get(case.defect_class) == "applicable"
    }
    return len(counted), len(proposed_classes)


def evaluate_supply_gate(
    groups: Sequence[CandidateFixGroup],
    matrix: Sequence[ApplicabilityRow],
    protocol: B0AProtocol,
) -> GateSummary:
    proposed_groups, proposed_classes = count_supply(groups, matrix)
    passed = (
        proposed_groups >= protocol.minimum_independent_groups
        and proposed_classes >= protocol.minimum_domain_applicable_classes
    )
    failure_reasons: list[str] = []
    if proposed_groups < protocol.minimum_independent_groups:
        failure_reasons.append(
            f"fewer than {protocol.minimum_independent_groups} independent proposed groups"
        )
    if proposed_classes < protocol.minimum_domain_applicable_classes:
        failure_reasons.append(
            f"fewer than {protocol.minimum_domain_applicable_classes} proposed defect classes"
        )
    return GateSummary(
        status="pass" if passed else "insufficient_evidence",
        independent_proposed_groups=proposed_groups,
        domain_applicable_proposed_classes=proposed_classes,
        required_groups=protocol.minimum_independent_groups,
        required_classes=protocol.minimum_domain_applicable_classes,
        failure_reasons=[] if passed else failure_reasons,
        next_action=("proceed_to_b0b" if passed else "stop_source_and_use_fallback"),
    )


def _validate_lineage(
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    groups: Sequence[CandidateFixGroup],
) -> None:
    links = {link.link_id: link for link in discovered.objective_lineage_links}
    resolution_ids = [resolution.link_id for resolution in evidence.lineage_resolutions]
    expected_ids = [link.link_id for link in discovered.objective_lineage_links]
    if resolution_ids != expected_ids:
        raise AdjudicationError(
            "lineage resolutions must cover every objective link in stable order"
        )

    group_by_commit = {oid: group for group in groups for oid in group.commits}
    decisions = {decision.commit_oid: decision for decision in evidence.candidate_decisions}
    candidate_by_oid = {
        candidate.commit.commit_oid: candidate for candidate in discovered.discovered_candidates
    }
    parent: dict[str, str] = {}

    def find(oid: str) -> str:
        parent.setdefault(oid, oid)
        if parent[oid] != oid:
            parent[oid] = find(parent[oid])
        return parent[oid]

    def join(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for resolution in evidence.lineage_resolutions:
        link = links[resolution.link_id]
        endpoint_group_ids = sorted(
            {
                group_by_commit[oid].fix_group_id
                for oid in (link.source_oid, link.target_oid)
                if oid in group_by_commit
            }
        )
        if resolution.affected_group_ids != endpoint_group_ids:
            raise AdjudicationError(
                "lineage resolution affected_group_ids must equal endpoint groups"
            )
        if link.link_type in {"patch_id", "cherry_pick", "backport"}:
            join(link.source_oid, link.target_oid)
        if link.link_type in {"cherry_pick", "backport", "revert"} and (
            resolution.resolution != "same_group"
        ):
            raise AdjudicationError(f"{link.link_type} lineage must resolve as the same fix")
        if link.link_type in {"cherry_pick", "backport"}:
            source_group = group_by_commit.get(link.source_oid)
            target_group = group_by_commit.get(link.target_oid)
            if (
                source_group is not None
                and target_group is not None
                and source_group.fix_group_id != target_group.fix_group_id
            ):
                raise AdjudicationError(f"{link.link_type} endpoints cannot be separate fix groups")
        if link.link_type == "revert":
            if link.target_oid in group_by_commit:
                raise AdjudicationError("a revert endpoint cannot enter a fix group")
            target = candidate_by_oid.get(link.target_oid)
            if target is not None:
                decision = decisions.get(link.target_oid)
                expected_reason = "revert" if target.config_only else "non_config_only"
                if decision is None or decision.reason_code != expected_reason:
                    raise AdjudicationError(
                        f"a revert endpoint must be excluded as {expected_reason}"
                    )
        if resolution.resolution == "same_group" and len(endpoint_group_ids) > 1:
            raise AdjudicationError(
                "same_group lineage endpoints must belong to at most one fix group"
            )

    components: dict[str, set[str]] = {}
    for oid in parent:
        components.setdefault(find(oid), set()).add(oid)
    for component in components.values():
        counted_groups = {
            group_by_commit[oid].fix_group_id
            for oid in component
            if oid in group_by_commit and group_by_commit[oid].counts_toward_gate
        }
        if len(counted_groups) > 1:
            raise AdjudicationError(
                "lineage siblings cannot both count as independent proposed groups"
            )
        for oid in component:
            if oid in group_by_commit:
                continue
            candidate = candidate_by_oid.get(oid)
            if candidate is None:
                continue
            decision = decisions.get(oid)
            expected_reason = "duplicate_lineage" if candidate.config_only else "non_config_only"
            if decision is None or decision.reason_code != expected_reason:
                raise AdjudicationError(
                    f"non-representative lineage endpoint must be excluded as {expected_reason}"
                )


def _with_reason_counts(
    gate: GateSummary,
    decisions: Iterable[CandidateDisposition],
) -> GateSummary:
    counts = dict(sorted(Counter(item.reason_code for item in decisions).items()))
    return GateSummary.model_validate(
        {**gate.model_dump(mode="json"), "reason_code_counts": counts}
    )


def adjudicate(
    discovered: DiscoveryLedger,
    evidence: BaseModel,
) -> tuple[CandidateLedger, B0ADecision]:
    """Replay reviewed evidence against immutable discovery facts without I/O."""

    discovered = _revalidate(DiscoveryLedger, discovered, "discovery ledger")
    evidence = _revalidate(AdjudicationEvidence, evidence, "adjudication evidence")
    if evidence.source_id != discovered.source_id:
        raise AdjudicationError("evidence source_id differs from discovery")
    discovery_hash = sha256_hex(canonical_bytes(discovered))
    if evidence.discovery_ledger_sha256 != discovery_hash:
        raise AdjudicationError("discovery ledger hash does not match evidence")
    if evidence.candidate_universe_sha256 != discovered.candidate_universe_sha256:
        raise AdjudicationError("candidate-universe hash does not match discovery")

    _validate_assignments(discovered, evidence)
    _validate_evidence_refs(discovered, evidence)
    candidates = {item.commit.commit_oid: item for item in discovered.discovered_candidates}
    groups = [
        _derive_group(decision, candidates, evidence.lineage_resolutions)
        for decision in evidence.group_decisions
    ]
    _validate_lineage(discovered, evidence, groups)
    matrix = derive_applicability_matrix(groups, discovered)
    gate = _with_reason_counts(
        evaluate_supply_gate(
            groups,
            matrix,
            discovered.source_profile.b0a_protocol,
        ),
        evidence.candidate_decisions,
    )
    adjudicator_ids = sorted(
        {
            *(group.adjudicator_id for group in evidence.group_decisions),
            *(item.adjudicator_id for item in evidence.candidate_decisions),
        }
    )
    ledger = CandidateLedger(
        source_id=discovered.source_id,
        source_profile=discovered.source_profile,
        source_profile_sha256=discovered.source_profile_sha256,
        search_registration=discovered.search_registration,
        discovery_ledger_sha256=discovery_hash,
        candidate_universe_sha256=discovered.candidate_universe_sha256,
        adjudication_evidence_sha256=sha256_hex(canonical_bytes(evidence)),
        evidence_revision=evidence.evidence_revision,
        adjudicator_ids=adjudicator_ids,
        reviewer_ids=[evidence.review_attestation.reviewer_id],
        groups=groups,
        candidate_decisions=list(evidence.candidate_decisions),
        applicability_matrix=list(matrix),
        gate_summary=gate,
        lineage_resolutions=list(evidence.lineage_resolutions),
    )
    decision = B0ADecision(
        source_id=discovered.source_id,
        candidate_ledger_sha256=sha256_hex(canonical_bytes(ledger)),
        gate=ledger.gate_summary,
    )
    return ledger, decision


def build_review_package(discovered: DiscoveryLedger) -> ReviewPackage:
    """Build complete assignment rows without review or disposition fields."""

    discovered = _revalidate(DiscoveryLedger, discovered, "discovery ledger")
    links_by_oid: dict[str, list[str]] = {
        candidate.commit.commit_oid: [] for candidate in discovered.discovered_candidates
    }
    for link in discovered.objective_lineage_links:
        if link.source_oid in links_by_oid:
            links_by_oid[link.source_oid].append(link.link_id)
        if link.target_oid in links_by_oid:
            links_by_oid[link.target_oid].append(link.link_id)
    return ReviewPackage(
        source_id=discovered.source_id,
        candidate_universe_sha256=discovered.candidate_universe_sha256,
        discovery_ledger_sha256=sha256_hex(canonical_bytes(discovered)),
        review_status="awaiting_human",
        rows=[
            ReviewPackageRow(
                commit=candidate.commit,
                full_message=candidate.diff_evidence.commit_message,
                changed_paths=candidate.changed_paths,
                config_only=candidate.config_only,
                patch_sha256=candidate.diff_evidence.patch_sha256,
                lineage_links=sorted(set(links_by_oid[candidate.commit.commit_oid])),
            )
            for candidate in discovered.discovered_candidates
        ],
    )
