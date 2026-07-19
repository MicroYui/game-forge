"""Strict app-owned decoder for the deferred BenchReport payload schema."""

from __future__ import annotations

import json
from typing import Mapping

from gameforge.bench.report_contracts import BenchReport, canonical_report_bytes
from gameforge.contracts.errors import IntegrityViolation


def decode_bench_report_v2(blob: bytes) -> Mapping[str, object]:
    """Parse exact canonical ``bench-report@2`` bytes through the owning contract."""

    try:
        raw = json.loads(blob.decode("utf-8"))
        report = BenchReport.model_validate(raw)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise IntegrityViolation("bench-report@2 payload is invalid") from exc
    if canonical_report_bytes(report) != blob:
        raise IntegrityViolation("bench-report@2 payload is not canonical")
    return report.model_dump(mode="json")


BENCH_PAYLOAD_DECODERS = {"bench-report@2": decode_bench_report_v2}


__all__ = ["BENCH_PAYLOAD_DECODERS", "decode_bench_report_v2"]
