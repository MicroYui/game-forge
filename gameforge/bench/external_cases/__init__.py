"""Lean, replayable evidence for real external before/after defect cases."""

from gameforge.bench.external_cases.contracts import (
    ExternalCaseEvidence,
    ExternalCaseRegistration,
    ExternalCaseSpec,
    ExternalCorpusManifest,
    FindingEvidence,
    HumanTarget,
    NativeEvidence,
    PredicateEvidence,
    TargetLocator,
    TreeArtifact,
    TreeFile,
    canonical_bytes,
    content_sha256,
)

__all__ = [
    "ExternalCaseEvidence",
    "ExternalCaseRegistration",
    "ExternalCaseSpec",
    "ExternalCorpusManifest",
    "FindingEvidence",
    "HumanTarget",
    "NativeEvidence",
    "PredicateEvidence",
    "TargetLocator",
    "TreeArtifact",
    "TreeFile",
    "canonical_bytes",
    "content_sha256",
]
