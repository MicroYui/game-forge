from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.api import ReviewProducerBindingViewV1
from gameforge.contracts.execution_profiles import RunKindRef


def _binding(**updates: object) -> ReviewProducerBindingViewV1:
    values: dict[str, object] = {
        "review_artifact_id": "artifact:review",
        "run_id": "run:review",
        "attempt_no": 1,
        "run_kind": RunKindRef(kind="review.run", version=1),
        "terminal_status": "succeeded",
        "terminal_manifest_id": "artifact:result",
        "terminal_manifest_kind": "run_result",
        "outcome_code": "review_completed",
        "outcome_policy_id": "review-completed",
        "outcome_policy_version": 1,
        "outcome_rule_id": "primary",
        "manifest_role": "output",
        "finding_authority": "exact-run-links",
    }
    values.update(updates)
    return ReviewProducerBindingViewV1.model_validate(values)


def test_review_producer_binding_is_an_occurrence_not_a_global_owner() -> None:
    first = _binding(run_id="run:first", terminal_manifest_id="artifact:result:first")
    second = _binding(run_id="run:second", terminal_manifest_id="artifact:result:second")

    assert first.review_artifact_id == second.review_artifact_id
    assert first.run_id != second.run_id


def test_generation_review_binding_keeps_failed_gate_and_embedded_authority_explicit() -> None:
    value = _binding(
        run_id="run:generation",
        run_kind=RunKindRef(kind="generation.propose", version=1),
        terminal_status="failed",
        terminal_manifest_id="artifact:failure",
        terminal_manifest_kind="run_failure",
        outcome_code="generation_gate_rejected",
        outcome_policy_id="generation-gate-rejected",
        outcome_rule_id="review",
        manifest_role="evidence",
        finding_authority="embedded-only",
    )

    assert value.terminal_manifest_kind == "run_failure"
    assert value.finding_authority == "embedded-only"


@pytest.mark.parametrize(
    "updates",
    (
        {"terminal_status": "failed"},
        {"run_kind": RunKindRef(kind="checker.run", version=1)},
        {"manifest_role": "evidence"},
        {"finding_authority": "embedded-only"},
    ),
)
def test_review_producer_binding_rejects_cross_authority_combinations(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _binding(**updates)


def test_empty_report_can_be_explicitly_not_applicable_for_either_supported_run_kind() -> None:
    assert _binding(finding_authority="not-applicable").finding_authority == "not-applicable"
    assert (
        _binding(
            run_kind=RunKindRef(kind="generation.propose", version=1),
            outcome_code="generation_gate_passed",
            outcome_policy_id="generation-gate-pass",
            outcome_rule_id="review",
            manifest_role="evidence",
            finding_authority="not-applicable",
        ).finding_authority
        == "not-applicable"
    )
