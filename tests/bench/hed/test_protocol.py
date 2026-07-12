from __future__ import annotations

import pytest
from pydantic import ValidationError

import gameforge.bench.hed.protocol as protocol_module
from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import content_sha256
from gameforge.bench.hed.delta import DISTANCE_METRIC, SEMANTIC_DELTA_VERSION
from gameforge.bench.hed.protocol import (
    HedProtocol,
    PROMPT_NAMES,
    assert_protocol_ready,
    canonical_protocol_bytes,
    current_prompt_version,
    load_protocol,
    prompt_bundle_sha256,
    seal_protocol,
)
from gameforge.bench.stats import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from gameforge.contracts.model_router import ModelSnapshot

_EXTERNAL_MANIFEST = (
    "scenarios/external_cases/endless_sky/external-corpus-manifest.json"
)


def _manifest():
    return load_manifest(_EXTERNAL_MANIFEST)


def _protocol():
    return seal_protocol(_manifest())


def test_protocol_binds_gpt56_prompts_external_denominator_and_metric_rules():
    protocol = _protocol()
    manifest = _manifest()

    assert protocol.model_snapshot == ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="pre-m4@1",
    )
    assert protocol.model_snapshot == DEFAULT_SNAPSHOT
    assert protocol.external_manifest_sha256 == manifest.manifest_sha256
    assert protocol.external_case_ids == tuple(
        sorted(item.spec.case_id for item in manifest.cases)
    )
    assert protocol.external_case_count == 8
    assert PROMPT_NAMES == ("repair.refine", "repair.system")
    assert protocol.repair_prompt_version == "repair@4"
    assert protocol.max_steps == 4
    assert protocol.run_regression is False
    assert protocol.semantic_delta_version == SEMANTIC_DELTA_VERSION
    assert protocol.distance_metric == DISTANCE_METRIC
    assert protocol.bootstrap_seed == BOOTSTRAP_SEED == 20260712
    assert protocol.bootstrap_resamples == BOOTSTRAP_RESAMPLES == 10_000
    assert protocol.frozen is True
    assert_protocol_ready(protocol, manifest, manifest_path=_EXTERNAL_MANIFEST)


def test_protocol_round_trips_as_canonical_hash_bound_json(tmp_path):
    protocol = _protocol()
    path = tmp_path / "hed-protocol.json"
    path.write_bytes(canonical_protocol_bytes(protocol))

    assert load_protocol(path) == protocol


def test_protocol_contract_rejects_historical_opus_snapshot():
    payload = _protocol().model_dump(mode="json")
    payload["model_snapshot"] = {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "snapshot_tag": "m2a@1",
    }
    payload["protocol_sha256"] = content_sha256(
        payload,
        exclude={"protocol_sha256"},
    )

    with pytest.raises(ValidationError, match="gpt-5.6-sol"):
        HedProtocol.model_validate(payload)


def test_protocol_rejects_duplicate_or_unsorted_case_ids():
    payload = _protocol().model_dump(mode="json")
    payload["external_case_ids"] = list(reversed(payload["external_case_ids"]))
    payload["protocol_sha256"] = content_sha256(
        payload,
        exclude={"protocol_sha256"},
    )
    with pytest.raises(ValidationError, match="sorted"):
        HedProtocol.model_validate(payload)

    payload = _protocol().model_dump(mode="json")
    payload["external_case_ids"][-1] = payload["external_case_ids"][0]
    payload["protocol_sha256"] = content_sha256(
        payload,
        exclude={"protocol_sha256"},
    )
    with pytest.raises(ValidationError, match="unique"):
        HedProtocol.model_validate(payload)


def test_prompt_bundle_hash_and_readiness_fail_on_prompt_text_drift(monkeypatch):
    protocol = _protocol()
    original_get_prompt = protocol_module.get_prompt

    def drifted(name: str):
        version, text = original_get_prompt(name)
        if name == "repair.system":
            text += " drift"
        return version, text

    monkeypatch.setattr(protocol_module, "get_prompt", drifted)

    assert prompt_bundle_sha256() != protocol.repair_prompt_bundle_sha256
    with pytest.raises(ValueError, match="prompt bundle"):
        assert_protocol_ready(
            protocol,
            _manifest(),
            manifest_path=_EXTERNAL_MANIFEST,
        )


def test_readiness_rejects_external_manifest_or_frozen_policy_drift():
    protocol = _protocol()
    manifest = _manifest()
    changed_manifest = manifest.model_copy(update={"manifest_sha256": "c" * 64})
    with pytest.raises(ValueError, match="external manifest"):
        assert_protocol_ready(protocol, changed_manifest)

    changed_protocol = protocol.model_copy(update={"max_steps": 3})
    with pytest.raises(ValueError, match="protocol_sha256"):
        assert_protocol_ready(changed_protocol, manifest)


def test_prompt_version_is_shared_by_exact_repair_prompt_pair():
    assert current_prompt_version() == "repair@4"
    assert len(prompt_bundle_sha256()) == 64


def test_protocol_forbids_extra_fields():
    payload = _protocol().model_dump(mode="json")
    payload["approval_id"] = "not-part-of-product-evidence"

    with pytest.raises(ValidationError, match="Extra inputs"):
        HedProtocol.model_validate(payload)
