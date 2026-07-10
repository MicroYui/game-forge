"""Read-only Git boundary and deterministic Flare candidate discovery."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

from gameforge.bench.flare_evidence import (
    FLARE_B0A_SCHEMA_VERSION,
    GIT_COMMON_PREFIX,
    GIT_DROP_INHERITED_PREFIXES,
    GIT_ELIGIBLE_PATH_SUFFIX,
    GIT_EMPTY_TREE_ARGS,
    GIT_FIXED_ENVIRONMENT,
    GIT_HISTORY_ARGS,
    GIT_INHERIT_ALLOWLIST,
    GIT_METADATA_ARGS,
    GIT_PATCH_ARGS,
    GIT_PATCH_ID_ARGS,
    GIT_PATHS_ARGS,
    GIT_RESOLVE_ARGS,
    GIT_VERSION_COMMAND,
    CandidateCommit,
    DiffEvidence,
    DiscoveredCandidate,
    DiscoveryLedger,
    DiscoveryTool,
    FlareSearchSpec,
    LineageLink,
    SearchRegistration,
    SelectionReason,
    canonical_bytes,
    posix_glob_matches,
    put_blob,
    sha256_hex,
)


DISCOVERY_TOOL_VERSION = "gameforge-flare-discovery@1"

_OID_RE = re.compile(r"[0-9a-f]{40}")
_OID_BYTES_RE = re.compile(rb"[0-9a-f]{40}")
_STATUS_RE = re.compile(rb"[A-Z][0-9]*")
_REASON_ORDER = {"direct_match": 0, "adjacent_context": 1, "lineage_context": 2}


class GitEvidenceError(RuntimeError):
    """Raised when Git cannot produce evidence under the frozen boundary."""


@dataclass(frozen=True)
class _CommitMetadata:
    facts: CandidateCommit
    full_message: str


@dataclass(frozen=True)
class _CommitState:
    metadata: _CommitMetadata
    changed_paths: tuple[str, ...]
    eligible_paths: tuple[str, ...]

    @property
    def config_only(self) -> bool:
        return len(self.changed_paths) == len(self.eligible_paths)


def _render_args(template: Sequence[str], **replacements: str) -> list[str]:
    rendered: list[str] = []
    for token in template:
        for name, value in replacements.items():
            token = token.replace("{" + name + "}", value)
        rendered.append(token)
    return rendered


def _validate_oid(value: str, *, label: str) -> str:
    if _OID_RE.fullmatch(value) is None:
        raise GitEvidenceError(f"{label} is not a lowercase full Git OID")
    return value


def _decode_utf8(value: bytes, *, label: str) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitEvidenceError(f"{label} is not valid UTF-8") from exc


def _normalize_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise GitEvidenceError("Git returned a non-POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise GitEvidenceError(f"Git returned an unsafe repository path: {value!r}")
    return value


def _validate_path_set(paths: Sequence[str]) -> list[str]:
    normalized = [_normalize_path(path) for path in paths]
    folded = [path.casefold() for path in normalized]
    if len(folded) != len(set(folded)):
        raise GitEvidenceError("Git returned duplicate case-folded paths")
    return sorted(normalized)


class ReadOnlyGitRepo:
    """Executes only frozen, read-only Git commands against one local clone."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.git_dir = self._locate_git_dir()

    def _locate_git_dir(self) -> Path:
        dot_git = self.path / ".git"
        if dot_git.is_dir():
            return dot_git.resolve()
        if dot_git.is_file():
            try:
                marker = dot_git.read_text(encoding="utf-8", errors="strict").strip()
            except (OSError, UnicodeDecodeError) as exc:
                raise GitEvidenceError("unable to read the repository .git marker") from exc
            prefix = "gitdir: "
            if not marker.startswith(prefix):
                raise GitEvidenceError("repository .git marker has an invalid format")
            location = Path(marker[len(prefix) :])
            if not location.is_absolute():
                location = self.path / location
            if not location.is_dir():
                raise GitEvidenceError("repository git directory does not exist")
            return location.resolve()
        if (self.path / "HEAD").is_file() and (self.path / "objects").is_dir():
            return self.path
        raise GitEvidenceError(f"not a Git repository: {self.path}")

    def _locate_common_git_dir(self) -> Path:
        marker = self.git_dir / "commondir"
        if not marker.exists() and not marker.is_symlink():
            return self.git_dir
        try:
            lines = marker.read_text(encoding="utf-8", errors="strict").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise GitEvidenceError("unable to read the repository commondir marker") from exc
        if len(lines) != 1 or not lines[0] or "\x00" in lines[0]:
            raise GitEvidenceError("repository commondir marker has an invalid format")
        location = Path(lines[0])
        if not location.is_absolute():
            location = self.git_dir / location
        if not location.is_dir():
            raise GitEvidenceError("repository common Git directory does not exist")
        return location.resolve()

    def _reject_local_attributes(self) -> None:
        git_dirs = dict.fromkeys((self.git_dir, self._locate_common_git_dir()))
        for git_dir in git_dirs:
            attributes = git_dir / "info" / "attributes"
            try:
                if attributes.is_symlink() or (
                    attributes.exists() and attributes.stat().st_size > 0
                ):
                    raise GitEvidenceError(
                        "nonempty repo-local info/attributes is forbidden for evidence discovery"
                    )
            except OSError as exc:
                raise GitEvidenceError("unable to inspect repo-local info/attributes") from exc

    @staticmethod
    def _child_environment() -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in GIT_INHERIT_ALLOWLIST:
            if any(name.startswith(prefix) for prefix in GIT_DROP_INHERITED_PREFIXES):
                continue
            value = os.environ.get(name)
            if value is None:
                raise GitEvidenceError(f"required inherited environment variable is missing: {name}")
            environment[name] = value
        environment.update(GIT_FIXED_ENVIRONMENT)
        return environment

    def _common_prefix(self) -> list[str]:
        return [str(self.path) if token == "{repo}" else token for token in GIT_COMMON_PREFIX]

    def _run(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        common_prefix: bool = True,
    ) -> bytes:
        self._reject_local_attributes()
        command = [*(self._common_prefix() if common_prefix else ()), *args]
        try:
            completed = subprocess.run(
                command,
                check=False,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_environment(),
                shell=False,
            )
        except OSError as exc:
            raise GitEvidenceError(f"unable to execute Git command: {command[0]}") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = f": {stderr}" if stderr else ""
            raise GitEvidenceError(f"Git command failed ({completed.returncode}){detail}")
        return completed.stdout

    def resolve(self, oid: str) -> str:
        oid = _validate_oid(oid, label="commit")
        output = self._run(_render_args(GIT_RESOLVE_ARGS, pinned_head=oid))
        resolved = output.decode("ascii", errors="strict").strip()
        return _validate_oid(resolved, label="resolved commit")

    def reachable_commits(self, spec: FlareSearchSpec) -> list[str]:
        pinned_head = _validate_oid(spec.pinned_head, label="pinned head")
        after_exclusive = (
            None
            if spec.after_exclusive is None
            else _validate_oid(spec.after_exclusive, label="after_exclusive")
        )
        revision_range = (
            pinned_head
            if after_exclusive is None
            else f"{after_exclusive}..{pinned_head}"
        )
        output = self._run(
            _render_args(GIT_HISTORY_ARGS, revision_range=revision_range)
        )
        try:
            commits = output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("Git history contains a non-ASCII object ID") from exc
        if any(_OID_RE.fullmatch(oid) is None for oid in commits):
            raise GitEvidenceError("Git history contains an invalid object ID")
        if len(commits) != len(set(commits)):
            raise GitEvidenceError("Git history contains duplicate commits")
        if len(commits) != spec.expected_revision_count:
            raise GitEvidenceError(
                "reachable revision count differs from frozen expectation: "
                f"expected {spec.expected_revision_count}, observed {len(commits)}"
            )
        return commits

    def _empty_tree_oid(self) -> str:
        output = self._run(GIT_EMPTY_TREE_ARGS, input_bytes=b"")
        oid = output.decode("ascii", errors="strict").strip()
        return _validate_oid(oid, label="empty-tree object")

    def _metadata(self, oid: str) -> _CommitMetadata:
        oid = _validate_oid(oid, label="commit")
        output = self._run(_render_args(GIT_METADATA_ARGS, commit=oid))
        fields = output.split(b"\x00", 4)
        if len(fields) != 5:
            raise GitEvidenceError("Git metadata did not contain the frozen five fields")
        raw_oid, raw_parents, raw_timestamp, raw_subject, raw_message = fields
        try:
            commit_oid = raw_oid.decode("ascii", errors="strict")
            parent_field = raw_parents.decode("ascii", errors="strict")
            committed_at = int(raw_timestamp.decode("ascii", errors="strict"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitEvidenceError("Git metadata contains invalid ASCII fields") from exc
        _validate_oid(commit_oid, label="metadata commit")
        if commit_oid != oid:
            raise GitEvidenceError("Git metadata returned a different commit")
        parent_oids = parent_field.split() if parent_field else []
        for parent_oid in parent_oids:
            _validate_oid(parent_oid, label="metadata parent")
        subject = _decode_utf8(raw_subject, label="commit subject")
        full_message = _decode_utf8(raw_message, label="commit message")
        selected_parent = parent_oids[0] if parent_oids else None
        diff_base = selected_parent or self._empty_tree_oid()
        return _CommitMetadata(
            facts=CandidateCommit(
                commit_oid=commit_oid,
                parent_oids=parent_oids,
                selected_parent_oid=selected_parent,
                diff_base_oid=diff_base,
                committed_at=committed_at,
                subject=subject,
            ),
            full_message=full_message,
        )

    def commit_facts(self, oid: str) -> CandidateCommit:
        return self._metadata(oid).facts

    def commit_message(self, oid: str) -> str:
        return self._metadata(oid).full_message

    def changed_paths(self, parent: str, oid: str) -> list[str]:
        parent = _validate_oid(parent, label="parent")
        oid = _validate_oid(oid, label="commit")
        output = self._run(_render_args(GIT_PATHS_ARGS, parent=parent, commit=oid))
        fields = output.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise GitEvidenceError("Git path output has an incomplete name-status record")
        paths: list[str] = []
        for index in range(0, len(fields), 2):
            status, raw_path = fields[index : index + 2]
            if _STATUS_RE.fullmatch(status) is None or status[:1] in {b"C", b"R"}:
                raise GitEvidenceError("Git path output contains an unsupported status")
            paths.append(_decode_utf8(raw_path, label="changed path"))
        return _validate_path_set(paths)

    def patch_bytes(self, parent: str, oid: str) -> bytes:
        parent = _validate_oid(parent, label="parent")
        oid = _validate_oid(oid, label="commit")
        return self._run(_render_args(GIT_PATCH_ARGS, parent=parent, commit=oid))

    def eligible_patch_bytes(
        self, parent: str, oid: str, eligible_paths: Sequence[str]
    ) -> bytes:
        parent = _validate_oid(parent, label="parent")
        oid = _validate_oid(oid, label="commit")
        paths = _validate_path_set(eligible_paths)
        if not paths:
            raise GitEvidenceError("eligible patch requires at least one path")
        if tuple(GIT_ELIGIBLE_PATH_SUFFIX) != ("--", "{eligible_paths...}"):
            raise GitEvidenceError("eligible-path suffix differs from the frozen contract")
        args = [
            *_render_args(GIT_PATCH_ARGS, parent=parent, commit=oid),
            GIT_ELIGIBLE_PATH_SUFFIX[0],
            *paths,
        ]
        return self._run(args)

    def stable_patch_id(self, patch: bytes) -> str:
        output = self._run(GIT_PATCH_ID_ARGS, input_bytes=patch)
        lines = output.splitlines()
        if len(lines) != 1:
            raise GitEvidenceError("Git did not produce exactly one stable patch ID")
        fields = lines[0].split()
        if not fields or _OID_BYTES_RE.fullmatch(fields[0]) is None:
            raise GitEvidenceError("Git produced an invalid stable patch ID")
        return fields[0].decode("ascii")

    def git_version(self) -> str:
        output = self._run(GIT_VERSION_COMMAND, common_prefix=False)
        version = _decode_utf8(output, label="git version").strip()
        if not version:
            raise GitEvidenceError("git --version returned an empty value")
        return version


def _is_eligible(path: str, spec: FlareSearchSpec) -> bool:
    return any(posix_glob_matches(path, pattern) for pattern in spec.config_path_globs) and not any(
        posix_glob_matches(path, pattern) for pattern in spec.excluded_path_globs
    )


def _link_id(payload: dict[str, str]) -> str:
    return sha256_hex(canonical_bytes(payload))


def _trailer_link(
    *,
    link_type: Literal["cherry_pick", "backport", "revert"],
    source_oid: str,
    target_oid: str,
    rule_id: str,
) -> LineageLink:
    payload = {
        "link_type": link_type,
        "source_oid": source_oid,
        "target_oid": target_oid,
        "rule_id": rule_id,
    }
    return LineageLink(link_id=_link_id(payload), **payload)


def _patch_link(
    *, source_oid: str, target_oid: str, patch_id: str
) -> LineageLink:
    payload = {
        "link_type": "patch_id",
        "source_oid": source_oid,
        "target_oid": target_oid,
        "patch_id": patch_id,
    }
    return LineageLink(link_id=_link_id(payload), **payload)


def _reason_key(reason: SelectionReason) -> tuple[int, str, str, tuple[str, ...]]:
    return (
        _REASON_ORDER[reason.kind],
        reason.anchor_oid or "",
        reason.lineage_link_id or "",
        tuple(reason.rule_ids),
    )


def _sorted_reasons(reasons: Sequence[SelectionReason]) -> list[SelectionReason]:
    unique = {
        canonical_bytes(reason).decode("utf-8"): reason
        for reason in reasons
    }
    return sorted(unique.values(), key=_reason_key)


def _link_sort_key(link: LineageLink) -> tuple[str, str, str, str, str, str]:
    return (
        link.link_type,
        link.source_oid,
        link.target_oid,
        link.rule_id or "",
        link.patch_id or "",
        link.link_id,
    )


def discover_candidates(
    repo: ReadOnlyGitRepo,
    spec: FlareSearchSpec,
    registration: SearchRegistration,
    round_name: Literal["initial", "expanded"],
    blob_dir: Path,
) -> DiscoveryLedger:
    """Discover auditable candidates from the complete frozen reachable range."""

    if round_name not in {"initial", "expanded"}:
        raise GitEvidenceError(f"unknown search round: {round_name}")
    # Revalidate model-copy inputs so callers cannot bypass the frozen contract.
    spec = FlareSearchSpec.model_validate(spec.model_dump(mode="python"))
    registration = SearchRegistration.model_validate(registration.model_dump(mode="python"))

    # Keep repository-state failures distinct from an unresolvable registered head.
    repo._reject_local_attributes()
    repo._child_environment()
    try:
        resolved_head = repo.resolve(spec.pinned_head)
    except GitEvidenceError as exc:
        raise GitEvidenceError("unable to resolve pinned head") from exc
    if resolved_head != spec.pinned_head:
        raise GitEvidenceError("resolved pinned head differs from the search frame")

    history = repo.reachable_commits(spec)
    reachable = set(history)
    if spec.pinned_head not in reachable:
        raise GitEvidenceError("pinned head is absent from its reachable history")

    states: dict[str, _CommitState] = {}
    for oid in history:
        metadata = repo._metadata(oid)
        changed_paths = tuple(repo.changed_paths(metadata.facts.diff_base_oid, oid))
        eligible_paths = tuple(path for path in changed_paths if _is_eligible(path, spec))
        states[oid] = _CommitState(
            metadata=metadata,
            changed_paths=changed_paths,
            eligible_paths=eligible_paths,
        )

    selected_round_index = 0 if round_name == "initial" else 1
    selected_rounds = spec.rounds[: selected_round_index + 1]
    message_rules = [
        (rule.rule_id, re.compile(rule.pattern))
        for search_round in selected_rounds
        for rule in search_round.message_regexes
    ]
    diff_rules: list[tuple[str, re.Pattern[bytes]]] = []
    for search_round in selected_rounds:
        for rule in search_round.diff_regexes:
            try:
                diff_rules.append((rule.rule_id, re.compile(rule.pattern.encode("ascii"))))
            except (UnicodeEncodeError, re.error) as exc:
                raise GitEvidenceError(f"invalid ASCII bytes diff rule: {rule.rule_id}") from exc

    reasons: dict[str, list[SelectionReason]] = {}
    direct_oids: set[str] = set()
    for oid in history:
        state = states[oid]
        if not state.eligible_paths:
            continue
        matched_rule_ids = {
            rule_id
            for rule_id, pattern in message_rules
            if pattern.search(state.metadata.facts.subject) is not None
        }
        if diff_rules and len(state.metadata.facts.parent_oids) <= 1:
            eligible_patch = repo.eligible_patch_bytes(
                state.metadata.facts.diff_base_oid,
                oid,
                state.eligible_paths,
            )
            matched_rule_ids.update(
                rule_id
                for rule_id, pattern in diff_rules
                if pattern.search(eligible_patch) is not None
            )
        if matched_rule_ids:
            direct_oids.add(oid)
            reasons[oid] = [
                SelectionReason(kind="direct_match", rule_ids=sorted(matched_rule_ids))
            ]

    first_parent_children: dict[str, list[str]] = {}
    for oid, state in states.items():
        parents = state.metadata.facts.parent_oids
        if parents:
            first_parent_children.setdefault(parents[0], []).append(oid)
    for children in first_parent_children.values():
        children.sort()

    for anchor_oid in sorted(direct_oids):
        anchor = states[anchor_oid]
        neighbors: list[str] = []
        if anchor.metadata.facts.parent_oids:
            predecessor = anchor.metadata.facts.parent_oids[0]
            if predecessor in reachable:
                neighbors.append(predecessor)
        neighbors.extend(first_parent_children.get(anchor_oid, ()))
        anchor_paths = set(anchor.eligible_paths)
        for neighbor_oid in sorted(set(neighbors)):
            if anchor_paths.isdisjoint(states[neighbor_oid].eligible_paths):
                continue
            reasons.setdefault(neighbor_oid, []).append(
                SelectionReason(kind="adjacent_context", anchor_oid=anchor_oid)
            )

    objective_links: dict[str, LineageLink] = {}
    pending = sorted(reasons)
    parsed_targets: set[str] = set()
    while pending:
        target_oid = pending.pop(0)
        if target_oid in parsed_targets:
            continue
        parsed_targets.add(target_oid)
        message = states[target_oid].metadata.full_message
        for rule in spec.lineage_regexes:
            try:
                pattern = re.compile(rule.pattern)
            except re.error as exc:
                raise GitEvidenceError(f"invalid lineage rule: {rule.rule_id}") from exc
            for match in pattern.finditer(message):
                source_oid = match.group(1)
                _validate_oid(source_oid, label="lineage source")
                if source_oid not in reachable:
                    raise GitEvidenceError(
                        f"lineage source {source_oid} is unreachable from the pinned head"
                    )
                link = _trailer_link(
                    link_type=rule.link_type,
                    source_oid=source_oid,
                    target_oid=target_oid,
                    rule_id=rule.rule_id,
                )
                objective_links[link.link_id] = link
                reasons.setdefault(source_oid, []).append(
                    SelectionReason(kind="lineage_context", lineage_link_id=link.link_id)
                )
                if source_oid not in parsed_targets and source_oid not in pending:
                    pending.append(source_oid)
                    pending.sort()

    candidate_oids = sorted(
        reasons,
        key=lambda oid: (states[oid].metadata.facts.committed_at, oid),
    )
    patches: dict[str, bytes] = {}
    patch_ids: dict[str, list[str]] = {}
    for oid in candidate_oids:
        state = states[oid]
        if not state.changed_paths:
            raise GitEvidenceError(f"selected candidate {oid} has no changed paths")
        patch = repo.patch_bytes(state.metadata.facts.diff_base_oid, oid)
        if not patch:
            raise GitEvidenceError(f"selected candidate {oid} has an empty selected-edge patch")
        patches[oid] = patch
        patch_id = repo.stable_patch_id(patch)
        patch_ids.setdefault(patch_id, []).append(oid)

    candidate_key = {
        oid: (states[oid].metadata.facts.committed_at, oid) for oid in candidate_oids
    }
    for patch_id, matching_oids in patch_ids.items():
        ordered_oids = sorted(matching_oids, key=candidate_key.__getitem__)
        for source_oid, target_oid in combinations(ordered_oids, 2):
            link = _patch_link(
                source_oid=source_oid,
                target_oid=target_oid,
                patch_id=patch_id,
            )
            objective_links[link.link_id] = link

    links = sorted(objective_links.values(), key=_link_sort_key)
    candidates: list[DiscoveredCandidate] = []
    for oid in candidate_oids:
        state = states[oid]
        patch_sha256, patch_blob = put_blob(blob_dir, patches[oid])
        candidates.append(
            DiscoveredCandidate(
                commit=state.metadata.facts,
                changed_paths=list(state.changed_paths),
                eligible_paths=list(state.eligible_paths),
                config_only=state.config_only,
                selection_reasons=_sorted_reasons(reasons[oid]),
                diff_evidence=DiffEvidence(
                    commit_oid=oid,
                    patch_sha256=patch_sha256,
                    patch_blob=patch_blob,
                    commit_message=state.metadata.full_message,
                ),
            )
        )

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
        ),
        discovered_candidates=candidates,
        objective_lineage_links=links,
        candidate_universe_sha256=sha256_hex(canonical_bytes(universe)),
    )
