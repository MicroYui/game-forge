"""Compatibility export for the app-owned BenchReport payload decoder."""

from __future__ import annotations

from gameforge.bench.payload_codec import BENCH_PAYLOAD_DECODERS, decode_bench_report_v2


__all__ = ["BENCH_PAYLOAD_DECODERS", "decode_bench_report_v2"]
