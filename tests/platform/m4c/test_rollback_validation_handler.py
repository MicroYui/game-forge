"""Task 13 — ``rollback_validator@1`` (deterministic 6-dimension rollback validation).

Gathers history / artifact / schema / profile / impact / regression dimensions into
ONE ``evidence-set@1`` primary plus one ``regression-evidence@1`` per dimension, and
reports passed / failed / unproven as a run,succeeded BUSINESS result — never a
``RunFailure``. The subject's ``RollbackRequestV1`` is parsed through the injected
inspector; the ``RollbackTargetBindingV1`` is built from the payload + analyzer.
"""

from __future__ import annotations

import json

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.jobs import (
    PreparedRunResult,
    RollbackValidationPayloadV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import EvidenceSet, RollbackRequestV1, RollbackTargetBindingV1
from gameforge.platform.run_handlers.rollback_validation import (
    DimensionCheckV1,
    RollbackTargetInspectionV1,
    RollbackValidationHandler,
)
from gameforge.platform.run_handlers.validation_common import RegressionSuiteResultV1
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    resolved_binding,
)

ROLLBACK_VALIDATE_KIND = RunKindRef(kind="rollback.validate", version=1)
SUBJECT_ID = "artifact:rollback-request"
TARGET_ID = "artifact:rollback-target"
REGRESSION_SUITE_ID = "artifact:regression-suite"
_HEX = "a" * 64
_ROLLBACK_PROFILE = ProfileRefV1(profile_id="rollback", version=1)
_SCHEMA_POLICY = ProfileRefV1(profile_id="schema-compat", version=1)
_IMPACT = ProfileRefV1(profile_id="impact", version=1)
_ROLLBACK_BINDING = resolved_binding(
    "/params/rollback_profile", profile_id="rollback", version=1, kind="rollback"
)
_EXPECTED_REF = RefValue(artifact_id="artifact:current-head", revision=5)


def _subject() -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id=SUBJECT_ID,
        subject_digest=_HEX,
        active_validation_run_id="run:1",
    )


def _rollback_request() -> RollbackRequestV1:
    return RollbackRequestV1(
        ref_name="ref:main",
        expected_current_ref=_EXPECTED_REF,
        target_artifact_id=TARGET_ID,
        target_history_revision=3,
        rollback_profile_binding=_ROLLBACK_BINDING,
        reason="restore the last known-good head",
    )


def _payload(*, impact_profiles=(_IMPACT,), regression=()) -> RollbackValidationPayloadV1:
    return RollbackValidationPayloadV1(
        subject=_subject(),
        ref_name="ref:main",
        expected_current_ref=_EXPECTED_REF,
        target_artifact_id=TARGET_ID,
        target_history_revision=3,
        rollback_profile=_ROLLBACK_PROFILE,
        schema_compatibility_policy=_SCHEMA_POLICY,
        impact_profiles=impact_profiles,
        regression_suite_artifact_ids=regression,
    )


class _FakeHistory:
    def __init__(self, status="passed", reason=None):
        self.status, self.reason = status, reason

    def verify(self, request) -> DimensionCheckV1:
        return DimensionCheckV1(status=self.status, reason_code=self.reason)


class _FakeSchema:
    def __init__(self, status="passed", reason=None):
        self.status, self.reason = status, reason

    def analyze(self, request) -> RollbackTargetInspectionV1:
        return RollbackTargetInspectionV1(
            status=self.status,
            target_artifact_kind="ir_snapshot",
            target_digest=_HEX,
            target_snapshot_id="snap:target",
            reason_code=self.reason,
        )


class _FakeImpact:
    def __init__(self, status="passed", reason=None):
        self.status, self.reason = status, reason

    def analyze(self, request) -> DimensionCheckV1:
        return DimensionCheckV1(status=self.status, reason_code=self.reason)


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            payload={"payload_schema_version": "regression-evidence@1", "status": "failed"},
        )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, {"payload_schema_version": "ir-core@1"})
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    return store


