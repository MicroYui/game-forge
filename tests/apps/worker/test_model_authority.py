from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gameforge.apps.worker.model_authority import (
    LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV,
    MODEL_GATEWAY_API_KEY_ENV,
    MODEL_GATEWAY_BASE_URL_ENV,
    MODEL_SNAPSHOT_MANIFEST_PATH_ENV,
    StaticCircuitBreakerAuthority,
    StaticStructuredModelSnapshotAuthority,
    StructuredModelSnapshotManifestV1,
    WorkerModelSnapshotResolver,
    WorkerModelExecutionAuthorities,
    load_local_model_execution_authorities,
    parse_structured_model_snapshot_manifest,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import (
    ModelCatalogSnapshotV1,
    ModelDescriptorV1,
    canonical_model_snapshot_id,
    compute_model_catalog_digest,
)
from gameforge.contracts.reliability import CircuitBreakerConfigV1
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cassette.legacy_authority_manifest import (
    LegacyCallToolVersionBindingV1,
    LegacyFrozenVersionTupleBindingV1,
    LegacyImportAuthorityManifestV1,
    LegacyRenderedRequestBindingV1,
)
from gameforge.runtime.cost.price_book import UnavailablePriceBook
from gameforge.runtime.reliability.breaker import CircuitBreaker
from tests.platform.m4c.test_replay_admission import _legacy_verified_fixture


def _manifest(snapshot: ModelSnapshot) -> dict[str, object]:
    payload: dict[str, object] = {
        "manifest_schema_version": "structured-model-snapshots@1",
        "authority_version": "deployment-models@1",
        "bindings": [
            {
                "model_snapshot_id": canonical_model_snapshot_id(snapshot),
                "snapshot": snapshot.model_dump(mode="json"),
            }
        ],
    }
    return {**payload, "manifest_digest": canonical_sha256(payload)}


def _catalog(snapshot: ModelSnapshot) -> tuple[ModelCatalogSnapshotV1, str]:
    model_snapshot_id = canonical_model_snapshot_id(snapshot)
    payload = {
        "catalog_version": 1,
        "models": (
            ModelDescriptorV1(
                provider=snapshot.provider,
                model_snapshot=model_snapshot_id,
                tier="reasoning",
                capabilities=("reasoning",),
                context_limit=200_000,
                max_output_tokens=32_000,
                prompt_cache_support=True,
                status="active",
            ),
        ),
        "created_at": datetime(2026, 7, 18, tzinfo=UTC),
    }
    return (
        ModelCatalogSnapshotV1(
            **payload,
            catalog_digest=compute_model_catalog_digest(payload),
        ),
        model_snapshot_id,
    )


def test_content_bound_manifest_resolves_without_reverse_parsing_opaque_id() -> None:
    snapshot = ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="2026-07",
    )
    manifest = parse_structured_model_snapshot_manifest(json.dumps(_manifest(snapshot)))
    authority = StaticStructuredModelSnapshotAuthority(manifest)

    assert authority.get_model_snapshot(canonical_model_snapshot_id(snapshot)) == snapshot
    assert authority.get_model_snapshot("openai:sha256:" + "0" * 64) is None


def test_worker_snapshot_resolution_uses_read_uow_without_sqlite_writer_scope() -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    catalog, model_snapshot_id = _catalog(snapshot)

    class CatalogAuthority:
        def get_model_catalog(self, catalog_version: int, catalog_digest: str):
            if (
                catalog_version == catalog.catalog_version
                and catalog_digest == catalog.catalog_digest
            ):
                return catalog
            return None

    class SnapshotAuthority:
        def get_model_snapshot(self, selected_model_snapshot_id: str):
            return snapshot if selected_model_snapshot_id == model_snapshot_id else None

    class ReadUnitOfWork:
        def __init__(self) -> None:
            self.read_count = 0

        def begin(self):
            raise AssertionError("snapshot resolution must not acquire a write UoW")

        @contextmanager
        def begin_read(self):
            self.read_count += 1
            yield SimpleNamespace(cost=CatalogAuthority())

    unit_of_work = ReadUnitOfWork()
    resolver = WorkerModelSnapshotResolver(
        unit_of_work=unit_of_work,
        snapshots=SnapshotAuthority(),
    )

    assert (
        resolver.resolve_model_snapshot(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            model_snapshot_id=model_snapshot_id,
        )
        == snapshot
    )
    assert unit_of_work.read_count == 1


def test_snapshot_authority_does_not_retain_caller_owned_nested_models() -> None:
    snapshot = ModelSnapshot(provider="openai", model="safe", snapshot_tag="v1")
    manifest = StructuredModelSnapshotManifestV1.model_validate(_manifest(snapshot))
    authority = StaticStructuredModelSnapshotAuthority(manifest)

    manifest.bindings[0].snapshot.model = "mutated-after-readiness"

    retained = authority.get_model_snapshot(canonical_model_snapshot_id(snapshot))
    assert retained is not None
    assert retained.model == "safe"


