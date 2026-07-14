"""Versioned M4b model-catalog and routing decision contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cost import CostDimension
from gameforge.contracts.model_router import ModelSnapshot


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
ProviderId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RequestHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _json_data(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_data(item) for item in value]
    return value


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


def canonical_model_snapshot_id(snapshot: ModelSnapshot) -> str:
    """Return a provider-qualified opaque identity for a structured legacy snapshot."""

    provider = snapshot.provider
    if not provider or provider.lower() != provider:
        raise ValueError("model snapshot provider must be a lowercase namespace")
    digest = canonical_sha256(snapshot.model_dump(mode="json"))
    return f"{provider}:sha256:{digest}"


class ModelDescriptorV1(_FrozenModel):
    descriptor_schema_version: Literal["model-descriptor@1"] = "model-descriptor@1"
    provider: ProviderId
    model_snapshot: NonEmptyStr
    tier: NonEmptyStr
    capabilities: tuple[NonEmptyStr, ...]
    context_limit: PositiveInt
    max_output_tokens: PositiveInt
    prompt_cache_support: bool
    status: Literal["active", "disabled"]

    @field_validator("capabilities")
    @classmethod
    def _canonical_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("model capabilities must be unique")
        return canonical

    @model_validator(mode="after")
    def _provider_namespace(self) -> ModelDescriptorV1:
        if not self.model_snapshot.startswith(f"{self.provider}:"):
            raise ValueError("model_snapshot namespace must match descriptor provider")
        if self.max_output_tokens > self.context_limit:
            raise ValueError("max output tokens cannot exceed model context limit")
        return self


def compute_model_catalog_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    raw.pop("catalog_digest", None)
    raw["catalog_schema_version"] = raw.get("catalog_schema_version", "model-catalog@1")
    raw["models"] = sorted(
        raw.get("models", ()),
        key=lambda item: (item["provider"], item["model_snapshot"]),
    )
    return canonical_sha256(raw)


class ModelCatalogSnapshotV1(_FrozenModel):
    catalog_schema_version: Literal["model-catalog@1"] = "model-catalog@1"
    catalog_version: PositiveInt
    models: tuple[ModelDescriptorV1, ...]
    created_at: datetime
    catalog_digest: Sha256Hex

    @field_validator("created_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("models")
    @classmethod
    def _canonical_models(
        cls, value: tuple[ModelDescriptorV1, ...]
    ) -> tuple[ModelDescriptorV1, ...]:
        provider_keys = [(item.provider, item.model_snapshot) for item in value]
        snapshot_ids = [item.model_snapshot for item in value]
        if len(provider_keys) != len(set(provider_keys)):
            raise ValueError("provider/model snapshot identities must be unique")
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("model snapshot ids must be globally unique across providers")
        return tuple(sorted(value, key=lambda item: (item.provider, item.model_snapshot)))

    @model_validator(mode="after")
    def _digest_matches(self) -> ModelCatalogSnapshotV1:
        if self.catalog_digest != compute_model_catalog_digest(self):
            raise ValueError("catalog_digest does not match canonical model catalog")
        return self


class RoutingBudgetPredicateV1(_FrozenModel):
    dimension: CostDimension
    operation: Literal["lt", "lte", "eq", "gte", "gt"]
    value: Annotated[Decimal, Field(ge=0)]
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None

    @model_validator(mode="after")
    def _currency_shape(self) -> RoutingBudgetPredicateV1:
        if (self.dimension == "monetary") != (self.currency is not None):
            raise ValueError("currency belongs exactly to monetary predicates")
        return self


class RoutingRuleV1(_FrozenModel):
    rule_id: NonEmptyStr
    task_kind: NonEmptyStr
    domain_scope: tuple[NonEmptyStr, ...] | None = None
    required_capabilities: tuple[NonEmptyStr, ...]
    primary_model_snapshot: NonEmptyStr
    allowed_fallback_chain: tuple[NonEmptyStr, ...]
    budget_predicates: tuple[RoutingBudgetPredicateV1, ...]

    @field_validator("domain_scope")
    @classmethod
    def _canonical_domain_scope(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        canonical = tuple(sorted(set(value)))
        if not canonical or len(canonical) != len(value):
            raise ValueError("domain scope must be non-empty and unique when provided")
        return canonical

    @field_validator("required_capabilities")
    @classmethod
    def _canonical_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        canonical = tuple(sorted(set(value)))
        if len(canonical) != len(value):
            raise ValueError("required capabilities must be unique")
        return canonical

    @field_validator("allowed_fallback_chain")
    @classmethod
    def _unique_fallbacks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("fallback chain members must be unique")
        return value

    @field_validator("budget_predicates")
    @classmethod
    def _canonical_predicates(
        cls, value: tuple[RoutingBudgetPredicateV1, ...]
    ) -> tuple[RoutingBudgetPredicateV1, ...]:
        keys = [(item.dimension, item.operation, item.currency or "") for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("routing budget predicates must have unique identities")
        return tuple(
            sorted(value, key=lambda item: (item.dimension, item.operation, item.currency or ""))
        )

    @model_validator(mode="after")
    def _model_chain(self) -> RoutingRuleV1:
        if self.primary_model_snapshot in self.allowed_fallback_chain:
            raise ValueError("primary model cannot be repeated in fallback chain")
        return self


def compute_routing_policy_digest(payload: Mapping[str, Any] | BaseModel) -> str:
    raw = _json_data(payload)
    raw.pop("routing_policy_digest", None)
    raw["routing_schema_version"] = raw.get("routing_schema_version", "routing-policy@1")
    raw["rules"] = sorted(raw.get("rules", ()), key=lambda item: item["rule_id"])
    return canonical_sha256(raw)


class RoutingPolicyV1(_FrozenModel):
    routing_schema_version: Literal["routing-policy@1"] = "routing-policy@1"
    policy_version: PositiveInt
    catalog_version: PositiveInt
    catalog_digest: Sha256Hex
    rules: tuple[RoutingRuleV1, ...]
    failure_classifier_version: NonEmptyStr
    routing_policy_digest: Sha256Hex

    @field_validator("rules")
    @classmethod
    def _canonical_rules(cls, value: tuple[RoutingRuleV1, ...]) -> tuple[RoutingRuleV1, ...]:
        ids = [item.rule_id for item in value]
        exact_selectors = [
            (
                item.task_kind,
                item.domain_scope,
                item.required_capabilities,
                item.budget_predicates,
            )
            for item in value
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("routing rule ids must be unique")
        if len(exact_selectors) != len(set(exact_selectors)):
            raise ValueError("exact duplicate routing selectors are ambiguous")
        return tuple(sorted(value, key=lambda item: item.rule_id))

    @model_validator(mode="after")
    def _digest_matches(self) -> RoutingPolicyV1:
        if self.routing_policy_digest != compute_routing_policy_digest(self):
            raise ValueError("routing_policy_digest does not match canonical policy")
        return self


class RoutingDecisionV1(_FrozenModel):
    decision_schema_version: Literal["routing-decision@1"] = "routing-decision@1"
    decision_id: NonEmptyStr
    run_id: NonEmptyStr
    attempt_no: PositiveInt
    request_hash: RequestHash
    rule_id: NonEmptyStr
    model_snapshot: NonEmptyStr
    tier: NonEmptyStr
    reason_code: NonEmptyStr
    budget_set_snapshot_id: NonEmptyStr
    fallback_from: NonEmptyStr | None = None
    fallback_index: NonNegativeInt
    policy_version: PositiveInt
    routing_policy_digest: Sha256Hex
    catalog_version: PositiveInt
    catalog_digest: Sha256Hex
    execution_source: Literal["online", "full_response_cache", "cassette_replay"]
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def _closed_identity(self) -> RoutingDecisionV1:
        if (self.fallback_index == 0) != (self.fallback_from is None):
            raise ValueError(
                "primary decision has no fallback source; fallback decisions require one"
            )
        payload = self.model_dump(mode="json", exclude={"decision_id"})
        expected = "routing-decision:sha256:" + canonical_sha256(payload)
        if self.decision_id != expected:
            raise ValueError("decision_id does not match canonical routing decision")
        return self

    @classmethod
    def create(cls, **values: Any) -> RoutingDecisionV1:
        payload = {"decision_schema_version": "routing-decision@1", **values}
        decision_id = "routing-decision:sha256:" + canonical_sha256(_json_data(payload))
        return cls(decision_id=decision_id, **values)


def validate_policy_catalog_closure(
    policy: RoutingPolicyV1,
    catalog: ModelCatalogSnapshotV1,
) -> None:
    if (
        policy.catalog_version != catalog.catalog_version
        or policy.catalog_digest != catalog.catalog_digest
    ):
        raise ValueError("routing policy catalog reference does not match exact catalog")
    known = {item.model_snapshot for item in catalog.models}
    for rule in policy.rules:
        referenced = (rule.primary_model_snapshot, *rule.allowed_fallback_chain)
        missing = set(referenced).difference(known)
        if missing:
            raise ValueError("routing rule references unknown model snapshots")


__all__ = [
    "ModelCatalogSnapshotV1",
    "ModelDescriptorV1",
    "RoutingBudgetPredicateV1",
    "RoutingDecisionV1",
    "RoutingPolicyV1",
    "RoutingRuleV1",
    "canonical_model_snapshot_id",
    "compute_model_catalog_digest",
    "compute_routing_policy_digest",
    "validate_policy_catalog_closure",
]
