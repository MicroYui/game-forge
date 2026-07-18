"""Trusted structured preimages for opaque, catalog-bound model identities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import stat
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.apps.worker.agent_prompt_context import (
    build_builtin_agent_prompt_context_authority,
)
from gameforge.contracts.cassette_import import LegacyImportVerificationPolicyRegistryV1
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.cost import PriceBook
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.contracts.reliability import CircuitBreakerConfigV1
from gameforge.apps.worker.prompt_rendering import CanonicalPromptRendererAuthority
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.run_handlers.model_routing import ExactModelCatalogSnapshotResolver
from gameforge.runtime.cassette.legacy_authority_manifest import (
    LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
    MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_CHARS,
    load_legacy_import_authority,
)
from gameforge.runtime.cassette.legacy_import import LegacyImportAuthority
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.price_book import UnavailablePriceBook
from gameforge.runtime.model_router.openai_responses_transport import OpenAIResponsesTransport
from gameforge.runtime.model_router.typed_transport import TypedLlmTransport
from gameforge.runtime.model_router.typed_transport import LegacyTypedTransportAdapter
from gameforge.runtime.reliability.breaker import CircuitBreaker


NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]
MODEL_SNAPSHOT_MANIFEST_PATH_ENV = "GAMEFORGE_WORKER_MODEL_SNAPSHOT_MANIFEST_PATH"
MODEL_GATEWAY_BASE_URL_ENV = "GAMEFORGE_LLM_BASE_URL"
MODEL_GATEWAY_API_KEY_ENV = "GAMEFORGE_LLM_KEY"
DEFAULT_MODEL_GATEWAY_BASE_URL = "http://localhost:4141"
_MAX_MODEL_SNAPSHOT_MANIFEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkerModelExecutionAuthorities:
    """Exact deployment closure required before any model-capable worker is ready."""

    transport: TypedLlmTransport
    snapshots: "StaticStructuredModelSnapshotAuthority"
    prompt_renderer: CanonicalPromptRendererAuthority
    price_book: PriceBook
    legacy_imports: LegacyImportAuthority | None
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
        if self.legacy_imports is not None:
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
            ) or any(
                not callable(getattr(self.legacy_imports, name, None)) for name in legacy_methods
            ):
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
    def __init__(
        self,
        manifest: (StructuredModelSnapshotManifestV1 | Sequence[StructuredModelSnapshotManifestV1]),
    ) -> None:
        source = (
            (manifest,) if isinstance(manifest, StructuredModelSnapshotManifestV1) else manifest
        )
        self.manifests = tuple(
            StructuredModelSnapshotManifestV1.model_validate(shard.model_dump(mode="python"))
            for shard in source
        )
        if not self.manifests:
            raise IntegrityViolation("structured model snapshot authority is empty")
        authority_versions = {shard.authority_version for shard in self.manifests}
        if len(authority_versions) != 1:
            raise IntegrityViolation(
                "structured model snapshot manifests have different authority versions"
            )
        self.manifest = self.manifests[0]
        self._by_id: dict[str, ModelSnapshot] = {}
        for shard in self.manifests:
            for item in shard.bindings:
                if item.model_snapshot_id in self._by_id:
                    raise IntegrityViolation(
                        "structured model snapshot identity appears in more than one manifest",
                        model_snapshot_id=item.model_snapshot_id,
                    )
                self._by_id[item.model_snapshot_id] = ModelSnapshot.model_validate(
                    item.snapshot.model_dump(mode="python")
                )

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
        with self._unit_of_work.begin_read() as transaction:  # type: ignore[attr-defined]
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


def load_structured_model_snapshot_authority(
    path: str | Path,
) -> StaticStructuredModelSnapshotAuthority:
    """Load one manifest file or every bounded shard in one flat directory."""

    manifest_paths = _model_snapshot_manifest_paths(Path(path).expanduser())
    manifests = tuple(
        parse_structured_model_snapshot_manifest(_read_bounded_manifest(manifest_path))
        for manifest_path in manifest_paths
    )
    return StaticStructuredModelSnapshotAuthority(manifests)


def load_local_model_execution_authorities(
    *,
    environment: Mapping[str, str] | None = None,
) -> WorkerModelExecutionAuthorities:
    """Load the concrete local worker's complete model execution authority."""

    source = os.environ if environment is None else environment
    api_key = source.get(MODEL_GATEWAY_API_KEY_ENV)
    if not isinstance(api_key, str) or not api_key or len(api_key) > 4096:
        raise IntegrityViolation("worker model gateway API key is required")
    base_url = source.get(MODEL_GATEWAY_BASE_URL_ENV, DEFAULT_MODEL_GATEWAY_BASE_URL)
    _validate_gateway_base_url(base_url)
    manifest_path_value = source.get(MODEL_SNAPSHOT_MANIFEST_PATH_ENV)
    if not isinstance(manifest_path_value, str) or not manifest_path_value:
        raise IntegrityViolation("worker model snapshot manifest path is required")
    snapshots = load_structured_model_snapshot_authority(manifest_path_value)
    clock = SystemUtcClock()
    breaker_config = CircuitBreakerConfigV1(
        config_version="local-model-provider-breaker@1",
        rolling_window_s=60,
        minimum_samples=2,
        failure_threshold=1,
        open_cooldown_s=10,
        half_open_max_concurrent_probes=1,
        half_open_success_threshold=1,
    )
    breakers = StaticCircuitBreakerAuthority(
        {
            model_snapshot_id: CircuitBreaker(
                dependency_id=f"model-provider:{model_snapshot_id}",
                config=breaker_config,
                clock=clock,
            )
            for model_snapshot_id in snapshots.model_snapshot_ids
        }
    )
    registry = build_builtin_registry()
    required_prompt_plan_keys = tuple(
        sorted(
            {
                (node.agent_node_id, node.prompt_version, node.tool_version)
                for graph in registry.list_agent_execution_graphs()
                if graph.status in {"active", "replay_only"}
                for node in graph.nodes
            }
        )
    )
    legacy_manifest_path = source.get(LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV)
    if legacy_manifest_path is None:
        legacy_imports = None
    elif (
        not isinstance(legacy_manifest_path, str)
        or not legacy_manifest_path
        or len(legacy_manifest_path) > MAX_LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in legacy_manifest_path)
    ):
        raise IntegrityViolation("worker legacy import authority manifest path is invalid")
    else:
        legacy_imports = load_legacy_import_authority(legacy_manifest_path)
    prompt_renderer = build_builtin_agent_prompt_context_authority(
        required_plan_keys=required_prompt_plan_keys,
    )
    transport = LegacyTypedTransportAdapter(
        OpenAIResponsesTransport(base_url=base_url, api_key=api_key)
    )
    try:
        return WorkerModelExecutionAuthorities(
            transport=transport,
            snapshots=snapshots,
            prompt_renderer=prompt_renderer,
            price_book=UnavailablePriceBook(),
            legacy_imports=legacy_imports,
            circuit_breaker_resolver=breakers,
        )
    except Exception:
        transport.close()
        raise