def test_local_model_authority_loader_closes_exact_environment_configuration(
    tmp_path,
) -> None:
    snapshot = ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="pre-m4@1",
    )
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")

    authority = load_local_model_execution_authorities(
        environment={
            MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
            MODEL_GATEWAY_BASE_URL_ENV: "http://localhost:4141",
            MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
        }
    )

    model_id = canonical_model_snapshot_id(snapshot)
    assert authority.snapshots.get_model_snapshot(model_id) == snapshot
    assert authority.circuit_breaker_resolver.model_snapshot_ids == (model_id,)
    assert authority.prompt_renderer.binding_plan_keys == (
        ("bench-agent-case", "bench-agent@1", "bench@1"),
        ("extraction", "extraction@1", "extraction@1"),
        ("generation", "generation@1", "generation@1"),
        ("generation", "generation@2", "generation@1"),
        ("playtest.executor", "playtest@2", "playtest@1"),
        ("playtest.memory", "playtest.memory.compact@1", "playtest@1"),
        ("playtest.planner", "playtest@1", "playtest@1"),
        ("playtest.reflect", "playtest@1", "playtest@1"),
        ("repair", "repair@4", "repair@1"),
        ("review-triage", "review-triage@1", "review-triage@1"),
    )
    assert authority.legacy_imports is None
    authority.transport.close()  # type: ignore[attr-defined]


def test_local_model_authority_loader_loads_real_legacy_manifest(tmp_path) -> None:
    fixture = _legacy_verified_fixture()
    legacy = fixture.authority
    legacy_manifest = LegacyImportAuthorityManifestV1.create(
        authority_version="m2-retained-history@1",
        verification_policy_registry=legacy.verification_policy_registry,
        model_catalogs=tuple(legacy.model_catalogs.values()),
        input_bindings=tuple(legacy.input_bindings.values()),
        profile_bindings=tuple(legacy.profile_bindings.values()),
        policy_bindings=tuple(legacy.policy_bindings.values()),
        schema_bindings=tuple(legacy.schema_bindings.values()),
        rendered_requests=tuple(
            LegacyRenderedRequestBindingV1(artifact_id=key, request=value)
            for key, value in legacy.rendered_requests.items()
        ),
        frozen_version_tuples=tuple(
            LegacyFrozenVersionTupleBindingV1(
                source_suite_id=key[0],
                source_case_id=key[1],
                version_tuple=value,
            )
            for key, value in legacy.frozen_version_tuples.items()
        ),
        call_tool_versions=tuple(
            LegacyCallToolVersionBindingV1(
                source_suite_id=key[0],
                source_case_id=key[1],
                source_call_ordinal=key[2],
                tool_version=value,
            )
            for key, value in legacy.call_tool_versions.items()
        ),
    )
    legacy_manifest_path = tmp_path / "legacy-authority.json"
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest.model_dump(mode="json")), encoding="utf-8"
    )
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")

    authority = load_local_model_execution_authorities(
        environment={
            MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
            LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV: str(legacy_manifest_path),
            MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
        }
    )

    assert authority.legacy_imports is not None
    frozen = legacy_manifest.frozen_version_tuples[0]
    assert (
        authority.legacy_imports.resolve_frozen_version_tuple(
            frozen.source_suite_id, frozen.source_case_id
        )
        == frozen.version_tuple
    )
    assert authority.legacy_imports.verification_policy_registry.policies
    authority.transport.close()  # type: ignore[attr-defined]

    shard_directory = tmp_path / "legacy-authority-shards"
    shard_directory.mkdir()
    for shard_name in ("01.json", "02.json"):
        (shard_directory / shard_name).write_text(
            json.dumps(legacy_manifest.model_dump(mode="json")),
            encoding="utf-8",
        )
    sharded_authority = load_local_model_execution_authorities(
        environment={
            MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
            LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV: str(shard_directory),
            MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
        }
    )
    assert sharded_authority.legacy_imports is not None
    assert (
        sharded_authority.legacy_imports.resolve_frozen_version_tuple(
            frozen.source_suite_id,
            frozen.source_case_id,
        )
        == frozen.version_tuple
    )
    sharded_authority.transport.close()  # type: ignore[attr-defined]


def test_local_model_authority_loader_requires_secret_and_bounded_manifest(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text("{}", encoding="utf-8")

    with pytest.raises(IntegrityViolation, match="gateway API key"):
        load_local_model_execution_authorities(
            environment={MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path)}
        )

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(IntegrityViolation, match="manifest exceeds"):
        load_local_model_execution_authorities(
            environment={
                MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(oversized),
                MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
            }
        )


@pytest.mark.parametrize(
    "legacy_manifest_path",
    ("", "legacy\x00authority.json", "x" * 4097),
)
def test_local_model_authority_rejects_unsafe_legacy_manifest_path(
    tmp_path,
    legacy_manifest_path: str,
) -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")

    with pytest.raises(IntegrityViolation, match="manifest path is invalid"):
        load_local_model_execution_authorities(
            environment={
                MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
                LEGACY_IMPORT_AUTHORITY_MANIFEST_PATH_ENV: legacy_manifest_path,
                MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
            }
        )


