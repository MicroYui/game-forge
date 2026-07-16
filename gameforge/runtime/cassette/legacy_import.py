"""Authoritative runtime verification for historical ``cassette@1`` imports.

The contract models prove that an evidence graph is internally coherent.  This
module adds the missing authority boundary: every claimed input, profile,
policy, schema, rendered request, and execution version is resolved from a
retained source before an import can become executable.

Artifact publication remains an M4c composition concern.  ``prepare`` derives
the leaf payloads, and ``finalize`` accepts the Artifact IDs assigned while
publishing those leaves and closes the three-level bundle tree.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from gameforge.contracts.cassette import (
    CassetteObservationViewV1,
    CassetteRecordV1,
    CassetteRecordV2,
    cassette_observation_view,
    parse_cassette_record,
)
from gameforge.contracts.cassette_import import (
    CassetteBundleV1,
    LegacyCassetteCallImportEvidenceV1,
    LegacyCassetteInputBindingV1,
    LegacyCassettePolicyBindingV1,
    LegacyCassetteProfileBindingV1,
    LegacyCassetteRunImportManifestV1,
    LegacyCassetteSchemaBindingV1,
    LegacyImportRoutingDecisionV1,
    LegacyImportVerificationPolicyRefV1,
    LegacyImportVerificationPolicyRegistryV1,
    LegacyImportVerificationPolicyV1,
    build_legacy_import_manifest,
    compute_legacy_profile_binding_digest,
    original_wire_sha256,
    require_verified_legacy_import_bundle_tree,
    resolve_legacy_import_verification_policy,
    validate_legacy_import_bundle_tree,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import (
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    VersionTuple,
    build_execution_identity,
)
from gameforge.contracts.model_router import ModelRequestV1, request_hash
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    canonical_model_snapshot_id,
)


@dataclass(frozen=True, slots=True)
class LegacyImportCallCandidate:
    """Untrusted import input for one historical logical model call."""

    original_wire_utf8: str
    rendered_request_artifact_id: str
    source_call_ordinal: int

    def __post_init__(self) -> None:
        if not self.original_wire_utf8:
            raise ValueError("legacy cassette wire must be non-empty")
        if not self.rendered_request_artifact_id:
            raise ValueError("rendered request artifact id must be non-empty")
        if self.source_call_ordinal <= 0:
            raise ValueError("source call ordinal must be positive")


@dataclass(frozen=True, slots=True)
class LegacyImportCandidate:
    """Untrusted run-level claims; notably contains no verification status."""

    source_suite_id: str
    source_case_id: str
    verification_policy: LegacyImportVerificationPolicyRefV1
    model_catalog_version: int
    model_catalog_digest: str
    input_artifact_bindings: tuple[LegacyCassetteInputBindingV1, ...]
    execution_profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...]
    policy_bindings: tuple[LegacyCassettePolicyBindingV1, ...]
    schema_bindings: tuple[LegacyCassetteSchemaBindingV1, ...]
    calls: tuple[LegacyImportCallCandidate, ...]
    importer_tool_version: str

    def __post_init__(self) -> None:
        if not self.source_suite_id or not self.source_case_id:
            raise ValueError("legacy import source identity must be non-empty")
        if self.model_catalog_version <= 0 or len(self.model_catalog_digest) != 64:
            raise ValueError("legacy import requires an exact model catalog reference")
        if not self.importer_tool_version:
            raise ValueError("legacy import tool version must be non-empty")
        ordinals = tuple(call.source_call_ordinal for call in self.calls)
        if ordinals != tuple(range(1, len(ordinals) + 1)):
            raise ValueError("legacy import calls must start at 1 and be contiguous")


class LegacyImportAuthority(Protocol):
    """Resolve claims against retained, authoritative historical state."""

    @property
    def verification_policy_registry(
        self,
    ) -> LegacyImportVerificationPolicyRegistryV1: ...

    def resolve_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None: ...

    def resolve_input_binding(
        self,
        binding_key: str,
        artifact_id: str,
    ) -> LegacyCassetteInputBindingV1 | None: ...

    def resolve_profile_binding(
        self,
        field_path: str,
        profile_id: str,
        profile_version: int,
    ) -> LegacyCassetteProfileBindingV1 | None: ...

    def resolve_policy_binding(
        self,
        binding_key: str,
        policy_kind: str,
        policy_id: str,
        policy_version: int,
    ) -> LegacyCassettePolicyBindingV1 | None: ...

    def resolve_schema_binding(
        self,
        binding_key: str,
        schema_id: str,
    ) -> LegacyCassetteSchemaBindingV1 | None: ...

    def resolve_rendered_request(self, artifact_id: str) -> ModelRequestV1 | None: ...

    def resolve_frozen_version_tuple(
        self,
        source_suite_id: str,
        source_case_id: str,
    ) -> VersionTuple | None: ...

    def resolve_call_tool_version(
        self,
        source_suite_id: str,
        source_case_id: str,
        source_call_ordinal: int,
    ) -> str | None: ...


class LegacyImportDecisionRepository(Protocol):
    def put_legacy_import_routing_decision(
        self,
        decision: LegacyImportRoutingDecisionV1,
    ) -> LegacyImportRoutingDecisionV1: ...

    def get_legacy_import_routing_decision(
        self,
        decision_id: str,
    ) -> LegacyImportRoutingDecisionV1 | None: ...


class InMemoryLegacyImportAuthority:
    """Deterministic local authority used by tests and offline import tooling."""

    def __init__(
        self,
        *,
        verification_policy_registry: LegacyImportVerificationPolicyRegistryV1,
        model_catalogs: Mapping[tuple[int, str], ModelCatalogSnapshotV1],
        input_bindings: Mapping[tuple[str, str], LegacyCassetteInputBindingV1],
        profile_bindings: Mapping[tuple[str, str, int], LegacyCassetteProfileBindingV1],
        policy_bindings: Mapping[tuple[str, str, str, int], LegacyCassettePolicyBindingV1],
        schema_bindings: Mapping[tuple[str, str], LegacyCassetteSchemaBindingV1],
        rendered_requests: Mapping[str, ModelRequestV1],
        frozen_version_tuples: Mapping[tuple[str, str], VersionTuple],
        call_tool_versions: Mapping[tuple[str, str, int], str],
    ) -> None:
        self.verification_policy_registry = verification_policy_registry
        self.model_catalogs = dict(model_catalogs)
        self.input_bindings = dict(input_bindings)
        self.profile_bindings = dict(profile_bindings)
        self.policy_bindings = dict(policy_bindings)
        self.schema_bindings = dict(schema_bindings)
        self.rendered_requests = dict(rendered_requests)
        self.frozen_version_tuples = dict(frozen_version_tuples)
        self.call_tool_versions = dict(call_tool_versions)

    def resolve_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1 | None:
        return self.model_catalogs.get((catalog_version, catalog_digest))

    def resolve_input_binding(
        self,
        binding_key: str,
        artifact_id: str,
    ) -> LegacyCassetteInputBindingV1 | None:
        return self.input_bindings.get((binding_key, artifact_id))

    def resolve_profile_binding(
        self,
        field_path: str,
        profile_id: str,
        profile_version: int,
    ) -> LegacyCassetteProfileBindingV1 | None:
        return self.profile_bindings.get((field_path, profile_id, profile_version))

    def resolve_policy_binding(
        self,
        binding_key: str,
        policy_kind: str,
        policy_id: str,
        policy_version: int,
    ) -> LegacyCassettePolicyBindingV1 | None:
        return self.policy_bindings.get((binding_key, policy_kind, policy_id, policy_version))

    def resolve_schema_binding(
        self,
        binding_key: str,
        schema_id: str,
    ) -> LegacyCassetteSchemaBindingV1 | None:
        return self.schema_bindings.get((binding_key, schema_id))

    def resolve_rendered_request(self, artifact_id: str) -> ModelRequestV1 | None:
        return self.rendered_requests.get(artifact_id)

    def resolve_frozen_version_tuple(
        self,
        source_suite_id: str,
        source_case_id: str,
    ) -> VersionTuple | None:
        return self.frozen_version_tuples.get((source_suite_id, source_case_id))

    def resolve_call_tool_version(
        self,
        source_suite_id: str,
        source_case_id: str,
        source_call_ordinal: int,
    ) -> str | None:
        return self.call_tool_versions.get((source_suite_id, source_case_id, source_call_ordinal))


class InMemoryLegacyImportDecisionRepository:
    def __init__(self) -> None:
        self.decisions: dict[str, LegacyImportRoutingDecisionV1] = {}

    def put_legacy_import_routing_decision(
        self,
        decision: LegacyImportRoutingDecisionV1,
    ) -> LegacyImportRoutingDecisionV1:
        existing = self.decisions.get(decision.decision_id)
        if existing is not None and existing != decision:
            raise IntegrityViolation(
                "legacy import decision identity has conflicting content",
                decision_id=decision.decision_id,
            )
        self.decisions[decision.decision_id] = decision
        return decision

    def get_legacy_import_routing_decision(
        self,
        decision_id: str,
    ) -> LegacyImportRoutingDecisionV1 | None:
        return self.decisions.get(decision_id)


@dataclass(frozen=True, slots=True)
class PreparedLegacyImport:
    candidate: LegacyImportCandidate
    policy: LegacyImportVerificationPolicyV1
    catalog: ModelCatalogSnapshotV1
    records: tuple[CassetteRecordV1, ...]
    evidences: tuple[LegacyCassetteCallImportEvidenceV1, ...]
    manifest: LegacyCassetteRunImportManifestV1
    record_shards: tuple[CassetteBundleV1, ...]
    decisions: tuple[LegacyImportRoutingDecisionV1, ...]
    rendered_requests: tuple[tuple[str, ModelRequestV1], ...]
    expected_invocations: tuple[tuple[str, InvocationVersionBindingV1], ...]
    status: Literal["verified", "evidence_missing"]


@dataclass(frozen=True, slots=True)
class LegacyReplayCall:
    call_ordinal: int
    request: ModelRequestV1
    record: CassetteRecordV1
    routing_decision: LegacyImportRoutingDecisionV1
    invocation: InvocationVersionBindingV1
    observation: CassetteObservationViewV1
    current_transport_attempt_count: Literal[0] = 0
    current_transport_retry_count: Literal[0] = 0
    recorded_transport_attempt_count: int | None = None
    recorded_transport_retry_count: int | None = None


class VerifiedLegacyReplaySource:
    """Executable, ordinal-addressed view over a fully verified import."""

    def __init__(self, *, import_id: str, calls: tuple[LegacyReplayCall, ...]) -> None:
        self.import_id = import_id
        self._calls = {call.call_ordinal: call for call in calls}
        if len(self._calls) != len(calls):
            raise IntegrityViolation("legacy replay source repeats a call ordinal")

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def replay(
        self,
        request: ModelRequestV1,
        *,
        call_ordinal: int,
    ) -> LegacyReplayCall:
        call = self.expected_call(call_ordinal=call_ordinal)
        if call.request.model_dump(mode="json") != request.model_dump(mode="json"):
            raise IntegrityViolation(
                "verified legacy replay request differs from retained rendered request",
                call_ordinal=call_ordinal,
            )
        if call.record.request_hash != request_hash(request):
            raise IntegrityViolation(
                "verified legacy replay request hash differs",
                call_ordinal=call_ordinal,
            )
        return call

    def expected_call(self, *, call_ordinal: int) -> LegacyReplayCall:
        """Return the exact retained v1 call authority without accepting a new wire."""

        call = self._calls.get(call_ordinal)
        if call is None:
            raise IntegrityViolation(
                "verified legacy replay call ordinal is absent",
                call_ordinal=call_ordinal,
            )
        return call


@dataclass(frozen=True, slots=True)
class LegacyImportBundleTree:
    root: CassetteBundleV1
    child_bundles_by_artifact_id: Mapping[str, CassetteBundleV1]
    status: Literal["verified", "evidence_missing"]
    replay_source: VerifiedLegacyReplaySource | None


class LegacyCassetteRuntimeImporter:
    def __init__(self, authority: LegacyImportAuthority) -> None:
        self._authority = authority

    def prepare(self, candidate: LegacyImportCandidate) -> PreparedLegacyImport:
        policy = resolve_legacy_import_verification_policy(
            self._authority.verification_policy_registry,
            candidate.verification_policy,
        )
        self._require_policy_binding_sets(candidate, policy)
        self._require_policy_limits(candidate, policy)
        catalog = self._resolve_exact_model_catalog(
            candidate.model_catalog_version,
            candidate.model_catalog_digest,
        )

        authority_complete = self._verify_claimed_authorities(candidate)
        profiles_complete = all(
            self._authority.resolve_profile_binding(
                item.field_path,
                item.profile_id,
                item.profile_version,
            )
            is not None
            for item in candidate.execution_profile_bindings
        )
        profile_digests = tuple(
            compute_legacy_profile_binding_digest(item)
            for item in candidate.execution_profile_bindings
        )

        records: list[CassetteRecordV1] = []
        evidences: list[LegacyCassetteCallImportEvidenceV1] = []
        decisions: list[LegacyImportRoutingDecisionV1] = []
        rendered_requests: list[tuple[str, ModelRequestV1]] = []
        expected_invocations: list[tuple[str, InvocationVersionBindingV1]] = []
        record_shards: list[CassetteBundleV1] = []

        for call in candidate.calls:
            record = _parse_v1_wire(call.original_wire_utf8)
            records.append(record)
            request = self._authority.resolve_rendered_request(call.rendered_request_artifact_id)
            tool_version = self._authority.resolve_call_tool_version(
                candidate.source_suite_id,
                candidate.source_case_id,
                call.source_call_ordinal,
            )
            missing_fields: list[str] = []
            if request is None:
                missing_fields.append("/resolved_rendered_request")
            if not profiles_complete:
                missing_fields.append("/execution_profile_bindings")
            if tool_version is None:
                missing_fields.append("/tool_version")

            decision: LegacyImportRoutingDecisionV1 | None = None
            invocation: InvocationVersionBindingV1 | None = None
            if request is not None and profiles_complete:
                model_snapshot = canonical_model_snapshot_id(request.model_snapshot)
                if model_snapshot not in {
                    descriptor.model_snapshot for descriptor in catalog.models
                }:
                    raise IntegrityViolation(
                        "legacy import rendered request model is absent from exact catalog",
                        rendered_request_artifact_id=call.rendered_request_artifact_id,
                    )
                decision = LegacyImportRoutingDecisionV1.create(
                    source_wire_sha256=original_wire_sha256(call.original_wire_utf8),
                    request_hash=request_hash(request),
                    agent_node_id=request.agent_node_id,
                    model_snapshot=model_snapshot,
                    execution_profile_binding_digests=profile_digests,
                    model_catalog_version=catalog.catalog_version,
                    model_catalog_digest=catalog.catalog_digest,
                    verification_policy=policy.ref(),
                )
                decisions.append(decision)
            if request is not None and decision is not None and tool_version is not None:
                invocation = _build_invocation(
                    request=request,
                    decision=decision,
                    call_ordinal=call.source_call_ordinal,
                    tool_version=tool_version,
                )
                rendered_requests.append((call.rendered_request_artifact_id, request))
                expected_invocations.append((call.rendered_request_artifact_id, invocation))

            evidence_status: Literal["verified", "evidence_missing"] = (
                "verified" if not missing_fields else "evidence_missing"
            )
            try:
                evidence = LegacyCassetteCallImportEvidenceV1.create(
                    original_wire_utf8=call.original_wire_utf8,
                    rendered_request_artifact_id=(
                        call.rendered_request_artifact_id if request is not None else None
                    ),
                    request_hash=request_hash(request) if request is not None else None,
                    import_routing_decision=decision,
                    invocation=invocation,
                    source_suite_id=candidate.source_suite_id,
                    source_case_id=candidate.source_case_id,
                    source_call_ordinal=call.source_call_ordinal,
                    importer_tool_version=candidate.importer_tool_version,
                    verification_status=evidence_status,
                    missing_fields=tuple(missing_fields),
                )
            except ValueError as exc:
                raise IntegrityViolation(
                    "legacy import call evidence contradicts the original wire",
                    source_call_ordinal=call.source_call_ordinal,
                ) from exc
            evidences.append(evidence)
            record_shards.append(
                CassetteBundleV1(
                    scope="record_shard",
                    attempt_no=1,
                    ordinal=call.source_call_ordinal,
                    records=(record,),
                    legacy_call_import_evidence=evidence,
                )
            )

        frozen_tuple = self._authority.resolve_frozen_version_tuple(
            candidate.source_suite_id,
            candidate.source_case_id,
        )
        call_evidence_complete = all(
            evidence.verification_status == "verified" for evidence in evidences
        )
        frozen_tuple_complete = self._frozen_tuple_is_complete(
            frozen_tuple,
            evidences=tuple(evidences),
        )
        aggregate_complete = (
            bool(evidences)
            and authority_complete
            and call_evidence_complete
            and frozen_tuple_complete
        )
        identity: ExecutionIdentityV1 | None = None
        if aggregate_complete:
            assert frozen_tuple is not None
            identity = build_execution_identity(
                scope="run",
                bindings=tuple(
                    evidence.invocation for evidence in evidences if evidence.invocation is not None
                ),
                agent_graph_version=frozen_tuple.agent_graph_version,
            )

        manifest_status: Literal["verified", "evidence_missing"] = (
            "verified" if aggregate_complete else "evidence_missing"
        )
        try:
            manifest = build_legacy_import_manifest(
                source_suite_id=candidate.source_suite_id,
                source_case_id=candidate.source_case_id,
                verification_policy=policy.ref(),
                input_artifact_bindings=candidate.input_artifact_bindings,
                execution_profile_bindings=candidate.execution_profile_bindings,
                frozen_version_tuple=frozen_tuple if aggregate_complete else None,
                policy_bindings=candidate.policy_bindings,
                schema_bindings=candidate.schema_bindings,
                ordered_call_evidence_digests=tuple(
                    evidence.evidence_digest for evidence in evidences
                ),
                execution_identity=identity,
                importer_tool_version=candidate.importer_tool_version,
                status=manifest_status,
            )
        except ValueError as exc:
            raise IntegrityViolation(
                "legacy import aggregate identity contradicts authoritative versions"
            ) from exc

        return PreparedLegacyImport(
            candidate=candidate,
            policy=policy,
            catalog=catalog,
            records=tuple(records),
            evidences=tuple(evidences),
            manifest=manifest,
            record_shards=tuple(record_shards),
            decisions=tuple(decisions),
            rendered_requests=tuple(rendered_requests),
            expected_invocations=tuple(expected_invocations),
            status=manifest_status,
        )

    def finalize(
        self,
        prepared: PreparedLegacyImport,
        *,
        record_shard_artifact_ids: tuple[str, ...],
        attempt_bundle_artifact_id: str,
        decision_repository: LegacyImportDecisionRepository,
    ) -> LegacyImportBundleTree:
        if len(record_shard_artifact_ids) != len(prepared.record_shards):
            raise IntegrityViolation("legacy import shard artifact IDs do not match call count")
        if len(record_shard_artifact_ids) != len(set(record_shard_artifact_ids)):
            raise IntegrityViolation("legacy import shard artifact IDs must be unique")
        if not attempt_bundle_artifact_id:
            raise IntegrityViolation("legacy import attempt bundle artifact ID is empty")

        attempt = CassetteBundleV1(
            scope="attempt",
            attempt_no=1,
            child_bundle_artifact_ids=record_shard_artifact_ids,
        )
        root = CassetteBundleV1(
            scope="run",
            child_bundle_artifact_ids=(attempt_bundle_artifact_id,),
            legacy_run_import_manifest=prepared.manifest,
        )
        children = {
            **dict(zip(record_shard_artifact_ids, prepared.record_shards, strict=True)),
            attempt_bundle_artifact_id: attempt,
        }
        if prepared.status != prepared.manifest.status:
            raise IntegrityViolation("legacy import prepared status differs from manifest status")
        rendered = dict(prepared.rendered_requests)
        invocations = dict(prepared.expected_invocations)
        validate_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=self._authority.verification_policy_registry,
            model_catalog=prepared.catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=invocations,
        )

        if prepared.status == "evidence_missing":
            return LegacyImportBundleTree(
                root=root,
                child_bundles_by_artifact_id=children,
                status="evidence_missing",
                replay_source=None,
            )

        require_verified_legacy_import_bundle_tree(
            root,
            children,
            policy_registry=self._authority.verification_policy_registry,
            model_catalog=prepared.catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=invocations,
        )
        manifest, replay_calls = self._load_verified_replay_calls(
            root=root,
            child_bundles_by_artifact_id=children,
            model_catalog_version=prepared.catalog.catalog_version,
            model_catalog_digest=prepared.catalog.catalog_digest,
        )
        decisions = tuple(call.routing_decision for call in replay_calls)
        for decision in decisions:
            retained = decision_repository.get_legacy_import_routing_decision(decision.decision_id)
            if retained is not None and retained != decision:
                raise IntegrityViolation(
                    "legacy import decision identity has conflicting retained content",
                    decision_id=decision.decision_id,
                )
        for decision in decisions:
            persisted = decision_repository.put_legacy_import_routing_decision(decision)
            if persisted != decision:
                raise IntegrityViolation(
                    "legacy import decision repository returned different content",
                    decision_id=decision.decision_id,
                )
            retained = decision_repository.get_legacy_import_routing_decision(decision.decision_id)
            if retained != decision:
                raise IntegrityViolation(
                    "legacy import decision was not retained exactly",
                    decision_id=decision.decision_id,
                )

        source = VerifiedLegacyReplaySource(
            import_id=manifest.import_id,
            calls=replay_calls,
        )
        return LegacyImportBundleTree(
            root=root,
            child_bundles_by_artifact_id=children,
            status="verified",
            replay_source=source,
        )

    def read_verified(
        self,
        *,
        root: CassetteBundleV1,
        child_bundles_by_artifact_id: Mapping[str, CassetteBundleV1],
        model_catalog_version: int,
        model_catalog_digest: str,
        decision_repository: LegacyImportDecisionRepository,
    ) -> VerifiedLegacyReplaySource:
        manifest, calls = self._load_verified_replay_calls(
            root=root,
            child_bundles_by_artifact_id=child_bundles_by_artifact_id,
            model_catalog_version=model_catalog_version,
            model_catalog_digest=model_catalog_digest,
        )
        for call in calls:
            decision = call.routing_decision
            retained = decision_repository.get_legacy_import_routing_decision(decision.decision_id)
            if retained != decision:
                raise IntegrityViolation(
                    "verified legacy import routing decision is not retained",
                    decision_id=decision.decision_id,
                )
        return VerifiedLegacyReplaySource(import_id=manifest.import_id, calls=calls)

    def _load_verified_replay_calls(
        self,
        *,
        root: CassetteBundleV1,
        child_bundles_by_artifact_id: Mapping[str, CassetteBundleV1],
        model_catalog_version: int,
        model_catalog_digest: str,
    ) -> tuple[LegacyCassetteRunImportManifestV1, tuple[LegacyReplayCall, ...]]:
        manifest = root.legacy_run_import_manifest
        if manifest is None or manifest.status != "verified":
            raise IntegrityViolation("legacy cassette import is not executable")
        policy = resolve_legacy_import_verification_policy(
            self._authority.verification_policy_registry,
            manifest.verification_policy,
        )
        catalog = self._resolve_exact_model_catalog(
            model_catalog_version,
            model_catalog_digest,
        )
        self._verify_manifest_authorities(manifest, policy)

        if len(root.child_bundle_artifact_ids) != 1:
            raise IntegrityViolation("legacy import requires one synthetic attempt bundle")
        attempt = child_bundles_by_artifact_id.get(root.child_bundle_artifact_ids[0])
        if attempt is None:
            raise IntegrityViolation("legacy import attempt bundle is not retained")
        rendered: dict[str, ModelRequestV1] = {}
        invocations: dict[str, InvocationVersionBindingV1] = {}
        ordered_evidences: list[LegacyCassetteCallImportEvidenceV1] = []
        for shard_id in attempt.child_bundle_artifact_ids:
            shard = child_bundles_by_artifact_id.get(shard_id)
            if shard is None or shard.legacy_call_import_evidence is None:
                raise IntegrityViolation("legacy import record shard is not retained")
            evidence = shard.legacy_call_import_evidence
            artifact_id = evidence.rendered_request_artifact_id
            decision = evidence.import_routing_decision
            if artifact_id is None or decision is None:
                raise IntegrityViolation("verified legacy call has incomplete evidence")
            request = self._authority.resolve_rendered_request(artifact_id)
            tool_version = self._authority.resolve_call_tool_version(
                manifest.source_suite_id,
                manifest.source_case_id,
                evidence.source_call_ordinal,
            )
            if request is None or tool_version is None:
                raise IntegrityViolation(
                    "verified legacy call authority is no longer retained",
                    source_call_ordinal=evidence.source_call_ordinal,
                )
            expected = _build_invocation(
                request=request,
                decision=decision,
                call_ordinal=evidence.source_call_ordinal,
                tool_version=tool_version,
            )
            rendered[artifact_id] = request
            invocations[artifact_id] = expected
            ordered_evidences.append(evidence)

        if not self._frozen_tuple_is_complete(
            manifest.frozen_version_tuple,
            evidences=tuple(ordered_evidences),
        ):
            raise IntegrityViolation(
                "verified legacy import frozen execution versions are incomplete"
            )

        records = require_verified_legacy_import_bundle_tree(
            root,
            child_bundles_by_artifact_id,
            policy_registry=self._authority.verification_policy_registry,
            model_catalog=catalog,
            rendered_requests_by_artifact_id=rendered,
            expected_invocations_by_artifact_id=invocations,
        )
        calls: list[LegacyReplayCall] = []
        for evidence, record in zip(ordered_evidences, records, strict=True):
            decision = evidence.import_routing_decision
            invocation = evidence.invocation
            artifact_id = evidence.rendered_request_artifact_id
            assert decision is not None and invocation is not None and artifact_id is not None
            raw_payload = json.loads(evidence.original_wire_utf8)
            observation = cassette_observation_view(record, raw_payload=raw_payload)
            calls.append(
                LegacyReplayCall(
                    call_ordinal=evidence.source_call_ordinal,
                    request=rendered[artifact_id],
                    record=record,
                    routing_decision=decision,
                    invocation=invocation,
                    observation=observation,
                    recorded_transport_attempt_count=(observation.transport_attempt_count),
                    recorded_transport_retry_count=(observation.transport_retry_count),
                )
            )
        return manifest, tuple(calls)

    def _verify_claimed_authorities(self, candidate: LegacyImportCandidate) -> bool:
        return self._verify_binding_authorities(
            input_artifact_bindings=candidate.input_artifact_bindings,
            execution_profile_bindings=candidate.execution_profile_bindings,
            policy_bindings=candidate.policy_bindings,
            schema_bindings=candidate.schema_bindings,
        )

    def _verify_binding_authorities(
        self,
        *,
        input_artifact_bindings: tuple[LegacyCassetteInputBindingV1, ...],
        execution_profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...],
        policy_bindings: tuple[LegacyCassettePolicyBindingV1, ...],
        schema_bindings: tuple[LegacyCassetteSchemaBindingV1, ...],
    ) -> bool:
        complete = True
        for claim in input_artifact_bindings:
            resolved = self._authority.resolve_input_binding(
                claim.binding_key,
                claim.artifact_id,
            )
            complete = _compare_authority(
                resolved,
                claim,
                label="input binding",
                complete=complete,
            )
        for claim in execution_profile_bindings:
            resolved = self._authority.resolve_profile_binding(
                claim.field_path,
                claim.profile_id,
                claim.profile_version,
            )
            complete = _compare_authority(
                resolved,
                claim,
                label="profile binding",
                complete=complete,
            )
        for claim in policy_bindings:
            resolved = self._authority.resolve_policy_binding(
                claim.binding_key,
                claim.policy_kind,
                claim.policy_id,
                claim.policy_version,
            )
            complete = _compare_authority(
                resolved,
                claim,
                label="policy binding",
                complete=complete,
            )
        for claim in schema_bindings:
            resolved = self._authority.resolve_schema_binding(
                claim.binding_key,
                claim.schema_id,
            )
            complete = _compare_authority(
                resolved,
                claim,
                label="schema binding",
                complete=complete,
            )
        return complete

    def _verify_manifest_authorities(
        self,
        manifest: LegacyCassetteRunImportManifestV1,
        policy: LegacyImportVerificationPolicyV1,
    ) -> None:
        self._require_binding_sets(
            input_artifact_bindings=manifest.input_artifact_bindings,
            execution_profile_bindings=manifest.execution_profile_bindings,
            policy_bindings=manifest.policy_bindings,
            schema_bindings=manifest.schema_bindings,
            policy=policy,
        )
        if not self._verify_binding_authorities(
            input_artifact_bindings=manifest.input_artifact_bindings,
            execution_profile_bindings=manifest.execution_profile_bindings,
            policy_bindings=manifest.policy_bindings,
            schema_bindings=manifest.schema_bindings,
        ):
            raise IntegrityViolation("verified legacy import manifest authority is not retained")
        frozen = self._authority.resolve_frozen_version_tuple(
            manifest.source_suite_id,
            manifest.source_case_id,
        )
        if frozen is None or frozen != manifest.frozen_version_tuple:
            raise IntegrityViolation("verified legacy import frozen version authority differs")

    def _resolve_exact_model_catalog(
        self,
        catalog_version: int,
        catalog_digest: str,
    ) -> ModelCatalogSnapshotV1:
        catalog = self._authority.resolve_model_catalog(
            catalog_version,
            catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation("legacy import exact model catalog is not retained")
        if catalog.catalog_version != catalog_version or catalog.catalog_digest != catalog_digest:
            raise IntegrityViolation(
                "legacy import model catalog resolver returned a different snapshot"
            )
        return catalog

    @staticmethod
    def _require_policy_limits(
        candidate: LegacyImportCandidate,
        policy: LegacyImportVerificationPolicyV1,
    ) -> None:
        if len(candidate.calls) > policy.max_calls_per_import:
            raise IntegrityViolation("legacy import call count exceeds verification policy")
        for call in candidate.calls:
            try:
                wire_size = len(call.original_wire_utf8.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise IntegrityViolation("legacy import wire is not valid UTF-8") from exc
            if wire_size > policy.max_wire_bytes_per_call:
                raise IntegrityViolation(
                    "legacy call original wire exceeds verification policy",
                    source_call_ordinal=call.source_call_ordinal,
                )

    @staticmethod
    def _frozen_tuple_is_complete(
        frozen_tuple: VersionTuple | None,
        *,
        evidences: tuple[LegacyCassetteCallImportEvidenceV1, ...],
    ) -> bool:
        if (
            frozen_tuple is None
            or frozen_tuple.agent_graph_version is None
            or frozen_tuple.tool_version is None
        ):
            return False
        invocations = tuple(evidence.invocation for evidence in evidences)
        if any(invocation is None for invocation in invocations):
            return False
        if any(
            invocation.tool_version != frozen_tuple.tool_version
            for invocation in invocations
            if invocation is not None
        ):
            raise IntegrityViolation(
                "legacy import invocation tool version differs from frozen version tuple"
            )
        return True

    @staticmethod
    def _require_policy_binding_sets(
        candidate: LegacyImportCandidate,
        policy: LegacyImportVerificationPolicyV1,
    ) -> None:
        LegacyCassetteRuntimeImporter._require_binding_sets(
            input_artifact_bindings=candidate.input_artifact_bindings,
            execution_profile_bindings=candidate.execution_profile_bindings,
            policy_bindings=candidate.policy_bindings,
            schema_bindings=candidate.schema_bindings,
            policy=policy,
        )

    @staticmethod
    def _require_binding_sets(
        *,
        input_artifact_bindings: tuple[LegacyCassetteInputBindingV1, ...],
        execution_profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...],
        policy_bindings: tuple[LegacyCassettePolicyBindingV1, ...],
        schema_bindings: tuple[LegacyCassetteSchemaBindingV1, ...],
        policy: LegacyImportVerificationPolicyV1,
    ) -> None:
        actual_expected = (
            (
                tuple(item.binding_key for item in input_artifact_bindings),
                policy.required_input_binding_keys,
                "input binding keys",
            ),
            (
                tuple(item.field_path for item in execution_profile_bindings),
                policy.required_profile_field_paths,
                "profile binding paths",
            ),
            (
                tuple(item.binding_key for item in policy_bindings),
                policy.required_policy_binding_keys,
                "policy binding keys",
            ),
            (
                tuple(item.binding_key for item in schema_bindings),
                policy.required_schema_binding_keys,
                "schema binding keys",
            ),
        )
        for actual, expected, label in actual_expected:
            if actual != expected:
                raise IntegrityViolation(f"legacy import {label} do not match verification policy")


def _compare_authority(
    resolved: object | None,
    claim: object,
    *,
    label: str,
    complete: bool,
) -> bool:
    if resolved is None:
        return False
    if resolved != claim:
        raise IntegrityViolation(f"legacy import {label} differs from authority")
    return complete


def _parse_v1_wire(original_wire_utf8: str) -> CassetteRecordV1:
    try:
        payload = json.loads(original_wire_utf8)
        if not isinstance(payload, dict):
            raise ValueError("wire must contain an object")
        record = parse_cassette_record(payload)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrityViolation("legacy import wire is invalid") from exc
    if not isinstance(record, CassetteRecordV1) or isinstance(record, CassetteRecordV2):
        raise IntegrityViolation("legacy import wire is not cassette@1")
    return record


def _build_invocation(
    *,
    request: ModelRequestV1,
    decision: LegacyImportRoutingDecisionV1,
    call_ordinal: int,
    tool_version: str,
) -> InvocationVersionBindingV1:
    try:
        return InvocationVersionBindingV1(
            attempt_no=1,
            call_ordinal=call_ordinal,
            route_ordinal=1,
            transport_attempt=None,
            routing_decision_kind="legacy_import",
            routing_decision_id=decision.decision_id,
            agent_node_id=request.agent_node_id,
            prompt_version=request.prompt_version,
            model_snapshot=decision.model_snapshot,
            tool_version=tool_version,
            execution_source="cassette_replay",
            response_consumed=True,
        )
    except ValueError as exc:
        raise IntegrityViolation(
            "legacy import authoritative invocation versions are invalid",
            call_ordinal=call_ordinal,
        ) from exc


__all__ = [
    "InMemoryLegacyImportAuthority",
    "InMemoryLegacyImportDecisionRepository",
    "LegacyCassetteRuntimeImporter",
    "LegacyImportAuthority",
    "LegacyImportBundleTree",
    "LegacyImportCallCandidate",
    "LegacyImportCandidate",
    "LegacyImportDecisionRepository",
    "LegacyReplayCall",
    "PreparedLegacyImport",
    "VerifiedLegacyReplaySource",
]
