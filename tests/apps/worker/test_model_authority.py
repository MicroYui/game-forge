from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gameforge.apps.worker.model_authority import (
    StaticCircuitBreakerAuthority,
    StaticStructuredModelSnapshotAuthority,
    StructuredModelSnapshotManifestV1,
    WorkerModelExecutionAuthorities,
    parse_structured_model_snapshot_manifest,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.contracts.routing import canonical_model_snapshot_id
from gameforge.contracts.reliability import CircuitBreakerConfigV1
from gameforge.runtime.clock import SystemUtcClock
from gameforge.runtime.cost.price_book import UnavailablePriceBook
from gameforge.runtime.reliability.breaker import CircuitBreaker


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
