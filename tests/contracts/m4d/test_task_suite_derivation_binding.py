from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.api import TaskSuiteDerivationBindingViewV1
from gameforge.contracts.execution_profiles import (
    MAX_PREPARED_OUTCOME_BYTES_V1,
    MAX_TASK_SUITE_SCENARIOS_V1,
)


def _payload() -> dict[str, object]:
    return {
        "binding_schema_version": "task-suite-derivation-binding@1",
        "derivation_profile": {
            "profile_id": "builtin.task_suite_derivation",
            "version": 2,
        },
        "profile_payload_hash": "a" * 64,
        "run_kind": {"kind": "task_suite.derive", "version": 1},
        "target_environment_profile": {
            "profile_id": "builtin.environment",
            "version": 1,
        },
        "completion_oracle_registry_ref": {
            "registry_version": 1,
            "digest": "b" * 64,
        },
        "max_scenarios": MAX_TASK_SUITE_SCENARIOS_V1,
        "max_total_prepared_artifact_bytes": MAX_PREPARED_OUTCOME_BYTES_V1,
    }


def test_task_suite_derivation_binding_exposes_the_complete_profile_authority() -> None:
    value = TaskSuiteDerivationBindingViewV1.model_validate(_payload())

    assert value.binding_schema_version == "task-suite-derivation-binding@1"
    assert value.derivation_profile.profile_id == "builtin.task_suite_derivation"
    assert value.run_kind.kind == "task_suite.derive"
    assert value.target_environment_profile.profile_id == "builtin.environment"
    assert value.completion_oracle_registry_ref.registry_version == 1
    assert value.max_scenarios == MAX_TASK_SUITE_SCENARIOS_V1
    assert value.max_total_prepared_artifact_bytes == MAX_PREPARED_OUTCOME_BYTES_V1


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "run_kind",
            {"kind": "playtest.run", "version": 1},
            "task_suite.derive@1",
        ),
        ("max_scenarios", 0, "greater than or equal to 1"),
        (
            "max_scenarios",
            MAX_TASK_SUITE_SCENARIOS_V1 + 1,
            "less than or equal to",
        ),
        ("max_total_prepared_artifact_bytes", 0, "greater than or equal to 1"),
        (
            "max_total_prepared_artifact_bytes",
            MAX_PREPARED_OUTCOME_BYTES_V1 + 1,
            "less than or equal to",
        ),
    ],
)
def test_task_suite_derivation_binding_rejects_non_exact_or_unbounded_values(
    field: str,
    replacement: object,
    message: str,
) -> None:
    payload = _payload()
    payload[field] = replacement

    with pytest.raises(ValidationError, match=message):
        TaskSuiteDerivationBindingViewV1.model_validate(payload)
