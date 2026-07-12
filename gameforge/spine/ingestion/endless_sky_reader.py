"""Lossless reader for Endless Sky's indentation-based data files.

The semantic view mirrors the upstream DataFile shape (tokens plus indentation
children). The physical-line view remains the rendering authority, so comments,
quotes, line endings, blank lines, and a missing final newline survive exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Mapping


READER_VERSION = "endless-sky-reader@1"


class EndlessSkyParseError(ValueError):
    def __init__(self, path: str, line: int, reason: str) -> None:
        self.path = path
        self.line = line
        self.reason = reason
        super().__init__(f"{path}:{line}: {reason}")


@dataclass(frozen=True)
class SourceSpan:
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class DataToken:
    value: str
    raw: bytes
    quote: Literal["bare", "double", "backtick"]


@dataclass(frozen=True)
class PhysicalLine:
    raw: bytes
    content: bytes
    indent: bytes
    kind: Literal["blank", "comment", "node"]
    tokens: tuple[DataToken, ...]
    line_number: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class DataNode:
    tokens: tuple[DataToken, ...]
    children: tuple["DataNode", ...]
    indent: bytes
    source_span: SourceSpan
    line_index: int


@dataclass(frozen=True)
class DataFile:
    path: str
    raw: bytes
    lines: tuple[PhysicalLine, ...]
    roots: tuple[DataNode, ...]
    reader_version: str = READER_VERSION


@dataclass(frozen=True)
class TopLevelChunk:
    path: str
    index: int
    kind: str
    name: str
    raw: bytes
    source_span: SourceSpan
    node: DataNode | None


@dataclass(frozen=True)
class EndlessSkyTree:
    files: tuple[DataFile, ...]
    reader_version: str = READER_VERSION

    def get(self, path: str) -> DataFile | None:
        return next((file for file in self.files if file.path == path), None)


@dataclass
class _MutableNode:
    line: PhysicalLine
    line_index: int
    children: list["_MutableNode"] = field(default_factory=list)


def _normalized_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("source path must be a nonempty normalized POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("source path must be a normalized repository-relative POSIX path")
    return value


def _raw_lines(raw: bytes) -> list[bytes]:
    lines: list[bytes] = []
    start = 0
    while start < len(raw):
        newline = raw.find(b"\n", start)
        if newline < 0:
            lines.append(raw[start:])
            break
        lines.append(raw[start : newline + 1])
        start = newline + 1
    return lines


def _without_newline(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2]
    if raw_line.endswith(b"\n"):
        return raw_line[:-1]
    return raw_line


def _is_space(value: int) -> bool:
    return value <= 0x20


def _tokenize(content: bytes, indent_len: int, path: str, line: int) -> tuple[DataToken, ...]:
    tokens: list[DataToken] = []
    pos = indent_len
    while pos < len(content):
        while pos < len(content) and _is_space(content[pos]):
            pos += 1
        if pos >= len(content) or content[pos] == ord("#"):
            break

        start = pos
        quote_byte = content[pos]
        if quote_byte in (ord('"'), ord("`")):
            pos += 1
            value_start = pos
            while pos < len(content) and content[pos] != quote_byte:
                pos += 1
            if pos >= len(content):
                raise EndlessSkyParseError(path, line, "unterminated quoted token")
            value_raw = content[value_start:pos]
            pos += 1
            raw_token = content[start:pos]
            quote: Literal["double", "backtick"] = (
                "double" if quote_byte == ord('"') else "backtick"
            )
        else:
            while pos < len(content) and not _is_space(content[pos]):
                pos += 1
            raw_token = content[start:pos]
            value_raw = raw_token
            quote = "bare"

        try:
            value = value_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EndlessSkyParseError(path, line, "invalid UTF-8 token") from exc
        tokens.append(DataToken(value=value, raw=raw_token, quote=quote))
    return tuple(tokens)


def _freeze_node(node: _MutableNode, path: str) -> DataNode:
    children = tuple(_freeze_node(child, path) for child in node.children)
    line = node.line
    end_line = children[-1].source_span.end_line if children else line.line_number
    end_byte = children[-1].source_span.end_byte if children else line.end_byte
    return DataNode(
        tokens=line.tokens,
        children=children,
        indent=line.indent,
        source_span=SourceSpan(
            path=path,
            start_line=line.line_number,
            end_line=end_line,
            start_byte=line.start_byte,
            end_byte=end_byte,
        ),
        line_index=node.line_index,
    )


def parse_data_file(raw: bytes, path: str) -> DataFile:
    """Parse bytes into a semantic token tree without losing physical bytes."""

    normalized_path = _normalized_path(path)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        raise EndlessSkyParseError(normalized_path, line, "invalid UTF-8 input") from exc
    nul = raw.find(b"\x00")
    if nul >= 0:
        line = raw[:nul].count(b"\n") + 1
        raise EndlessSkyParseError(normalized_path, line, "NUL byte is not allowed")

    physical: list[PhysicalLine] = []
    offset = 0
    for line_index, raw_line in enumerate(_raw_lines(raw)):
        line_number = line_index + 1
        content = _without_newline(raw_line)
        indent_len = 0
        while indent_len < len(content) and _is_space(content[indent_len]):
            indent_len += 1
        indent = content[:indent_len]
        remainder = content[indent_len:]
        if not remainder:
            kind: Literal["blank", "comment", "node"] = "blank"
            tokens: tuple[DataToken, ...] = ()
        elif remainder.startswith(b"#"):
            kind = "comment"
            tokens = ()
        else:
            kind = "node"
            tokens = _tokenize(content, indent_len, normalized_path, line_number)
            if not tokens:
                kind = "comment" if b"#" in remainder else "blank"
        physical.append(
            PhysicalLine(
                raw=raw_line,
                content=content,
                indent=indent,
                kind=kind,
                tokens=tokens,
                line_number=line_number,
                start_byte=offset,
                end_byte=offset + len(raw_line),
            )
        )
        offset += len(raw_line)

    roots: list[_MutableNode] = []
    stack: list[_MutableNode] = []
    for line_index, line in enumerate(physical):
        if line.kind != "node":
            continue
        node = _MutableNode(line=line, line_index=line_index)
        while stack and len(stack[-1].line.indent) >= len(line.indent):
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    return DataFile(
        path=normalized_path,
        raw=raw,
        lines=tuple(physical),
        roots=tuple(_freeze_node(root, normalized_path) for root in roots),
    )


def render_data_file(data_file: DataFile) -> bytes:
    if data_file.reader_version != READER_VERSION:
        raise ValueError(f"unsupported reader version: {data_file.reader_version}")
    rendered = b"".join(line.raw for line in data_file.lines)
    if rendered != data_file.raw:
        raise ValueError("physical lines do not partition original source bytes")
    return rendered


def top_level_chunks(data_file: DataFile) -> tuple[TopLevelChunk, ...]:
    """Partition a file into exact chunks anchored by top-level semantic nodes."""

    if not data_file.roots:
        end_line = len(data_file.lines) or 1
        return (
            TopLevelChunk(
                path=data_file.path,
                index=0,
                kind="raw",
                name=data_file.path,
                raw=data_file.raw,
                source_span=SourceSpan(
                    path=data_file.path,
                    start_line=1,
                    end_line=end_line,
                    start_byte=0,
                    end_byte=len(data_file.raw),
                ),
                node=None,
            ),
        )

    chunks: list[TopLevelChunk] = []
    boundaries = [root.line_index for root in data_file.roots] + [len(data_file.lines)]
    for index, root in enumerate(data_file.roots):
        start_line_index = 0 if index == 0 else boundaries[index]
        end_line_index = boundaries[index + 1]
        selected_lines = data_file.lines[start_line_index:end_line_index]
        raw = b"".join(line.raw for line in selected_lines)
        start_byte = selected_lines[0].start_byte if selected_lines else root.source_span.start_byte
        end_byte = selected_lines[-1].end_byte if selected_lines else root.source_span.end_byte
        kind = root.tokens[0].value
        name = root.tokens[1].value if len(root.tokens) > 1 else kind
        chunks.append(
            TopLevelChunk(
                path=data_file.path,
                index=index,
                kind=kind,
                name=name,
                raw=raw,
                source_span=SourceSpan(
                    path=data_file.path,
                    start_line=start_line_index + 1,
                    end_line=end_line_index or 1,
                    start_byte=start_byte,
                    end_byte=end_byte,
                ),
                node=root,
            )
        )
    return tuple(chunks)


def read_source_tree(files: Mapping[str, bytes]) -> EndlessSkyTree:
    parsed = tuple(parse_data_file(raw, path) for path, raw in sorted(files.items()))
    return EndlessSkyTree(files=parsed)


def render_source_tree(tree: EndlessSkyTree) -> dict[str, bytes]:
    if tree.reader_version != READER_VERSION:
        raise ValueError(f"unsupported reader version: {tree.reader_version}")
    return {file.path: render_data_file(file) for file in tree.files}


def count_nodes(data_file: DataFile) -> int:
    def count(node: DataNode) -> int:
        return 1 + sum(count(child) for child in node.children)

    return sum(count(root) for root in data_file.roots)


def count_tokens(data_file: DataFile) -> int:
    return sum(len(line.tokens) for line in data_file.lines)


__all__ = [
    "DataFile",
    "DataNode",
    "DataToken",
    "EndlessSkyParseError",
    "EndlessSkyTree",
    "PhysicalLine",
    "READER_VERSION",
    "SourceSpan",
    "TopLevelChunk",
    "count_nodes",
    "count_tokens",
    "parse_data_file",
    "read_source_tree",
    "render_data_file",
    "render_source_tree",
    "top_level_chunks",
]
