"""Fail-closed admission proof for an exact run-scoped REPLAY cassette.

The ordinary Run admission path verifies the RunKind, payload schema, profiles,
domain and budgets.  This module owns the additional authority boundary that is
specific to ``llm_execution_mode=replay``:

* every cassette Artifact and byte payload is content/hash/schema checked;
* the run -> attempt -> record-shard tree closes over child IDs and lineage;
* native bundles resolve to the exact retained terminal RECORD Run; and
* imported legacy bundles are executable only through the retained legacy
  verification authorities (never from the self-asserted manifest alone).

The returned proof deliberately exposes a second RBAC requirement.  Callers
must authorize ``replay`` on ``run`` in the already-derived resource domain in
addition to the RunKind's ordinary permission before creating the Run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.cassette import CassetteRecordV1, CassetteRecordV2
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.config_export import (
    MAX_CONFIG_EXPORT_MANIFEST_BYTES,
    MAX_CONFIG_EXPORT_PACKAGE_BYTES,
)
from gameforge.contracts.errors import DependencyUnavailable, IntegrityViolation
from gameforge.contracts.identity import DomainScopeValue, Permission
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    RunIntermediateArtifactLinkV1,
    RunAttempt,
    RunFailureV1,
    RunKindRef,
    RunModelResponseConsumptionV1,
    RunModelRouteLinkV1,
    RunPayloadEnvelope,
    RunRecord,
    RunResultV1,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    VersionTuple,
)
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    parse_model_request,
    request_hash,
)
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.runtime.cassette.legacy_import import (
    LegacyCassetteRuntimeImporter,
    LegacyImportAuthority,
    LegacyImportDecisionRepository,
)


MAX_REPLAY_ARTIFACT_BYTES = (
    MAX_CONFIG_EXPORT_PACKAGE_BYTES + MAX_CONFIG_EXPORT_MANIFEST_BYTES + 16 * 1024 * 1024
)
MAX_REPLAY_TREE_NODES = 8192
MAX_REPLAY_TREE_BYTES = 512 * 1024 * 1024
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
_CLOSED_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out", "lease_expired"}
)
_RUN_KIND_BY_PAYLOAD_SCHEMA: dict[str, tuple[str, int]] = {
    "generation-propose@1": ("generation.propose", 1),
    "patch-repair@1": ("patch.repair", 1),
    "constraint-proposal-propose@1": ("constraint_proposal.propose", 1),
    "review-run@1": ("review.run", 1),
    "checker-run@1": ("checker.run", 1),
    "simulation-run@1": ("simulation.run", 1),
    "task-suite-derive@1": ("task_suite.derive", 1),
    "playtest-run@1": ("playtest.run", 1),
    "patch-validation@1": ("patch.validate", 1),
    "constraint-validation@1": ("constraint_proposal.validate", 1),
    "rollback-validation@1": ("rollback.validate", 1),
    "bench-run@1": ("bench.run", 1),
    "artifact-migration@1": ("artifact.migrate", 1),
    "dr-drill@1": ("dr.drill", 1),
}


class ReplayAdmissionReader(Protocol):
    """Small retained-state surface needed to prove a cassette before Run create."""

    def get_artifact(self, artifact_id: str) -> ArtifactV2 | None: ...

    def read_artifact_bytes(self, artifact_id: str) -> bytes: ...

    def get_run(self, run_id: str) -> RunRecord | None: ...

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None: ...

    def get_prompt_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None: ...

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None: ...

    def get_model_route_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelRouteLinkV1 | None: ...

    def get_model_response_consumption(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
        route_ordinal: int,
    ) -> RunModelResponseConsumptionV1 | None: ...


@dataclass(frozen=True, slots=True)
class ReplayAdmissionProof:
    cassette_artifact_id: str
    source_kind: Literal["native", "legacy_import"]
    source_run_id: str | None
    legacy_import_id: str | None
    attempt_count: int
    record_count: int
    selected_source_attempt_no: int | None

    def __post_init__(self) -> None:
        expected = self.attempt_count if self.attempt_count > 0 else None
        if self.selected_source_attempt_no != expected:
            raise ValueError("replay source selection must be the terminal cassette attempt")

    def required_permission(self, domain_scope: DomainScopeValue) -> Permission:
        """Return the extra permission admission must authorize for this proof."""

        return Permission(
            action="replay",
            resource_kind="run",
            domain_scope=domain_scope,
        )


@dataclass(frozen=True, slots=True)
class ReplayExecutionProfileAuthority:
    """Cassette-derived profile-catalog authority used before Run construction."""

    source_kind: Literal["native", "legacy_import"]
    catalog_version: int
    catalog_digest: str


@dataclass(frozen=True, slots=True)
class _BundleNode:
    artifact: ArtifactV2
    payload: CassetteBundleV1


@dataclass(frozen=True, slots=True)
class _AttemptNode:
    aggregate: _BundleNode
    shards: tuple[_BundleNode, ...]


@dataclass(frozen=True, slots=True)
class _BundleTree:
    root: _BundleNode
    attempts: tuple[_AttemptNode, ...]

    @property
    def child_payloads(self) -> dict[str, CassetteBundleV1]:
        result: dict[str, CassetteBundleV1] = {}
        for attempt in self.attempts:
            result[attempt.aggregate.artifact.artifact_id] = attempt.aggregate.payload
            result.update((shard.artifact.artifact_id, shard.payload) for shard in attempt.shards)
        return result

    @property
    def record_count(self) -> int:
        return sum(len(attempt.shards) for attempt in self.attempts)


class ReplayAdmissionValidator:
    """Validate one prospective REPLAY payload against retained authority."""

    def __init__(
        self,
        reader: ReplayAdmissionReader,
        *,
        legacy_authority: LegacyImportAuthority | None = None,
        legacy_decisions: LegacyImportDecisionRepository | None = None,
    ) -> None:
        self._reader = reader
        self._legacy_authority = legacy_authority
        self._legacy_decisions = legacy_decisions

    def validate(
        self,
        *,
        kind: RunKindRef,
        payload: RunPayloadEnvelope,
    ) -> ReplayAdmissionProof:
        expected_kind = _RUN_KIND_BY_PAYLOAD_SCHEMA.get(payload.payload_schema_version)
        if expected_kind != (kind.kind, kind.version):
            raise IntegrityViolation("replay Run kind differs from its typed payload schema")
        cassette_artifact_id = self._require_replay_payload(payload)
        tree = self._load_tree(cassette_artifact_id)
        if payload.version_tuple.cassette_id != f"sha256:{tree.root.artifact.payload_hash}":
            raise IntegrityViolation("replay VersionTuple does not bind the exact cassette")
        root = tree.root.payload
        if root.run_id is not None:
            return self._validate_native(kind=kind, payload=payload, tree=tree)
        return self._validate_legacy(payload=payload, tree=tree)

    def resolve_execution_profile_catalog(
        self,
        *,
        kind: RunKindRef,
        cassette_artifact_id: str,
    ) -> tuple[int, str]:
        """Resolve the cassette-authoritative profile catalog for admission.

        A REPLAY request cannot be resolved against the mutable current catalog:
        native replay must retain the source RECORD Run's exact catalog, while a
        verified legacy import must retain the one catalog named by every imported
        profile binding.  This preflight authenticates the content-addressed bundle
        tree and derives that reference from retained authority before admission
        resolves any profile.  :meth:`validate` still performs the complete replay
        proof before the Run is created.
        """

        authority = self.resolve_execution_profile_authority(
            kind=kind,
            cassette_artifact_id=cassette_artifact_id,
        )
        return authority.catalog_version, authority.catalog_digest

    def resolve_execution_profile_authority(
        self,
        *,
        kind: RunKindRef,
        cassette_artifact_id: str,
    ) -> ReplayExecutionProfileAuthority:
        """Return both the exact historical catalog and its replay source branch."""

        tree = self._load_tree(cassette_artifact_id)
        if tree.root.payload.run_id is not None:
            source = self._require_native_source(kind=kind, tree=tree)
            return ReplayExecutionProfileAuthority(
                source_kind="native",
                catalog_version=source.payload.execution_profile_catalog_version,
                catalog_digest=source.payload.execution_profile_catalog_digest,
            )

        manifest = tree.root.payload.legacy_run_import_manifest
        if manifest is None:
            raise IntegrityViolation("legacy replay root lacks its import manifest")
        if manifest.status != "verified":
            raise IntegrityViolation("legacy replay import is not verified")
        if self._legacy_authority is None:
            raise DependencyUnavailable(
                "legacy replay verification authority is unavailable",
                component="legacy_import_authority",
            )
        if self._legacy_decisions is None:
            raise DependencyUnavailable(
                "legacy replay decision authority is unavailable",
                component="legacy_import_decisions",
            )
        catalog_refs = {
            (binding.catalog_version, binding.catalog_digest)
            for binding in manifest.execution_profile_bindings
        }
        if len(catalog_refs) != 1:
            raise IntegrityViolation(
                "verified legacy replay must bind exactly one execution-profile catalog"
            )
        catalog_version, catalog_digest = next(iter(catalog_refs))
        return ReplayExecutionProfileAuthority(
            source_kind="legacy_import",
            catalog_version=catalog_version,
            catalog_digest=catalog_digest,
        )

    def _require_replay_payload(self, payload: RunPayloadEnvelope) -> str:
        if payload.llm_execution_mode != "replay":
            raise IntegrityViolation("replay validator requires llm_execution_mode=replay")
        if payload.execution_version_plan is None or payload.cassette_artifact_id is None:
            raise IntegrityViolation("replay payload lacks its exact plan or cassette")
        if payload.cassette_artifact_id not in payload.input_artifact_ids:
            raise IntegrityViolation("replay cassette is absent from the exact input set")
        if payload.seed != payload.version_tuple.seed:
            raise IntegrityViolation("replay payload seed differs from its VersionTuple")
        self._require_schema_binding_closure(payload, label="replay")
        self._require_profile_catalog_closure(payload, label="replay")
        return payload.cassette_artifact_id

    def _load_tree(self, root_artifact_id: str) -> _BundleTree:
        node_count = 0
        total_bytes = 0

        def load(artifact_id: str) -> _BundleNode:
            nonlocal node_count, total_bytes
            artifact = self._require_artifact(artifact_id, label="cassette")
            node_count += 1
            total_bytes += artifact.object_ref.size_bytes
            if node_count > MAX_REPLAY_TREE_NODES:
                raise IntegrityViolation("replay cassette tree exceeds the node limit")
            if total_bytes > MAX_REPLAY_TREE_BYTES:
                raise IntegrityViolation("replay cassette tree exceeds the aggregate byte limit")
            return self._load_bundle(artifact)

        root = load(root_artifact_id)
        if root.payload.scope != "run":
            raise IntegrityViolation("replay cassette root must have run scope")
        self._require_aggregate_lineage(root)

        visited = {root_artifact_id}
        attempts: list[_AttemptNode] = []
        previous_attempt_no = 0
        for attempt_id in root.payload.child_bundle_artifact_ids:
            if attempt_id in visited:
                raise IntegrityViolation("replay cassette tree repeats a child Artifact")
            visited.add(attempt_id)
            attempt = load(attempt_id)
            if attempt.payload.scope != "attempt" or attempt.payload.attempt_no is None:
                raise IntegrityViolation("run cassette child is not an attempt bundle")
            if attempt.payload.attempt_no <= previous_attempt_no:
                raise IntegrityViolation("attempt bundles are not in canonical attempt order")
            previous_attempt_no = attempt.payload.attempt_no
            if attempt.payload.run_id != root.payload.run_id:
                raise IntegrityViolation("attempt bundle run identity differs from its root")
            self._require_aggregate_lineage(attempt)

            shards: list[_BundleNode] = []
            previous_ordinal = 0
            for shard_id in attempt.payload.child_bundle_artifact_ids:
                if shard_id in visited:
                    raise IntegrityViolation("replay cassette tree repeats a child Artifact")
                visited.add(shard_id)
                shard = load(shard_id)
                if (
                    shard.payload.scope != "record_shard"
                    or shard.payload.attempt_no != attempt.payload.attempt_no
                    or shard.payload.run_id != root.payload.run_id
                    or shard.payload.ordinal is None
                ):
                    raise IntegrityViolation("record shard identity differs from its attempt")
                if shard.payload.ordinal <= previous_ordinal:
                    raise IntegrityViolation("record shards are not in canonical call order")
                previous_ordinal = shard.payload.ordinal
                shards.append(shard)
            attempts.append(_AttemptNode(aggregate=attempt, shards=tuple(shards)))

        attempt_numbers = tuple(item.aggregate.payload.attempt_no for item in attempts)
        if root.payload.run_id is None:
            if attempt_numbers != (1,):
                raise IntegrityViolation("legacy replay requires one synthetic attempt")
        elif attempt_numbers and attempt_numbers != tuple(range(1, len(attempt_numbers) + 1)):
            raise IntegrityViolation("native replay attempt numbers must be contiguous")
        return _BundleTree(root=root, attempts=tuple(attempts))

    def _load_bundle(self, artifact: ArtifactV2) -> _BundleNode:
        if artifact.kind != "cassette_bundle":
            raise IntegrityViolation("replay tree contains a non-cassette Artifact")
        blob = self._read_exact_blob(artifact, label="cassette Artifact bytes")
        try:
            decoded = json.loads(blob.decode("utf-8"))
            payload = CassetteBundleV1.model_validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise IntegrityViolation("cassette Artifact is not canonical CassetteBundleV1") from exc
        canonical = canonical_json(payload.model_dump(mode="json")).encode("utf-8")
        if canonical != blob:
            raise IntegrityViolation("cassette Artifact bytes are not canonical CassetteBundleV1")
        expected_schema = (
            "cassette-record-shard@1" if payload.scope == "record_shard" else "cassette-bundle@1"
        )
        if artifact.meta.get("payload_schema_id") != expected_schema:
            raise IntegrityViolation("cassette Artifact schema does not match its bundle scope")
        if artifact.version_tuple.tool_version is None:
            raise IntegrityViolation("cassette Artifact lacks its producer tool version")
        if artifact.version_tuple.cassette_id != f"sha256:{artifact.payload_hash}":
            raise IntegrityViolation("cassette Artifact VersionTuple is not content-bound")
        if any(
            getattr(artifact.version_tuple, field_name) is not None
            for field_name in (
                "doc_version",
                "ir_snapshot_id",
                "constraint_snapshot_id",
                "env_contract_version",
                "seed",
            )
        ):
            raise IntegrityViolation(
                "cassette Artifact contains an inapplicable VersionTuple field"
            )
        identity = self._execution_identity(artifact)
        if identity is None and any(
            getattr(artifact.version_tuple, field_name) is not None
            for field_name in ("prompt_version", "model_snapshot", "agent_graph_version")
        ):
            raise IntegrityViolation("cassette Artifact claims model versions without identity")
        if identity is not None:
            expected_identity_tuple = {
                "prompt_version": identity.prompt_projection.tuple_value,
                "model_snapshot": identity.model_projection.tuple_value,
                "agent_graph_version": identity.agent_graph_version,
            }
            if any(
                getattr(artifact.version_tuple, field_name) != expected_value
                for field_name, expected_value in expected_identity_tuple.items()
            ):
                raise IntegrityViolation(
                    "cassette Artifact VersionTuple differs from execution identity"
                )
        return _BundleNode(artifact=artifact, payload=payload)

    @staticmethod
    def _require_aggregate_lineage(node: _BundleNode) -> None:
        expected = tuple(sorted(node.payload.child_bundle_artifact_ids))
        if node.artifact.lineage != expected:
            raise IntegrityViolation("cassette aggregate lineage differs from its child IDs")

    def _validate_native(
        self,
        *,
        kind: RunKindRef,
        payload: RunPayloadEnvelope,
        tree: _BundleTree,
    ) -> ReplayAdmissionProof:
        source = self._require_native_source(kind=kind, tree=tree)
        source_run_id = source.run_id

        new_inputs = self._non_cassette_inputs(payload, tree.root.artifact.artifact_id)
        if source.payload.input_artifact_ids != new_inputs:
            raise IntegrityViolation("native replay input Artifact IDs differ from source")
        for artifact_id in new_inputs:
            artifact = self._require_artifact(artifact_id, label="input")
            self._read_exact_blob(artifact, label="input Artifact bytes")

        if source.payload.params != payload.params:
            raise IntegrityViolation("native replay params differ from source")
        if source.payload.execution_version_plan != payload.execution_version_plan:
            raise IntegrityViolation("native replay execution plan differs from source")
        if (
            source.payload.execution_profile_catalog_version
            != payload.execution_profile_catalog_version
            or source.payload.execution_profile_catalog_digest
            != payload.execution_profile_catalog_digest
        ):
            raise IntegrityViolation("native replay profile catalog differs from source")
        if source.payload.resolved_profiles != payload.resolved_profiles:
            raise IntegrityViolation("native replay resolved profiles differ from source")
        if source.payload.policy_bindings != payload.policy_bindings:
            raise IntegrityViolation("native replay policy bindings differ from source")
        if source.payload.schema_bindings != payload.schema_bindings:
            raise IntegrityViolation("native replay schema bindings differ from source")
        if source.payload.resolved_policy_snapshots != payload.resolved_policy_snapshots:
            raise IntegrityViolation("native replay resolved-policy snapshots differ from source")
        if (
            source.payload.seed != payload.seed
            or source.payload.seed != source.payload.version_tuple.seed
        ):
            raise IntegrityViolation("native replay seed differs from source")
        self._require_tuple_equal_except_cassette(
            source.payload.version_tuple,
            payload.version_tuple,
            label="native replay VersionTuple",
        )
        self._validate_native_source_attempts(tree=tree, source=source)
        self._validate_native_outcome(tree=tree, source=source)
        self._validate_native_identity(tree=tree, source=source)
        return ReplayAdmissionProof(
            cassette_artifact_id=tree.root.artifact.artifact_id,
            source_kind="native",
            source_run_id=source_run_id,
            legacy_import_id=None,
            attempt_count=len(tree.attempts),
            record_count=tree.record_count,
            selected_source_attempt_no=source.current_attempt_no,
        )

    def _require_native_source(
        self,
        *,
        kind: RunKindRef,
        tree: _BundleTree,
    ) -> RunRecord:
        source_run_id = tree.root.payload.run_id
        assert source_run_id is not None
        source = self._reader.get_run(source_run_id)
        if not isinstance(source, RunRecord):
            raise IntegrityViolation("native replay source Run is not retained")
        if source.status not in _TERMINAL_RUN_STATUSES:
            raise IntegrityViolation("native replay source Run is not terminal")
        if source.payload.llm_execution_mode != "record":
            raise IntegrityViolation("native replay source is not a RECORD Run")
        if source.payload.version_tuple.cassette_id is not None:
            raise IntegrityViolation("native RECORD source has a pre-bound cassette VersionTuple")
        self._require_schema_binding_closure(source.payload, label="native source")
        self._require_profile_catalog_closure(source.payload, label="native source")
        if source.terminal_cassette_artifact_id != tree.root.artifact.artifact_id:
            raise IntegrityViolation("native source terminal cassette binding differs")
        if source.kind != kind:
            raise IntegrityViolation("native replay Run kind differs from its source")
        return source

    def _validate_native_source_attempts(
        self,
        *,
        tree: _BundleTree,
        source: RunRecord,
    ) -> None:
        if not tree.attempts:
            if (
                source.status not in {"failed", "cancelled", "timed_out"}
                or source.current_attempt_no is not None
                or source.next_attempt_no != 1
                or source.next_fencing_token != 1
            ):
                raise IntegrityViolation(
                    "native zero-attempt cassette differs from the source Run head"
                )
            return
        final_attempt_no = tree.attempts[-1].aggregate.payload.attempt_no
        assert final_attempt_no is not None
        if (
            source.current_attempt_no != final_attempt_no
            or source.next_attempt_no != final_attempt_no + 1
            or final_attempt_no > source.max_attempts
        ):
            raise IntegrityViolation("native cassette attempts differ from the source Run head")
        for attempt_node in tree.attempts:
            attempt_no = attempt_node.aggregate.payload.attempt_no
            assert attempt_no is not None
            retained = self._reader.get_attempt(source.run_id, attempt_no)
            max_record_ordinal = max(
                (shard.payload.ordinal or 0 for shard in attempt_node.shards),
                default=0,
            )
            attempt_identity = self._execution_identity(attempt_node.aggregate.artifact)
            max_identity_ordinal = max(
                (
                    binding.call_ordinal
                    for binding in (() if attempt_identity is None else attempt_identity.bindings)
                ),
                default=0,
            )
            if (
                not isinstance(retained, RunAttempt)
                or retained.run_id != source.run_id
                or retained.attempt_no != attempt_no
                or retained.status not in _CLOSED_ATTEMPT_STATUSES
                or retained.cassette_bundle_artifact_id
                != attempt_node.aggregate.artifact.artifact_id
                or retained.next_call_ordinal <= max(max_record_ordinal, max_identity_ordinal)
            ):
                raise IntegrityViolation(
                    "native cassette attempt differs from retained RunAttempt authority"
                )

    def _validate_native_outcome(self, *, tree: _BundleTree, source: RunRecord) -> None:
        manifest_artifact_id = (
            source.result_artifact_id
            if source.status == "succeeded"
            else source.failure_artifact_id
        )
        if manifest_artifact_id is None:
            raise IntegrityViolation("native source Run lacks its terminal manifest")
        manifest_artifact = self._require_artifact(
            manifest_artifact_id,
            label="source terminal manifest",
        )
        expected_kind = "run_result" if source.status == "succeeded" else "run_failure"
        expected_schema = "run-result@1" if source.status == "succeeded" else "run-failure@1"
        if (
            manifest_artifact.kind != expected_kind
            or manifest_artifact.meta.get("payload_schema_id") != expected_schema
        ):
            raise IntegrityViolation("native source terminal manifest kind/schema differs")
        blob = self._read_exact_blob(
            manifest_artifact,
            label="source terminal manifest Artifact bytes",
        )
        try:
            decoded = json.loads(blob.decode("utf-8"))
            if source.status == "succeeded":
                manifest: RunResultV1 | RunFailureV1 = RunResultV1.model_validate(decoded)
                outcome_code = manifest.outcome_code
            else:
                manifest = RunFailureV1.model_validate(decoded)
                outcome_code = manifest.cause_code
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise IntegrityViolation("native source terminal manifest is malformed") from exc
        if canonical_json(manifest.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation("native source terminal manifest is not canonical")
        if (
            manifest.run_id != source.run_id
            or manifest.run_kind != source.kind
            or manifest.attempt_no != source.current_attempt_no
            or tree.root.payload.outcome_code != outcome_code
            or manifest.version_projection.run_payload_hash != source.payload_hash
            or manifest.version_projection.frozen_input_version_tuple
            != source.payload.version_tuple
            or manifest.version_projection.terminal_version_tuple != manifest_artifact.version_tuple
            or manifest_artifact.version_tuple.cassette_id
            != tree.root.artifact.version_tuple.cassette_id
        ):
            raise IntegrityViolation(
                "native cassette outcome differs from source terminal manifest"
            )
        cassette_parents = tuple(
            parent
            for parent in manifest.version_projection.parents
            if parent.cassette_scope == "run_bundle"
        )
        if (
            len(cassette_parents) != 1
            or cassette_parents[0].artifact_id != tree.root.artifact.artifact_id
            or cassette_parents[0].role != "intermediate"
            or cassette_parents[0].publication != "run_published"
            or tree.root.artifact.artifact_id not in manifest_artifact.lineage
        ):
            raise IntegrityViolation(
                "native source terminal manifest does not bind the run cassette parent"
            )

    def _validate_native_identity(self, *, tree: _BundleTree, source: RunRecord) -> None:
        plan = source.payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("native RECORD source lacks an execution plan")
        root_identity = self._execution_identity(tree.root.artifact)
        if root_identity is None:
            raise IntegrityViolation("native run cassette lacks its execution identity")
        if (
            root_identity.scope != "run"
            or root_identity.agent_graph_version != plan.agent_graph_version
        ):
            raise IntegrityViolation("native run cassette execution identity differs from plan")
        self._require_bindings_fit_plan(root_identity.bindings, plan=plan, legacy=False)
        self._require_consumed_route_is_terminal(root_identity)
        retained_decisions = {
            binding.routing_decision_id: self._retained_native_decision(
                source=source,
                plan=plan,
                binding=binding,
            )
            for binding in root_identity.bindings
        }

        known_attempts = {attempt.aggregate.payload.attempt_no for attempt in tree.attempts}
        if any(binding.attempt_no not in known_attempts for binding in root_identity.bindings):
            raise IntegrityViolation("native cassette identity references an absent attempt")

        consumed_decisions: set[str] = set()
        for attempt in tree.attempts:
            attempt_no = attempt.aggregate.payload.attempt_no
            assert attempt_no is not None
            expected_attempt_bindings = tuple(
                binding for binding in root_identity.bindings if binding.attempt_no == attempt_no
            )
            attempt_identity = self._execution_identity(attempt.aggregate.artifact)
            if (
                attempt_identity is None
                or attempt_identity.scope != "attempt"
                or attempt_identity.agent_graph_version != plan.agent_graph_version
                or attempt_identity.bindings != expected_attempt_bindings
            ):
                raise IntegrityViolation("native attempt cassette identity is incomplete")

            for shard in attempt.shards:
                ordinal = shard.payload.ordinal
                assert ordinal is not None
                expected_call_bindings = tuple(
                    binding
                    for binding in expected_attempt_bindings
                    if binding.call_ordinal == ordinal and binding.response_consumed
                )
                shard_identity = self._execution_identity(shard.artifact)
                if (
                    shard_identity is None
                    or shard_identity.scope != "record_shard"
                    or shard_identity.agent_graph_version != plan.agent_graph_version
                    or shard_identity.bindings != expected_call_bindings
                ):
                    raise IntegrityViolation("native record shard identity is incomplete")
                record = shard.payload.records[0]
                if not isinstance(record, CassetteRecordV2):
                    raise IntegrityViolation("native record shard is not cassette@2")
                self._validate_native_record(
                    source=source,
                    shard=shard,
                    record=record,
                    bindings=expected_call_bindings,
                    retained_decisions=retained_decisions,
                )
                consumed = tuple(
                    binding for binding in expected_call_bindings if binding.response_consumed
                )
                if len(consumed) != 1:
                    raise IntegrityViolation("native record shard lacks one consumed route")
                consumed_decisions.add(consumed[0].routing_decision_id)

        expected_consumed = {
            binding.routing_decision_id
            for binding in root_identity.bindings
            if binding.response_consumed
        }
        if consumed_decisions != expected_consumed:
            raise IntegrityViolation("native cassette records differ from consumed route identity")

    @staticmethod
    def _require_consumed_route_is_terminal(identity: ExecutionIdentityV1) -> None:
        """A response ends its logical route chain; later routes are impossible."""

        calls: dict[tuple[int, int], list[InvocationVersionBindingV1]] = {}
        for binding in identity.bindings:
            calls.setdefault((binding.attempt_no, binding.call_ordinal), []).append(binding)
        for bindings in calls.values():
            consumed = tuple(binding for binding in bindings if binding.response_consumed)
            if consumed and consumed[0] != bindings[-1]:
                raise IntegrityViolation(
                    "native cassette logical call has a route after its consumed response"
                )

    def _retained_native_decision(
        self,
        *,
        source: RunRecord,
        plan: ExecutionVersionPlanV1,
        binding: InvocationVersionBindingV1,
    ) -> RoutingDecisionV1:
        decision = self._reader.get_routing_decision(binding.routing_decision_id)
        route = self._reader.get_model_route_link(
            source.run_id,
            binding.attempt_no,
            binding.call_ordinal,
            binding.route_ordinal,
        )
        consumption = self._reader.get_model_response_consumption(
            source.run_id,
            binding.attempt_no,
            binding.call_ordinal,
            binding.route_ordinal,
        )
        node = next(
            (item for item in plan.nodes if item.agent_node_id == binding.agent_node_id),
            None,
        )
        if (
            not isinstance(decision, RoutingDecisionV1)
            or decision.decision_id != binding.routing_decision_id
            or decision.run_id != source.run_id
            or decision.attempt_no != binding.attempt_no
            or decision.model_snapshot != binding.model_snapshot
            or decision.execution_source != binding.execution_source
            or decision.budget_set_snapshot_id != source.payload.budget_set_snapshot_id
            or decision.policy_version != plan.routing_policy_version
            or decision.routing_policy_digest != plan.routing_policy_digest
            or decision.catalog_version != plan.model_catalog_version
            or decision.catalog_digest != plan.model_catalog_digest
            or not isinstance(route, RunModelRouteLinkV1)
            or route.run_id != source.run_id
            or route.attempt_no != binding.attempt_no
            or route.call_ordinal != binding.call_ordinal
            or route.route_ordinal != binding.route_ordinal
            or route.routing_decision_kind != binding.routing_decision_kind
            or route.routing_decision_id != binding.routing_decision_id
            or route.request_hash != decision.request_hash.removeprefix("sha256:")
            or node is None
            or decision.model_snapshot not in node.allowed_model_snapshots
        ):
            raise IntegrityViolation(
                "native invocation differs from retained RoutingDecision authority"
            )
        if binding.response_consumed:
            if (
                not isinstance(consumption, RunModelResponseConsumptionV1)
                or consumption.execution_source != binding.execution_source
                or consumption.transport_attempt != binding.transport_attempt
                or consumption.cassette_shard_artifact_id is None
            ):
                raise IntegrityViolation(
                    "native consumed invocation differs from response-consumption authority"
                )
        elif consumption is not None:
            raise IntegrityViolation("native unconsumed route has response-consumption authority")
        return decision

    def _validate_native_record(
        self,
        *,
        source: RunRecord,
        shard: _BundleNode,
        record: CassetteRecordV2,
        bindings: tuple[InvocationVersionBindingV1, ...],
        retained_decisions: dict[str, RoutingDecisionV1],
    ) -> None:
        attempt_no = shard.payload.attempt_no
        ordinal = shard.payload.ordinal
        assert attempt_no is not None and ordinal is not None
        decision = record.routing_decision
        plan = source.payload.execution_version_plan
        assert plan is not None
        if decision.run_id != source.run_id or decision.attempt_no != attempt_no:
            raise IntegrityViolation("native cassette routing decision differs from shard identity")
        if retained_decisions.get(decision.decision_id) != decision:
            raise IntegrityViolation(
                "native cassette route differs from retained RoutingDecision authority"
            )
        if decision.execution_source == "cassette_replay":
            raise IntegrityViolation("native RECORD cassette cannot contain a replay decision")
        if (
            decision.budget_set_snapshot_id != source.payload.budget_set_snapshot_id
            or decision.policy_version != plan.routing_policy_version
            or decision.routing_policy_digest != plan.routing_policy_digest
            or decision.catalog_version != plan.model_catalog_version
            or decision.catalog_digest != plan.model_catalog_digest
        ):
            raise IntegrityViolation("native cassette route differs from the execution plan")
        node = next(
            (item for item in plan.nodes if item.agent_node_id == record.agent_node_id),
            None,
        )
        if node is None or decision.model_snapshot not in node.allowed_model_snapshots:
            raise IntegrityViolation("native cassette record is outside the execution plan")
        matched = tuple(
            binding for binding in bindings if binding.routing_decision_id == decision.decision_id
        )
        if len(matched) != 1 or not matched[0].response_consumed:
            raise IntegrityViolation("native cassette record does not bind one consumed route")
        binding = matched[0]
        link = self._reader.get_prompt_link(
            source.run_id,
            attempt_no,
            ordinal,
            binding.route_ordinal,
        )
        if (
            not isinstance(link, RunIntermediateArtifactLinkV1)
            or link.run_id != source.run_id
            or link.attempt_no != attempt_no
            or link.call_ordinal != ordinal
            or link.route_ordinal != binding.route_ordinal
            or link.request_hash != record.request_hash.removeprefix("sha256:")
        ):
            raise IntegrityViolation("native cassette shard lacks its exact prompt link")
        if shard.artifact.lineage != (link.artifact_id,):
            raise IntegrityViolation("native record shard lineage differs from its prompt")
        consumption = self._reader.get_model_response_consumption(
            source.run_id,
            attempt_no,
            ordinal,
            binding.route_ordinal,
        )
        if (
            not isinstance(consumption, RunModelResponseConsumptionV1)
            or consumption.cassette_shard_artifact_id != shard.artifact.artifact_id
        ):
            raise IntegrityViolation(
                "native record shard differs from response-consumption authority"
            )
        prompt = self._require_artifact(link.artifact_id, label="rendered prompt")
        if (
            prompt.kind != "source_rendered"
            or prompt.meta.get("payload_schema_id") != "source-rendered@1"
        ):
            raise IntegrityViolation("native prompt link does not resolve source_rendered")
        rendered_request = self._load_rendered_request(prompt)
        if not isinstance(rendered_request, ModelRequestV2):
            raise IntegrityViolation("native rendered prompt is not model-router@2")
        rendered_hash = request_hash(rendered_request)
        rendered_model = canonical_model_snapshot_id(rendered_request.model_snapshot)
        if (
            rendered_hash != record.request_hash
            or rendered_hash.removeprefix("sha256:") != link.request_hash
            or rendered_request.agent_node_id != record.agent_node_id
            or rendered_request.prompt_version != node.prompt_version
            or rendered_model != decision.model_snapshot
            or prompt.version_tuple.prompt_version != rendered_request.prompt_version
            # The rendered request and retained route close the chosen model.
            # ``source_rendered`` itself is pre-response renderer evidence and the
            # producer matrix therefore keeps its model projection null.
            or prompt.version_tuple.model_snapshot is not None
            or prompt.version_tuple.agent_graph_version != plan.agent_graph_version
            or not isinstance(prompt.meta.get("renderer_version"), str)
            or prompt.version_tuple.tool_version != prompt.meta.get("renderer_version")
        ):
            raise IntegrityViolation(
                "native rendered ModelRequest differs from prompt link, route, or plan"
            )

        expected_transport_attempt = (
            record.transport_attempt_count if decision.execution_source == "online" else None
        )
        if (
            binding.routing_decision_kind != "native"
            or binding.attempt_no != attempt_no
            or binding.call_ordinal != ordinal
            or binding.transport_attempt != expected_transport_attempt
            or binding.agent_node_id != record.agent_node_id
            or binding.prompt_version != node.prompt_version
            or binding.model_snapshot != decision.model_snapshot
            or binding.tool_version != node.tool_version
            or binding.execution_source != decision.execution_source
        ):
            raise IntegrityViolation("native cassette invocation differs from record and plan")

    def _validate_legacy(
        self,
        *,
        payload: RunPayloadEnvelope,
        tree: _BundleTree,
    ) -> ReplayAdmissionProof:
        manifest = tree.root.payload.legacy_run_import_manifest
        if manifest is None:
            raise IntegrityViolation("legacy replay root lacks its import manifest")
        if manifest.status == "evidence_missing":
            raise IntegrityViolation("legacy replay import has status evidence_missing")
        if manifest.status != "verified":
            raise IntegrityViolation("legacy replay import is not verified")
        if self._legacy_authority is None:
            raise DependencyUnavailable(
                "legacy replay verification authority is unavailable",
                component="legacy_import_authority",
            )
        if self._legacy_decisions is None:
            raise DependencyUnavailable(
                "legacy replay decision authority is unavailable",
                component="legacy_import_decisions",
            )
        plan = payload.execution_version_plan
        if plan is None:
            raise IntegrityViolation("legacy replay lacks its execution plan")

        self._validate_legacy_inputs(payload=payload, tree=tree)
        self._validate_legacy_profiles(payload=payload, tree=tree)
        if tuple(item.model_dump(mode="json") for item in payload.policy_bindings) != tuple(
            item.model_dump(mode="json") for item in manifest.policy_bindings
        ):
            raise IntegrityViolation("legacy replay policy bindings differ from manifest")
        if tuple(item.model_dump(mode="json") for item in payload.schema_bindings) != tuple(
            item.model_dump(mode="json") for item in manifest.schema_bindings
        ):
            raise IntegrityViolation("legacy replay schema bindings differ from manifest")
        if payload.payload_schema_version not in {
            item.schema_id for item in manifest.schema_bindings
        }:
            raise IntegrityViolation("legacy replay manifest does not bind the Run payload schema")
        frozen_tuple = manifest.frozen_version_tuple
        identity = manifest.execution_identity
        if frozen_tuple is None or identity is None:
            raise IntegrityViolation("verified legacy replay lacks frozen execution evidence")
        self._require_tuple_equal_except_cassette(
            frozen_tuple,
            payload.version_tuple,
            label="legacy replay VersionTuple",
        )
        if payload.seed != frozen_tuple.seed:
            raise IntegrityViolation("legacy replay seed differs from frozen manifest")
        self._require_bindings_fit_plan(identity.bindings, plan=plan, legacy=True)
        if identity.agent_graph_version != plan.agent_graph_version:
            raise IntegrityViolation("legacy execution identity differs from the replay plan")

        self._validate_legacy_artifact_identity(tree=tree, identity=identity)
        source = LegacyCassetteRuntimeImporter(self._legacy_authority).read_verified(
            root=tree.root.payload,
            child_bundles_by_artifact_id=tree.child_payloads,
            model_catalog_version=plan.model_catalog_version,
            model_catalog_digest=plan.model_catalog_digest,
            decision_repository=self._legacy_decisions,
        )
        if source.import_id != manifest.import_id:
            raise IntegrityViolation("legacy replay source identity differs from manifest")
        return ReplayAdmissionProof(
            cassette_artifact_id=tree.root.artifact.artifact_id,
            source_kind="legacy_import",
            source_run_id=None,
            legacy_import_id=manifest.import_id,
            attempt_count=len(tree.attempts),
            record_count=source.call_count,
            selected_source_attempt_no=1,
        )

    def _validate_legacy_inputs(
        self,
        *,
        payload: RunPayloadEnvelope,
        tree: _BundleTree,
    ) -> None:
        manifest = tree.root.payload.legacy_run_import_manifest
        assert manifest is not None
        actual_ids = self._non_cassette_inputs(payload, tree.root.artifact.artifact_id)
        expected_ids = tuple(sorted(item.artifact_id for item in manifest.input_artifact_bindings))
        if actual_ids != expected_ids:
            raise IntegrityViolation("legacy replay input Artifact IDs differ from manifest")
        by_id = {item.artifact_id: item for item in manifest.input_artifact_bindings}
        for artifact_id in actual_ids:
            artifact = self._require_artifact(artifact_id, label="input")
            self._read_exact_blob(artifact, label="input Artifact bytes")
            binding = by_id[artifact_id]
            if (
                artifact.payload_hash != binding.payload_hash
                or artifact.version_tuple != binding.version_tuple
            ):
                raise IntegrityViolation("legacy replay input binding differs from Artifact")

    @staticmethod
    def _validate_legacy_profiles(
        *,
        payload: RunPayloadEnvelope,
        tree: _BundleTree,
    ) -> None:
        manifest = tree.root.payload.legacy_run_import_manifest
        assert manifest is not None
        actual = tuple(
            {
                "field_path": item.field_path,
                "profile_id": item.profile.profile_id,
                "profile_version": item.profile.version,
                "profile_payload_hash": item.profile_payload_hash,
                "catalog_version": item.catalog_version,
                "catalog_digest": item.catalog_digest,
            }
            for item in payload.resolved_profiles
        )
        expected = tuple(
            item.model_dump(mode="json") for item in manifest.execution_profile_bindings
        )
        if actual != expected:
            raise IntegrityViolation("legacy replay resolved profiles differ from manifest")
        if any(
            item.catalog_version != payload.execution_profile_catalog_version
            or item.catalog_digest != payload.execution_profile_catalog_digest
            for item in manifest.execution_profile_bindings
        ):
            raise IntegrityViolation("legacy replay profile catalog differs from manifest")

    def _validate_legacy_artifact_identity(
        self,
        *,
        tree: _BundleTree,
        identity: ExecutionIdentityV1,
    ) -> None:
        manifest = tree.root.payload.legacy_run_import_manifest
        assert manifest is not None
        bundle_artifacts = (
            tree.root.artifact,
            *(attempt.aggregate.artifact for attempt in tree.attempts),
            *(shard.artifact for attempt in tree.attempts for shard in attempt.shards),
        )
        if any(
            artifact.version_tuple.tool_version != manifest.importer_tool_version
            for artifact in bundle_artifacts
        ):
            raise IntegrityViolation("legacy cassette tool version differs from import manifest")
        root_identity = self._execution_identity(tree.root.artifact)
        if root_identity != identity:
            raise IntegrityViolation("legacy run Artifact identity differs from manifest")
        if len(tree.attempts) != 1:
            raise IntegrityViolation("legacy replay requires one attempt Artifact")
        attempt = tree.attempts[0]
        attempt_identity = self._execution_identity(attempt.aggregate.artifact)
        if (
            attempt_identity is None
            or attempt_identity.scope != "attempt"
            or attempt_identity.agent_graph_version != identity.agent_graph_version
            or attempt_identity.bindings != identity.bindings
        ):
            raise IntegrityViolation("legacy attempt Artifact identity is incomplete")
        for shard in attempt.shards:
            evidence = shard.payload.legacy_call_import_evidence
            if (
                evidence is None
                or evidence.invocation is None
                or evidence.rendered_request_artifact_id is None
            ):
                raise IntegrityViolation("verified legacy shard has incomplete call evidence")
            shard_identity = self._execution_identity(shard.artifact)
            if (
                shard_identity is None
                or shard_identity.scope != "record_shard"
                or shard_identity.agent_graph_version != identity.agent_graph_version
                or shard_identity.bindings != (evidence.invocation,)
            ):
                raise IntegrityViolation("legacy record-shard Artifact identity is incomplete")
            if shard.artifact.lineage != (evidence.rendered_request_artifact_id,):
                raise IntegrityViolation(
                    "legacy record shard lineage differs from rendered request"
                )
            rendered = self._require_artifact(
                evidence.rendered_request_artifact_id,
                label="rendered request",
            )
            if (
                rendered.kind != "source_rendered"
                or rendered.meta.get("payload_schema_id") != "source-rendered@1"
            ):
                raise IntegrityViolation("legacy rendered request Artifact has wrong schema")
            rendered_request = self._load_rendered_request(rendered)
            authoritative_request = self._legacy_authority.resolve_rendered_request(
                evidence.rendered_request_artifact_id
            )
            if (
                not isinstance(rendered_request, ModelRequestV1)
                or isinstance(rendered_request, ModelRequestV2)
                or authoritative_request != rendered_request
                or request_hash(rendered_request) != evidence.request_hash
                or rendered.version_tuple.prompt_version != rendered_request.prompt_version
                or rendered.version_tuple.model_snapshot
                != canonical_model_snapshot_id(rendered_request.model_snapshot)
                or rendered.version_tuple.agent_graph_version != identity.agent_graph_version
            ):
                raise IntegrityViolation(
                    "legacy rendered request Artifact differs from retained authority"
                )
            if len(shard.payload.records) != 1 or not isinstance(
                shard.payload.records[0], CassetteRecordV1
            ):
                raise IntegrityViolation("legacy record shard is not cassette@1")

    @staticmethod
    def _require_bindings_fit_plan(
        bindings: tuple[InvocationVersionBindingV1, ...],
        *,
        plan: ExecutionVersionPlanV1,
        legacy: bool,
    ) -> None:
        nodes = {item.agent_node_id: item for item in plan.nodes}
        for binding in bindings:
            node = nodes.get(binding.agent_node_id)
            if (
                node is None
                or binding.prompt_version != node.prompt_version
                or binding.tool_version != node.tool_version
                or binding.model_snapshot not in node.allowed_model_snapshots
            ):
                raise IntegrityViolation("cassette invocation falls outside execution plan")
            if legacy and (
                binding.routing_decision_kind != "legacy_import"
                or binding.execution_source != "cassette_replay"
            ):
                raise IntegrityViolation("legacy invocation is not an imported replay route")
            if not legacy and (
                binding.routing_decision_kind != "native"
                or binding.execution_source == "cassette_replay"
            ):
                raise IntegrityViolation("native cassette contains a legacy invocation")

    @staticmethod
    def _execution_identity(artifact: ArtifactV2) -> ExecutionIdentityV1 | None:
        value = artifact.meta.get("execution_identity")
        if value is None:
            return None
        try:
            return (
                value
                if isinstance(value, ExecutionIdentityV1)
                else ExecutionIdentityV1.model_validate(value)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("cassette Artifact execution identity is malformed") from exc

    def _require_artifact(self, artifact_id: str, *, label: str) -> ArtifactV2:
        artifact = self._reader.get_artifact(artifact_id)
        if not isinstance(artifact, ArtifactV2) or artifact.artifact_id != artifact_id:
            raise IntegrityViolation(f"{label} Artifact is not retained exactly")
        return artifact

    def _load_rendered_request(self, artifact: ArtifactV2) -> ModelRequestV1 | ModelRequestV2:
        blob = self._read_exact_blob(artifact, label="rendered request Artifact bytes")
        try:
            decoded = json.loads(blob.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("rendered request must be an object")
            request = parse_model_request(decoded)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise IntegrityViolation("rendered request Artifact is malformed") from exc
        if canonical_json(request.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation("rendered request Artifact is not canonical")
        return request

    def _read_exact_blob(self, artifact: ArtifactV2, *, label: str) -> bytes:
        if artifact.object_ref.size_bytes > MAX_REPLAY_ARTIFACT_BYTES:
            raise IntegrityViolation(f"{label} exceed the replay admission byte limit")
        try:
            blob = self._reader.read_artifact_bytes(artifact.artifact_id)
        except (KeyError, FileNotFoundError, OSError) as exc:
            raise IntegrityViolation(f"{label} are unavailable") from exc
        if not isinstance(blob, bytes):
            raise IntegrityViolation(f"{label} are not bytes")
        if (
            len(blob) != artifact.object_ref.size_bytes
            or sha256_lowerhex(blob) != artifact.payload_hash
            or artifact.object_ref.sha256 != artifact.payload_hash
        ):
            raise IntegrityViolation(f"{label} differ from their ObjectRef/hash")
        return blob

    @staticmethod
    def _non_cassette_inputs(
        payload: RunPayloadEnvelope,
        cassette_artifact_id: str,
    ) -> tuple[str, ...]:
        values = tuple(
            artifact_id
            for artifact_id in payload.input_artifact_ids
            if artifact_id != cassette_artifact_id
        )
        if len(values) + 1 != len(payload.input_artifact_ids):
            raise IntegrityViolation("replay cassette input binding is not unique")
        return values

    @staticmethod
    def _require_tuple_equal_except_cassette(
        expected: VersionTuple,
        actual: VersionTuple,
        *,
        label: str,
    ) -> None:
        for field_name in VersionTuple.model_fields:
            if field_name == "cassette_id":
                continue
            if getattr(expected, field_name) != getattr(actual, field_name):
                raise IntegrityViolation(f"{label} field {field_name} differs")

    @staticmethod
    def _require_schema_binding_closure(
        payload: RunPayloadEnvelope,
        *,
        label: str,
    ) -> None:
        matches = tuple(
            binding for binding in payload.schema_bindings if binding.binding_key == "run_payload"
        )
        if len(matches) != 1 or matches[0].schema_id != payload.payload_schema_version:
            raise IntegrityViolation(f"{label} lacks its exact run_payload schema binding")

    @staticmethod
    def _require_profile_catalog_closure(
        payload: RunPayloadEnvelope,
        *,
        label: str,
    ) -> None:
        if any(
            binding.catalog_version != payload.execution_profile_catalog_version
            or binding.catalog_digest != payload.execution_profile_catalog_digest
            for binding in payload.resolved_profiles
        ):
            raise IntegrityViolation(f"{label} resolved profile catalog is not closed")


__all__ = [
    "MAX_REPLAY_ARTIFACT_BYTES",
    "MAX_REPLAY_TREE_BYTES",
    "MAX_REPLAY_TREE_NODES",
    "ReplayAdmissionProof",
    "ReplayAdmissionReader",
    "ReplayAdmissionValidator",
    "ReplayExecutionProfileAuthority",
]
