"""Read-only Git boundary for source-neutral external-corpus evidence."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Sequence

from gameforge.bench.external_corpus.contracts import (
    GIT_COMMON_PREFIX,
    GIT_DROP_INHERITED_PREFIXES,
    GIT_ELIGIBLE_PATH_SUFFIX,
    GIT_EMPTY_TREE_ARGS,
    GIT_FIXED_ENVIRONMENT,
    GIT_INHERIT_ALLOWLIST,
    GIT_METADATA_ARGS,
    GIT_PATCH_ARGS,
    GIT_PATCH_ID_ARGS,
    GIT_PATHS_ARGS,
    GIT_RESOLVE_ARGS,
    GIT_VERSION_COMMAND,
    CandidateCommit,
    CommitMetadata,
    SourceProfile,
)


_OID_RE = re.compile(r"[0-9a-f]{40}")
_OID_BYTES_RE = re.compile(rb"[0-9a-f]{40}")
_STATUS_RE = re.compile(rb"[A-Z][0-9]*")
_PROMISOR_KEY_RE = re.compile(rb"remote\..*\.promisor")
_PARTIAL_CLONE_FILTER_KEY_RE = re.compile(rb"remote\..*\.partialclonefilter")
_PARTIAL_CLONE_GET_REGEXP = (
    r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$"
)


class GitEvidenceError(RuntimeError):
    """Raised when Git cannot produce evidence under the frozen boundary."""


def _render_args(template: Sequence[str], **replacements: str) -> list[str]:
    rendered: list[str] = []
    for template_token in template:
        token = template_token
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
    if len(normalized) != len(set(normalized)):
        raise GitEvidenceError("Git returned duplicate paths")
    return sorted(normalized)


class ReadOnlyGitRepo:
    """Execute only deterministic, read-only Git commands against one clone."""

    def __init__(self, path: str | Path) -> None:
        self.input_path = Path(path)
        try:
            self.path = self.input_path.resolve()
        except (OSError, RuntimeError) as exc:
            raise GitEvidenceError(f"unable to resolve repository path: {self.input_path}") from exc
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
        raise GitEvidenceError(f"not a Git repository: {self.input_path}")

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
                raise GitEvidenceError(
                    f"required inherited environment variable is missing: {name}"
                )
            environment[name] = value
        environment.update(GIT_FIXED_ENVIRONMENT)
        return environment

    def _common_prefix(self) -> list[str]:
        return [str(self.git_dir) if token == "{repo}" else token for token in GIT_COMMON_PREFIX]

    def _run_safety_config(
        self,
        args: Sequence[str],
        *,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> bytes:
        command = [*self._common_prefix(), *args]
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_environment(),
                shell=False,
            )
        except OSError as exc:
            raise GitEvidenceError("unable to inspect effective local Git configuration") from exc
        if completed.returncode not in allowed_returncodes:
            raise GitEvidenceError("unable to inspect effective local Git configuration")
        if completed.returncode == 1 and (completed.stdout or completed.stderr):
            raise GitEvidenceError("Git config reported no matches with unexpected output")
        return completed.stdout

    def _effective_local_partial_clone_entries(
        self,
    ) -> list[tuple[str, bytes, bytes | None]]:
        output = self._run_safety_config(
            (
                "config",
                "--includes",
                "--null",
                "--show-scope",
                "--get-regexp",
                _PARTIAL_CLONE_GET_REGEXP,
            ),
            allowed_returncodes=frozenset({0, 1}),
        )
        if output and not output.endswith(b"\x00"):
            raise GitEvidenceError("effective local Git configuration has invalid output")
        fields = output.split(b"\x00")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) % 2:
            raise GitEvidenceError("effective local Git configuration has invalid output")
        entries: list[tuple[str, bytes, bytes | None]] = []
        for index in range(0, len(fields), 2):
            raw_scope, record = fields[index : index + 2]
            try:
                scope = raw_scope.decode("ascii", errors="strict")
            except UnicodeDecodeError as exc:
                raise GitEvidenceError("effective Git configuration has an invalid scope") from exc
            raw_key, separator, raw_value = record.partition(b"\n")
            if not raw_key:
                raise GitEvidenceError("effective local Git configuration has an empty key")
            if scope not in {"local", "worktree"}:
                continue
            entries.append((scope, raw_key, raw_value if separator else None))
        return entries

    @staticmethod
    def _parse_git_boolean(value: bytes | None) -> bool:
        if value is None:
            return True
        folded = value.lower()
        if folded in {b"true", b"yes", b"on", b"1"}:
            return True
        if folded in {b"", b"false", b"no", b"off", b"0"}:
            return False
        if re.fullmatch(rb"[+-]?[0-9]+", folded) is not None:
            return int(folded) != 0
        raise GitEvidenceError("promisor configuration is not a valid Git boolean")

    def _reject_partial_clone_config(self) -> None:
        for _scope, raw_key, raw_value in self._effective_local_partial_clone_entries():
            folded = raw_key.lower()
            if folded == b"extensions.partialclone" or _PARTIAL_CLONE_FILTER_KEY_RE.fullmatch(
                folded
            ):
                raise GitEvidenceError(
                    "partial clone configuration is forbidden for evidence discovery"
                )
            if _PROMISOR_KEY_RE.fullmatch(folded):
                remote_name = folded.removeprefix(b"remote.").removesuffix(b".promisor")
                if not remote_name:
                    raise GitEvidenceError(
                        "promisor remote name must not be empty for evidence discovery"
                    )
                if self._parse_git_boolean(raw_value):
                    raise GitEvidenceError(
                        "promisor remote configuration is forbidden for evidence discovery"
                    )

    def _reject_promisor_object_store(self) -> None:
        object_store = self._locate_common_git_dir() / "objects"
        try:
            if not object_store.is_dir():
                raise GitEvidenceError("repository common object store does not exist")
            pack_dir = object_store / "pack"
            if pack_dir.is_dir() and any(
                path.name.endswith(".promisor") for path in pack_dir.iterdir()
            ):
                raise GitEvidenceError(
                    "promisor object markers are forbidden for evidence discovery"
                )
        except OSError as exc:
            raise GitEvidenceError("unable to inspect repository common object store") from exc

    def preflight(self) -> None:
        self._reject_local_attributes()
        self._reject_partial_clone_config()
        self._reject_promisor_object_store()

    def _preflight_object_reads(self) -> None:
        """Compatibility alias for pre-existing evidence entry points."""
        self.preflight()

    def _run(
        self,
        args: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        common_prefix: bool = True,
    ) -> bytes:
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
        try:
            resolved = output.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("resolved commit is not ASCII") from exc
        return _validate_oid(resolved, label="resolved commit")

    @staticmethod
    def _ascii_oid_lines(output: bytes, label: str) -> list[str]:
        try:
            values = output.decode("ascii", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError(f"{label} contains a non-ASCII object ID") from exc
        if any(_OID_RE.fullmatch(value) is None for value in values):
            raise GitEvidenceError(f"{label} contains an invalid object ID")
        if len(values) != len(set(values)):
            raise GitEvidenceError(f"{label} contains duplicate commits")
        return values

    def reachable_commits(self, profile: SourceProfile) -> list[str]:
        return self._reachable_commits(
            pinned_head=profile.pinned_head,
            after_exclusive_oid=profile.history_range.after_exclusive_oid,
            committed_at_gte=profile.history_range.committed_at_gte,
            expected_commit_count=profile.history_range.expected_commit_count,
        )

    def _reachable_commits(
        self,
        *,
        pinned_head: str,
        after_exclusive_oid: str | None,
        committed_at_gte: int | None,
        expected_commit_count: int,
    ) -> list[str]:
        pinned_head = _validate_oid(pinned_head, label="pinned head")
        revision = pinned_head
        if after_exclusive_oid is not None:
            after_exclusive = _validate_oid(after_exclusive_oid, label="after_exclusive")
            revision = f"{after_exclusive}..{pinned_head}"
        args = ["rev-list", "--topo-order", "--reverse"]
        if committed_at_gte is not None:
            args.append(f"--since={committed_at_gte}")
        args.append(revision)
        commits = self._ascii_oid_lines(self._run(args), "Git history")
        if len(commits) != expected_commit_count:
            raise GitEvidenceError(
                "reachable revision count differs from frozen expectation: "
                f"expected {expected_commit_count}, observed {len(commits)}"
            )
        return commits

    def _empty_tree_oid(self) -> str:
        output = self._run(GIT_EMPTY_TREE_ARGS, input_bytes=b"")
        try:
            oid = output.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("empty-tree object is not ASCII") from exc
        return _validate_oid(oid, label="empty-tree object")

    def commit_metadata(self, oid: str) -> CommitMetadata:
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
        selected_parent = parent_oids[0] if parent_oids else None
        return CommitMetadata(
            commit=CandidateCommit(
                commit_oid=commit_oid,
                parent_oids=parent_oids,
                selected_parent_oid=selected_parent,
                diff_base_oid=selected_parent or self._empty_tree_oid(),
                committed_at=committed_at,
                subject=_decode_utf8(raw_subject, label="commit subject"),
            ),
            full_message=_decode_utf8(raw_message, label="commit message"),
        )

    def commit_facts(self, oid: str) -> CandidateCommit:
        return self.commit_metadata(oid).commit

    def commit_message(self, oid: str) -> str:
        return self.commit_metadata(oid).full_message

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

    def eligible_patch_bytes(self, parent: str, oid: str, eligible_paths: Sequence[str]) -> bytes:
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
