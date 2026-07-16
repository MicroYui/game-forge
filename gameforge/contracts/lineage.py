"""Version, lineage, object-reference, execution-identity, and audit contracts.

The M0b ``lineage@1`` / ``audit@1`` constructors remain available as
``Artifact`` and ``AuditRecord``.  M4 writes the strict v2 variants and reads
the wire unions only through their explicit discriminator parsers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256, compute_snapshot_id, sha256_lowerhex
from gameforge.contracts.versions import (
    AUDIT_SCHEMA_VERSION,
    AUDIT_SCHEMA_VERSION_V2,
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    LINEAGE_SCHEMA_VERSION,
    LINEAGE_SCHEMA_VERSION_V2,
    OBJECT_BINDING_SCHEMA_VERSION,
    OBJECT_LOCATION_SCHEMA_VERSION,
    OBJECT_REF_SCHEMA_VERSION,
)

LowerHexSha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
MAX_RUNTIME_AUTHORITY_BINDINGS = 32_768

ArtifactKind = Literal[
    "source_raw",
    "source_rendered",
    "ir_snapshot",
    "constraint_snapshot",
    "constraint_proposal",
    "config_export",
    "scenario_spec",
    "task_suite",
    "regression_suite",
    "golden_suite",
    "bench_dataset",
    "benchmark_spec",
    "review_report",
    "checker_run",
    "simulation_run",
    "playtest_trace",
    "patch",
    "validation_evidence",
    "regression_evidence",
    "rollback_request",
    "run_result",
    "run_failure",
    "cassette_bundle",
    "migration_report",
    "bench_report",
    "operational_evidence",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class VersionTuple(BaseModel):
    """The frozen ten-field tuple; optional means not applicable, not unknown."""

    doc_version: str | None = None
    ir_snapshot_id: str | None = None
    constraint_snapshot_id: str | None = None
    prompt_version: str | None = None
    model_snapshot: str | None = None
    agent_graph_version: str | None = None
    tool_version: str | None = None
    env_contract_version: str | None = None
    seed: int | None = None
    cassette_id: str | None = None


class ArtifactV1(BaseModel):
    """Permanent reader and source-compatible constructor for ``lineage@1``."""

    artifact_id: str
    lineage_schema_version: Literal["lineage@1"] = LINEAGE_SCHEMA_VERSION
    kind: ArtifactKind
    version_tuple: VersionTuple
    lineage: list[str] = Field(default_factory=list)
    payload_hash: str | None = None
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


# Historical imports construct ``Artifact(...)`` directly.  Keep that API v1;
# callers that accept both versions must use ``parse_artifact``.
Artifact = ArtifactV1


def object_key_for_sha256(digest: LowerHexSha256) -> str:
    """Derive the portable v1 logical key from the plaintext content digest."""

    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("object digest must be 64 lowercase hexadecimal characters")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("object digest must be 64 lowercase hexadecimal characters")
    return f"objects/v1/sha256/{digest[:2]}/{digest}"


class ObjectRef(_StrictModel):
    object_ref_schema_version: Literal["object-ref@1"] = OBJECT_REF_SCHEMA_VERSION
    key: NonEmptyStr
    sha256: LowerHexSha256
    size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_content_addressed_key(self) -> ObjectRef:
        if self.key != object_key_for_sha256(self.sha256):
            raise ValueError("ObjectRef key does not match the content-addressed key")
        return self


def object_ref_for_bytes(payload: bytes | bytearray | memoryview) -> ObjectRef:
    raw = bytes(payload)
    digest = sha256_lowerhex(raw)
    return ObjectRef(key=object_key_for_sha256(digest), sha256=digest, size_bytes=len(raw))


class ObjectLocation(_StrictModel):
    location_schema_version: Literal["object-location@1"] = OBJECT_LOCATION_SCHEMA_VERSION
    store_id: NonEmptyStr
    key: NonEmptyStr
    backend_generation: NonEmptyStr
    etag: str | None = None
    storage_class: str | None = None


class ObjectBinding(_StrictModel):
    binding_schema_version: Literal["object-binding@1"] = OBJECT_BINDING_SCHEMA_VERSION
    object_ref: ObjectRef
    location: ObjectLocation
    status: Literal["active", "retired"]
    revision: int = Field(ge=1)
    verified_at: NonEmptyStr

    @model_validator(mode="after")
    def validate_location_key(self) -> ObjectBinding:
        if self.location.key != self.object_ref.key:
            raise ValueError("ObjectBinding location key does not match ObjectRef key")
        return self


class InvocationVersionBindingV1(_StrictModel):
    attempt_no: int = Field(ge=1)
    call_ordinal: int = Field(ge=1)
    route_ordinal: int = Field(ge=1)
    transport_attempt: int | None = Field(default=None, ge=1)
    routing_decision_kind: Literal["native", "legacy_import"]
    routing_decision_id: NonEmptyStr
    agent_node_id: NonEmptyStr
    prompt_version: NonEmptyStr
    model_snapshot: NonEmptyStr
    tool_version: NonEmptyStr
    execution_source: Literal["online", "full_response_cache", "cassette_replay"]
    response_consumed: bool

    @model_validator(mode="after")
    def validate_execution_source(self) -> InvocationVersionBindingV1:
        if (
            self.execution_source == "online"
            and self.response_consumed
            and self.transport_attempt is None
        ):
            raise ValueError("consumed online invocation requires a positive transport_attempt")
        if self.execution_source != "online" and self.transport_attempt is not None:
            raise ValueError("cache/replay invocation transport_attempt must be null")
        if self.routing_decision_kind == "legacy_import" and (
            self.execution_source != "cassette_replay"
        ):
            raise ValueError("legacy_import routing decisions are cassette replay only")
        return self


ProjectionField = Literal["prompt_version", "model_snapshot"]
ProjectionMode = Literal["not_applicable", "single", "set"]


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _projection_tuple_value(field: ProjectionField, members: tuple[str, ...]) -> str | None:
    if not members:
        return None
    if len(members) == 1:
        return members[0]
    digest = canonical_sha256({"field": field, "members": list(members)})
    prefix = "prompt-set" if field == "prompt_version" else "model-set"
    return f"{prefix}:sha256:{digest}"


class VersionSetProjectionV1(_StrictModel):
    field: ProjectionField
    mode: ProjectionMode
    members: tuple[NonEmptyStr, ...]
    tuple_value: str | None = None

    @model_validator(mode="after")
    def validate_projection(self) -> VersionSetProjectionV1:
        if len(set(self.members)) != len(self.members):
            raise ValueError("projection members must be stable-unique")
        expected_mode: ProjectionMode
        if not self.members:
            expected_mode = "not_applicable"
        elif len(self.members) == 1:
            expected_mode = "single"
        else:
            expected_mode = "set"
        if self.mode != expected_mode:
            raise ValueError("projection mode does not match its members")
        expected_value = _projection_tuple_value(self.field, self.members)
        if self.tuple_value != expected_value:
            raise ValueError("projection tuple_value does not match its members")
        return self


def build_version_set_projection(
    field: ProjectionField, values: Iterable[str]
) -> VersionSetProjectionV1:
    members = _stable_unique(values)
    mode: ProjectionMode = (
        "not_applicable" if not members else "single" if len(members) == 1 else "set"
    )
    return VersionSetProjectionV1(
        field=field,
        mode=mode,
        members=members,
        tuple_value=_projection_tuple_value(field, members),
    )


class ExecutionIdentityV1(_StrictModel):
    identity_schema_version: Literal["execution-identity@1"] = EXECUTION_IDENTITY_SCHEMA_VERSION
    scope: Literal["record_shard", "attempt", "run", "artifact"]
    agent_graph_version: str | None = None
    bindings: tuple[InvocationVersionBindingV1, ...] = Field(
        max_length=MAX_RUNTIME_AUTHORITY_BINDINGS
    )
    prompt_projection: VersionSetProjectionV1
    model_projection: VersionSetProjectionV1
    digest: LowerHexSha256

    @model_validator(mode="after")
    def validate_identity(self) -> ExecutionIdentityV1:
        keys = [
            (binding.attempt_no, binding.call_ordinal, binding.route_ordinal)
            for binding in self.bindings
        ]
        if keys != sorted(keys):
            raise ValueError("execution identity bindings must be canonically sorted")
        if len(keys) != len(set(keys)):
            raise ValueError("execution identity binding tuple must be unique")

        logical_calls: dict[tuple[int, int], list[InvocationVersionBindingV1]] = {}
        for binding in self.bindings:
            logical_calls.setdefault((binding.attempt_no, binding.call_ordinal), []).append(binding)
        if self.scope == "record_shard":
            if len(self.bindings) != 1 or not self.bindings[0].response_consumed:
                raise ValueError("record_shard identity requires exactly one consumed route")
        for call_bindings in logical_calls.values():
            route_ordinals = [binding.route_ordinal for binding in call_bindings]
            if self.scope != "record_shard" and route_ordinals != list(
                range(1, len(route_ordinals) + 1)
            ):
                raise ValueError("route_ordinal must start at 1 and increase without gaps")
            if sum(binding.response_consumed for binding in call_bindings) > 1:
                raise ValueError("a logical call may have at most one response_consumed route")

        projection_bindings = (
            tuple(binding for binding in self.bindings if binding.response_consumed)
            if self.scope == "record_shard"
            else self.bindings
        )
        expected_prompt = build_version_set_projection(
            "prompt_version", (binding.prompt_version for binding in projection_bindings)
        )
        expected_model = build_version_set_projection(
            "model_snapshot", (binding.model_snapshot for binding in projection_bindings)
        )
        if self.prompt_projection != expected_prompt:
            raise ValueError("prompt projection does not match invocation bindings")
        if self.model_projection != expected_model:
            raise ValueError("model projection does not match invocation bindings")
        if self.digest != execution_identity_digest(self):
            raise ValueError("execution identity digest does not match its payload")
        return self


def execution_identity_digest(identity: ExecutionIdentityV1) -> str:
    return canonical_sha256(identity.model_dump(mode="json", exclude={"digest"}))


def build_execution_identity(
    *,
    scope: Literal["record_shard", "attempt", "run", "artifact"],
    bindings: Iterable[InvocationVersionBindingV1],
    agent_graph_version: str | None = None,
) -> ExecutionIdentityV1:
    ordered = tuple(
        sorted(
            bindings,
            key=lambda item: (item.attempt_no, item.call_ordinal, item.route_ordinal),
        )
    )
    projection_bindings = (
        tuple(binding for binding in ordered if binding.response_consumed)
        if scope == "record_shard"
        else ordered
    )
    prompt_projection = build_version_set_projection(
        "prompt_version", (binding.prompt_version for binding in projection_bindings)
    )
    model_projection = build_version_set_projection(
        "model_snapshot", (binding.model_snapshot for binding in projection_bindings)
    )
    payload = {
        "identity_schema_version": EXECUTION_IDENTITY_SCHEMA_VERSION,
        "scope": scope,
        "agent_graph_version": agent_graph_version,
        "bindings": [binding.model_dump(mode="json") for binding in ordered],
        "prompt_projection": prompt_projection.model_dump(mode="json"),
        "model_projection": model_projection.model_dump(mode="json"),
    }
    return ExecutionIdentityV1(
        **payload,
        digest=canonical_sha256(payload),
    )


def _canonical_lineage(lineage: Iterable[str]) -> tuple[str, ...]:
    values = tuple(lineage)
    if any(not isinstance(parent, str) or not parent for parent in values):
        raise ValueError("lineage parent ids must be non-empty strings")
    if len(values) != len(set(values)):
        raise ValueError("lineage contains a duplicate parent artifact id")
    return tuple(sorted(values))


def artifact_id_v2_for(
    *,
    kind: ArtifactKind,
    version_tuple: VersionTuple,
    lineage: Iterable[str],
    payload_hash: LowerHexSha256,
    meta: Mapping[str, Any],
) -> str:
    parents = _canonical_lineage(lineage)
    return compute_snapshot_id(
        {
            "lineage_schema_version": LINEAGE_SCHEMA_VERSION_V2,
            "kind": kind,
            "version_tuple": version_tuple.model_dump(mode="json"),
            "lineage": list(parents),
            "payload_hash": payload_hash,
            "meta": _json_value(meta),
        }
    )


class ArtifactV2(_StrictModel):
    artifact_id: NonEmptyStr
    lineage_schema_version: Literal["lineage@2"] = LINEAGE_SCHEMA_VERSION_V2
    kind: ArtifactKind
    version_tuple: VersionTuple
    lineage: tuple[NonEmptyStr, ...] = Field(max_length=MAX_RUNTIME_AUTHORITY_BINDINGS)
    payload_hash: LowerHexSha256
    object_ref: ObjectRef
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("lineage", mode="before")
    @classmethod
    def canonicalize_lineage(cls, value: Iterable[str]) -> tuple[str, ...]:
        return _canonical_lineage(value)

    @field_validator("meta", mode="before")
    @classmethod
    def parse_reserved_meta(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError("ArtifactV2 meta must be an object")
        parsed = dict(value)
        if "execution_identity" in parsed:
            parsed["execution_identity"] = ExecutionIdentityV1.model_validate(
                parsed["execution_identity"]
            )
        return parsed

    @model_validator(mode="after")
    def validate_artifact(self) -> ArtifactV2:
        if self.payload_hash != self.object_ref.sha256:
            raise ValueError("ArtifactV2 payload_hash must equal ObjectRef.sha256")

        identity = self.meta.get("execution_identity")
        if identity is not None:
            if not isinstance(identity, ExecutionIdentityV1):
                raise ValueError("meta.execution_identity must be ExecutionIdentityV1")
            expected = {
                "prompt_version": identity.prompt_projection.tuple_value,
                "model_snapshot": identity.model_projection.tuple_value,
                "agent_graph_version": identity.agent_graph_version,
            }
            for field_name, expected_value in expected.items():
                if getattr(self.version_tuple, field_name) != expected_value:
                    raise ValueError(f"VersionTuple.{field_name} does not match execution identity")

        expected_id = artifact_id_v2_for(
            kind=self.kind,
            version_tuple=self.version_tuple,
            lineage=self.lineage,
            payload_hash=self.payload_hash,
            meta=self.meta,
        )
        if self.artifact_id != expected_id:
            raise ValueError("ArtifactV2 artifact_id does not match canonical content")
        return self


def build_artifact_v2(
    *,
    kind: ArtifactKind,
    version_tuple: VersionTuple,
    lineage: Iterable[str],
    payload_hash: LowerHexSha256,
    object_ref: ObjectRef,
    meta: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> ArtifactV2:
    lineage_values = tuple(lineage)
    canonical_parents = tuple(sorted(set(lineage_values)))
    immutable_meta = dict(meta or {})
    artifact_id = artifact_id_v2_for(
        kind=kind,
        version_tuple=version_tuple,
        lineage=canonical_parents,
        payload_hash=payload_hash,
        meta=immutable_meta,
    )
    return ArtifactV2(
        artifact_id=artifact_id,
        kind=kind,
        version_tuple=version_tuple,
        lineage=lineage_values,
        payload_hash=payload_hash,
        object_ref=object_ref,
        created_at=created_at,
        meta=immutable_meta,
    )


ArtifactWire = Annotated[ArtifactV1 | ArtifactV2, Field(discriminator="lineage_schema_version")]
_ARTIFACT_ADAPTER = TypeAdapter(ArtifactWire)


def parse_artifact(value: Any) -> ArtifactV1 | ArtifactV2:
    return _ARTIFACT_ADAPTER.validate_python(value)


class AuditActor(_StrictModel):
    principal_id: NonEmptyStr
    principal_kind: Literal["human", "service", "system"]


class AuditSubject(_StrictModel):
    resource_kind: NonEmptyStr
    resource_id: NonEmptyStr
    artifact_id: str | None = None


class AuditCorrelation(_StrictModel):
    request_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None


class AuditRecordV1(BaseModel):
    """Permanent reader and source-compatible constructor for ``audit@1``."""

    audit_schema_version: Literal["audit@1"] = AUDIT_SCHEMA_VERSION
    seq: int
    actor: str
    action: str
    artifact_id: str | None = None
    ts: str
    content_hash: str
    prev_hash: str | None = None


# Historical imports construct ``AuditRecord(...)`` directly.
AuditRecord = AuditRecordV1


def audit_content_hash_v2(
    *,
    chain_id: str,
    seq: int,
    actor: AuditActor,
    initiated_by: AuditActor | None,
    action: str,
    subject: AuditSubject,
    correlation: AuditCorrelation,
    ts: str,
    prev_hash: str | None,
) -> str:
    return canonical_sha256(
        {
            "audit_schema_version": AUDIT_SCHEMA_VERSION_V2,
            "chain_id": chain_id,
            "seq": seq,
            "actor": actor.model_dump(mode="json"),
            "initiated_by": (
                None if initiated_by is None else initiated_by.model_dump(mode="json")
            ),
            "action": action,
            "subject": subject.model_dump(mode="json"),
            "correlation": correlation.model_dump(mode="json"),
            "ts": ts,
            "prev_hash": prev_hash,
        }
    )


class AuditRecordV2(_StrictModel):
    audit_schema_version: Literal["audit@2"] = AUDIT_SCHEMA_VERSION_V2
    chain_id: NonEmptyStr
    seq: int = Field(ge=1)
    actor: AuditActor
    initiated_by: AuditActor | None = None
    action: NonEmptyStr
    subject: AuditSubject
    correlation: AuditCorrelation
    ts: NonEmptyStr
    prev_hash: LowerHexSha256 | None = None
    content_hash: LowerHexSha256

    @model_validator(mode="after")
    def validate_content_hash(self) -> AuditRecordV2:
        expected = audit_content_hash_v2(
            chain_id=self.chain_id,
            seq=self.seq,
            actor=self.actor,
            initiated_by=self.initiated_by,
            action=self.action,
            subject=self.subject,
            correlation=self.correlation,
            ts=self.ts,
            prev_hash=self.prev_hash,
        )
        if self.content_hash != expected:
            raise ValueError("AuditRecordV2 content_hash does not match canonical content")
        return self


def build_audit_record_v2(
    *,
    chain_id: str,
    seq: int,
    actor: AuditActor,
    initiated_by: AuditActor | None,
    action: str,
    subject: AuditSubject,
    correlation: AuditCorrelation,
    ts: str,
    prev_hash: str | None,
) -> AuditRecordV2:
    content_hash = audit_content_hash_v2(
        chain_id=chain_id,
        seq=seq,
        actor=actor,
        initiated_by=initiated_by,
        action=action,
        subject=subject,
        correlation=correlation,
        ts=ts,
        prev_hash=prev_hash,
    )
    return AuditRecordV2(
        chain_id=chain_id,
        seq=seq,
        actor=actor,
        initiated_by=initiated_by,
        action=action,
        subject=subject,
        correlation=correlation,
        ts=ts,
        prev_hash=prev_hash,
        content_hash=content_hash,
    )


AuditRecordWire = Annotated[
    AuditRecordV1 | AuditRecordV2,
    Field(discriminator="audit_schema_version"),
]
_AUDIT_ADAPTER = TypeAdapter(AuditRecordWire)


def parse_audit_record(value: Any) -> AuditRecordV1 | AuditRecordV2:
    return _AUDIT_ADAPTER.validate_python(value)
