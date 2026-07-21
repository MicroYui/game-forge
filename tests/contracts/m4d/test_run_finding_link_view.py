from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.api import RunFindingLinkViewV1
from gameforge.contracts.findings import (
    FindingPayloadV1,
    FindingRevisionV1,
    finding_revision_digest,
)


def _finding(*, producer_run_id: str = "run:review:1") -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id="finding:quest-cycle",
        revision=2,
        supersedes_revision=1,
        created_at="2026-07-20T08:00:00Z",
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker:quest-graph",
            producer_run_id=producer_run_id,
            oracle_type="deterministic",
            defect_class="quest_cycle",
            severity="major",
            snapshot_id="snapshot:preview:7",
            evidence={"cycle": ["quest:a", "quest:b", "quest:a"]},
            minimal_repro={"entity_id": "quest:a"},
            status="confirmed",
            message="Quest dependency cycle detected.",
        ),
    )


def _payload() -> dict[str, object]:
    finding = _finding()
    return {
        "view_schema_version": "run-finding-link-view@1",
        "run_id": "run:review:1",
        "attempt_no": 1,
        "ordinal": 3,
        "finding": finding.model_dump(mode="json"),
        "finding_digest": finding_revision_digest(finding),
        "evidence_artifact_id": "artifact:checker-run:quest-cycle",
    }


def test_run_finding_link_view_binds_exact_revision_digest_and_evidence() -> None:
    view = RunFindingLinkViewV1.model_validate(_payload())

    assert view.view_schema_version == "run-finding-link-view@1"
    assert view.run_id == view.finding.payload.producer_run_id
    assert view.finding.finding_id == "finding:quest-cycle"
    assert view.finding.revision == 2
    assert view.finding_digest == finding_revision_digest(view.finding)
    assert view.evidence_artifact_id == "artifact:checker-run:quest-cycle"


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("finding_digest", "0" * 64, "digest"),
        (
            "finding",
            _finding(producer_run_id="run:another").model_dump(mode="json"),
            "producer Run",
        ),
    ),
)
def test_run_finding_link_view_rejects_cross_authority_bindings(
    field: str,
    replacement: object,
    message: str,
) -> None:
    payload = _payload()
    payload[field] = replacement

    with pytest.raises(ValidationError, match=message):
        RunFindingLinkViewV1.model_validate(payload)
