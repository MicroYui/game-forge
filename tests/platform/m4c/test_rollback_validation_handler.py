"""Task 13 — ``rollback_validator@1`` (deterministic 6-dimension rollback validation).

Gathers history / artifact / schema / profile / impact / regression dimensions into
ONE ``evidence-set@1`` primary plus one ``regression-evidence@1`` per dimension, and
reports passed / failed / unproven as a run,succeeded BUSINESS result — never a
``RunFailure``. The subject's ``RollbackRequestV1`` is parsed through the injected
inspector; the ``RollbackTargetBindingV1`` is built from the payload + analyzer.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.execution_profiles import (
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    ProfileRefV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    PreparedRunResult,
    RollbackValidationPayloadV1,
    ValidationSubjectBindingV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import EvidenceSet, RollbackRequestV1, RollbackTargetBindingV1
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.platform.run_handlers.rollback_validation import (
    DimensionCheckV1,
    RollbackImpactRequest,
    RollbackSchemaRequest,
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
SECOND_REGRESSION_SUITE_ID = "artifact:regression-suite:2"
_HEX = "a" * 64
_ROLLBACK_PROFILE = ProfileRefV1(profile_id="rollback", version=1)
_SCHEMA_POLICY = ProfileRefV1(profile_id="schema-compat", version=1)
_IMPACT = ProfileRefV1(profile_id="impact", version=1)
_ROLLBACK_BINDING = resolved_binding(
    "/params/rollback_profile", profile_id="rollback", version=1, kind="rollback"
)
_SCHEMA_BINDING = resolved_binding(
    "/params/schema_compatibility_policy",
    profile_id="schema-compat",
    version=1,
    kind="schema_compatibility",
)
_IMPACT_BINDING = resolved_binding(
    "/params/impact_profiles/0",
    profile_id="impact",
    version=1,
    kind="impact_analysis",
)
_EXPECTED_REF = RefValue(artifact_id="artifact:current-head", revision=5)
_TARGET_SNAPSHOT = Snapshot({}, {})


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
            target_snapshot_id=_TARGET_SNAPSHOT.snapshot_id,
            target_version_tuple=VersionTuple(
                ir_snapshot_id=_TARGET_SNAPSHOT.snapshot_id,
                tool_version="fixture@1",
            ),
            reason_code=self.reason,
        )


class _FakeImpact:
    def __init__(self, status="passed", reason=None):
        self.status, self.reason = status, reason

    def analyze(self, request) -> DimensionCheckV1:
        return DimensionCheckV1(
            status=self.status,
            reason_code=self.reason,
        )


class _PassingRegressionRunner:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request) -> RegressionSuiteResultV1:
        self.requests.append(request)
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="passed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "passed",
                "reason_code": None,
            },
            action_work_units=1,
        )


class _FailingRegressionRunner:
    def run(self, request) -> RegressionSuiteResultV1:
        finding = Finding(
            id="regression:rollback-mismatch",
            source="playtest",
            producer_id="agent-env-action-replay@1",
            producer_run_id="regression-runner",
            oracle_type="deterministic",
            defect_class="rollback_regression_mismatch",
            severity="major",
            snapshot_id=request.snapshot_id,
            status="confirmed",
            message="rollback target violates the committed regression expectation",
        )
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="failed",
            env_contract_version="suite-env@1",
            payload={
                "payload_schema_version": "regression-evidence@1",
                "status": "failed",
                "findings": [finding.model_dump(mode="json")],
            },
            action_work_units=1,
        )


class _UnprovenRegressionRunner:
    reason = "adapter_environment_unavailable"

    def run(self, request) -> RegressionSuiteResultV1:
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status="unproven",
            reason_code=self.reason,
            payload={
                "payload_schema_version": "regression-evidence@1",
                "suite_artifact_id": request.suite_artifact_id,
                "snapshot_id": request.snapshot_id,
                "status": "unproven",
            },
        )


def _store() -> FakeArtifactStore:
    store = FakeArtifactStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)
    store.register(REGRESSION_SUITE_ID, {"suite": "s"})
    store.register(SECOND_REGRESSION_SUITE_ID, {"suite": "s2"})
    return store


def _handler(
    store: FakeArtifactStore,
    *,
    history=None,
    schema=None,
    impact=None,
    regression_runner=None,
    profile_binding_validator=None,
) -> RollbackValidationHandler:
    kwargs = {}
    if regression_runner is not None:
        kwargs["regression_runner"] = regression_runner
    if profile_binding_validator is not None:
        kwargs["profile_binding_validator"] = profile_binding_validator
    return RollbackValidationHandler(
        blobs=store,
        store=store,
        history_verifier=history or _FakeHistory(),
        schema_analyzer=schema or _FakeSchema(),
        impact_analyzer=impact or _FakeImpact(),
        **kwargs,
    )


def _context(
    store: FakeArtifactStore,
    payload: RollbackValidationPayloadV1,
    *,
    seed: int | None = None,
):
    resolved = [_ROLLBACK_BINDING, _SCHEMA_BINDING]
    resolved.extend(_IMPACT_BINDING for _ in payload.impact_profiles)
    return build_context(
        params=payload,
        kind=ROLLBACK_VALIDATE_KIND,
        resolved_profiles=tuple(sorted(resolved, key=lambda item: item.field_path)),
        seed=seed,
    )


def _evidence_set(store: FakeArtifactStore, outcome: PreparedRunResult) -> EvidenceSet:
    primary = outcome.artifacts[outcome.primary_index]
    return EvidenceSet.model_validate(json.loads(store.read_prepared(primary.object_ref)))


def test_artifact_dimension_reuses_schema_inspection_without_rereading_target() -> None:
    class ReadTrackingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_ids: list[str] = []

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_ids.append(artifact_id)
            return super().read_bytes(artifact_id)

    store = ReadTrackingStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)

    _handler(store)(_context(store, _payload()))

    assert TARGET_ID not in store.read_ids


def test_unread_schema_target_still_checks_object_integrity() -> None:
    class IntegrityFaultStore(FakeArtifactStore):
        def read_bytes(self, artifact_id: str) -> bytes:
            if artifact_id == TARGET_ID:
                raise IntegrityViolation("target object binding is corrupt")
            return super().read_bytes(artifact_id)

    store = IntegrityFaultStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)

    with pytest.raises(IntegrityViolation, match="object binding is corrupt"):
        _handler(
            store,
            schema=_FakeSchema(
                status="unproven",
                reason="rollback_schema_reader_unavailable",
            ),
        )(_context(store, _payload()))


def test_unreadable_ir_target_during_regression_is_an_integrity_failure() -> None:
    store = _store()
    store.register(TARGET_ID, {"not": "a canonical IR snapshot"})

    with pytest.raises(IntegrityViolation):
        _handler(store, regression_runner=_PassingRegressionRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
        )


@pytest.mark.parametrize("forgery", ("missing", "extra", "duplicate", "catalog"))
def test_rollback_validation_rejects_non_exact_profile_sets_before_any_input_or_analyzer(
    forgery: str,
) -> None:
    class ReadTrackingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_count += 1
            return super().read_bytes(artifact_id)

    class ForbiddenAnalyzer:
        def analyze(self, request):
            raise AssertionError("invalid profiles must fail before analyzer execution")

    store = ReadTrackingStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)
    payload = _payload()
    context = _context(store, payload)
    profiles = list(context.payload.resolved_profiles)
    if forgery == "missing":
        profiles.pop()
    elif forgery == "extra":
        profiles.append(
            resolved_binding(
                "/params/injected_profile",
                profile_id="injected",
                version=1,
                kind="impact_analysis",
            )
        )
    elif forgery == "duplicate":
        profiles.append(profiles[-1])
    else:
        profiles[-1] = profiles[-1].model_copy(update={"catalog_digest": "b" * 64})
    forged_payload = context.payload.model_copy(update={"resolved_profiles": tuple(profiles)})
    forged_context = replace(context, payload=forged_payload)

    with pytest.raises(IntegrityViolation, match="execution profile"):
        _handler(store, schema=ForbiddenAnalyzer(), impact=ForbiddenAnalyzer())(forged_context)

    assert store.read_count == 0
    assert store.put_count == 0


@pytest.mark.parametrize("authority_failure", ("payload_hash", "lifecycle"))
def test_rollback_validation_resolves_profile_authority_before_subject_or_analyzers(
    authority_failure: str,
) -> None:
    class ReadTrackingStore(FakeArtifactStore):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read_bytes(self, artifact_id: str) -> bytes:
            self.read_count += 1
            return super().read_bytes(artifact_id)

    class ForbiddenAnalyzer:
        def analyze(self, request):
            raise AssertionError("invalid profile authority must fail before analyzer execution")

    store = ReadTrackingStore()
    store.register(SUBJECT_ID, _rollback_request().model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)
    context = _context(store, _payload())
    if authority_failure == "payload_hash":
        profiles = tuple(
            binding.model_copy(update={"profile_payload_hash": "0" * 64})
            if binding.field_path == "/params/impact_profiles/0"
            else binding
            for binding in context.payload.resolved_profiles
        )
        context = replace(
            context,
            payload=context.payload.model_copy(update={"resolved_profiles": profiles}),
        )
    seen: list[str] = []

    def reject_authority(binding, *, llm_execution_mode, run_kind) -> None:
        seen.append(binding.field_path)
        assert llm_execution_mode == "not_applicable"
        assert run_kind == ROLLBACK_VALIDATE_KIND
        if binding.field_path == "/params/impact_profiles/0":
            if authority_failure == "payload_hash":
                assert binding.profile_payload_hash == "0" * 64
            raise IntegrityViolation(f"execution profile {authority_failure} is invalid")

    with pytest.raises(IntegrityViolation, match=authority_failure):
        _handler(
            store,
            schema=ForbiddenAnalyzer(),
            impact=ForbiddenAnalyzer(),
            profile_binding_validator=reject_authority,
        )(context)

    assert seen == [
        "/params/rollback_profile",
        "/params/schema_compatibility_policy",
        "/params/impact_profiles/0",
    ]
    assert store.read_count == 0
    assert store.put_count == 0


def test_all_dimensions_pass_is_a_successful_business_result() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_PassingRegressionRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )

    assert isinstance(outcome, PreparedRunResult)  # NOT a RunFailure
    assert outcome.summary.outcome_code == "rollback_validation_passed"
    assert all(artifact.version_tuple.seed is None for artifact in outcome.artifacts)
    assert all(
        artifact.version_tuple.ir_snapshot_id == _TARGET_SNAPSHOT.snapshot_id
        for artifact in outcome.artifacts
    )
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
    suite_evidence = next(
        item
        for item in regression
        if item.meta.get("requirement_id") == f"regression:{REGRESSION_SUITE_ID}"
    )
    assert suite_evidence.version_tuple.env_contract_version == "suite-env@1"
    assert all(
        item.version_tuple.env_contract_version is None
        for item in regression
        if item is not suite_evidence
    )


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


@pytest.mark.parametrize(
    "error",
    (
        DependencyUnavailable("impact backend unavailable"),
        IntegrityViolation("impact evidence is corrupt"),
        RuntimeError("impact implementation crashed"),
        NotImplementedError("impact adapter is not implemented"),
    ),
)
def test_impact_operational_and_internal_errors_propagate(error: BaseException) -> None:
    class FailingImpact:
        def analyze(self, request):
            raise error

    store = _store()
    with pytest.raises(type(error), match=str(error)):
        _handler(store, impact=FailingImpact())(_context(store, _payload()))


def test_subject_payload_mismatch_fails_profile_dimension() -> None:
    store = FakeArtifactStore()
    # subject request references a DIFFERENT target than the payload -> profile mismatch.
    request = _rollback_request().model_copy(update={"target_artifact_id": "artifact:other"})
    store.register(SUBJECT_ID, request.model_dump(mode="json"))
    store.register(TARGET_ID, _TARGET_SNAPSHOT.content_payload)
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
    assert len(outcome.findings) == 1
    regression_artifact = outcome.artifacts[outcome.findings[0].evidence_artifact_index]
    embedded = json.loads(store.read_prepared(regression_artifact.object_ref))["detail"]["findings"]
    assert embedded[0]["producer_run_id"] == "run:1"
    assert outcome.findings[0].payload.producer_run_id == "run:1"


def test_rollback_regression_rejects_llm_finding_authority() -> None:
    class LlmRegressionRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            finding = Finding(
                id="regression:llm",
                source="llm",
                producer_id="llm-routed",
                producer_run_id="regression-runner",
                oracle_type="llm-assisted",
                defect_class="llm_assisted_predicate",
                severity="major",
                snapshot_id=request.snapshot_id,
                status="unproven",
                message="suggestion-only output",
            )
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="unproven",
                reason_code="llm_only",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "status": "unproven",
                    "reason_code": "llm_only",
                    "findings": [finding.model_dump(mode="json")],
                },
            )

    store = _store()
    with pytest.raises(IntegrityViolation, match="deterministic oracle authority"):
        _handler(store, regression_runner=LlmRegressionRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
        )


def test_regression_without_a_bound_runner_is_unproven_never_a_default_pass() -> None:
    store = _store()
    outcome = _handler(store)(_context(store, _payload(regression=(REGRESSION_SUITE_ID,))))

    assert outcome.summary.outcome_code == "rollback_validation_unproven"
    evidence = _evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )
    assert requirement.status == "unproven"
    assert requirement.reason_code == "regression_runner_unavailable"


def test_schema_and_impact_ports_receive_exact_resolved_profile_bindings() -> None:
    store = _store()
    seen: dict[str, object] = {}

    class Schema(_FakeSchema):
        def analyze(self, request: RollbackSchemaRequest) -> RollbackTargetInspectionV1:
            seen["schema"] = request
            return super().analyze(request)

    class Impact(_FakeImpact):
        def analyze(self, request: RollbackImpactRequest) -> DimensionCheckV1:
            seen["impact"] = request
            return super().analyze(request)

    _handler(store, schema=Schema(), impact=Impact())(_context(store, _payload()))

    schema = seen["schema"]
    impact = seen["impact"]
    assert isinstance(schema, RollbackSchemaRequest)
    assert schema.schema_profile_binding == _SCHEMA_BINDING
    assert schema.rollback_profile_binding == _ROLLBACK_BINDING
    assert isinstance(impact, RollbackImpactRequest)
    assert impact.current_artifact_id == _EXPECTED_REF.artifact_id
    assert impact.current_ref_revision == _EXPECTED_REF.revision
    assert impact.impact_profile_binding == _IMPACT_BINDING
    assert impact.rollback_profile_binding == _ROLLBACK_BINDING


def test_regression_request_closes_target_snapshot_seed_profile_and_budget() -> None:
    store = _store()
    runner = _PassingRegressionRunner()

    _handler(store, regression_runner=runner)(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
    )

    [request] = runner.requests
    assert request.snapshot_id == _TARGET_SNAPSHOT.snapshot_id
    assert request.snapshot is not None
    assert request.snapshot.snapshot_id == _TARGET_SNAPSHOT.snapshot_id
    assert request.root_seed == 17
    assert request.run_kind == ROLLBACK_VALIDATE_KIND
    assert request.profile == _ROLLBACK_PROFILE
    assert request.max_action_work_units is not None


def test_regression_dimension_seed_comes_from_the_exact_execution_request() -> None:
    class MisreportingSeedRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                env_contract_version="suite-env@1",
                payload={
                    "payload_schema_version": "forged-regression-evidence@9",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "seed": request.seed + 1,
                    "status": "failed",
                },
                action_work_units=1,
            )

    store = _store()
    outcome = _handler(store, regression_runner=MisreportingSeedRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=None)
    )
    artifact = next(
        item
        for item in outcome.artifacts
        if item.meta.get("requirement_id") == f"regression:{REGRESSION_SUITE_ID}"
    )
    sealed = json.loads(store.read_prepared(artifact.object_ref))

    assert sealed["detail"]["payload_schema_version"] == "regression-evidence@1"
    assert sealed["detail"]["seed"] == 0
    assert sealed["detail"]["status"] == "passed"


def test_regression_requires_measured_work_for_an_executed_verdict() -> None:
    class MissingWorkRunner:
        def run(self, request) -> RegressionSuiteResultV1:
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "status": "passed",
                },
            )

    store = _store()
    with pytest.raises(IntegrityViolation, match="omitted measured action work"):
        _handler(store, regression_runner=MissingWorkRunner())(
            _context(store, _payload(regression=(REGRESSION_SUITE_ID,)), seed=17)
        )


def test_regression_suites_share_one_aggregate_work_ledger() -> None:
    class BudgetRunner:
        def __init__(self) -> None:
            self.requests = []
            self.works = iter((MAX_REPAIR_REGRESSION_WORK_UNITS_V1 - 1, 2))

        def run(self, request) -> RegressionSuiteResultV1:
            self.requests.append(request)
            return RegressionSuiteResultV1(
                suite_artifact_id=request.suite_artifact_id,
                status="passed",
                payload={
                    "payload_schema_version": "regression-evidence@1",
                    "suite_artifact_id": request.suite_artifact_id,
                    "snapshot_id": request.snapshot_id,
                    "status": "passed",
                },
                action_work_units=next(self.works),
            )

    store = _store()
    runner = BudgetRunner()
    with pytest.raises(IntegrityViolation, match="aggregate work budget"):
        _handler(store, regression_runner=runner)(
            _context(
                store,
                _payload(regression=(REGRESSION_SUITE_ID, SECOND_REGRESSION_SUITE_ID)),
                seed=17,
            )
        )

    assert [request.max_action_work_units for request in runner.requests] == [
        MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
        1,
    ]


def test_dimension_local_lineage_retains_consumed_current_and_suite_inputs() -> None:
    store = _store()
    outcome = _handler(store, regression_runner=_PassingRegressionRunner())(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    payloads_by_requirement = {
        json.loads(store.read_prepared(item.object_ref))["requirement_id"]: (
            item,
            json.loads(store.read_prepared(item.object_ref)),
        )
        for item in outcome.artifacts
        if item.kind == "regression_evidence"
    }

    impact, impact_payload = payloads_by_requirement["impact:impact@1"]
    regression, regression_payload = payloads_by_requirement[f"regression:{REGRESSION_SUITE_ID}"]
    assert _EXPECTED_REF.artifact_id in impact.lineage
    assert impact_payload["lineage_suite_artifact_ids"] == []
    assert REGRESSION_SUITE_ID in regression.lineage
    assert regression_payload["lineage_suite_artifact_ids"] == [REGRESSION_SUITE_ID]


def test_unproven_regression_seals_the_adapter_reason_on_both_wires() -> None:
    store = _store()
    runner = _UnprovenRegressionRunner()
    outcome = _handler(store, regression_runner=runner)(
        _context(store, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    evidence = _evidence_set(store, outcome)
    requirement = next(
        item
        for item in evidence.requirements
        if item.requirement_id == f"regression:{REGRESSION_SUITE_ID}"
    )
    artifact = next(
        item
        for item in outcome.artifacts
        if item.payload_schema_id == "regression-evidence@1"
        and json.loads(store.read_prepared(item.object_ref)).get("requirement_id")
        == f"regression:{REGRESSION_SUITE_ID}"
    )
    sealed = json.loads(store.read_prepared(artifact.object_ref))

    assert requirement.reason_code == runner.reason
    assert sealed["reason_code"] == runner.reason
    assert sealed["detail"]["reason_code"] == runner.reason


def test_rollback_validation_is_byte_deterministic() -> None:
    store_a, store_b = _store(), _store()
    out_a = _handler(store_a, regression_runner=_PassingRegressionRunner())(
        _context(store_a, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    out_b = _handler(store_b, regression_runner=_PassingRegressionRunner())(
        _context(store_b, _payload(regression=(REGRESSION_SUITE_ID,)))
    )
    assert [a.payload_hash for a in out_a.artifacts] == [a.payload_hash for a in out_b.artifacts]
