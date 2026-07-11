"""Frozen Flare discovery surface backed by the generic evidence engine."""

from __future__ import annotations

import platform
import unicodedata
from pathlib import Path
from typing import Literal

from gameforge.bench.external_corpus.contracts import (
    CandidateOrderTerm,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.external_corpus.discovery import (
    DirectRuleGroup,
    DiscoveryPolicy,
    discover_objective_candidates,
)
from gameforge.bench.external_corpus.git import (
    GitEvidenceError,
    ReadOnlyGitRepo as _GenericReadOnlyGitRepo,
    _validate_path_set as _validate_path_set,
)
from gameforge.bench.flare_evidence import (
    DISCOVERY_TOOL_VERSION,
    FLARE_B0A_SCHEMA_VERSION,
    DiscoveryLedger,
    DiscoveryTool,
    FlareSearchSpec,
    SearchRegistration,
)


class ReadOnlyGitRepo(_GenericReadOnlyGitRepo):
    """Compatibility facade retaining the legacy Flare history signature."""

    def reachable_commits(self, spec: FlareSearchSpec) -> list[str]:
        return self._reachable_commits(
            pinned_head=spec.pinned_head,
            after_exclusive_oid=spec.after_exclusive,
            committed_at_gte=None,
            expected_commit_count=spec.expected_revision_count,
        )


def _policy(
    spec: FlareSearchSpec,
    round_name: Literal["initial", "expanded"],
) -> DiscoveryPolicy:
    selected_count = 1 if round_name == "initial" else 2
    selected_rounds = spec.rounds[:selected_count]
    return DiscoveryPolicy(
        include_globs=tuple(spec.config_path_globs),
        exclude_globs=tuple(spec.excluded_path_globs),
        direct_rule_groups=tuple(
            DirectRuleGroup(
                message_rules=tuple(search_round.message_regexes),
                diff_rules=tuple(search_round.diff_regexes),
            )
            for search_round in selected_rounds
        ),
        lineage_rules=tuple(spec.lineage_regexes),
        candidate_order=tuple(
            CandidateOrderTerm(field=field, direction="ascending") for field in spec.candidate_order
        ),
        candidate_limit=None,
    )


def discover_candidates(
    repo: ReadOnlyGitRepo,
    spec: FlareSearchSpec,
    registration: SearchRegistration,
    round_name: Literal["initial", "expanded"],
    blob_dir: Path,
) -> DiscoveryLedger:
    """Run generic objective discovery and serialize the frozen Flare ledger."""

    if round_name not in {"initial", "expanded"}:
        raise GitEvidenceError(f"unknown search round: {round_name}")
    spec = FlareSearchSpec.model_validate(spec.model_dump(mode="json"))
    registration = SearchRegistration.model_validate(registration.model_dump(mode="json"))

    repo._preflight_object_reads()
    try:
        resolved_head = repo.resolve(spec.pinned_head)
    except GitEvidenceError as exc:
        raise GitEvidenceError("unable to resolve pinned head") from exc
    if resolved_head != spec.pinned_head:
        raise GitEvidenceError("resolved pinned head differs from the search frame")

    history = repo.reachable_commits(spec)
    if spec.pinned_head not in set(history):
        raise GitEvidenceError("pinned head is absent from its reachable history")
    objective = discover_objective_candidates(
        repo,
        history,
        _policy(spec, round_name),
        blob_dir,
    )

    candidates = list(objective.candidates)
    links = list(objective.lineage_links)
    search_spec_sha256 = sha256_hex(canonical_bytes(spec))
    universe = {
        "schema_version": FLARE_B0A_SCHEMA_VERSION,
        "search_spec_sha256": search_spec_sha256,
        "search_round": round_name,
        "discovered_candidates": [
            candidate.model_dump(mode="json", exclude_none=True) for candidate in candidates
        ],
        "objective_lineage_links": [
            link.model_dump(mode="json", exclude_none=True) for link in links
        ],
    }
    return DiscoveryLedger(
        search_frame=spec,
        search_spec_sha256=search_spec_sha256,
        search_registration=registration,
        search_round=round_name,
        observed_revision_count=len(history),
        discovery_tool=DiscoveryTool(
            tool_version=DISCOVERY_TOOL_VERSION,
            project_commit_oid=registration.project_commit_oid,
            git_version=repo.git_version(),
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
            python_build=platform.python_build(),
            unicode_version=unicodedata.unidata_version,
        ),
        discovered_candidates=candidates,
        objective_lineage_links=links,
        candidate_universe_sha256=sha256_hex(canonical_bytes(universe)),
    )
