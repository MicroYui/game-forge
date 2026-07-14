"""Deterministic verified import contracts for historical ``cassette@1`` wires.

This module validates evidence only. Publishing cassette-bundle Artifacts and
resolving the referenced persisted objects belong to the M4c composition layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256, sha256_lowerhex
from gameforge.contracts.cassette import (
    CassetteRecordV1,
    CassetteRecordV2,
    parse_cassette_record,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import (
    ExecutionIdentityV1,
    InvocationVersionBindingV1,
    VersionTuple,
)
from gameforge.contracts.model_router import ModelRequestV1, request_hash
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    canonical_model_snapshot_id,
)


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
JsonPointer = Annotated[
    str,
    StringConstraints(
        max_length=2048,
        pattern=r"^(?:|(?:/(?:[^~/]|~[01])*)+)$",
    ),
]
PositiveInt = Annotated[int, Field(ge=1)]
MAX_BINDINGS = 4096


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


def _without(payload: Mapping[str, Any] | BaseModel, *keys: str) -> dict[str, Any]:
    raw = dict(_json_data(payload))
    for key in keys:
        raw.pop(key, None)
    return raw


def _canonical_unique_strings(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return tuple(sorted(values))


def original_wire_sha256(original_wire_utf8: str) -> str:
    try:
        raw = original_wire_utf8.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("legacy cassette wire must be valid UTF-8") from exc
    return sha256_lowerhex(raw)


class LegacyImportVerificationPolicyRefV1(_FrozenModel):
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    policy_digest: Sha256Hex


def compute_legacy_import_verification_policy_digest(
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    raw = _without(payload, "policy_digest")
    raw.setdefault(
        "policy_schema_version",
        "legacy-import-verification-policy@1",
    )
    for field_name in (
        "required_input_binding_keys",
        "required_profile_field_paths",
        "required_policy_binding_keys",
        "required_schema_binding_keys",
    ):
        raw[field_name] = sorted(raw.get(field_name, ()))
    return canonical_sha256(raw)


class LegacyImportVerificationPolicyV1(_FrozenModel):
    policy_schema_version: Literal["legacy-import-verification-policy@1"] = (
        "legacy-import-verification-policy@1"
    )
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    source_cassette_schema_version: Literal["cassette@1"] = "cassette@1"
    ordinal_mapping: Literal["single_attempt_by_source_call_ordinal"] = (
        "single_attempt_by_source_call_ordinal"
    )
    required_input_binding_keys: tuple[NonEmptyStr, ...] = Field(max_length=MAX_BINDINGS)
    required_profile_field_paths: tuple[JsonPointer, ...] = Field(max_length=MAX_BINDINGS)
    required_policy_binding_keys: tuple[NonEmptyStr, ...] = Field(max_length=MAX_BINDINGS)
    required_schema_binding_keys: tuple[NonEmptyStr, ...] = Field(max_length=MAX_BINDINGS)
    max_wire_bytes_per_call: PositiveInt
    max_calls_per_import: PositiveInt
    policy_digest: Sha256Hex

    @field_validator(
        "required_input_binding_keys",
        "required_profile_field_paths",
        "required_policy_binding_keys",
        "required_schema_binding_keys",
    )
    @classmethod
    def _canonical_required_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, label="required policy bindings")

    @model_validator(mode="after")
    def _digest_matches(self) -> LegacyImportVerificationPolicyV1:
        if self.policy_digest != compute_legacy_import_verification_policy_digest(self):
            raise ValueError("policy_digest does not match verification policy")
        return self

    @classmethod
    def create(cls, **values: Any) -> LegacyImportVerificationPolicyV1:
        payload = {
            "policy_schema_version": "legacy-import-verification-policy@1",
            "source_cassette_schema_version": "cassette@1",
            "ordinal_mapping": "single_attempt_by_source_call_ordinal",
            **values,
        }
        for field_name in (
            "required_input_binding_keys",
            "required_profile_field_paths",
            "required_policy_binding_keys",
            "required_schema_binding_keys",
        ):
            payload[field_name] = tuple(sorted(payload.get(field_name, ())))
        return cls(
            **payload,
            policy_digest=compute_legacy_import_verification_policy_digest(payload),
        )

    def ref(self) -> LegacyImportVerificationPolicyRefV1:
        return LegacyImportVerificationPolicyRefV1(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            policy_digest=self.policy_digest,
        )


def compute_legacy_import_verification_registry_digest(
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    raw = _without(payload, "registry_digest")
    raw.setdefault(
        "registry_schema_version",
        "legacy-import-verification-policy-registry@1",
    )
    raw["policies"] = sorted(
        raw.get("policies", ()),
        key=lambda item: (item["policy_id"], item["policy_version"]),
    )
    return canonical_sha256(raw)


class LegacyImportVerificationPolicyRegistryV1(_FrozenModel):
    registry_schema_version: Literal["legacy-import-verification-policy-registry@1"] = (
        "legacy-import-verification-policy-registry@1"
    )
    registry_version: PositiveInt
    policies: tuple[LegacyImportVerificationPolicyV1, ...] = Field(max_length=MAX_BINDINGS)
    registry_digest: Sha256Hex

    @field_validator("policies")
    @classmethod
    def _canonical_policies(
        cls,
        value: tuple[LegacyImportVerificationPolicyV1, ...],
    ) -> tuple[LegacyImportVerificationPolicyV1, ...]:
        keys = [(policy.policy_id, policy.policy_version) for policy in value]
        if len(keys) != len(set(keys)):
            raise ValueError("verification policy identities must be unique")
        return tuple(sorted(value, key=lambda item: (item.policy_id, item.policy_version)))

    @model_validator(mode="after")
    def _digest_matches(self) -> LegacyImportVerificationPolicyRegistryV1:
        if self.registry_digest != compute_legacy_import_verification_registry_digest(self):
            raise ValueError("registry_digest does not match verification policy registry")
        return self

    @classmethod
    def create(
        cls,
        *,
        registry_version: int,
        policies: Sequence[LegacyImportVerificationPolicyV1],
    ) -> LegacyImportVerificationPolicyRegistryV1:
        ordered = tuple(sorted(policies, key=lambda item: (item.policy_id, item.policy_version)))
        payload = {
            "registry_schema_version": "legacy-import-verification-policy-registry@1",
            "registry_version": registry_version,
            "policies": [_json_data(policy) for policy in ordered],
        }
        return cls(
            registry_version=registry_version,
            policies=ordered,
            registry_digest=compute_legacy_import_verification_registry_digest(payload),
        )


def resolve_legacy_import_verification_policy(
    registry: LegacyImportVerificationPolicyRegistryV1,
    ref: LegacyImportVerificationPolicyRefV1,
) -> LegacyImportVerificationPolicyV1:
    for policy in registry.policies:
        if policy.policy_id == ref.policy_id and policy.policy_version == ref.policy_version:
            if policy.policy_digest != ref.policy_digest:
                raise IntegrityViolation(
                    "legacy import verification policy digest differs from retained policy"
                )
            return policy
    raise IntegrityViolation("legacy import verification policy is not retained")


class LegacyCassetteInputBindingV1(_FrozenModel):
    binding_key: NonEmptyStr
    artifact_id: NonEmptyStr
    payload_hash: Sha256Hex
    version_tuple: VersionTuple


class LegacyCassetteProfileBindingV1(_FrozenModel):
    field_path: JsonPointer
    profile_id: NonEmptyStr
    profile_version: PositiveInt
    profile_payload_hash: Sha256Hex
    catalog_version: PositiveInt
    catalog_digest: Sha256Hex


def compute_legacy_profile_binding_digest(
    binding: LegacyCassetteProfileBindingV1,
) -> str:
    return canonical_sha256(binding.model_dump(mode="json"))


class LegacyCassettePolicyBindingV1(_FrozenModel):
    binding_key: NonEmptyStr
    policy_kind: NonEmptyStr
    policy_id: NonEmptyStr
    policy_version: PositiveInt
    policy_digest: Sha256Hex


class LegacyCassetteSchemaBindingV1(_FrozenModel):
    binding_key: NonEmptyStr
    schema_id: NonEmptyStr


def compute_legacy_import_routing_decision_id(
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    digest = canonical_sha256(_without(payload, "decision_id"))
    return f"legacy-import-route:sha256:{digest}"


class LegacyImportRoutingDecisionV1(_FrozenModel):
    decision_schema_version: Literal["legacy-import-routing-decision@1"] = (
        "legacy-import-routing-decision@1"
    )
    decision_id: NonEmptyStr
    source_wire_sha256: Sha256Hex
    request_hash: RequestHash
    agent_node_id: NonEmptyStr
    model_snapshot: NonEmptyStr
    execution_source: Literal["cassette_replay"] = "cassette_replay"
    execution_profile_binding_digests: tuple[Sha256Hex, ...] = Field(max_length=MAX_BINDINGS)
    model_catalog_version: PositiveInt
    model_catalog_digest: Sha256Hex
    verification_policy: LegacyImportVerificationPolicyRefV1

    @field_validator("execution_profile_binding_digests")
    @classmethod
    def _unique_profile_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("execution profile binding digests must be unique")
        return value

    @model_validator(mode="after")
    def _decision_id_matches(self) -> LegacyImportRoutingDecisionV1:
        if self.decision_id != compute_legacy_import_routing_decision_id(self):
            raise ValueError("decision_id does not match legacy import routing decision")
        return self

    @classmethod
    def create(cls, **values: Any) -> LegacyImportRoutingDecisionV1:
        payload = {
            "decision_schema_version": "legacy-import-routing-decision@1",
            "execution_source": "cassette_replay",
            **values,
        }
        return cls(
            **payload,
            decision_id=compute_legacy_import_routing_decision_id(payload),
        )


def _parse_original_v1_wire(original_wire_utf8: str) -> tuple[dict[str, Any], CassetteRecordV1]:
    try:
        payload = json.loads(original_wire_utf8)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("original legacy cassette wire is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("original legacy cassette wire must contain one JSON object")
    if payload.get("cassette_schema_version") != "cassette@1":
        raise ValueError("original legacy cassette wire is not cassette@1")
    try:
        parsed = parse_cassette_record(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("original legacy cassette wire is invalid") from exc
    if not isinstance(parsed, CassetteRecordV1) or isinstance(parsed, CassetteRecordV2):
        raise ValueError("original legacy cassette wire is not cassette@1")
    return payload, parsed


def compute_legacy_call_evidence_digest(
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    return canonical_sha256(_without(payload, "evidence_digest"))


class LegacyCassetteCallImportEvidenceV1(_FrozenModel):
    evidence_schema_version: Literal["legacy-cassette-call-import@1"] = (
        "legacy-cassette-call-import@1"
    )
    original_wire_utf8: str
    original_wire_sha256: Sha256Hex
    rendered_request_artifact_id: NonEmptyStr | None = None
    request_hash: RequestHash | None = None
    import_routing_decision: LegacyImportRoutingDecisionV1 | None = None
    invocation: InvocationVersionBindingV1 | None = None
    source_suite_id: NonEmptyStr
    source_case_id: NonEmptyStr
    source_call_ordinal: PositiveInt
    importer_tool_version: NonEmptyStr
    verification_status: Literal["verified", "evidence_missing"]
    missing_fields: tuple[JsonPointer, ...] = Field(max_length=MAX_BINDINGS)
    evidence_digest: Sha256Hex

    @field_validator("missing_fields")
    @classmethod
    def _canonical_missing_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_unique_strings(value, label="missing_fields")

    @model_validator(mode="after")
    def _closed_evidence(self) -> LegacyCassetteCallImportEvidenceV1:
        if self.original_wire_sha256 != original_wire_sha256(self.original_wire_utf8):
            raise ValueError("original_wire_sha256 does not match original wire bytes")
        _, record = _parse_original_v1_wire(self.original_wire_utf8)
        complete = (
            self.rendered_request_artifact_id is not None
            and self.request_hash is not None
            and self.import_routing_decision is not None
            and self.invocation is not None
        )
        if self.verification_status == "verified":
            if self.missing_fields or not complete:
                raise ValueError(
                    "verified evidence requires every proof field and no missing_fields"
                )
        elif not self.missing_fields:
            raise ValueError("evidence_missing requires non-empty missing_fields")

        if self.request_hash is not None and record.request_hash != self.request_hash:
            raise ValueError("legacy record request hash differs from evidence")
        decision = self.import_routing_decision
        if decision is not None:
            if decision.source_wire_sha256 != self.original_wire_sha256:
                raise ValueError("legacy routing decision source wire differs from evidence")
            if self.request_hash is not None and decision.request_hash != self.request_hash:
                raise ValueError("legacy routing decision request hash differs from evidence")
            if decision.agent_node_id != record.agent_node_id:
                raise ValueError("legacy routing decision agent differs from original wire")
            if decision.model_snapshot != canonical_model_snapshot_id(record.model_snapshot):
                raise ValueError("legacy routing decision model differs from original wire")
        invocation = self.invocation
        if invocation is not None:
            if (
                invocation.attempt_no != 1
                or invocation.call_ordinal != self.source_call_ordinal
                or invocation.route_ordinal != 1
                or invocation.transport_attempt is not None
                or invocation.routing_decision_kind != "legacy_import"
                or invocation.execution_source != "cassette_replay"
                or not invocation.response_consumed
            ):
                raise ValueError("legacy invocation does not use the fixed synthetic mapping")
            if invocation.agent_node_id != record.agent_node_id:
                raise ValueError("legacy invocation agent differs from original wire")
            if invocation.model_snapshot != canonical_model_snapshot_id(record.model_snapshot):
                raise ValueError("legacy invocation model differs from original wire")
            if decision is not None and invocation.routing_decision_id != decision.decision_id:
                raise ValueError("legacy invocation routing decision differs from evidence")
        if self.evidence_digest != compute_legacy_call_evidence_digest(self):
            raise ValueError("evidence_digest does not match call import evidence")
        return self

    @classmethod
    def create(
        cls, *, original_wire_utf8: str, **values: Any
    ) -> LegacyCassetteCallImportEvidenceV1:
        payload = {
            "evidence_schema_version": "legacy-cassette-call-import@1",
            "original_wire_utf8": original_wire_utf8,
            "original_wire_sha256": original_wire_sha256(original_wire_utf8),
            **values,
        }
        payload["missing_fields"] = tuple(sorted(payload.get("missing_fields", ())))
        return cls(
            **payload,
            evidence_digest=compute_legacy_call_evidence_digest(payload),
        )


def compute_legacy_import_id(payload: Mapping[str, Any] | BaseModel) -> str:
    digest = canonical_sha256(_without(payload, "import_id", "digest"))
    return f"legacy-cassette-import:sha256:{digest}"


def compute_legacy_import_manifest_digest(
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    return canonical_sha256(_without(payload, "digest"))


class LegacyCassetteRunImportManifestV1(_FrozenModel):
    manifest_schema_version: Literal["legacy-cassette-run-import@1"] = (
        "legacy-cassette-run-import@1"
    )
    import_id: NonEmptyStr
    source_suite_id: NonEmptyStr
    source_case_id: NonEmptyStr
    verification_policy: LegacyImportVerificationPolicyRefV1
    input_artifact_bindings: tuple[LegacyCassetteInputBindingV1, ...] = Field(
        max_length=MAX_BINDINGS
    )
    execution_profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...] = Field(
        max_length=MAX_BINDINGS
    )
    frozen_version_tuple: VersionTuple | None = None
    policy_bindings: tuple[LegacyCassettePolicyBindingV1, ...] = Field(max_length=MAX_BINDINGS)
    schema_bindings: tuple[LegacyCassetteSchemaBindingV1, ...] = Field(max_length=MAX_BINDINGS)
    ordered_call_evidence_digests: tuple[Sha256Hex, ...] = Field(max_length=MAX_BINDINGS)
    execution_identity: ExecutionIdentityV1 | None = None
    importer_tool_version: NonEmptyStr
    status: Literal["verified", "evidence_missing"]
    digest: Sha256Hex

    @field_validator("input_artifact_bindings")
    @classmethod
    def _canonical_inputs(
        cls,
        value: tuple[LegacyCassetteInputBindingV1, ...],
    ) -> tuple[LegacyCassetteInputBindingV1, ...]:
        keys = [item.binding_key for item in value]
        artifact_ids = [item.artifact_id for item in value]
        if len(keys) != len(set(keys)) or len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("legacy input binding keys and artifact ids must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("execution_profile_bindings")
    @classmethod
    def _canonical_profiles(
        cls,
        value: tuple[LegacyCassetteProfileBindingV1, ...],
    ) -> tuple[LegacyCassetteProfileBindingV1, ...]:
        keys = [item.field_path for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("legacy profile binding paths must be unique")
        return tuple(sorted(value, key=lambda item: item.field_path))

    @field_validator("policy_bindings")
    @classmethod
    def _canonical_policy_bindings(
        cls,
        value: tuple[LegacyCassettePolicyBindingV1, ...],
    ) -> tuple[LegacyCassettePolicyBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("legacy policy binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("schema_bindings")
    @classmethod
    def _canonical_schema_bindings(
        cls,
        value: tuple[LegacyCassetteSchemaBindingV1, ...],
    ) -> tuple[LegacyCassetteSchemaBindingV1, ...]:
        keys = [item.binding_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("legacy schema binding keys must be unique")
        return tuple(sorted(value, key=lambda item: item.binding_key))

    @field_validator("ordered_call_evidence_digests")
    @classmethod
    def _unique_evidence_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("ordered call evidence digests must be unique")
        return value

    @model_validator(mode="after")
    def _closed_manifest(self) -> LegacyCassetteRunImportManifestV1:
        if self.import_id != compute_legacy_import_id(self):
            raise ValueError("import_id does not match legacy import manifest")
        if self.status == "verified" and (
            self.frozen_version_tuple is None or self.execution_identity is None
        ):
            raise ValueError("verified manifest requires version tuple and execution identity")
        if self.status == "evidence_missing" and (
            self.frozen_version_tuple is not None or self.execution_identity is not None
        ):
            raise ValueError("evidence-missing manifest cannot claim aggregate execution identity")
        identity = self.execution_identity
        version_tuple = self.frozen_version_tuple
        if identity is not None:
            if identity.scope != "run":
                raise ValueError("legacy import execution identity must have run scope")
            for binding in identity.bindings:
                if (
                    binding.routing_decision_kind != "legacy_import"
                    or binding.execution_source != "cassette_replay"
                    or binding.transport_attempt is not None
                    or not binding.response_consumed
                ):
                    raise ValueError("legacy import identity contains a non-import invocation")
        if identity is not None and version_tuple is not None:
            if (
                version_tuple.prompt_version != identity.prompt_projection.tuple_value
                or version_tuple.model_snapshot != identity.model_projection.tuple_value
                or version_tuple.agent_graph_version != identity.agent_graph_version
            ):
                raise ValueError("frozen version tuple differs from aggregate execution identity")
        if self.digest != compute_legacy_import_manifest_digest(self):
            raise ValueError("manifest digest does not match legacy import manifest")
        return self


def build_legacy_import_manifest(**values: Any) -> LegacyCassetteRunImportManifestV1:
    payload = {
        "manifest_schema_version": "legacy-cassette-run-import@1",
        **values,
    }
    for field_name, key in (
        ("input_artifact_bindings", "binding_key"),
        ("execution_profile_bindings", "field_path"),
        ("policy_bindings", "binding_key"),
        ("schema_bindings", "binding_key"),
    ):

        def sort_key(item: Any, field: str = key) -> Any:
            return item[field] if isinstance(item, Mapping) else getattr(item, field)

        payload[field_name] = tuple(sorted(payload.get(field_name, ()), key=sort_key))
    import_id = compute_legacy_import_id(payload)
    with_id = {**payload, "import_id": import_id}
    return LegacyCassetteRunImportManifestV1(
        **with_id,
        digest=compute_legacy_import_manifest_digest(with_id),
    )


CassetteRecordWire = CassetteRecordV1 | CassetteRecordV2


class CassetteBundleV1(_FrozenModel):
    bundle_schema_version: Literal["cassette-bundle@1"] = "cassette-bundle@1"
    scope: Literal["record_shard", "attempt", "run"]
    run_id: NonEmptyStr | None = None
    attempt_no: PositiveInt | None = None
    ordinal: PositiveInt | None = None
    outcome_code: NonEmptyStr | None = None
    child_bundle_artifact_ids: tuple[NonEmptyStr, ...] = Field(
        default=(),
        max_length=MAX_BINDINGS,
    )
    records: tuple[CassetteRecordWire, ...] = Field(default=(), max_length=1)
    legacy_call_import_evidence: LegacyCassetteCallImportEvidenceV1 | None = None
    legacy_run_import_manifest: LegacyCassetteRunImportManifestV1 | None = None

    @field_validator("child_bundle_artifact_ids")
    @classmethod
    def _unique_child_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("cassette bundle child artifact ids must be unique")
        return value

    @field_validator("records", mode="before")
    @classmethod
    def _parse_record_union(cls, value: Any) -> tuple[CassetteRecordWire, ...]:
        parsed: list[CassetteRecordWire] = []
        for item in value or ():
            if isinstance(item, (CassetteRecordV1, CassetteRecordV2)):
                parsed.append(item)
            elif isinstance(item, Mapping):
                parsed.append(parse_cassette_record(item))
            else:
                raise ValueError("cassette bundle record is not a supported wire object")
        return tuple(parsed)

    @model_validator(mode="after")
    def _closed_bundle_shape(self) -> CassetteBundleV1:
        if self.scope == "record_shard":
            if self.attempt_no is None or self.ordinal is None:
                raise ValueError("record shard requires attempt_no and ordinal")
            if self.child_bundle_artifact_ids or len(self.records) != 1:
                raise ValueError("record shard requires one record and no children")
            if self.legacy_run_import_manifest is not None:
                raise ValueError("record shard cannot carry a run import manifest")
            record = self.records[0]
            if self.legacy_call_import_evidence is None:
                if self.run_id is None or not isinstance(record, CassetteRecordV2):
                    raise ValueError("native record shard requires run_id and cassette@2")
            elif (
                self.run_id is not None
                or not isinstance(record, CassetteRecordV1)
                or isinstance(record, CassetteRecordV2)
            ):
                raise ValueError("imported record shard requires no run_id and cassette@1")
            if (
                isinstance(record, CassetteRecordV1)
                and record.cassette_schema_version != "cassette@1"
            ):
                raise ValueError("imported record shard must retain cassette@1 discriminator")
        elif self.scope == "attempt":
            if self.attempt_no is None or self.ordinal is not None:
                raise ValueError("attempt bundle requires only attempt_no")
            if self.records:
                raise ValueError("attempt bundle cannot inline records")
            if (
                self.legacy_call_import_evidence is not None
                or self.legacy_run_import_manifest is not None
            ):
                raise ValueError("attempt bundle cannot carry legacy import evidence directly")
        else:
            if self.attempt_no is not None or self.ordinal is not None or self.records:
                raise ValueError("run bundle cannot carry attempt identity or inline records")
            if self.legacy_call_import_evidence is not None:
                raise ValueError("run bundle cannot carry call import evidence")
            if self.run_id is None:
                if self.legacy_run_import_manifest is None:
                    raise ValueError("imported run bundle requires a run import manifest")
            elif self.legacy_run_import_manifest is not None:
                raise ValueError("native run bundle cannot carry a legacy import manifest")
        return self


def _same_v1_record(left: CassetteRecordV1, right: CassetteRecordV1) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def _require_exact_keys(
    actual: Sequence[str],
    expected: Sequence[str],
    *,
    label: str,
) -> None:
    if tuple(actual) != tuple(expected):
        raise IntegrityViolation(f"legacy import {label} do not match verification policy")


def _validate_call(
    *,
    evidence: LegacyCassetteCallImportEvidenceV1,
    record: CassetteRecordV1,
    policy: LegacyImportVerificationPolicyV1,
    profile_bindings: tuple[LegacyCassetteProfileBindingV1, ...],
    model_catalog: ModelCatalogSnapshotV1,
    rendered_request: ModelRequestV1 | None,
    expected_invocation: InvocationVersionBindingV1 | None,
) -> None:
    try:
        wire_size = len(evidence.original_wire_utf8.encode("utf-8"))
        _, parsed = _parse_original_v1_wire(evidence.original_wire_utf8)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IntegrityViolation("legacy call original wire is invalid") from exc
    if wire_size > policy.max_wire_bytes_per_call:
        raise IntegrityViolation("legacy call original wire exceeds verification policy")
    if not _same_v1_record(parsed, record):
        raise IntegrityViolation("legacy bundle record differs from original wire")

    decision = evidence.import_routing_decision
    invocation = evidence.invocation
    if decision is not None:
        if decision.verification_policy != policy.ref():
            raise IntegrityViolation("legacy routing decision verification policy differs")
        expected_profile_digests = tuple(
            compute_legacy_profile_binding_digest(binding) for binding in profile_bindings
        )
        if decision.execution_profile_binding_digests != expected_profile_digests:
            raise IntegrityViolation("legacy routing decision profile binding digests differ")
        if (
            decision.model_catalog_version != model_catalog.catalog_version
            or decision.model_catalog_digest != model_catalog.catalog_digest
        ):
            raise IntegrityViolation("legacy routing decision model catalog differs")
        known_models = {descriptor.model_snapshot for descriptor in model_catalog.models}
        if decision.model_snapshot not in known_models:
            raise IntegrityViolation("legacy routing decision model is absent from exact catalog")

    if evidence.verification_status == "verified":
        if rendered_request is None or expected_invocation is None:
            raise IntegrityViolation("verified legacy call lacks rendered request evidence")
        if rendered_request.model_router_schema_version != "model-router@1":
            raise IntegrityViolation("verified legacy call rendered request is not model-router@1")
        expected_hash = request_hash(rendered_request)
        if (
            evidence.request_hash != expected_hash
            or record.request_hash != expected_hash
            or decision is None
            or decision.request_hash != expected_hash
        ):
            raise IntegrityViolation("verified legacy call request hash does not close")
        expected_model = canonical_model_snapshot_id(rendered_request.model_snapshot)
        if (
            rendered_request.agent_node_id != record.agent_node_id
            or expected_model != canonical_model_snapshot_id(record.model_snapshot)
            or decision.model_snapshot != expected_model
        ):
            raise IntegrityViolation("verified legacy call request identity does not close")
        if invocation is None or invocation != expected_invocation:
            raise IntegrityViolation("verified legacy call invocation evidence differs")
        if (
            invocation.agent_node_id != rendered_request.agent_node_id
            or invocation.prompt_version != rendered_request.prompt_version
            or invocation.model_snapshot != expected_model
        ):
            raise IntegrityViolation("verified legacy call invocation request binding differs")


def _validate_manifest(
    *,
    manifest: LegacyCassetteRunImportManifestV1,
    policy: LegacyImportVerificationPolicyV1,
    evidences: tuple[LegacyCassetteCallImportEvidenceV1, ...],
) -> None:
    _require_exact_keys(
        tuple(item.binding_key for item in manifest.input_artifact_bindings),
        policy.required_input_binding_keys,
        label="input binding keys",
    )
    _require_exact_keys(
        tuple(item.field_path for item in manifest.execution_profile_bindings),
        policy.required_profile_field_paths,
        label="profile binding paths",
    )
    _require_exact_keys(
        tuple(item.binding_key for item in manifest.policy_bindings),
        policy.required_policy_binding_keys,
        label="policy binding keys",
    )
    _require_exact_keys(
        tuple(item.binding_key for item in manifest.schema_bindings),
        policy.required_schema_binding_keys,
        label="schema binding keys",
    )
    if len(evidences) > policy.max_calls_per_import:
        raise IntegrityViolation("legacy import call count exceeds verification policy")
    expected_digests = tuple(evidence.evidence_digest for evidence in evidences)
    if manifest.ordered_call_evidence_digests != expected_digests:
        raise IntegrityViolation("legacy import ordered call evidence digests differ")
    for evidence in evidences:
        if (
            evidence.source_suite_id != manifest.source_suite_id
            or evidence.source_case_id != manifest.source_case_id
            or evidence.importer_tool_version != manifest.importer_tool_version
        ):
            raise IntegrityViolation("legacy call provenance differs from run import manifest")

    any_missing = (
        manifest.frozen_version_tuple is None
        or manifest.execution_identity is None
        or any(item.verification_status != "verified" for item in evidences)
    )
    expected_status = "evidence_missing" if any_missing else "verified"
    if manifest.status != expected_status:
        raise IntegrityViolation("legacy import manifest status differs from call evidence")
    if manifest.status == "verified":
        identity = manifest.execution_identity
        if identity is None:
            raise IntegrityViolation("verified legacy import lacks execution identity")
        expected_bindings = tuple(item.invocation for item in evidences)
        if any(binding is None for binding in expected_bindings):
            raise IntegrityViolation("verified legacy import has an incomplete invocation")
        if identity.bindings != expected_bindings:
            raise IntegrityViolation("legacy import execution identity differs from call evidence")


def validate_legacy_import_bundle_tree(
    root: CassetteBundleV1,
    child_bundles_by_artifact_id: Mapping[str, CassetteBundleV1],
    *,
    policy_registry: LegacyImportVerificationPolicyRegistryV1,
    model_catalog: ModelCatalogSnapshotV1,
    rendered_requests_by_artifact_id: Mapping[str, ModelRequestV1],
    expected_invocations_by_artifact_id: Mapping[str, InvocationVersionBindingV1],
) -> tuple[CassetteRecordV1, ...]:
    """Validate one imported three-level bundle tree and return replay order.

    The child mapping is exact: unreachable extras are rejected so callers
    cannot validate a convenient subset while publishing contradictory shards.
    """

    manifest = root.legacy_run_import_manifest
    if root.scope != "run" or root.run_id is not None or manifest is None:
        raise IntegrityViolation("legacy import root must be an imported run bundle")
    policy = resolve_legacy_import_verification_policy(
        policy_registry,
        manifest.verification_policy,
    )
    if len(root.child_bundle_artifact_ids) != 1:
        raise IntegrityViolation("legacy import requires one synthetic attempt bundle")

    visited: set[str] = set()
    attempt_id = root.child_bundle_artifact_ids[0]
    attempt = child_bundles_by_artifact_id.get(attempt_id)
    if attempt is None:
        raise IntegrityViolation("legacy import attempt bundle is missing")
    visited.add(attempt_id)
    if attempt.scope != "attempt" or attempt.run_id is not None or attempt.attempt_no != 1:
        raise IntegrityViolation("legacy import attempt bundle has invalid synthetic identity")

    records: list[CassetteRecordV1] = []
    evidences: list[LegacyCassetteCallImportEvidenceV1] = []
    previous_call_ordinal = 0
    for shard_id in attempt.child_bundle_artifact_ids:
        shard = child_bundles_by_artifact_id.get(shard_id)
        if shard is None:
            raise IntegrityViolation("legacy import record shard is missing")
        if shard_id in visited:
            raise IntegrityViolation("legacy import bundle tree repeats a child artifact")
        visited.add(shard_id)
        evidence = shard.legacy_call_import_evidence
        if (
            shard.scope != "record_shard"
            or shard.run_id is not None
            or shard.attempt_no != 1
            or evidence is None
            or shard.ordinal != evidence.source_call_ordinal
            or len(shard.records) != 1
            or not isinstance(shard.records[0], CassetteRecordV1)
            or isinstance(shard.records[0], CassetteRecordV2)
        ):
            raise IntegrityViolation("legacy import record shard has invalid structure")
        if evidence.source_call_ordinal <= previous_call_ordinal:
            raise IntegrityViolation("legacy import record shards are not in canonical call order")
        previous_call_ordinal = evidence.source_call_ordinal
        record = shard.records[0]
        rendered_request = (
            rendered_requests_by_artifact_id.get(evidence.rendered_request_artifact_id)
            if evidence.rendered_request_artifact_id is not None
            else None
        )
        expected_invocation = (
            expected_invocations_by_artifact_id.get(evidence.rendered_request_artifact_id)
            if evidence.rendered_request_artifact_id is not None
            else None
        )
        _validate_call(
            evidence=evidence,
            record=record,
            policy=policy,
            profile_bindings=manifest.execution_profile_bindings,
            model_catalog=model_catalog,
            rendered_request=rendered_request,
            expected_invocation=expected_invocation,
        )
        records.append(record)
        evidences.append(evidence)

    extra = set(child_bundles_by_artifact_id).difference(visited)
    if extra:
        raise IntegrityViolation("legacy import child mapping contains unreachable bundles")
    _validate_manifest(
        manifest=manifest,
        policy=policy,
        evidences=tuple(evidences),
    )
    return tuple(records)


def require_verified_legacy_import_bundle_tree(
    root: CassetteBundleV1,
    child_bundles_by_artifact_id: Mapping[str, CassetteBundleV1],
    *,
    policy_registry: LegacyImportVerificationPolicyRegistryV1,
    model_catalog: ModelCatalogSnapshotV1,
    rendered_requests_by_artifact_id: Mapping[str, ModelRequestV1],
    expected_invocations_by_artifact_id: Mapping[str, InvocationVersionBindingV1],
) -> tuple[CassetteRecordV1, ...]:
    records = validate_legacy_import_bundle_tree(
        root,
        child_bundles_by_artifact_id,
        policy_registry=policy_registry,
        model_catalog=model_catalog,
        rendered_requests_by_artifact_id=rendered_requests_by_artifact_id,
        expected_invocations_by_artifact_id=expected_invocations_by_artifact_id,
    )
    manifest = root.legacy_run_import_manifest
    if manifest is None or manifest.status != "verified":
        raise IntegrityViolation("legacy cassette import is not executable")
    return records


__all__ = [
    "CassetteBundleV1",
    "LegacyCassetteCallImportEvidenceV1",
    "LegacyCassetteInputBindingV1",
    "LegacyCassettePolicyBindingV1",
    "LegacyCassetteProfileBindingV1",
    "LegacyCassetteRunImportManifestV1",
    "LegacyCassetteSchemaBindingV1",
    "LegacyImportRoutingDecisionV1",
    "LegacyImportVerificationPolicyRefV1",
    "LegacyImportVerificationPolicyRegistryV1",
    "LegacyImportVerificationPolicyV1",
    "build_legacy_import_manifest",
    "compute_legacy_call_evidence_digest",
    "compute_legacy_import_id",
    "compute_legacy_import_manifest_digest",
    "compute_legacy_import_routing_decision_id",
    "compute_legacy_import_verification_policy_digest",
    "compute_legacy_import_verification_registry_digest",
    "compute_legacy_profile_binding_digest",
    "original_wire_sha256",
    "require_verified_legacy_import_bundle_tree",
    "resolve_legacy_import_verification_policy",
    "validate_legacy_import_bundle_tree",
]
