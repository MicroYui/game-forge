from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.apps.worker.bench_codec import BENCH_PAYLOAD_DECODERS, decode_bench_report_v2
from gameforge.bench.report_contracts import BenchReport, canonical_report_bytes
from gameforge.contracts.errors import IntegrityViolation
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload


_REPORT = Path(__file__).parents[3] / "scenarios" / "bench" / "bench-report.json"


def test_worker_bench_decoder_accepts_only_the_canonical_report_contract() -> None:
    blob = _REPORT.read_bytes()
    payload = decode_bench_report_v2(blob)
    assert payload["schema_version"] == "bench-report@2"
    assert (
        decode_and_validate_artifact_payload(
            payload_schema_id="bench-report@2",
            blob=blob,
            external_decoders=BENCH_PAYLOAD_DECODERS,
        )
        == payload
    )

    with pytest.raises(IntegrityViolation, match="not canonical"):
        decode_bench_report_v2(blob.rstrip() + b"  \n")


def test_worker_bench_decoder_preserves_an_explicit_absent_root_seed() -> None:
    report = BenchReport.model_validate(json.loads(_REPORT.read_bytes()))
    without_seed = report.model_copy(update={"meta": report.meta.model_copy(update={"seed": None})})

    payload = decode_bench_report_v2(canonical_report_bytes(without_seed))

    assert "seed" in payload["meta"]
    assert payload["meta"]["seed"] is None


@pytest.mark.parametrize("blob", (b"not-json", b"{}\n", b'{"schema_version":"bench-report@1"}\n'))
def test_worker_bench_decoder_rejects_malformed_or_wrong_schema_bytes(blob: bytes) -> None:
    with pytest.raises(IntegrityViolation, match="payload is invalid"):
        decode_bench_report_v2(blob)
