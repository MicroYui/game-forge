"""Binary-safe offline replay helpers for Git patch evidence."""

from __future__ import annotations

import re
from collections.abc import Sequence


def _git_c_quote_path(value: bytes) -> bytes:
    escapes = {
        0x07: b"\\a",
        0x08: b"\\b",
        0x09: b"\\t",
        0x0A: b"\\n",
        0x0B: b"\\v",
        0x0C: b"\\f",
        0x0D: b"\\r",
        0x22: b'\\"',
        0x5C: b"\\\\",
    }
    rendered = bytearray()
    quoted = False
    for byte in value:
        escape = escapes.get(byte)
        if escape is not None:
            rendered.extend(escape)
            quoted = True
        elif byte < 0x20 or byte >= 0x7F:
            rendered.extend(f"\\{byte:03o}".encode("ascii"))
            quoted = True
        else:
            rendered.append(byte)
    result = bytes(rendered)
    return b'"' + result + b'"' if quoted else result


def _frozen_diff_header(path: str) -> bytes:
    encoded = path.encode("utf-8", errors="strict")
    return b"diff --git " + _git_c_quote_path(b"a/" + encoded) + b" " + _git_c_quote_path(
        b"b/" + encoded
    )


def extract_eligible_patch_bytes(
    full_patch: bytes,
    *,
    changed_paths: Sequence[str],
    eligible_paths: Sequence[str],
) -> bytes:
    """Derive Git's path-filtered patch by selecting exact full-patch file blocks."""

    changed = list(changed_paths)
    eligible = set(eligible_paths)
    if not changed or len(changed) != len(set(changed)):
        raise ValueError("full patch replay requires unique changed paths")
    if not eligible <= set(changed):
        raise ValueError("eligible patch replay paths must be changed paths")
    starts = [match.start() for match in re.finditer(rb"(?m)^diff --git ", full_patch)]
    if not starts or starts[0] != 0:
        raise ValueError("full patch does not start with a Git file-diff block")
    expected_headers = {_frozen_diff_header(path): path for path in changed}
    if len(expected_headers) != len(changed):
        raise ValueError("changed paths do not map to unique frozen diff headers")

    seen: set[str] = set()
    selected: list[bytes] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(full_patch)
        block = full_patch[start:end]
        header, separator, _body = block.partition(b"\n")
        if not separator:
            raise ValueError("Git file-diff block has no header terminator")
        path = expected_headers.get(header)
        if path is None:
            raise ValueError("full patch contains a file-diff header outside changed_paths")
        if path in seen:
            raise ValueError("full patch contains a duplicate changed-path block")
        seen.add(path)
        if path in eligible:
            selected.append(block)
    if seen != set(changed):
        raise ValueError("full patch blocks do not exactly cover changed_paths")
    return b"".join(selected)


__all__ = ["extract_eligible_patch_bytes"]
