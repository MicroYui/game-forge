"""The seeded GameForge-Bench corpus (M3a Task 6 / design §4).

`build_corpus` produces ≥500 injected samples spanning all 15 defect classes,
sized per class by the power target (`power.required_n`), plus a set of clean
snapshots that form the false-positive denominator. Everything is seeded off a
single `seed` so the whole corpus is reproducible (design §0 determinism).
"""
from __future__ import annotations

from dataclasses import dataclass

from gameforge.bench.bases import clean_base
from gameforge.bench.inject import InjectedSample, inject
from gameforge.bench.power import required_n
from gameforge.bench.taxonomy import Bucket, CLASS_META, DefectClass
from gameforge.spine.ir.snapshot import Snapshot

# Narrative (llm-assisted) detection is bound to M2 Consistency cassettes, not
# the cheap deterministic sweep, so those classes are recorded at a bounded n —
# honestly under-powered by design (the BenchReport flags it via PowerRow),
# rather than pretending to a full-power narrative BDR the harness can't afford.
_NARRATIVE_N = 20


def default_per_class_n() -> dict[DefectClass, int]:
    """Power-driven n per class: high-BDR deterministic/simulation classes get
    `required_n(0.95)`; the 4 narrative classes get the bounded `_NARRATIVE_N`."""
    det_n = required_n(0.95)
    return {
        dc: (_NARRATIVE_N if CLASS_META[dc].bucket is Bucket.llm_assisted else det_n)
        for dc in DefectClass
    }


@dataclass
class Corpus:
    samples: list[InjectedSample]
    clean: list[Snapshot]
    per_class_n: dict[DefectClass, int]


def build_corpus(
    seed: int = 0,
    per_class_n: dict[DefectClass, int] | None = None,
    n_clean: int = 40,
    base: Snapshot | None = None,
) -> Corpus:
    """Assemble the seeded corpus. Each sample's inject seed is derived from the
    corpus `seed`, the class, and the within-class index, so the full sequence
    is reproducible. `base` defaults to the clean Aureus baseline."""
    if per_class_n is None:
        per_class_n = default_per_class_n()
    if base is None:
        base = clean_base()

    samples: list[InjectedSample] = []
    for dc in DefectClass:  # enum iteration order is stable
        for i in range(per_class_n.get(dc, 0)):
            # a distinct, reproducible inject seed per (class, index)
            sample_seed = seed * 1_000_003 + i
            samples.append(inject(base, dc, sample_seed))

    # The clean denominator is the (single, deterministic) clean base repeated;
    # since it is immutable and content-identical, `metrics.score_seeded`
    # measures the pure oracle-FP=0 anchor on it AND enriches the FP signal with
    # cross-class false detections on the injected samples (Task 7).
    clean = [base for _ in range(n_clean)]
    return Corpus(samples=samples, clean=clean, per_class_n=dict(per_class_n))
