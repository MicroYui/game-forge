from __future__ import annotations

import json

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.execution_profiles import (
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.jobs import (
    FailureClassifierRefV1,
    MAX_COLLECTION_ITEMS,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunFailureV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunResultSummaryV1,
    RunResultV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.routing import MAX_ROUTING_FALLBACKS
from gameforge.platform.publication.payload_schema import validate_artifact_payload


_HASH = "a" * 64
_MAX_REPAIR_SEARCH_STEPS = 1_000
_MAX_RETRY_ATTEMPTS = 3


def _run_result(parent_count: int) -> RunResultV1:
    artifact_ids = tuple(f"artifact:{index}" for index in range(parent_count))
    projection = RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="patch.repair", version=1),
        run_payload_hash=_HASH,
        frozen_input_version_tuple=VersionTuple(ir_snapshot_id="snapshot:base"),
        terminal_version_tuple=VersionTuple(ir_snapshot_id="snapshot:base"),
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest=_HASH,
        ),
        parents=tuple(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role="intermediate",
                publication="run_published",
                attempt_no=1,
                ordinal=index + 1,
            )
            for index, artifact_id in enumerate(artifact_ids)
        ),
    )
    return RunResultV1(
        run_id="run:repair-capacity",
        attempt_no=1,
        run_kind=projection.run_kind,
        primary_artifact_id=artifact_ids[0],
        produced_artifact_ids=artifact_ids,
        finding_count=0,
        outcome_code="repair_verified",
        summary=RunResultSummaryV1(
            outcome_code="repair_verified",
            primary_artifact_kind="patch",
            produced_artifact_count=parent_count,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )


def test_task11_manifest_capacity_covers_the_full_repair_contract() -> None:
    routes_per_call = 1 + MAX_ROUTING_FALLBACKS
    per_call_runtime_parents = 1 + routes_per_call + 1  # context + prompts + shard
    runtime_parents = _MAX_RETRY_ATTEMPTS * _MAX_REPAIR_SEARCH_STEPS * per_call_runtime_parents
    maximum_run_manifest_parents = (
        runtime_parents
        + MAX_COLLECTION_ITEMS  # frozen Run inputs
        + MAX_COLLECTION_ITEMS  # prepared domain outputs/evidence
        + _MAX_RETRY_ATTEMPTS  # closed-attempt failure manifests
        + 1  # aggregate run cassette bundle
    )

    assert maximum_run_manifest_parents == 20_052
    assert maximum_run_manifest_parents < MAX_RUN_MANIFEST_PARENT_BINDINGS


def test_run_result_payload_accepts_a_manifest_larger_than_generic_json_array_bound() -> None:
    result = _run_result(16_385)

    retained = validate_artifact_payload(
        payload_schema_id="run-result@1",
        payload=json.loads(canonical_json(result.model_dump(mode="json"))),
    )

    assert len(retained["produced_artifact_ids"]) == 16_385
    assert len(retained["version_projection"]["parents"]) == 16_385


def test_run_failure_payload_accepts_a_manifest_larger_than_generic_json_array_bound() -> None:
    projection = _run_result(16_385).version_projection
    evidence_ids = tuple(parent.artifact_id for parent in projection.parents)
    decision = RetryDecisionV1(
        cause_code="execution_failed",
        failure_class="execution",
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
        classifier=FailureClassifierRefV1(classifier_version=1, classifier_digest=_HASH),
        retry_policy=RetryPolicyRefV1(
            retry_policy_id="llm_transient",
            retry_policy_version=1,
            retry_policy_digest=_HASH,
        ),
        evaluated_at_utc="2026-07-17T00:00:00Z",
    )
    failure = RunFailureV1(
        run_id="run:repair-capacity",
        attempt_no=1,
        run_kind=projection.run_kind,
        cause_code=decision.cause_code,
        failure_class=decision.failure_class,
        retryable=False,
        retry_decision=decision,
        redacted_message="execution failed",
        evidence_artifact_ids=evidence_ids,
        requirement_dispositions=(),
        occurred_at="2026-07-17T00:00:00Z",
        version_projection=projection,
    )

    retained = validate_artifact_payload(
        payload_schema_id="run-failure@1",
        payload=json.loads(canonical_json(failure.model_dump(mode="json"))),
    )

    assert len(retained["evidence_artifact_ids"]) == 16_385
    assert len(retained["version_projection"]["parents"]) == 16_385
