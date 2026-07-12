from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import gameforge.bench.narrative.protocol as protocol_module
from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.bench.narrative.contracts import content_sha256
from gameforge.bench.narrative.corpus import load_manifest
from gameforge.bench.narrative.protocol import (
    NarrativeProtocol,
    assert_verification_ready,
    canonical_protocol_bytes,
    load_protocol,
    prompt_bundle_sha256,
    seal_protocol,
)
from gameforge.contracts.model_router import ModelSnapshot

_CORPUS_ROOT = "scenarios/narrative_bench"


def _corpus_manifest():
    return load_manifest(f"{_CORPUS_ROOT}/corpus-manifest.json")


def _protocol():
    return seal_protocol(_corpus_manifest())


def test_protocol_binds_current_corpora_prompts_matcher_and_gpt56():
    protocol = _protocol()
    manifest = _corpus_manifest()

    assert protocol.model_snapshot == ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="pre-m4@1",
    )
    assert protocol.model_snapshot == DEFAULT_SNAPSHOT
    assert protocol.prompt_version == "consistency@2"
    assert protocol.perspectives == (
        "constraint_matching",
        "causal_world_state",
        "adversarial_falsification",
    )
    assert protocol.threshold == 2
    assert protocol.rebuttal_enabled is False
    assert protocol.matcher_version == "narrative-span@1"
    assert protocol.frozen is True
    assert protocol.development_corpus_sha256 == manifest.files[0].sha256
    assert protocol.verification_corpus_sha256 == manifest.files[1].sha256
    assert_verification_ready(protocol, manifest, corpus_root=_CORPUS_ROOT)


def test_protocol_round_trips_as_canonical_hash_bound_json(tmp_path):
    protocol = _protocol()
    path = tmp_path / "protocol.json"
    path.write_bytes(canonical_protocol_bytes(protocol))

    assert load_protocol(path) == protocol
    assert json.loads(path.read_text())["protocol_sha256"] == protocol.protocol_sha256


def test_verification_refuses_mutated_protocol_even_when_model_copy_skips_validation():
    protocol = _protocol()
    changed = protocol.model_copy(update={"threshold": 3})

    with pytest.raises(ValueError, match="protocol_sha256"):
        assert_verification_ready(
            changed,
            _corpus_manifest(),
            corpus_root=_CORPUS_ROOT,
        )


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
        NarrativeProtocol.model_validate(payload)


def test_prompt_bundle_hash_and_protocol_readiness_fail_on_text_drift(monkeypatch):
    protocol = _protocol()
    original_get_prompt = protocol_module.get_prompt

    def drifted(name: str):
        version, text = original_get_prompt(name)
        if name == "consistency.system":
            text += " drift"
        return version, text

    monkeypatch.setattr(protocol_module, "get_prompt", drifted)

    assert prompt_bundle_sha256() != protocol.prompt_bundle_sha256
    with pytest.raises(ValueError, match="prompt_bundle_sha256"):
        assert_verification_ready(
            protocol,
            _corpus_manifest(),
            corpus_root=_CORPUS_ROOT,
        )


def test_protocol_hash_rejects_direct_payload_tampering():
    payload = _protocol().model_dump(mode="json")
    payload["verification_corpus_sha256"] = "f" * 64

    with pytest.raises(ValidationError, match="protocol_sha256"):
        NarrativeProtocol.model_validate(payload)