def _handler(
    store: FakeArtifactStore,
    *,
    history=None,
    schema=None,
    impact=None,
    regression_runner=None,
) -> RollbackValidationHandler:
    kwargs = {}
    if regression_runner is not None:
        kwargs["regression_runner"] = regression_runner
    return RollbackValidationHandler(
        blobs=store,
        store=store,
        history_verifier=history or _FakeHistory(),
        schema_analyzer=schema or _FakeSchema(),
        impact_analyzer=impact or _FakeImpact(),
        **kwargs,
    )


def _context(store: FakeArtifactStore, payload: RollbackValidationPayloadV1):
    return build_context(
        params=payload,
        kind=ROLLBACK_VALIDATE_KIND,
        resolved_profiles=(_ROLLBACK_BINDING,),
        seed=9,
    )


def _evidence_set(store: FakeArtifactStore, outcome: PreparedRunResult) -> EvidenceSet:
    primary = outcome.artifacts[outcome.primary_index]
    return EvidenceSet.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def test_all_dimensions_pass_is_a_successful_business_result() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload(regression=(REGRESSION_SUITE_ID,))))

    assert isinstance(outcome, PreparedRunResult)  # NOT a RunFailure
    assert outcome.summary.outcome_code == "rollback_validation_passed"
    evidence = _evidence_set(store, outcome)
    assert evidence.overall_status == "passed"
    assert isinstance(evidence.target_binding, RollbackTargetBindingV1)
    assert evidence.target_binding.target_artifact_id == TARGET_ID
    assert evidence.target_binding.expected_ref == _EXPECTED_REF
    # history/artifact/schema/profile + 1 impact + 1 regression = 6 dimensions.
    kinds = sorted({req.kind for req in evidence.requirements})
    assert kinds == ["artifact", "history", "impact", "profile", "regression", "schema"]
    regression = [a for a in outcome.artifacts if a.kind == "regression_evidence"]
    assert len(regression) == 6


def test_schema_incompatibility_fails_as_business_result() -> None:
    store = _store()
    outcome = _handler(store, schema=_FakeSchema(status="failed", reason="schema_incompatible"))(
        _context(store, _payload())
    )
    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "rollback_validation_failed"
    evidence = _evidence_set(store, outcome)
    assert evidence.overall_status == "failed"
    assert any(req.kind == "schema" and req.status == "failed" for req in evidence.requirements)


def test_impact_unproven_is_unproven_business_result() -> None:
    store = _store()
    outcome = _handler(
        store, impact=_FakeImpact(status="unproven", reason="impact_budget_exhausted")
    )(_context(store, _payload()))
    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "rollback_validation_unproven"
    evidence = _evidence_set(store, outcome)
    assert evidence.overall_status == "unproven"
    impact_req = next(req for req in evidence.requirements if req.kind == "impact")
    assert impact_req.status == "unproven"
    assert impact_req.reason_code == "impact_budget_exhausted"


def test_subject_payload_mismatch_fails_profile_dimension() -> None:
    store = FakeArtifactStore()
    # subject request references a DIFFERENT target than the payload -> profile mismatch.
    request = _rollback_request().model_copy(update={"target_artifact_id": "artifact:other"})
    store.register(SUBJECT_ID, request.model_dump(mode="json"))
    store.register(TARGET_ID, {"payload_schema_version": "ir-core@1"})
    outcome = _handler(store)(_context(store, _payload()))
    assert outcome.summary.outcome_code == "rollback_validation_failed"
    evidence = _evidence_set(store, outcome)
    profile_req = next(req for req in evidence.requirements if req.kind == "profile")
    assert profile_req.status == "failed"


def test_failing_regression_fails_business_result() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_FailingRegressionRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    assert outcome.summary.outcome_code == "rollback_validation_failed"


def test_rollback_validation_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a)(_context(store_a, _payload(regression=(REGRESSION_SUITE_ID,))))
    out_b = _handler(store_b)(_context(store_b, _payload(regression=(REGRESSION_SUITE_ID,))))
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
