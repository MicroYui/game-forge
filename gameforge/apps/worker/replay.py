"""Artifact-backed, zero-network replay sources for the persistent worker.

Run admission proves a cassette tree before creating a REPLAY Run.  A worker may
start much later (or after a process restart), so it must rebuild execution only
from the persisted ``Run.payload.cassette_artifact_id`` and retained authorities.
This module repeats the complete admission proof, then exposes one of two explicit
runtime branches:

* native M4 ``cassette@2`` records through a read-only ``replay_native`` store;
* verified historical ``cassette@1`` imports through the existing legacy router.

Neither branch owns or accepts a provider transport. Missing or additional calls
fail closed instead of falling through to online execution. Ordering remains
owned by the persisted RunAttempt head and response-consumption authority, so
this restartable source has no process-local cursor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecordV2
from gameforge.contracts.cassette_import import CassetteBundleV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    RunIntermediateArtifactLinkV1,
    RunModelRouteLinkV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2, ExecutionIdentityV1, InvocationVersionBindingV1
from gameforge.contracts.model_router import (
    ModelRequestV1,
    ModelRequestV2,
    parse_model_request,
    request_hash,
)
from gameforge.contracts.routing import RoutingDecisionV1, canonical_model_snapshot_id
from gameforge.platform.runs.replay import (
    MAX_REPLAY_ARTIFACT_BYTES,
    ReplayAdmissionProof,
    ReplayAdmissionReader,
    ReplayAdmissionValidator,
)
from gameforge.runtime.cassette.legacy_import import (
    LegacyCassetteRuntimeImporter,
    LegacyImportAuthority,
    LegacyImportDecisionRepository,
    LegacyReplayCall,
    VerifiedLegacyReplaySource,
)
from gameforge.runtime.cassette.store import CassetteRouteKey
from gameforge.runtime.model_router.m4_router import M4RouterResultV1, VerifiedLegacyReplayRouter


CurrentDecisionResolver = Callable[[str], RoutingDecisionV1 | None]


@dataclass(frozen=True, slots=True)
class NativeReplayRoute:
    """One source-authoritative route within a logical native model call."""

    invocation: InvocationVersionBindingV1
    request: ModelRequestV2
    source_decision: RoutingDecisionV1
    record: CassetteRecordV2 | None

    @property
    def attempt_no(self) -> int:
        return self.invocation.attempt_no

    @property
    def call_ordinal(self) -> int:
        return self.invocation.call_ordinal

    @property
    def route_ordinal(self) -> int:
        return self.invocation.route_ordinal


@dataclass(frozen=True, slots=True)
class NativeReplayCallPlan:
    attempt_no: int
    call_ordinal: int
    routes: tuple[NativeReplayRoute, ...]

    @property
    def consumed_route(self) -> NativeReplayRoute | None:
        consumed = tuple(route for route in self.routes if route.invocation.response_consumed)
        if len(consumed) > 1:
            raise IntegrityViolation("native replay call consumes more than one source route")
        if consumed and consumed[0] != self.routes[-1]:
            raise IntegrityViolation("native replay call has a route after its consumed response")
        return consumed[0] if consumed else None


class NativeArtifactReplaySource:
    """Read-only ``CassetteStore`` shape backed by a validated native run bundle."""

    source_kind: Literal["native"] = "native"

    def __init__(
        self,
        *,
        proof: ReplayAdmissionProof,
        calls: tuple[NativeReplayCallPlan, ...],
        replay_run_id: str,
        replay_budget_set_snapshot_id: str,
        current_decision_resolver: CurrentDecisionResolver,
    ) -> None:
        if proof.source_kind != "native" or proof.source_run_id is None:
            raise IntegrityViolation("native replay source received another proof kind")
        selected_attempt = proof.selected_source_attempt_no
        if selected_attempt is None and calls:
            raise IntegrityViolation("zero-attempt native replay source contains model calls")
        identities = tuple((call.attempt_no, call.call_ordinal) for call in calls)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise IntegrityViolation("native replay calls are not uniquely ordered")
        record_count = 0
        calls_by_attempt: dict[int, list[int]] = {}
        for call in calls:
            calls_by_attempt.setdefault(call.attempt_no, []).append(call.call_ordinal)
            route_ordinals = tuple(route.route_ordinal for route in call.routes)
            if route_ordinals != tuple(range(1, len(route_ordinals) + 1)):
                raise IntegrityViolation("native replay routes are not contiguous and ordered")
            for route in call.routes:
                if route.attempt_no != call.attempt_no or route.call_ordinal != call.call_ordinal:
                    raise IntegrityViolation("native replay route escaped its logical call")
                if route.invocation.response_consumed != (route.record is not None):
                    raise IntegrityViolation("native replay response binding differs from record")
                record_count += int(route.record is not None)
            call.consumed_route
        for call_ordinals in calls_by_attempt.values():
            if tuple(call_ordinals) != tuple(range(1, len(call_ordinals) + 1)):
                raise IntegrityViolation("native replay calls are not contiguous within attempt")
        if record_count != proof.record_count:
            raise IntegrityViolation("native replay proof record count differs from its routes")
        self.proof = proof
        self.source_run_id = proof.source_run_id
        self.replay_run_id = replay_run_id
        self.replay_budget_set_snapshot_id = replay_budget_set_snapshot_id
        self._calls = {(call.attempt_no, call.call_ordinal): call for call in calls}
        self.selected_source_attempt_no = selected_attempt
        self._resolver = current_decision_resolver

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def attempt_numbers(self) -> tuple[int, ...]:
        return tuple(sorted({attempt_no for attempt_no, _ in self._calls}))

    def call_plan(self, *, attempt_no: int, call_ordinal: int) -> NativeReplayCallPlan | None:
        # ``attempt_no`` belongs to the new REPLAY Run. Every new worker attempt
        # reruns its executor from call 1, so it must replay the same terminal
        # source attempt rather than accidentally advancing through source history.
        if attempt_no <= 0:
            raise IntegrityViolation("current replay attempt number must be positive")
        selected = self.selected_source_attempt_no
        return None if selected is None else self._calls.get((selected, call_ordinal))

    def project_current_decision(
        self,
        route: NativeReplayRoute,
        *,
        attempt_no: int,
        decided_at: datetime,
    ) -> RoutingDecisionV1:
        """Project a new Run decision from the immutable source route.

        Replay never invokes the current policy selector.  All semantic routing
        fields are copied from the RECORD source; only the new execution identity,
        budget snapshot, timestamp, and explicit ``cassette_replay`` source differ.
        """

        source = route.source_decision
        if attempt_no <= 0:
            raise IntegrityViolation("current replay attempt number must be positive")
        if route.attempt_no != self.selected_source_attempt_no:
            raise IntegrityViolation("native replay route is outside the selected source attempt")
        retained_call = self._calls.get((route.attempt_no, route.call_ordinal))
        if retained_call is None or route not in retained_call.routes:
            raise IntegrityViolation("native replay decision projection requires a source route")
        if decided_at.tzinfo is None or decided_at.utcoffset() != UTC.utcoffset(decided_at):
            raise IntegrityViolation("native replay decision time must be UTC")
        return RoutingDecisionV1.create(
            run_id=self.replay_run_id,
            attempt_no=attempt_no,
            request_hash=source.request_hash,
            rule_id=source.rule_id,
            model_snapshot=source.model_snapshot,
            tier=source.tier,
            reason_code="recorded_replay",
            budget_set_snapshot_id=self.replay_budget_set_snapshot_id,
            fallback_from=source.fallback_from,
            fallback_index=source.fallback_index,
            policy_version=source.policy_version,
            routing_policy_digest=source.routing_policy_digest,
            catalog_version=source.catalog_version,
            catalog_digest=source.catalog_digest,
            execution_source="cassette_replay",
            decided_at=decided_at.astimezone(UTC),
        )

    def replay_native(self, key: CassetteRouteKey):
        """Return exactly the consumed source record or a deterministic MISS."""

        selected = self.selected_source_attempt_no
        call = None if selected is None else self._calls.get((selected, key.call_ordinal))
        if call is None:
            return CASSETTE_MISS
        route = next(
            (item for item in call.routes if item.route_ordinal == key.route_ordinal),
            None,
        )
        if route is None or route.record is None or not route.invocation.response_consumed:
            return CASSETTE_MISS
        current = self._resolver(key.routing_decision_id)
        self._validate_current_decision(key=key, current=current, source=route.source_decision)
        return route.record

    def record_native(self, key: CassetteRouteKey, record: CassetteRecordV2) -> None:
        del key, record
        raise IntegrityViolation("artifact-backed REPLAY is immutable")

    def _validate_current_decision(
        self,
        *,
        key: CassetteRouteKey,
        current: RoutingDecisionV1 | None,
        source: RoutingDecisionV1,
    ) -> None:
        if not isinstance(current, RoutingDecisionV1):
            raise IntegrityViolation("native replay current RoutingDecision is unavailable")
        if (
            current.decision_id != key.routing_decision_id
            or current.run_id != key.run_id
            or current.run_id != self.replay_run_id
            or current.attempt_no != key.attempt_no
            or current.execution_source != "cassette_replay"
            or current.reason_code != "recorded_replay"
            or current.budget_set_snapshot_id != self.replay_budget_set_snapshot_id
        ):
            raise IntegrityViolation("native replay route key differs from current decision")
        semantic_fields = (
            "request_hash",
            "rule_id",
            "model_snapshot",
            "tier",
            "fallback_from",
            "fallback_index",
            "policy_version",
            "routing_policy_digest",
            "catalog_version",
            "catalog_digest",
        )
        if any(getattr(current, field) != getattr(source, field) for field in semantic_fields):
            raise IntegrityViolation("native replay current decision differs from source route")


class LegacyArtifactReplaySource:
    """Stateless wrapper over an authority-verified legacy import."""

    source_kind: Literal["legacy_import"] = "legacy_import"

    def __init__(
        self,
        *,
        proof: ReplayAdmissionProof,
        source: VerifiedLegacyReplaySource,
    ) -> None:
        if (
            proof.source_kind != "legacy_import"
            or proof.legacy_import_id is None
            or source.import_id != proof.legacy_import_id
        ):
            raise IntegrityViolation("legacy replay source differs from its admission proof")
        self.proof = proof
        self.import_id = proof.legacy_import_id
        self._source = source
        self._router = VerifiedLegacyReplayRouter(
            source=source,
            expected_import_id=self.import_id,
        )

    @property
    def call_count(self) -> int:
        return self._source.call_count

    def plan(self, request: ModelRequestV1, *, call_ordinal: int) -> LegacyReplayCall:
        return self._source.replay(request, call_ordinal=call_ordinal)

    def expected_call(self, *, call_ordinal: int) -> LegacyReplayCall:
        return self._source.expected_call(call_ordinal=call_ordinal)

    def replay(self, request: ModelRequestV1, *, call_ordinal: int) -> M4RouterResultV1:
        return self._router.call(request, call_ordinal=call_ordinal)


ArtifactReplaySource = NativeArtifactReplaySource | LegacyArtifactReplaySource


class ArtifactReplayLoader:
    """Rebuild a REPLAY source from Artifact/DB authority after any restart."""

    def __init__(
        self,
        reader: ReplayAdmissionReader,
        *,
        current_decision_resolver: CurrentDecisionResolver,
        legacy_authority: LegacyImportAuthority | None = None,
        legacy_decisions: LegacyImportDecisionRepository | None = None,
    ) -> None:
        self._reader = reader
        self._current_decision_resolver = current_decision_resolver
        self._legacy_authority = legacy_authority
        self._legacy_decisions = legacy_decisions

    def load(self, run: RunRecord) -> ArtifactReplaySource:
        payload = run.payload
        validator = ReplayAdmissionValidator(
            self._reader,
            legacy_authority=self._legacy_authority,
            legacy_decisions=self._legacy_decisions,
        )
        proof = validator.validate(kind=run.kind, payload=payload)
        root_id = payload.cassette_artifact_id
        if root_id is None:  # closed by the validator; defensive at the worker edge
            raise IntegrityViolation("REPLAY Run omitted its cassette Artifact")
        root, children = self._load_bundle_tree(root_id)
        if proof.source_kind == "native":
            return NativeArtifactReplaySource(
                proof=proof,
                calls=self._native_calls(root_id=root_id, root=root, children=children),
                replay_run_id=run.run_id,
                replay_budget_set_snapshot_id=run.budget_set_snapshot_id,
                current_decision_resolver=self._current_decision_resolver,
            )
        if self._legacy_authority is None or self._legacy_decisions is None:
            raise IntegrityViolation("legacy replay runtime authority closure is unavailable")
        plan = payload.execution_version_plan
        if plan is None:  # closed by the validator
            raise IntegrityViolation("legacy REPLAY Run omitted its execution plan")
        source = LegacyCassetteRuntimeImporter(self._legacy_authority).read_verified(
            root=root,
            child_bundles_by_artifact_id=children,
            model_catalog_version=plan.model_catalog_version,
            model_catalog_digest=plan.model_catalog_digest,
            decision_repository=self._legacy_decisions,
        )
        return LegacyArtifactReplaySource(proof=proof, source=source)

    def _native_calls(
        self,
        *,
        root_id: str,
        root: CassetteBundleV1,
        children: Mapping[str, CassetteBundleV1],
    ) -> tuple[NativeReplayCallPlan, ...]:
        source_run_id = root.run_id
        if source_run_id is None:
            raise IntegrityViolation("native replay root omitted its source Run")
        root_artifact = self._artifact(root_id)
        identity = self._execution_identity(root_artifact)
        if identity.scope != "run":
            raise IntegrityViolation("native replay root identity is not run-scoped")
        records: dict[tuple[int, int], CassetteRecordV2] = {}
        for attempt_id in root.child_bundle_artifact_ids:
            attempt = children[attempt_id]
            attempt_no = attempt.attempt_no
            if attempt_no is None:
                raise IntegrityViolation("native replay attempt omitted its ordinal")
            for shard_id in attempt.child_bundle_artifact_ids:
                shard = children[shard_id]
                if shard.ordinal is None or len(shard.records) != 1:
                    raise IntegrityViolation("native replay shard has another record cardinality")
                record = shard.records[0]
                if not isinstance(record, CassetteRecordV2):
                    raise IntegrityViolation("native replay shard is not cassette@2")
                record_key = (attempt_no, shard.ordinal)
                if record_key in records:
                    raise IntegrityViolation("native replay repeats a record shard ordinal")
                records[record_key] = record

        grouped: dict[tuple[int, int], list[NativeReplayRoute]] = {}
        for binding in identity.bindings:
            if binding.routing_decision_kind != "native":
                raise IntegrityViolation("native replay identity contains a legacy route")
            decision = self._reader.get_routing_decision(binding.routing_decision_id)
            if (
                not isinstance(decision, RoutingDecisionV1)
                or decision.run_id != source_run_id
                or decision.attempt_no != binding.attempt_no
                or decision.model_snapshot != binding.model_snapshot
                or decision.execution_source != binding.execution_source
            ):
                raise IntegrityViolation("native replay route lost its retained decision")
            route_link = self._reader.get_model_route_link(
                source_run_id,
                binding.attempt_no,
                binding.call_ordinal,
                binding.route_ordinal,
            )
            if (
                not isinstance(route_link, RunModelRouteLinkV1)
                or route_link.run_id != source_run_id
                or route_link.attempt_no != binding.attempt_no
                or route_link.call_ordinal != binding.call_ordinal
                or route_link.routing_decision_kind != "native"
                or route_link.routing_decision_id != binding.routing_decision_id
                or route_link.route_ordinal != binding.route_ordinal
            ):
                raise IntegrityViolation("native replay route lost its explicit route authority")
            prompt = self._reader.get_prompt_link(
                source_run_id,
                binding.attempt_no,
                binding.call_ordinal,
                binding.route_ordinal,
            )
            if (
                not isinstance(prompt, RunIntermediateArtifactLinkV1)
                or prompt.run_id != source_run_id
                or prompt.attempt_no != binding.attempt_no
                or prompt.call_ordinal != binding.call_ordinal
                or prompt.route_ordinal != binding.route_ordinal
                or prompt.artifact_id != route_link.prompt_artifact_id
                or prompt.request_hash != route_link.request_hash
            ):
                raise IntegrityViolation("native replay route lost its rendered prompt")
            request = self._rendered_request(prompt.artifact_id)
            rendered_hash = request_hash(request)
            if (
                rendered_hash != decision.request_hash
                or rendered_hash.removeprefix("sha256:") != prompt.request_hash
                or request.agent_node_id != binding.agent_node_id
                or request.prompt_version != binding.prompt_version
                or canonical_model_snapshot_id(request.model_snapshot) != binding.model_snapshot
            ):
                raise IntegrityViolation("native replay rendered request differs from its route")
            record = records.get((binding.attempt_no, binding.call_ordinal))
            if binding.response_consumed:
                if record is None or record.routing_decision != decision:
                    raise IntegrityViolation("native replay consumed route differs from its record")
            else:
                record = None
            grouped.setdefault((binding.attempt_no, binding.call_ordinal), []).append(
                NativeReplayRoute(
                    invocation=binding,
                    request=request,
                    source_decision=decision,
                    record=record,
                )
            )
        calls = []
        for (attempt_no, call_ordinal), routes in sorted(grouped.items()):
            ordered = tuple(sorted(routes, key=lambda route: route.route_ordinal))
            calls.append(
                NativeReplayCallPlan(
                    attempt_no=attempt_no,
                    call_ordinal=call_ordinal,
                    routes=ordered,
                )
            )
        if len(records) != sum(call.consumed_route is not None for call in calls):
            raise IntegrityViolation("native replay records differ from consumed logical calls")
        return tuple(calls)

    def _load_bundle_tree(
        self,
        root_id: str,
    ) -> tuple[CassetteBundleV1, dict[str, CassetteBundleV1]]:
        root = self._bundle(root_id)
        children: dict[str, CassetteBundleV1] = {}
        for attempt_id in root.child_bundle_artifact_ids:
            attempt = self._bundle(attempt_id)
            children[attempt_id] = attempt
            for shard_id in attempt.child_bundle_artifact_ids:
                children[shard_id] = self._bundle(shard_id)
        return root, children

    def _bundle(self, artifact_id: str) -> CassetteBundleV1:
        try:
            blob = self._read_exact_blob(artifact_id)
            decoded = json.loads(blob.decode("utf-8"))
            bundle = CassetteBundleV1.model_validate(decoded)
        except (
            FileNotFoundError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise IntegrityViolation("worker replay cassette Artifact is unreadable") from exc
        if canonical_json(bundle.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation("worker replay cassette Artifact is not canonical")
        return bundle

    def _rendered_request(self, artifact_id: str) -> ModelRequestV2:
        try:
            blob = self._read_exact_blob(artifact_id)
            decoded = json.loads(blob.decode("utf-8"))
            if not isinstance(decoded, Mapping):
                raise ValueError("rendered request must be an object")
            request = parse_model_request(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IntegrityViolation("native replay rendered request is unreadable") from exc
        if not isinstance(request, ModelRequestV2):
            raise IntegrityViolation("native replay rendered request is not model-router@2")
        if canonical_json(request.model_dump(mode="json")).encode("utf-8") != blob:
            raise IntegrityViolation("native replay rendered request is not canonical")
        return request

    def _read_exact_blob(self, artifact_id: str) -> bytes:
        artifact = self._artifact(artifact_id)
        if artifact.object_ref.size_bytes > MAX_REPLAY_ARTIFACT_BYTES:
            raise IntegrityViolation("worker replay Artifact exceeds the byte limit")
        try:
            blob = self._reader.read_artifact_bytes(artifact_id)
        except (KeyError, FileNotFoundError, OSError) as exc:
            raise IntegrityViolation("worker replay Artifact bytes disappeared") from exc
        if not isinstance(blob, bytes):
            raise IntegrityViolation("worker replay Artifact content is not bytes")
        if (
            len(blob) != artifact.object_ref.size_bytes
            or sha256_lowerhex(blob) != artifact.payload_hash
            or artifact.object_ref.sha256 != artifact.payload_hash
        ):
            raise IntegrityViolation("worker replay Artifact differs from its ObjectRef/hash")
        return blob

    def _artifact(self, artifact_id: str) -> ArtifactV2:
        artifact = self._reader.get_artifact(artifact_id)
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation("worker replay Artifact authority disappeared")
        return artifact

    @staticmethod
    def _execution_identity(artifact: ArtifactV2) -> ExecutionIdentityV1:
        try:
            return ExecutionIdentityV1.model_validate(artifact.meta.get("execution_identity"))
        except (ValidationError, ValueError) as exc:
            raise IntegrityViolation("native replay root lacks execution identity") from exc


__all__ = [
    "ArtifactReplayLoader",
    "ArtifactReplaySource",
    "LegacyArtifactReplaySource",
    "NativeArtifactReplaySource",
    "NativeReplayCallPlan",
    "NativeReplayRoute",
]
