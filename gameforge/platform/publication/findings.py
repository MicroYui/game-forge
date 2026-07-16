"""Finding revision compare-and-set + RunFindingLink projection.

Implements the Finding half of the publication contract (M4 design line 1126):
each PreparedFinding's evidence Artifact must match the finding-output policy
allowlist (outcome rule / oracle / source); the publisher assigns an immutable
positive revision via ``(finding_id, expected_previous_revision)`` series-head
CAS, recomputes the digest, and inserts the ``FindingRevisionV1`` plus a
``RunFindingLinkV1`` in the same terminal UoW that publishes the domain Artifact.
"""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import (
    FindingRevisionV1,
    finding_revision_digest,
)
from gameforge.contracts.jobs import (
    FindingOutputPolicyV1,
    PreparedFindingV1,
    RunFindingLinkV1,
)
from gameforge.contracts.lineage import VersionTuple


@dataclass(frozen=True, slots=True)
class PlannedFindingWrite:
    """A resolved finding revision + link ready to persist inside the UoW."""

    revision: FindingRevisionV1
    expected_current_revision: int | None
    link: RunFindingLinkV1


def plan_finding_write(
    *,
    prepared: PreparedFindingV1,
    finding_policy: FindingOutputPolicyV1,
    evidence_rule_id: str,
    evidence_artifact_id: str,
    evidence_version_tuple: VersionTuple,
    run_id: str,
    attempt_no: int,
    ordinal: int,
    occurred_at: str,
) -> PlannedFindingWrite:
    """Validate one PreparedFinding against policy and project its writes."""

    if evidence_rule_id not in finding_policy.allowed_evidence_outcome_rule_ids:
        raise IntegrityViolation(
            "finding evidence rule is outside the finding-output policy allowlist",
            evidence_rule_id=evidence_rule_id,
        )
    payload = prepared.payload
    if payload.oracle_type not in finding_policy.allowed_oracle_types:
        raise IntegrityViolation(
            "finding oracle type is outside the finding-output policy allowlist",
            oracle_type=payload.oracle_type,
        )
    if payload.source not in finding_policy.allowed_sources:
        raise IntegrityViolation(
            "finding source is outside the finding-output policy allowlist",
            source=payload.source,
        )
    if payload.producer_run_id != run_id:
        raise IntegrityViolation(
            "finding producer_run_id differs from the current Run", finding_id=prepared.finding_id
        )
    grounded_snapshots = {
        value
        for value in (
            evidence_version_tuple.ir_snapshot_id,
            evidence_version_tuple.constraint_snapshot_id,
        )
        if value is not None
    }
    if payload.snapshot_id not in grounded_snapshots:
        raise IntegrityViolation(
            "finding snapshot differs from its exact evidence Artifact",
            finding_id=prepared.finding_id,
            snapshot_id=payload.snapshot_id,
        )

    expected = prepared.expected_previous_revision
    if expected is None:
        revision_no = 1
        supersedes = None
    else:
        revision_no = expected + 1
        supersedes = expected

    revision = FindingRevisionV1(
        finding_id=prepared.finding_id,
        revision=revision_no,
        supersedes_revision=supersedes,
        created_at=occurred_at,
        payload=payload,
    )
    link = RunFindingLinkV1(
        run_id=run_id,
        attempt_no=attempt_no,
        ordinal=ordinal,
        finding_id=prepared.finding_id,
        finding_revision=revision_no,
        finding_digest=finding_revision_digest(revision),
        evidence_artifact_id=evidence_artifact_id,
    )
    return PlannedFindingWrite(
        revision=revision,
        expected_current_revision=expected,
        link=link,
    )


__all__ = ["PlannedFindingWrite", "plan_finding_write"]
