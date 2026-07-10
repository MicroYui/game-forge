"""Shared NON-test helper for `tests/bench/`: re-exports the package's clean
base loader (`gameforge.bench.bases.clean_base`) so tests and the corpus
builder share ONE definition of the clean Aureus baseline (M0b/M1 baseline —
the same fixture `tests/apps/test_m1_acceptance.py` proves oracle-FP=0 against).

NOT a test module itself (no `test_*` functions — pytest never collects it);
imported by `tests/bench/test_inject_*.py` and `tests/bench/test_corpus.py`.
"""
from __future__ import annotations

from gameforge.bench.bases import clean_base

__all__ = ["clean_base"]
