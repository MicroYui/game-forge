"""Generic CLI for external-corpus discovery, review, and adjudication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from gameforge.bench.external_corpus.adjudication import (
    AdjudicationError,
    adjudicate,
    build_review_package,
)
from gameforge.bench.external_corpus.contracts import (
    AdjudicationEvidence,
    DiscoveryLedger,
    SearchRegistration,
    SourceProfile,
    canonical_bytes,
    read_regular_file,
    sha256_hex,
    write_new_or_identical,
    write_set_new_or_identical,
)
from gameforge.bench.external_corpus.discovery import (
    discover_candidates,
    verify_discovery_direct_matches,
)
from gameforge.bench.external_corpus.git import GitEvidenceError, ReadOnlyGitRepo
from gameforge.bench.external_corpus.profiles import get_profile_binding


_ModelT = TypeVar("_ModelT", bound=BaseModel)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mine and adjudicate external-corpus evidence.")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover", help="discover a registered candidate universe")
    discover.add_argument("--repo", required=True, type=Path)
    discover.add_argument("--profile", required=True, type=Path)
    discover.add_argument("--registration-commit", required=True)
    discover.add_argument("--registration-path", required=True)
    discover.add_argument("--out", required=True, type=Path)
    discover.add_argument("--blob-dir", required=True, type=Path)

    review = commands.add_parser(
        "review-package", help="build a non-approving complete assignment package"
    )
    review.add_argument("--ledger", required=True, type=Path)
    review.add_argument("--out", required=True, type=Path)

    adjudicate_parser = commands.add_parser(
        "adjudicate", help="replay human-reviewed evidence offline"
    )
    adjudicate_parser.add_argument("--ledger", required=True, type=Path)
    adjudicate_parser.add_argument("--evidence", required=True, type=Path)
    adjudicate_parser.add_argument("--blob-dir", required=True, type=Path)
    adjudicate_parser.add_argument("--out", required=True, type=Path)
    adjudicate_parser.add_argument("--decision-out", required=True, type=Path)
    return parser


def _load_canonical(path: Path, model_type: type[_ModelT], label: str) -> _ModelT:
    try:
        data = read_regular_file(path)
    except OSError as exc:
        raise ValueError(f"unable to read {label}: {path}") from exc
    try:
        model = model_type.model_validate_json(data)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid: {path}: {exc}") from exc
    if canonical_bytes(model) != data:
        raise ValueError(f"{label} is not canonical JSON: {path}")
    return model


def _verify_blob(blob_dir: Path, digest: str, label: str) -> None:
    try:
        data = read_regular_file(blob_dir / digest)
    except OSError as exc:
        raise ValueError(f"unable to read {label} CAS blob {digest}") from exc
    if sha256_hex(data) != digest:
        raise ValueError(f"{label} CAS blob does not match digest {digest}")


def _verify_adjudication_cas(
    blob_dir: Path,
    discovered: DiscoveryLedger,
    evidence: AdjudicationEvidence,
) -> None:
    try:
        verify_discovery_direct_matches(blob_dir, discovered)
    except ValueError as exc:
        raise ValueError(f"discovery direct-match replay failed: {exc}") from exc
    for artifact in evidence.source_artifacts:
        _verify_blob(blob_dir, artifact.blob_sha256, "source artifact")


def _discover(args: argparse.Namespace) -> int:
    registration = SearchRegistration(
        project_commit_oid=args.registration_commit,
        profile_repo_relative_path=args.registration_path,
    )
    project_repo = ReadOnlyGitRepo(PROJECT_ROOT)
    project_repo.preflight()
    project_repo.assert_tracked_worktree_clean()
    if project_repo.head_commit() != registration.project_commit_oid:
        raise ValueError("registration commit must match the current project HEAD")

    registered_profile_path = (
        PROJECT_ROOT / registration.profile_repo_relative_path
    ).resolve()
    if args.profile.resolve() != registered_profile_path:
        raise ValueError("source profile path must match the registered profile path")

    registered_profile_bytes = project_repo.blob_bytes_at(
        registration.project_commit_oid,
        registration.profile_repo_relative_path,
    )
    try:
        current_profile_bytes = read_regular_file(args.profile)
    except OSError as exc:
        raise ValueError(f"unable to read source profile: {args.profile}") from exc
    if current_profile_bytes != registered_profile_bytes:
        raise ValueError("source profile differs from the registered profile bytes")

    profile = _load_canonical(args.profile, SourceProfile, "source profile")
    profile = get_profile_binding(profile.source_id).validate_source_profile(profile)
    ledger = discover_candidates(
        ReadOnlyGitRepo(args.repo),
        profile,
        registration,
        args.blob_dir,
    )
    write_new_or_identical(args.out, canonical_bytes(ledger))
    print(
        "discovery complete: "
        f"selected={len(ledger.discovered_candidates)} "
        f"matched={ledger.matched_candidate_count} "
        f"config_only={ledger.config_only_candidate_count}",
        file=sys.stderr,
    )
    return 0


def _review_package(args: argparse.Namespace) -> int:
    discovered = _load_canonical(args.ledger, DiscoveryLedger, "discovery ledger")
    package = build_review_package(discovered)
    write_new_or_identical(args.out, canonical_bytes(package))
    print(f"review package complete: {len(package.rows)} candidates", file=sys.stderr)
    return 0


def _adjudicate(args: argparse.Namespace) -> int:
    if args.out == args.decision_out:
        raise ValueError("candidate-ledger and decision output paths must be distinct")
    if args.decision_out.exists() and not args.out.exists():
        raise FileExistsError("decision completion marker exists without candidate ledger")
    discovered = _load_canonical(args.ledger, DiscoveryLedger, "discovery ledger")
    evidence = _load_canonical(args.evidence, AdjudicationEvidence, "adjudication evidence")
    _verify_adjudication_cas(args.blob_dir, discovered, evidence)
    ledger, decision = adjudicate(discovered, evidence)
    write_set_new_or_identical(
        {
            args.out: canonical_bytes(ledger),
            args.decision_out: canonical_bytes(decision),
        }
    )
    print(f"adjudication complete: {decision.gate.status}", file=sys.stderr)
    return 0 if decision.gate.status == "pass" else 3


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            return _discover(args)
        if args.command == "review-package":
            return _review_package(args)
        return _adjudicate(args)
    except (AdjudicationError, GitEvidenceError, OSError, ValueError) as exc:
        print(f"external corpus mining failed: {_one_line(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
