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
    read_regular_file,
    sha256_hex,
    verify_discovery_direct_matches,
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
    adjudicate_parser.add_argument("--prior-discovery", type=Path)
    adjudicate_parser.add_argument("--prior-evidence", type=Path)
    adjudicate_parser.add_argument("--prior-ledger", type=Path)
    adjudicate_parser.add_argument("--prior-decision", type=Path)
    adjudicate_parser.add_argument("--out", required=True, type=Path)
    adjudicate_parser.add_argument("--decision-out", required=True, type=Path)
    return parser


def _load_canonical(path: Path, model_type: type[_ModelT], label: str) -> _ModelT:
    try:
        data = read_regular_file(path)
    except OSError as exc:
        raise ValueError(f"unable to read {label}: {path}") from exc
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8: {path}") from exc
    try:
        model = model_type.model_validate_json(text)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid: {path}: {exc}") from exc
    if data != canonical_bytes(model):
        raise ValueError(f"{label} is not canonical JSON: {path}")
    return model


def _verify_blob(blob_dir: Path, digest: str, label: str) -> None:
    path = blob_dir / digest
    try:
        data = read_regular_file(path)
    except OSError as exc:
        raise ValueError(f"unable to read {label} CAS blob {digest}") from exc
    if sha256_hex(data) != digest:
        raise ValueError(f"{label} CAS blob does not match digest {digest}")


def _verify_adjudication_cas(
    blob_dir: Path,
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
    *,
    label_prefix: str = "",
) -> None:
    try:
        verify_discovery_direct_matches(blob_dir, discovered)
    except ValueError as exc:
        raise ValueError(f"{label_prefix}discovery direct-match replay failed: {exc}") from exc
    for artifact in evidence.source_artifacts:
        _verify_blob(blob_dir, artifact.blob_sha256, f"{label_prefix}source artifact")


def _require_distinct_outputs(ledger_path: Path, decision_path: Path) -> None:
    if ledger_path == decision_path:
        raise ValueError("output paths must be distinct")


def _require_valid_completion_state(ledger_path: Path, decision_path: Path) -> None:
    if decision_path.exists() and not ledger_path.exists():
        raise FileExistsError("decision completion marker exists without candidate ledger")


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
    _require_valid_completion_state(args.out, args.decision_out)
    discovered = _load_canonical(args.ledger, DiscoveryLedger, "discovery ledger")
    evidence = _load_canonical(args.evidence, AdjudicationEvidence, "adjudication evidence")
    prior_discovery = (
        _load_canonical(args.prior_discovery, DiscoveryLedger, "prior discovery ledger")
        if args.prior_discovery is not None
        else None
    )
    prior_evidence = (
        _load_canonical(args.prior_evidence, AdjudicationEvidence, "prior evidence")
        if args.prior_evidence is not None
        else None
    )
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
    if prior_discovery is not None and prior_evidence is not None:
        _verify_adjudication_cas(
            args.blob_dir,
            prior_discovery,
            prior_evidence,
            label_prefix="prior ",
        )
    _verify_adjudication_cas(args.blob_dir, discovered, evidence)
    ledger, decision = adjudicate(
        discovered,
        evidence,
        prior_discovery,
        prior_evidence,
        prior_ledger,
        prior_decision,
    )
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
    if args.command == "adjudicate":
        prior_values = (
            args.prior_discovery,
            args.prior_evidence,
            args.prior_ledger,
            args.prior_decision,
        )
        if any(value is not None for value in prior_values) and not all(
            value is not None for value in prior_values
        ):
            parser.error(
                "--prior-discovery, --prior-evidence, --prior-ledger, and "
                "--prior-decision must be provided together"
            )

    try:
        if args.command == "discover":
            return _discover(args)
        return _adjudicate(args)
    except (GitEvidenceError, OSError, ValueError) as exc:
        print(f"flare mining failed: {_one_line(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
