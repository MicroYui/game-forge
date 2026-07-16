"""Trusted structured preimages for opaque, catalog-bound model identities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cassette_import import LegacyImportVerificationPolicyRegistryV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.cost import PriceBook
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.platform.run_handlers.model_routing import ExactModelCatalogSnapshotResolver
from gameforge.runtime.cassette.legacy_import import LegacyImportAuthority
from gameforge.runtime.model_router.typed_transport import TypedLlmTransport
from gameforge.runtime.reliability.breaker import CircuitBreaker


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]


@dataclass(frozen=True, slots=True)
class WorkerModelExecutionAuthorities:
    """Exact deployment closure required before any model-capable worker is ready."""

    transport: TypedLlmTransport
    snapshots: "StaticStructuredModelSnapshotAuthority"
    prompt_renderer: CanonicalPromptRendererAuthority
    price_book: PriceBook
    legacy_imports: LegacyImportAuthority
    circuit_breaker_resolver: "StaticCircuitBreakerAuthority"

    def __post_init__(self) -> None:
        required = (
            (self.transport, "complete", "model transport"),
            (self.price_book, "lookup", "price-book authority"),
        )
        for authority, method, label in required:
            if not callable(getattr(authority, method, None)):
                raise ValueError(f"{label} is incomplete")
        if not isinstance(self.prompt_renderer, CanonicalPromptRendererAuthority):
            raise ValueError("prompt renderer authority has an invalid type")
        if not isinstance(self.snapshots, StaticStructuredModelSnapshotAuthority):
            raise ValueError("model snapshot authority must retain an exact manifest")
        legacy_methods = (
            "resolve_model_catalog",
            "resolve_input_binding",
            "resolve_profile_binding",
            "resolve_policy_binding",
            "resolve_schema_binding",
            "resolve_rendered_request",
            "resolve_frozen_version_tuple",
            "resolve_call_tool_version",
        )
        if not isinstance(
            getattr(self.legacy_imports, "verification_policy_registry", None),
            LegacyImportVerificationPolicyRegistryV1,
        ) or any(not callable(getattr(self.legacy_imports, name, None)) for name in legacy_methods):
            raise ValueError("legacy import authority surface is incomplete")
        if not isinstance(self.circuit_breaker_resolver, StaticCircuitBreakerAuthority):
            raise ValueError("circuit-breaker authority must retain exact dependencies")


class StructuredModelSnapshotBindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    model_snapshot_id: NonEmptyStr
    snapshot: ModelSnapshot

    @model_validator(mode="after")
    def _canonical_preimage(self) -> "StructuredModelSnapshotBindingV1":
        try:
            canonical = canonical_model_snapshot_id(self.snapshot)
        except ValueError as exc:
            raise ValueError("structured model snapshot preimage is invalid") from exc
        if canonical != self.model_snapshot_id:
            raise ValueError("structured model snapshot does not hash to its opaque identity")
        return self


def _manifest_digest(value: object) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json", exclude={"manifest_digest"})
    elif isinstance(value, Mapping):
        payload = dict(value)
        payload.pop("manifest_digest", None)
    else:
        raise TypeError("structured model manifest must be a model or mapping")
    payload.setdefault("manifest_schema_version", "structured-model-snapshots@1")
    payload["bindings"] = sorted(
        payload.get("bindings", ()),
        key=lambda item: item["model_snapshot_id"],
    )
    return canonical_sha256(payload)


class StructuredModelSnapshotManifestV1(BaseModel):
    """Content-bound deployment authority; it contains no provider credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    manifest_schema_version: Literal["structured-model-snapshots@1"] = (
        "structured-model-snapshots@1"
    )
    authority_version: NonEmptyStr
    bindings: tuple[StructuredModelSnapshotBindingV1, ...] = Field(min_length=1, max_length=1024)
    manifest_digest: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("bindings")
    @classmethod
    def _canonical_bindings(
        cls,
        value: tuple[StructuredModelSnapshotBindingV1, ...],
    ) -> tuple[StructuredModelSnapshotBindingV1, ...]:
        identities = [item.model_snapshot_id for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("structured model snapshot identities must be unique")
        return tuple(sorted(value, key=lambda item: item.model_snapshot_id))

    @model_validator(mode="after")
    def _digest(self) -> "StructuredModelSnapshotManifestV1":
        if self.manifest_digest != _manifest_digest(self):
            raise ValueError("structured model snapshot manifest digest differs")
        return self


class StaticStructuredModelSnapshotAuthority:
    def __init__(self, manifest: StructuredModelSnapshotManifestV1) -> None:
        self.manifest = StructuredModelSnapshotManifestV1.model_validate(
            manifest.model_dump(mode="python")
        )
        self._by_id = {item.model_snapshot_id: item.snapshot for item in manifest.bindings}

    def get_model_snapshot(self, model_snapshot_id: str) -> ModelSnapshot | None:
        retained = self._by_id.get(model_snapshot_id)
        return (
            None
            if retained is None
            else ModelSnapshot.model_validate(retained.model_dump(mode="python"))
        )

    @property
    def model_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))


class StaticCircuitBreakerAuthority:
    """Exact dependency-scoped breaker closure for all deployed model snapshots."""

    def __init__(self, breakers: Mapping[str, CircuitBreaker]) -> None:
        retained = dict(breakers)
        if not retained:
            raise ValueError("circuit-breaker authority cannot be empty")
        for model_snapshot_id, breaker in retained.items():
            if (
                not isinstance(model_snapshot_id, str)
                or not model_snapshot_id
                or not isinstance(breaker, CircuitBreaker)
                or breaker.dependency_id != f"model-provider:{model_snapshot_id}"
            ):
                raise ValueError("circuit-breaker dependency binding is invalid")
        self._breakers = retained

    @property
    def model_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._breakers))

    def __call__(self, decision: RoutingDecisionV1) -> CircuitBreaker:
        retained = self._breakers.get(decision.model_snapshot)
        if retained is None:
            raise IntegrityViolation(
                "model snapshot has no dependency-scoped circuit breaker",
                model_snapshot=decision.model_snapshot,
            )
        return retained


class WorkerModelSnapshotResolver:
    """Resolve every call against its exact persisted catalog and deployment preimage."""

    def __init__(self, *, unit_of_work: object, snapshots: object) -> None:
        self._unit_of_work = unit_of_work
        self._snapshots = snapshots

    def resolve_model_snapshot(
        self,
        *,
        catalog_version: int,
        catalog_digest: str,
        model_snapshot_id: str,
    ) -> ModelSnapshot:
        with self._unit_of_work.begin() as transaction:  # type: ignore[attr-defined]
            resolver = ExactModelCatalogSnapshotResolver(
                catalogs=transaction.cost,
                snapshots=self._snapshots,
            )
            return resolver.resolve_model_snapshot(
                catalog_version=catalog_version,
                catalog_digest=catalog_digest,
                model_snapshot_id=model_snapshot_id,
            )


def parse_structured_model_snapshot_manifest(
    raw: str,
) -> StructuredModelSnapshotManifestV1:
    try:
        payload = json.loads(raw)
        return StructuredModelSnapshotManifestV1.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise IntegrityViolation(
            "structured model snapshot deployment authority is invalid"
        ) from exc


__all__ = [
    "StaticStructuredModelSnapshotAuthority",
    "StaticCircuitBreakerAuthority",
    "StructuredModelSnapshotBindingV1",
    "StructuredModelSnapshotManifestV1",
    "WorkerModelExecutionAuthorities",
    "WorkerModelSnapshotResolver",
    "parse_structured_model_snapshot_manifest",
]
