"""The generic terminal publication engine.

``TerminalPublisher`` is the concrete
:class:`gameforge.platform.runs.lifecycle.RunLifecyclePublicationGateway`.  The Run
lifecycle service has already selected the unique outcome policy per scope, ordered
attempt-close before run-aggregate, and owns cost/event closure; this engine turns
the (non-authoritative) ``PreparedRunOutcome`` into authoritative Artifacts, Finding
revisions/links, workflow effects, RunResult/RunFailure manifests and audit — all
inside the one transaction the caller owns.  Any write failure raises and the
owning UoW rolls back every authority.

The domain / manifest blobs are content-addressed and (per §3.3) hashed outside the
write transaction; this engine only re-reads each PreparedArtifact blob to re-verify
``payload_hash``/size/location, re-derives every VersionTuple and manifest
projection from the retained exact registry version, and writes the Artifact rows,
Finding rows, workflow effect and audit through the injected transaction-bound
ports.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Protocol

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import FindingRevisionV1
from gameforge.contracts.jobs import (
    OutcomeArtifactPolicyV1,
    PreparedRunFailure,
    PreparedRunResult,
    RequirementDispositionV1,
    ResolvedPolicySnapshotV1,
    RetryDecisionV1,
    RunAttempt,
    RunFailureV1,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunRecord,
    RunResultSummaryV1,
    RunResultV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
    parse_artifact,
)
from gameforge.platform.publication.effects import WorkflowEffectContext, apply_workflow_effect
from gameforge.platform.publication.findings import plan_finding_write
from gameforge.platform.publication.lineage import (
    LineageParentSources,
    ParentInfo,
    project_typed_lineage,
)
from gameforge.platform.publication.planner import (
    PublicationPlan,
    PublicationRegistry,
    build_publication_plan,
    resolve_definition,
)
from gameforge.platform.publication.validator import (
    PreparedArtifactView,
    RuleAllocation,
    allocate_artifacts,
    validate_rule_cardinality,
)
from gameforge.platform.publication.version import (
    project_domain_version_tuple,
    project_manifest_version_tuple,
)
from gameforge.platform.runs.lifecycle import (
    AttemptFailurePublication,
    RunFailurePublication,
    RunResultPublication,
)


class ArtifactPort(Protocol):
    def get(self, artifact_id: str) -> object | None: ...

    def put(self, artifact: ArtifactV2) -> ArtifactV2: ...


class BlobStore(Protocol):
    def read(self, object_ref: ObjectRef) -> bytes: ...

    def put(self, payload: bytes) -> ObjectRef: ...


class FindingStore(Protocol):
    def put(
        self, revision: FindingRevisionV1, *, expected_current_revision: int | None
    ) -> FindingRevisionV1: ...


class ManifestLedger(Protocol):
    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]: ...

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]: ...

    def put_finding_link(self, link: RunFindingLinkV1) -> None: ...


class AuditPort(Protocol):
    def record(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
    ) -> None: ...


def _role_for_manifest(rule_role: str) -> str:
    return "evidence" if rule_role == "evidence" else "output"


class TerminalPublisher:
    """Concrete ``RunLifecyclePublicationGateway`` for every M4 Run kind."""

    def __init__(
        self,
        *,
        registry: PublicationRegistry,
        artifacts: ArtifactPort,
        blobs: BlobStore,
        findings: FindingStore,
        ledger: ManifestLedger,
        audit: AuditPort,
    ) -> None:
        self._registry = registry
        self._artifacts = artifacts
        self._blobs = blobs
        self._findings = findings
        self._ledger = ledger
        self._audit = audit

    # ------------------------------------------------------------------ audit
    def record_attempt_started(self, **kwargs: object) -> None:
        self._record_event("run.attempt_started", kwargs)

    def record_attempt_progress(self, **kwargs: object) -> None:
        self._record_event("run.attempt_progress", kwargs)

    def record_attempt_closed(self, **kwargs: object) -> None:
        self._record_event("run.attempt_closed", kwargs)

    def record_run_terminal(self, **kwargs: object) -> None:
        self._record_event("run.terminal", kwargs)

    def _record_event(self, action: str, kwargs: Mapping[str, object]) -> None:
        run = kwargs.get("run")
        actor = kwargs.get("actor")
        event = kwargs.get("event")
        occurred_at = getattr(event, "occurred_at", None) if event is not None else None
        if isinstance(run, RunRecord) and isinstance(actor, AuditActor):
            self._audit.record(
                action=action,
                run=run,
                artifact_id=None,
                actor=actor,
                occurred_at=occurred_at or run.updated_at,
            )

    # ----------------------------------------------------------------- success
    def publish_run_result(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunResultPublication:
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="run"
        )
        self._verify_success_metadata(run=run, attempt=attempt, prepared=prepared, policy=policy)

        views = self._read_views(prepared.artifacts)
        primary_payload = dict(views[prepared.primary_index].payload)
        allocations = allocate_artifacts(plan_rules=plan.plan_rules, artifacts=views)
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=primary_payload,
            dispositions=prepared.requirement_dispositions,
        )

        published = self._publish_domain_artifacts(
            run=run, plan=plan, allocations=allocations, views=views, occurred_at=occurred_at
        )
        primary_rule_id = self._primary_rule_id(plan)
        primary_artifact_id = published.ids_by_rule[primary_rule_id][0]
        if views[prepared.primary_index].index not in published.index_to_id:  # defensive
            raise IntegrityViolation("primary prepared artifact was not published")
        if published.index_to_id[prepared.primary_index] != primary_artifact_id:
            raise IntegrityViolation("primary artifact id differs from the primary rule output")

        finding_count = self._publish_findings(
            run=run,
            attempt=attempt,
            prepared=prepared,
            plan=plan,
            allocations=allocations,
            published=published,
            occurred_at=occurred_at,
        )

        output_parents = self._domain_manifest_parents(published)
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt.attempt_no,
            scope="run",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=output_parents,
        )
        produced_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        result = RunResultV1(
            run_id=run.run_id,
            attempt_no=attempt.attempt_no,
            run_kind=run.kind,
            primary_artifact_id=primary_artifact_id,
            produced_artifact_ids=produced_ids,
            finding_count=finding_count,
            outcome_code=policy.outcome_code,
            summary=RunResultSummaryV1(
                outcome_code=policy.outcome_code,
                primary_artifact_kind=prepared.summary.primary_artifact_kind,
                produced_artifact_count=len(produced_ids),
                finding_count=finding_count,
            ),
            requirement_dispositions=prepared.requirement_dispositions,
            version_projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_result",
            payload=result.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            occurred_at=occurred_at,
        )

        apply_workflow_effect(
            policy.workflow_effect_key,
            WorkflowEffectContext(
                run=run,
                policy=policy,
                scope="run",
                published_primary_artifact_id=primary_artifact_id,
                published_output_artifact_ids=produced_ids,
                approvals=None,
                actor=run.initiated_by,
                occurred_at=occurred_at,
            ),
        )
        self._audit.record(
            action=definition.terminal_hooks.on_success,
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return RunResultPublication(
            result_artifact_id=manifest_id,
            attempt_cassette_artifact_id=None,
            terminal_cassette_artifact_id=self._terminal_cassette_id(run),
        )

    # ------------------------------------------------------- attempt failure
    def publish_attempt_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        occurred_at: str,
        actor: AuditActor,
    ) -> AttemptFailurePublication:
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="attempt"
        )
        # attempt-close policies never consume business evidence/dispositions.
        current_prompt_parents = self._runtime_prompt_parents(
            run.run_id, attempt_no=attempt.attempt_no
        )
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt.attempt_no,
            scope="attempt",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=current_prompt_parents,
        )
        evidence_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        failure = self._build_run_failure(
            run=run,
            attempt_no=attempt.attempt_no,
            prepared=prepared,
            retry_decision=retry_decision,
            evidence_ids=evidence_ids,
            dispositions=(),
            occurred_at=occurred_at,
            projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_failure",
            payload=failure.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            occurred_at=occurred_at,
        )
        apply_workflow_effect(
            policy.workflow_effect_key,
            WorkflowEffectContext(
                run=run,
                policy=policy,
                scope="attempt",
                published_primary_artifact_id=None,
                published_output_artifact_ids=(),
                approvals=None,
                actor=run.initiated_by,
                occurred_at=occurred_at,
            ),
        )
        self._audit.record(
            action="run.attempt_failure",
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return AttemptFailurePublication(
            failure_artifact_id=manifest_id, cassette_bundle_artifact_id=None
        )

    # ----------------------------------------------------------- run failure
    def publish_run_failure(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        policy: OutcomeArtifactPolicyV1,
        attempt_failure_artifact_id: str | None,
        occurred_at: str,
        actor: AuditActor,
    ) -> RunFailurePublication:
        definition = resolve_definition(registry=self._registry, run=run)
        plan = build_publication_plan(
            registry=self._registry, definition=definition, policy=policy, scope="run"
        )
        attempt_no = attempt.attempt_no if attempt is not None else None

        views = self._read_views(prepared.artifacts)
        primary_payload = dict(views[0].payload) if views else None
        allocations = allocate_artifacts(plan_rules=plan.plan_rules, artifacts=views)
        self._validate_cardinalities(
            allocations=allocations,
            views=views,
            run=run,
            primary_payload=primary_payload,
            dispositions=prepared.requirement_dispositions,
        )
        published = self._publish_domain_artifacts(
            run=run, plan=plan, allocations=allocations, views=views, occurred_at=occurred_at
        )

        extra_parents = list(self._domain_manifest_parents(published))
        extra_parents.extend(self._runtime_prompt_parents(run.run_id, attempt_no=None))
        extra_parents.extend(
            self._closed_attempt_parents(
                run.run_id,
                current_attempt_no=attempt_no,
                current_attempt_failure_id=attempt_failure_artifact_id,
            )
        )
        projection = self._manifest_projection(
            run=run,
            attempt_no=attempt_no,
            scope="run",
            transition_policy=plan.transition_policy,
            transition_ref=plan.policy.version_transition_policy_ref,
            extra_parents=tuple(extra_parents),
        )
        evidence_ids = tuple(
            parent.artifact_id
            for parent in projection.parents
            if parent.publication == "run_published" and parent.role != "input"
        )
        failure = self._build_run_failure(
            run=run,
            attempt_no=attempt_no,
            prepared=prepared,
            retry_decision=retry_decision,
            evidence_ids=evidence_ids,
            dispositions=prepared.requirement_dispositions,
            occurred_at=occurred_at,
            projection=projection,
        )
        manifest_id = self._publish_manifest(
            kind="run_failure",
            payload=failure.model_dump(mode="json"),
            version_tuple=projection.terminal_version_tuple,
            parents=projection.parents,
            occurred_at=occurred_at,
        )
        apply_workflow_effect(
            policy.workflow_effect_key,
            WorkflowEffectContext(
                run=run,
                policy=policy,
                scope="run",
                published_primary_artifact_id=None,
                published_output_artifact_ids=evidence_ids,
                approvals=None,
                actor=run.initiated_by,
                occurred_at=occurred_at,
            ),
        )
        self._audit.record(
            action="run.failure",
            run=run,
            artifact_id=manifest_id,
            actor=actor,
            occurred_at=occurred_at,
        )
        return RunFailurePublication(
            failure_artifact_id=manifest_id,
            terminal_cassette_artifact_id=self._terminal_cassette_id(run),
        )

    # -------------------------------------------------------------- internals
    def _verify_success_metadata(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        policy: OutcomeArtifactPolicyV1,
    ) -> None:
        if prepared.run_id != run.run_id or prepared.attempt_no != attempt.attempt_no:
            raise IntegrityViolation("prepared result differs from the current Run attempt")
        if prepared.run_kind != run.kind:
            raise IntegrityViolation("prepared result Run kind differs from the RunRecord")
        if prepared.summary.outcome_code != policy.outcome_code:
            raise IntegrityViolation("prepared summary outcome differs from the selected policy")
        if prepared.summary.prepared_domain_artifact_count != len(prepared.artifacts):
            raise IntegrityViolation("prepared domain artifact count is fabricated")
        if prepared.summary.prepared_finding_count != len(prepared.findings):
            raise IntegrityViolation("prepared finding count is fabricated")

    def _read_views(self, artifacts: Sequence[object]) -> tuple[PreparedArtifactView, ...]:
        views: list[PreparedArtifactView] = []
        for index, prepared in enumerate(artifacts):
            blob = self._blobs.read(prepared.object_ref)
            digest = sha256_lowerhex(blob)
            if digest != prepared.payload_hash or digest != prepared.object_ref.sha256:
                raise IntegrityViolation(
                    "prepared artifact blob hash differs from its declared payload hash",
                    artifact_index=index,
                )
            if prepared.object_ref.size_bytes != len(blob):
                raise IntegrityViolation(
                    "prepared artifact blob size differs from its ObjectRef", artifact_index=index
                )
            payload = _decode_payload(blob, index=index)
            views.append(
                PreparedArtifactView(
                    index=index,
                    kind=prepared.kind,
                    payload_schema_id=prepared.payload_schema_id,
                    version_tuple=prepared.version_tuple,
                    lineage=tuple(prepared.lineage),
                    payload_hash=prepared.payload_hash,
                    object_ref=prepared.object_ref,
                    location=prepared.location,
                    meta=dict(prepared.meta),
                    payload=payload,
                )
            )
        return tuple(views)

    def _validate_cardinalities(
        self,
        *,
        allocations: Sequence[RuleAllocation],
        views: Sequence[PreparedArtifactView],
        run: RunRecord,
        primary_payload: Mapping[str, object] | None,
        dispositions: Sequence[RequirementDispositionV1],
    ) -> None:
        by_index = {view.index: view for view in views}
        run_payload = run.payload.model_dump(mode="python")
        snapshots = _snapshots_by_id(run.payload.resolved_policy_snapshots)
        for allocation in allocations:
            validate_rule_cardinality(
                allocation=allocation,
                artifacts_by_index=by_index,
                run_payload=run_payload,
                primary_payload=primary_payload,
                snapshots_by_id=snapshots,
                dispositions=dispositions,
            )

    def _publish_domain_artifacts(
        self,
        *,
        run: RunRecord,
        plan: PublicationPlan,
        allocations: Sequence[RuleAllocation],
        views: Sequence[PreparedArtifactView],
        occurred_at: str,
    ) -> "_PublishedArtifacts":
        by_index = {view.index: view for view in views}
        if not any(allocation.artifact_indexes for allocation in allocations):
            return _PublishedArtifacts(ids_by_rule={}, index_to_id={}, roles={})
        run_inputs = self._input_parents(run.payload.input_artifact_ids)
        siblings: dict[str, dict[str, ParentInfo]] = {}
        ids_by_rule: dict[str, list[str]] = {}
        index_to_id: dict[int, str] = {}
        roles: dict[str, str] = {}

        for allocation in _topological_rule_order(allocations, plan):
            rule = allocation.plan_rule.rule
            lineage_policy = plan.lineage_by_rule_id[rule.rule_id]
            ids_by_rule.setdefault(rule.rule_id, [])
            for index in allocation.artifact_indexes:
                view = by_index[index]
                sources = LineageParentSources(
                    run_inputs=run_inputs,
                    run_intermediates=self._intermediate_parents(run.run_id),
                    prepared_siblings={key: dict(value) for key, value in siblings.items()},
                )
                typed = project_typed_lineage(
                    policy=lineage_policy,
                    child_kind=view.kind,
                    child_payload_schema_id=view.payload_schema_id,
                    child_lineage=view.lineage,
                    sources=sources,
                )
                expected_tuple = project_domain_version_tuple(
                    policy=lineage_policy,
                    parent_tuples={
                        role: tuple(info.version_tuple for info in parents)
                        for role, parents in typed.parents_by_role.items()
                    },
                    producer_tuple=run.payload.version_tuple,
                )
                if expected_tuple != view.version_tuple:
                    raise IntegrityViolation(
                        "prepared VersionTuple differs from the re-derived lineage projection",
                        artifact_index=index,
                        rule_id=rule.rule_id,
                    )
                artifact = build_artifact_v2(
                    kind=view.kind,
                    version_tuple=view.version_tuple,
                    lineage=view.lineage,
                    payload_hash=view.payload_hash,
                    object_ref=view.object_ref,
                    meta=view.meta,
                    created_at=occurred_at,
                )
                stored = self._artifacts.put(artifact)
                ids_by_rule[rule.rule_id].append(stored.artifact_id)
                index_to_id[index] = stored.artifact_id
                roles[stored.artifact_id] = rule.role
                siblings.setdefault(rule.rule_id, {})[stored.artifact_id] = ParentInfo(
                    artifact_id=stored.artifact_id,
                    kind=view.kind,
                    payload_schema_id=view.payload_schema_id,
                    version_tuple=view.version_tuple,
                )
        return _PublishedArtifacts(
            ids_by_rule={key: tuple(value) for key, value in ids_by_rule.items()},
            index_to_id=index_to_id,
            roles=roles,
        )

    def _publish_findings(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt,
        prepared: PreparedRunResult,
        plan: PublicationPlan,
        allocations: Sequence[RuleAllocation],
        published: "_PublishedArtifacts",
        occurred_at: str,
    ) -> int:
        if not prepared.findings:
            return 0
        if plan.finding_policy is None:
            raise IntegrityViolation("Run kind has no finding-output policy but prepared findings")
        if len(prepared.findings) > plan.finding_policy.max_findings:
            raise IntegrityViolation("prepared findings exceed the policy maximum")
        rule_of_index = {
            index: allocation.plan_rule.rule.rule_id
            for allocation in allocations
            for index in allocation.artifact_indexes
        }
        planned = []
        for prepared_finding in prepared.findings:
            evidence_index = prepared_finding.evidence_artifact_index
            evidence_artifact_id = published.index_to_id[evidence_index]
            evidence_rule_id = rule_of_index[evidence_index]
            planned.append(
                plan_finding_write(
                    prepared=prepared_finding,
                    finding_policy=plan.finding_policy,
                    evidence_rule_id=evidence_rule_id,
                    evidence_artifact_id=evidence_artifact_id,
                    run_id=run.run_id,
                    attempt_no=attempt.attempt_no,
                    ordinal=1,
                    occurred_at=occurred_at,
                )
            )
        planned.sort(key=lambda write: (write.revision.finding_id, write.revision.revision))
        for ordinal, write in enumerate(planned, start=1):
            self._findings.put(
                write.revision, expected_current_revision=write.expected_current_revision
            )
            link = write.link.model_copy(update={"ordinal": ordinal})
            self._ledger.put_finding_link(link)
        return len(planned)

    def _build_run_failure(
        self,
        *,
        run: RunRecord,
        attempt_no: int | None,
        prepared: PreparedRunFailure,
        retry_decision: RetryDecisionV1,
        evidence_ids: tuple[str, ...],
        dispositions: Sequence[RequirementDispositionV1],
        occurred_at: str,
        projection: RunManifestVersionProjectionV1,
    ) -> RunFailureV1:
        return RunFailureV1(
            run_id=run.run_id,
            attempt_no=attempt_no,
            run_kind=run.kind,
            cause_code=prepared.cause_code,
            failure_class=prepared.failure_class,
            retryable=(retry_decision.decision == "retry"),
            retry_decision=retry_decision,
            dependency=prepared.dependency,
            redacted_message=prepared.redacted_message,
            evidence_artifact_ids=evidence_ids,
            requirement_dispositions=tuple(dispositions),
            occurred_at=occurred_at,
            version_projection=projection,
        )

    def _manifest_projection(
        self,
        *,
        run: RunRecord,
        attempt_no: int | None,
        scope: str,
        transition_policy: object,
        transition_ref: object,
        extra_parents: Sequence[RunManifestParentBindingV1],
    ) -> RunManifestVersionProjectionV1:
        parents = [
            RunManifestParentBindingV1(artifact_id=input_id, role="input", publication="existing")
            for input_id in run.payload.input_artifact_ids
        ]
        parents.extend(extra_parents)
        terminal_tuple = project_manifest_version_tuple(
            policy=transition_policy,  # type: ignore[arg-type]
            manifest_scope=scope,
            llm_execution_mode=run.payload.llm_execution_mode,
            frozen_tuple=run.payload.version_tuple,
            execution_identity=None,
            cassette_ids_by_scope={},
        )
        return RunManifestVersionProjectionV1(
            manifest_scope=scope,
            attempt_no=attempt_no,
            run_kind=run.kind,
            run_payload_hash=run.payload_hash,
            frozen_input_version_tuple=run.payload.version_tuple,
            terminal_version_tuple=terminal_tuple,
            version_transition_policy_ref=transition_ref,  # type: ignore[arg-type]
            parents=tuple(parents),
        )

    def _publish_manifest(
        self,
        *,
        kind: str,
        payload: Mapping[str, object],
        version_tuple: VersionTuple,
        parents: Sequence[RunManifestParentBindingV1],
        occurred_at: str,
    ) -> str:
        blob = canonical_json(payload).encode("utf-8")
        object_ref = self._blobs.put(blob)
        expected_ref = object_ref_for_bytes(blob)
        if object_ref != expected_ref:
            raise IntegrityViolation("manifest blob store returned a non-canonical ObjectRef")
        lineage = tuple(sorted({parent.artifact_id for parent in parents}))
        artifact = build_artifact_v2(
            kind=kind,
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=object_ref.sha256,
            object_ref=object_ref,
            meta={"manifest_scope": payload["version_projection"]["manifest_scope"]},
            created_at=occurred_at,
        )
        if artifact.artifact_id in lineage:
            raise IntegrityViolation("manifest artifact references itself in its lineage")
        stored = self._artifacts.put(artifact)
        return stored.artifact_id

    def _domain_manifest_parents(
        self, published: "_PublishedArtifacts"
    ) -> tuple[RunManifestParentBindingV1, ...]:
        return tuple(
            RunManifestParentBindingV1(
                artifact_id=artifact_id,
                role=_role_for_manifest(role),
                publication="run_published",
            )
            for artifact_id, role in published.roles.items()
        )

    def _runtime_prompt_parents(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunManifestParentBindingV1, ...]:
        links = self._ledger.prompt_links(run_id, attempt_no=attempt_no)
        return tuple(
            RunManifestParentBindingV1(
                artifact_id=link.artifact_id,
                role="intermediate",
                publication="run_published",
                attempt_no=link.attempt_no,
                ordinal=link.call_ordinal,
            )
            for link in links
        )

    def _closed_attempt_parents(
        self, run_id: str, *, current_attempt_no: int | None, current_attempt_failure_id: str | None
    ) -> tuple[RunManifestParentBindingV1, ...]:
        aggregated: dict[str, int | None] = {}
        for closed_attempt_no, failure_id in self._ledger.closed_attempt_failures(run_id):
            if failure_id in aggregated:
                raise IntegrityViolation(
                    "closed attempt failure aggregated more than once", failure_id=failure_id
                )
            aggregated[failure_id] = closed_attempt_no
        if current_attempt_failure_id is not None:
            if current_attempt_failure_id in aggregated:
                raise IntegrityViolation(
                    "current attempt failure is already a closed-attempt parent",
                    failure_id=current_attempt_failure_id,
                )
            aggregated[current_attempt_failure_id] = current_attempt_no
        return tuple(
            RunManifestParentBindingV1(
                artifact_id=failure_id,
                role="intermediate",
                publication="run_published",
                attempt_no=attempt_no,
            )
            for failure_id, attempt_no in aggregated.items()
        )

    def _input_parents(self, input_ids: Sequence[str]) -> Mapping[str, ParentInfo]:
        return {input_id: self._parent_info(input_id) for input_id in input_ids}

    def _intermediate_parents(self, run_id: str) -> Mapping[str, ParentInfo]:
        parents: dict[str, ParentInfo] = {}
        for link in self._ledger.prompt_links(run_id, attempt_no=None):
            parents[link.artifact_id] = self._parent_info(link.artifact_id)
        return parents

    def _parent_info(self, artifact_id: str) -> ParentInfo:
        wire = self._artifacts.get(artifact_id)
        if wire is None:
            raise IntegrityViolation(
                "lineage parent artifact is not published", artifact_id=artifact_id
            )
        parsed = wire if isinstance(wire, ArtifactV2) else parse_artifact(wire)
        meta = getattr(parsed, "meta", {}) or {}
        schema = meta.get("payload_schema_id")
        if not isinstance(schema, str):
            raise IntegrityViolation(
                "parent artifact does not declare its payload schema", artifact_id=artifact_id
            )
        return ParentInfo(
            artifact_id=parsed.artifact_id,
            kind=parsed.kind,
            payload_schema_id=schema,
            version_tuple=parsed.version_tuple,
        )

    @staticmethod
    def _primary_rule_id(plan: PublicationPlan) -> str:
        for plan_rule in plan.plan_rules:
            if plan_rule.rule.role == "primary":
                return plan_rule.rule.rule_id
        raise IntegrityViolation("success policy has no primary artifact rule")

    @staticmethod
    def _terminal_cassette_id(run: RunRecord) -> str | None:
        if run.payload.llm_execution_mode == "replay":
            return run.payload.cassette_artifact_id
        return None


def _decode_payload(blob: bytes, *, index: int) -> dict[str, object]:
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IntegrityViolation(
            "prepared artifact blob is not valid JSON", artifact_index=index
        ) from exc
    if not isinstance(payload, dict):
        raise IntegrityViolation(
            "prepared artifact payload must be a JSON object", artifact_index=index
        )
    return payload


def _snapshots_by_id(
    snapshots: Sequence[ResolvedPolicySnapshotV1],
) -> Mapping[str, ResolvedPolicySnapshotV1]:
    return {snapshot.resolved_policy_id: snapshot for snapshot in snapshots}


def _topological_rule_order(
    allocations: Sequence[RuleAllocation], plan: PublicationPlan
) -> tuple[RuleAllocation, ...]:
    dependencies: dict[str, set[str]] = {}
    for allocation in allocations:
        rule_id = allocation.plan_rule.rule.rule_id
        lineage_policy = plan.lineage_by_rule_id[rule_id]
        dependencies[rule_id] = {
            parent.source_rule_id
            for parent in lineage_policy.parent_rules
            if parent.source == "prepared_rule" and parent.source_rule_id is not None
        }
    ordered: list[RuleAllocation] = []
    emitted: set[str] = set()
    remaining = list(allocations)
    while remaining:
        progressed = False
        for allocation in list(remaining):
            rule_id = allocation.plan_rule.rule.rule_id
            if dependencies[rule_id] <= emitted | {rule_id}:
                ordered.append(allocation)
                emitted.add(rule_id)
                remaining.remove(allocation)
                progressed = True
        if not progressed:
            raise IntegrityViolation(
                "outcome artifact rules have a cyclic prepared-rule dependency"
            )
    return tuple(ordered)


class _PublishedArtifacts:
    __slots__ = ("ids_by_rule", "index_to_id", "roles")

    def __init__(
        self,
        *,
        ids_by_rule: Mapping[str, tuple[str, ...]],
        index_to_id: Mapping[int, str],
        roles: Mapping[str, str],
    ) -> None:
        self.ids_by_rule = ids_by_rule
        self.index_to_id = index_to_id
        self.roles = roles


__all__ = [
    "ArtifactPort",
    "AuditPort",
    "BlobStore",
    "FindingStore",
    "ManifestLedger",
    "TerminalPublisher",
]
