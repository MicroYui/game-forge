"""Exact retained-Run authority used by local workflow commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from gameforge.apps.api.local import _RetainedAgentProducerRunGateway
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import RunKindRef, VersionTransitionPolicyRefV1
from gameforge.contracts.jobs import (
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunRecord,
    RunResultSummaryV1,
    RunResultV1,
)
from gameforge.contracts.lineage import (
    AuditActor,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)


_HASH = "a" * 64
_ACTOR = AuditActor(principal_id="principal:maker", principal_kind="human")


class _Artifacts:
    def __init__(self, *values: object) -> None:
        self.values = {value.artifact_id: value for value in values}

    def get(self, artifact_id: str):
        return self.values.get(artifact_id)


@dataclass
class _Readers:
    result: RunResultV1

    def load_run_result(self, artifact: object) -> RunResultV1:
        del artifact
        return self.result

    def inspect_draft_subject(self, artifact: object) -> object:
        del artifact
        return SimpleNamespace(
            subject_kind="patch",
            produced_by="agent",
            producer_run_id="run:generation",
        )


def _gateway_fixture(
    *,
    outcome: str = "generation_gate_passed",
    frozen_tuple: VersionTuple | None = None,
    input_ids: tuple[str, ...] = ("artifact:input", "artifact:cassette"),
    cassette_scope: str | None = "replay_input",
    primary_role: str = "output",
    primary_publication: str = "run_published",
    primary_attempt: int | None = None,
    primary_ordinal: int | None = None,
    primary_cassette_scope: str | None = None,
):
    input_tuple = VersionTuple(ir_snapshot_id="snapshot:base", seed=7)
    terminal_tuple = VersionTuple(
        ir_snapshot_id="snapshot:preview",
        tool_version="generation@1",
        seed=7,
    )
    primary_ref = object_ref_for_bytes(b"{}")
    primary = build_artifact_v2(
        kind="patch",
        # Domain Artifact projections intentionally carry only their lineage-policy
        # fields; the run_result Artifact alone carries the run-wide terminal tuple.
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:preview",
            tool_version="generation@1",
        ),
        lineage=(),
        payload_hash=primary_ref.sha256,
        object_ref=primary_ref,
        meta={"payload_schema_id": "patch@2"},
    )
    parents = tuple(
        RunManifestParentBindingV1.model_construct(
            artifact_id=artifact_id,
            role="input",
            publication="existing",
            cassette_scope=(cassette_scope if artifact_id == "artifact:cassette" else None),
        )
        for artifact_id in input_ids
    ) + (
        RunManifestParentBindingV1.model_construct(
            artifact_id=primary.artifact_id,
            role=primary_role,
            publication=primary_publication,
            attempt_no=primary_attempt,
            ordinal=primary_ordinal,
            cassette_scope=primary_cassette_scope,
        ),
    )
    projection = RunManifestVersionProjectionV1.model_construct(
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="generation.propose", version=1),
        run_payload_hash=_HASH,
        frozen_input_version_tuple=frozen_tuple or input_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=VersionTransitionPolicyRefV1(
            policy_id="generation-gate-pass",
            policy_version=1,
            digest=_HASH,
        ),
        parents=parents,
    )
    result = RunResultV1.model_construct(
        run_id="run:generation",
        attempt_no=1,
        run_kind=RunKindRef(kind="generation.propose", version=1),
        primary_artifact_id=primary.artifact_id,
        produced_artifact_ids=(primary.artifact_id,),
        finding_count=0,
        outcome_code=outcome,
        summary=RunResultSummaryV1(
            outcome_code=outcome,
            primary_artifact_kind="patch",
            produced_artifact_count=1,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result_ref = object_ref_for_bytes(b"result")
    manifest = build_artifact_v2(
        kind="run_result",
        version_tuple=terminal_tuple,
        lineage=tuple(sorted(parent.artifact_id for parent in parents)),
        payload_hash=result_ref.sha256,
        object_ref=result_ref,
        meta={"payload_schema_id": "run-result@1"},
    )
    run = RunRecord.model_construct(
        run_id="run:generation",
        kind=RunKindRef(kind="generation.propose", version=1),
        status="succeeded",
        initiated_by=_ACTOR,
        result_artifact_id=manifest.artifact_id,
        current_attempt_no=1,
        payload_hash=_HASH,
        payload=SimpleNamespace(
            input_artifact_ids=("artifact:input", "artifact:cassette"),
            cassette_artifact_id="artifact:cassette",
            llm_execution_mode="replay",
            version_tuple=input_tuple,
        ),
    )
    gateway = _RetainedAgentProducerRunGateway(
        runs=SimpleNamespace(get=lambda run_id: run),
        artifacts=_Artifacts(primary, manifest),
        readers=_Readers(result),  # type: ignore[arg-type]
        registry=SimpleNamespace(),
        clock=SimpleNamespace(),  # type: ignore[arg-type]
        command_audit=SimpleNamespace(),  # type: ignore[arg-type]
    )
    return gateway, primary


def test_retained_agent_gateway_accepts_exact_terminal_manifest() -> None:
    gateway, primary = _gateway_fixture()

    gateway.verify_producer_membership(
        run_id="run:generation",
        artifact_id=primary.artifact_id,
        initiated_by=_ACTOR,
    )
    gateway.verify_prepared_terminal_producer_authority(
        run_id="run:generation",
        initiated_by=_ACTOR,
    )


@pytest.mark.parametrize(
    "fixture_args",
    (
        {"outcome": "repair_verified"},
        {"frozen_tuple": VersionTuple(ir_snapshot_id="snapshot:other", seed=7)},
        {"input_ids": ("artifact:input",)},
        {"cassette_scope": None},
        {"primary_role": "evidence"},
        {"primary_publication": "existing"},
        {"primary_attempt": 1},
        {"primary_ordinal": 1},
        {"primary_cassette_scope": "replay_input"},
    ),
)
def test_retained_agent_gateway_rejects_tampered_terminal_projection(
    fixture_args: dict[str, object],
) -> None:
    gateway, primary = _gateway_fixture(**fixture_args)

    with pytest.raises(IntegrityViolation, match="exact terminal"):
        gateway.verify_producer_membership(
            run_id="run:generation",
            artifact_id=primary.artifact_id,
            initiated_by=_ACTOR,
        )


class _ActiveRuns:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.record = None
        self.events = ()

    def get(self, run_id: str) -> RunRecord:
        assert run_id == self.run.run_id
        return self.run

    def accept_command(self, *, expected_run_revision: int, record, events):
        assert expected_run_revision == self.run.revision
        self.record = record
        self.events = events
        self.run = self.run.model_copy(
            update={
                "revision": self.run.revision + 1,
                "next_event_seq": self.run.next_event_seq + 1,
                "cancel_requested_at": events[0].occurred_at,
                "cancel_requested_by": record.actor,
            }
        )
        return SimpleNamespace(run=self.run, record=record, events=events)


def _cancel_gateway(status: str = "running"):
    run = RunRecord.model_construct(
        run_id="run:validation",
        kind=RunKindRef(kind="patch.validate", version=1),
        status=status,
        revision=4,
        next_event_seq=9,
        cancel_requested_at=None,
        cancel_requested_by=None,
        initiated_by=_ACTOR,
    )
    runs = _ActiveRuns(run)
    audit_calls: list[dict[str, object]] = []
    gateway = _RetainedAgentProducerRunGateway(
        runs=runs,
        artifacts=SimpleNamespace(),
        readers=SimpleNamespace(),  # type: ignore[arg-type]
        registry=SimpleNamespace(
            get_run_kind=lambda kind: SimpleNamespace(allowed_command_schema_ids=("run-cancel@1",))
        ),
        clock=SimpleNamespace(now_utc=lambda: datetime(2026, 7, 19, tzinfo=timezone.utc)),
        command_audit=SimpleNamespace(
            record_command_submitted=lambda **kwargs: audit_calls.append(kwargs)
        ),
    )
    return gateway, runs, audit_calls


def test_retained_agent_gateway_requests_active_validation_cancel() -> None:
    gateway, runs, audit_calls = _cancel_gateway()

    gateway.request_validation_cancel(
        run_id="run:validation",
        reason="subject_superseded",
        requested_by=_ACTOR,
    )

    assert runs.record.command.type == "cancel"
    assert runs.record.command.expected_run_revision == 4
    assert runs.events[0].event_type == "run.cancel_requested"
    assert runs.run.cancel_requested_by == _ACTOR
    assert len(audit_calls) == 1


@pytest.mark.parametrize("status", ("queued", "retry_wait"))
def test_retained_agent_gateway_requests_inactive_validation_cancel(status: str) -> None:
    gateway, runs, audit_calls = _cancel_gateway(status=status)

    gateway.request_validation_cancel(
        run_id="run:validation",
        reason="subject_superseded",
        requested_by=_ACTOR,
    )

    assert runs.run.status == status
    assert runs.run.cancel_requested_by == _ACTOR
    assert len(audit_calls) == 1


def test_retained_agent_gateway_fails_closed_without_validation_authority() -> None:
    gateway, _runs, _audit_calls = _cancel_gateway(status="succeeded")

    with pytest.raises(IntegrityViolation, match="terminal validation Run"):
        gateway.request_validation_cancel(
            run_id="run:validation",
            reason="subject_superseded",
            requested_by=_ACTOR,
        )
    with pytest.raises(IntegrityViolation, match="atomic Run admission"):
        gateway.start_validation()