def _read_bounded_manifest(path: Path) -> str:
    try:
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise IntegrityViolation(
                "worker model snapshot directory must contain only regular manifest files"
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise IntegrityViolation("worker model snapshot manifest changed while opening")
            payload = handle.read(_MAX_MODEL_SNAPSHOT_MANIFEST_BYTES + 1)
    except IntegrityViolation:
        raise
    except OSError as exc:
        raise IntegrityViolation("worker model snapshot manifest is unreadable") from exc
    if len(payload) > _MAX_MODEL_SNAPSHOT_MANIFEST_BYTES:
        raise IntegrityViolation("worker model snapshot manifest exceeds its byte bound")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityViolation("worker model snapshot manifest is not UTF-8") from exc


def _model_snapshot_manifest_paths(path: Path) -> tuple[Path, ...]:
    try:
        root = path.lstat()
    except OSError as exc:
        raise IntegrityViolation("worker model snapshot manifest path is unreadable") from exc
    if stat.S_ISLNK(root.st_mode):
        raise IntegrityViolation("worker model snapshot manifest path cannot be a symbolic link")
    if stat.S_ISREG(root.st_mode):
        return (path,)
    if not stat.S_ISDIR(root.st_mode):
        raise IntegrityViolation(
            "worker model snapshot manifest path must be a regular file or directory"
        )
    try:
        entries = tuple(sorted(path.iterdir(), key=lambda entry: entry.name))
    except OSError as exc:
        raise IntegrityViolation("worker model snapshot manifest directory is unreadable") from exc
    if not entries:
        raise IntegrityViolation("worker model snapshot manifest directory is empty")
    for entry in entries:
        try:
            entry_stat = entry.lstat()
        except OSError as exc:
            raise IntegrityViolation(
                "worker model snapshot manifest directory is unreadable"
            ) from exc
        if not stat.S_ISREG(entry_stat.st_mode):
            raise IntegrityViolation(
                "worker model snapshot directory must contain only regular manifest files"
            )
    return entries


def _validate_gateway_base_url(base_url: object) -> None:
    if (
        not isinstance(base_url, str)
        or not base_url
        or len(base_url) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in base_url)
    ):
        raise IntegrityViolation("worker model gateway base URL is invalid")
    parsed = urlsplit(base_url)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise IntegrityViolation("worker model gateway base URL is invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.query
        or parsed.fragment
    ):
        raise IntegrityViolation("worker model gateway base URL is invalid")
    if parsed.scheme == "http" and not _is_loopback_hostname(hostname):
        raise IntegrityViolation("worker model gateway base URL is invalid")


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


__all__ = [
    "DEFAULT_MODEL_GATEWAY_BASE_URL",
    "LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV",
    "MODEL_GATEWAY_API_KEY_ENV",
    "MODEL_GATEWAY_BASE_URL_ENV",
    "MODEL_SNAPSHOT_MANIFEST_PATH_ENV",
    "StaticStructuredModelSnapshotAuthority",
    "StaticCircuitBreakerAuthority",
    "StructuredModelSnapshotBindingV1",
    "StructuredModelSnapshotManifestV1",
    "WorkerModelExecutionAuthorities",
    "WorkerModelSnapshotResolver",
    "load_local_model_execution_authorities",
    "load_structured_model_snapshot_authority",
    "parse_structured_model_snapshot_manifest",
]
