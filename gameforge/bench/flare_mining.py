"""Command-line orchestration for the Flare B0A evidence workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from gameforge.bench.flare_adjudication import adjudicate
from gameforge.bench.flare_evidence import (
    AdjudicationEvidence,
    B0ADecision,
    CandidateLedger,
    DiscoveryLedger,
    FlareSearchSpec,
    SearchRegistration,
    canonical_bytes,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)
from gameforge.bench.flare_git import GitEvidenceError, ReadOnlyGitRepo, discover_candidates


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mine and adjudicate Flare B0A evidence.")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="discover candidates from a frozen Git range")
    discover.add_argument("--repo", required=True, type=Path)
    discover.add_argument("--search-spec", required=True, type=Path)
    discover.add_argument("--registration-commit", required=True)
    discover.add_argument("--registration-path", required=True)
    discover.add_argument("--round", required=True, choices=("initial", "expanded"))
    discover.add_argument("--out", required=True, type=Path)
    discover.add_argument("--blob-dir", required=True, type=Path)

    adjudicate_parser = commands.add_parser(
        "adjudicate", help="replay reviewed evidence against a discovery ledger"
    )
    adjudicate_parser.add_argument("--ledger", required=True, type=Path)
    adjudicate_parser.add_argument("--evidence", required=True, type=Path)
    adjudicate_parser.add_argument("--blob-dir", required=True, type=Path)
    adjudicate_parser.add_argument("--prior-ledger", type=Path)
    adjudicate_parser.add_argument("--prior-decision", type=Path)
    adjudicate_parser.add_argument("--out", required=True, type=Path)
    adjudicate_parser.add_argument("--decision-out", required=True, type=Path)
    return parser


def _load_canonical(path: Path, model_type: type[_ModelT], label: str) -> _ModelT:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8: {path}") from exc
    model = model_type.model_validate_json(text)
    if data != canonical_bytes(model):
        raise ValueError(f"{label} is not canonical JSON: {path}")
    return model


def _verify_blob(blob_dir: Path, digest: str, label: str) -> None:
    path = blob_dir / digest
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read {label} CAS blob {digest}") from exc
    if sha256_hex(data) != digest:
        raise ValueError(f"{label} CAS blob does not match digest {digest}")


def _verify_adjudication_cas(
    blob_dir: Path,
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    for candidate in discovered.discovered_candidates:
        _verify_blob(blob_dir, candidate.diff_evidence.patch_sha256, "discovery patch")
    for artifact in evidence.source_artifacts:
        _verify_blob(blob_dir, artifact.blob_sha256, "source artifact")


def _require_distinct_outputs(ledger_path: Path, decision_path: Path) -> None:
    try:
        if ledger_path.resolve(strict=False) == decision_path.resolve(strict=False):
            raise ValueError("output paths resolve to the same location")
        if ledger_path.exists() and decision_path.exists() and ledger_path.samefile(decision_path):
            raise ValueError("output paths alias the same existing file")
    except OSError as exc:
        raise ValueError("unable to verify that output paths are distinct") from exc


def _discover(args: argparse.Namespace) -> int:
    search_spec = _load_canonical(args.search_spec, FlareSearchSpec, "search spec")
    registration = SearchRegistration(
        project_commit_oid=args.registration_commit,
        repo_relative_path=args.registration_path,
    )
    ledger = discover_candidates(
        ReadOnlyGitRepo(args.repo),
        search_spec,
        registration,
        args.round,
        args.blob_dir,
    )
    write_new_or_identical(args.out, canonical_bytes(ledger))
    print(f"discovery complete: {len(ledger.discovered_candidates)} candidates", file=sys.stderr)
    return 0


def _adjudicate(args: argparse.Namespace) -> int:
    _require_distinct_outputs(args.out, args.decision_out)
    discovered = _load_canonical(args.ledger, DiscoveryLedger, "discovery ledger")
    evidence = _load_canonical(args.evidence, AdjudicationEvidence, "adjudication evidence")
    prior_ledger = (
        _load_canonical(args.prior_ledger, CandidateLedger, "prior candidate ledger")
        if args.prior_ledger is not None
        else None
    )
    prior_decision = (
        _load_canonical(args.prior_decision, B0ADecision, "prior decision")
        if args.prior_decision is not None
        else None
    )
    _verify_adjudication_cas(args.blob_dir, discovered, evidence)
    ledger, decision = adjudicate(discovered, evidence, prior_ledger, prior_decision)
    ledger_bytes = canonical_bytes(ledger)
    decision_bytes = canonical_bytes(decision)
    write_set_new_or_identical(
        {
            args.out: ledger_bytes,
            args.decision_out: decision_bytes,
        }
    )
    print(f"adjudication complete: {decision.gate.status}", file=sys.stderr)
    return 0 if decision.gate.status == "provisional_pass" else 3


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "adjudicate" and (
        (args.prior_ledger is None) != (args.prior_decision is None)
    ):
        parser.error("--prior-ledger and --prior-decision must be provided together")

    try:
        if args.command == "discover":
            return _discover(args)
        return _adjudicate(args)
    except (GitEvidenceError, OSError, ValueError) as exc:
        print(f"flare mining failed: {_one_line(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
