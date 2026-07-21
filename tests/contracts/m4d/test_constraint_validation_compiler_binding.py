from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.api import ConstraintValidationCompilerBindingViewV1


def _payload() -> dict[str, object]:
    return {
        "binding_schema_version": "constraint-validation-compiler-binding@1",
        "compiler_profile": {
            "profile_id": "builtin.constraint_compiler",
            "version": 1,
        },
        "profile_payload_hash": "a" * 64,
        "run_kind": {"kind": "constraint_proposal.validate", "version": 1},
        "differential_engines": [
            {"engine_id": "clingo", "version": 1},
            {"engine_id": "graph-reference", "version": 1},
            {"engine_id": "numeric-reference", "version": 1},
            {"engine_id": "z3", "version": 1},
        ],
    }


def test_constraint_validation_compiler_binding_is_exact_and_canonical() -> None:
    value = ConstraintValidationCompilerBindingViewV1.model_validate(_payload())

    assert value.binding_schema_version == "constraint-validation-compiler-binding@1"
    assert value.compiler_profile.profile_id == "builtin.constraint_compiler"
    assert value.run_kind.kind == "constraint_proposal.validate"
    assert tuple(item.engine_id for item in value.differential_engines) == (
        "clingo",
        "graph-reference",
        "numeric-reference",
        "z3",
    )


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        (
            "run_kind",
            {"kind": "checker.run", "version": 1},
            "constraint_proposal.validate@1",
        ),
        (
            "differential_engines",
            [{"engine_id": "clingo", "version": 1}],
            "at least 2",
        ),
        (
            "differential_engines",
            [
                {"engine_id": "z3", "version": 1},
                {"engine_id": "clingo", "version": 1},
            ],
            "canonical",
        ),
        (
            "differential_engines",
            [
                {"engine_id": "clingo", "version": 1},
                {"engine_id": "clingo", "version": 1},
            ],
            "canonical",
        ),
    ],
)
def test_constraint_validation_compiler_binding_rejects_non_exact_values(
    field: str,
    replacement: object,
    message: str,
) -> None:
    payload = _payload()
    payload[field] = replacement

    with pytest.raises(ValidationError, match=message):
        ConstraintValidationCompilerBindingViewV1.model_validate(payload)