def test_local_model_authority_rejects_cleartext_remote_gateway(
    tmp_path,
) -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")

    with pytest.raises(IntegrityViolation, match="base URL"):
        load_local_model_execution_authorities(
            environment={
                MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
                MODEL_GATEWAY_BASE_URL_ENV: "http://models.example.test",
                MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
            }
        )

    authority = load_local_model_execution_authorities(
        environment={
            MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
            MODEL_GATEWAY_BASE_URL_ENV: "https://models.example.test",
            MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
        }
    )
    authority.transport.close()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "base_url",
    (
        "https://models.example.test\n",
        "https://models.example.test/\x00v1",
        "https://models.example.test/\x7fv1",
        "https://models.example.test:0",
    ),
)
def test_local_model_authority_rejects_gateway_ascii_controls(
    tmp_path,
    base_url: str,
) -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")

    with pytest.raises(IntegrityViolation, match="base URL"):
        load_local_model_execution_authorities(
            environment={
                MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
                MODEL_GATEWAY_BASE_URL_ENV: base_url,
                MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
            }
        )


def test_local_loader_builds_all_non_network_authority_before_opening_http_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gameforge.apps.worker.model_authority as model_authority

    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    manifest_path = tmp_path / "model-snapshots.json"
    manifest_path.write_text(json.dumps(_manifest(snapshot)), encoding="utf-8")
    monkeypatch.setattr(
        model_authority,
        "build_builtin_agent_prompt_context_authority",
        lambda **_: (_ for _ in ()).throw(IntegrityViolation("prompt authority rejected")),
    )

    def forbidden_transport(**kwargs):
        del kwargs
        raise AssertionError("HTTP client opened before non-network authority closed")

    monkeypatch.setattr(model_authority, "OpenAIResponsesTransport", forbidden_transport)

    with pytest.raises(IntegrityViolation, match="prompt authority rejected"):
        load_local_model_execution_authorities(
            environment={
                MODEL_SNAPSHOT_MANIFEST_PATH_ENV: str(manifest_path),
                MODEL_GATEWAY_API_KEY_ENV: "test-only-secret",
            }
        )


def test_manifest_rejects_tampered_digest_and_non_preimage_binding() -> None:
    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    tampered = _manifest(snapshot)
    tampered["authority_version"] = "deployment-models@2"
    with pytest.raises(IntegrityViolation, match="authority is invalid"):
        parse_structured_model_snapshot_manifest(json.dumps(tampered))

    forged = _manifest(snapshot)
    forged["bindings"][0]["model_snapshot_id"] = "openai:sha256:" + "0" * 64  # type: ignore[index]
    body = {key: value for key, value in forged.items() if key != "manifest_digest"}
    forged["manifest_digest"] = canonical_sha256(body)
    with pytest.raises(ValueError, match="does not hash"):
        StructuredModelSnapshotManifestV1.model_validate(forged)


def test_manifest_and_breaker_authorities_require_nonempty_exact_closures() -> None:
    empty = {
        "manifest_schema_version": "structured-model-snapshots@1",
        "authority_version": "deployment-models@1",
        "bindings": [],
    }
    empty["manifest_digest"] = canonical_sha256(empty)
    with pytest.raises(IntegrityViolation, match="authority is invalid"):
        parse_structured_model_snapshot_manifest(json.dumps(empty))

    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    model_id = canonical_model_snapshot_id(snapshot)
    wrong = CircuitBreaker(
        dependency_id="model-provider:another-model",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=SystemUtcClock(),
    )
    with pytest.raises(ValueError, match="dependency binding"):
        StaticCircuitBreakerAuthority({model_id: wrong})


def test_model_authority_bundle_rejects_partial_legacy_surface() -> None:
    from tests.apps.worker.test_prompt_rendering import _authority

    snapshot = ModelSnapshot(provider="openai", model="gpt", snapshot_tag="v1")
    model_id = canonical_model_snapshot_id(snapshot)
    snapshots = StaticStructuredModelSnapshotAuthority(
        StructuredModelSnapshotManifestV1.model_validate(_manifest(snapshot))
    )
    breaker = CircuitBreaker(
        dependency_id=f"model-provider:{model_id}",
        config=CircuitBreakerConfigV1(
            config_version="breaker@1",
            rolling_window_s=60,
            minimum_samples=2,
            failure_threshold=1,
            open_cooldown_s=10,
            half_open_max_concurrent_probes=1,
            half_open_success_threshold=1,
        ),
        clock=SystemUtcClock(),
    )

    with pytest.raises(ValueError, match="legacy import authority surface"):
        WorkerModelExecutionAuthorities(
            transport=SimpleNamespace(complete=lambda _: None),
            snapshots=snapshots,
            prompt_renderer=_authority(),
            price_book=UnavailablePriceBook(),
            legacy_imports=SimpleNamespace(resolve_rendered_request=lambda _: None),
            circuit_breaker_resolver=StaticCircuitBreakerAuthority({model_id: breaker}),
        )
