"""Deterministic text rendering for planning-document exchange formats.

The parsers never execute HTML, Office formulas, macros, external XML entities,
or archive paths.  Their sole output is bounded canonical UTF-8 prompt context.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO, StringIO
import json
from pathlib import PurePosixPath
import re
from typing import Any, Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MaterialSourceFormat = Literal[
    "plain_text",
    "markdown",
    "html",
    "feishu_blocks_json",
    "docx",
    "xlsx",
    "csv",
]
DEFAULT_MAX_INPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_COMPRESSION_RATIO = 200


class MaterialParseError(ValueError):
    """The supplied material cannot be rendered safely and deterministically."""


@dataclass(frozen=True, slots=True)
class ParsedPlanningMaterial:
    text: str
    parser_id: str
    parser_version: str
    warnings: tuple[str, ...] = ()


def _decode_utf8(payload: bytes) -> str:
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MaterialParseError("planning material must be valid UTF-8") from exc


def _newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_text(value: str) -> str:
    value = _newlines(value)
    if not value.strip():
        raise MaterialParseError("planning material rendered to empty text")
    return value


class _SafeHtmlText(HTMLParser):
    _BLOCKS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "dl",
        "dt",
        "dd",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif self._ignored_depth == 0 and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def rendered(self) -> str:
        lines = [
            re.sub(r"[ \t\f\v]+", " ", line).strip() for line in "".join(self.parts).splitlines()
        ]
        return "\n".join(line for line in lines if line)


def _markdown_cell(value: str) -> str:
    return _newlines(value).replace("|", "\\|").replace("\n", "<br>").strip()


def _render_table(title: str, rows: list[list[str]]) -> str:
    if not rows:
        return f"# Sheet: {title}\n\n（空表）"
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    header = padded[0]
    body = padded[1:]
    lines = [f"# Sheet: {title}", "", " | ".join(_markdown_cell(cell) for cell in header)]
    lines.append(" | ".join("---" for _ in range(width)))
    lines.extend(" | ".join(_markdown_cell(cell) for cell in row) for row in body)
    return "\n".join(lines)


def _parse_csv(payload: bytes) -> str:
    text = _decode_utf8(payload)
    try:
        rows = [list(row) for row in csv.reader(StringIO(_newlines(text)), strict=True)]
    except csv.Error as exc:
        raise MaterialParseError("CSV material is malformed") from exc
    while rows and not any(cell for cell in rows[-1]):
        rows.pop()
    return _render_table("CSV", rows)


def _runs_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    elements = value.get("elements")
    if not isinstance(elements, list):
        return ""
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run")
        if isinstance(text_run, dict) and isinstance(text_run.get("content"), str):
            parts.append(text_run["content"])
        equation = element.get("equation")
        if isinstance(equation, dict) and isinstance(equation.get("content"), str):
            parts.append(equation["content"])
    return "".join(parts).strip()


def _feishu_block_text(block: dict[str, Any]) -> str:
    for level in range(1, 10):
        text = _runs_text(block.get(f"heading{level}"))
        if text:
            return f"{'#' * level} {text}"
    text = _runs_text(block.get("text"))
    if text:
        return text
    for key, prefix in (("bullet", "- "), ("ordered", "1. "), ("todo", "- [ ] ")):
        text = _runs_text(block.get(key))
        if text:
            return prefix + text
    for key in ("quote", "callout", "code"):
        text = _runs_text(block.get(key))
        if text:
            return text
    return ""


def _parse_feishu(payload: bytes) -> str:
    try:
        document = json.loads(_decode_utf8(payload))
    except (json.JSONDecodeError, TypeError) as exc:
        raise MaterialParseError("Feishu block material is invalid JSON") from exc
    blocks = document.get("blocks") if isinstance(document, dict) else None
    if not isinstance(blocks, list):
        raise MaterialParseError("Feishu block material requires a blocks array")
    rendered = [_feishu_block_text(block) for block in blocks if isinstance(block, dict)]
    return "\n\n".join(text for text in rendered if text)


def _safe_archive(
    payload: bytes,
    *,
    max_expanded_bytes: int,
) -> dict[str, bytes]:
    try:
        archive = ZipFile(BytesIO(payload))
    except BadZipFile as exc:
        raise MaterialParseError("Office material is not a valid ZIP package") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise MaterialParseError("Office archive has too many members")
        total = 0
        result: dict[str, bytes] = {}
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise MaterialParseError("unsafe archive path")
            if info.flag_bits & 0x1:
                raise MaterialParseError("encrypted Office archives are unsupported")
            total += info.file_size
            if total > max_expanded_bytes:
                raise MaterialParseError("Office archive exceeds expanded size limit")
            if info.file_size and info.compress_size == 0:
                raise MaterialParseError("Office archive has an unsafe compression ratio")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise MaterialParseError("Office archive has an unsafe compression ratio")
            if not info.is_dir():
                result[str(path)] = archive.read(info)
        return result


def _xml(payload: bytes, *, label: str) -> ElementTree.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise MaterialParseError(f"{label} XML declares an external entity")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise MaterialParseError(f"{label} XML is malformed") from exc


def _parse_docx(payload: bytes, *, max_expanded_bytes: int) -> tuple[str, tuple[str, ...]]:
    files = _safe_archive(payload, max_expanded_bytes=max_expanded_bytes)
    document = files.get("word/document.xml")
    if document is None:
        raise MaterialParseError("DOCX package has no word/document.xml")
    root = _xml(document, label="DOCX document")
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if not paragraph.tag.endswith("}p"):
            continue
        parts = [
            node.text or ""
            for node in paragraph.iter()
            if node.tag.endswith("}t") or node.tag.endswith("}tab")
        ]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs), ()


def _relationship_targets(payload: bytes) -> dict[str, str]:
    root = _xml(payload, label="XLSX relationships")
    result: dict[str, str] = {}
    for relationship in root:
        identity = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        mode = relationship.attrib.get("TargetMode")
        if mode == "External":
            continue
        if identity and target:
            normalized = str(PurePosixPath("xl") / target).replace("xl/../", "")
            result[identity] = normalized
    return result


def _shared_strings(payload: bytes | None) -> list[str]:
    if payload is None:
        return []
    root = _xml(payload, label="XLSX shared strings")
    return [
        "".join(node.text or "" for node in item.iter() if node.tag.endswith("}t"))
        for item in root
        if item.tag.endswith("}si")
    ]


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha()).upper()
    if not letters:
        raise MaterialParseError("XLSX cell has no column reference")
    result = 0
    for character in letters:
        result = result * 26 + ord(character) - 64
    return result - 1


def _xlsx_rows(
    payload: bytes,
    *,
    shared: list[str],
    warnings: set[str],
) -> list[list[str]]:
    root = _xml(payload, label="XLSX worksheet")
    rows: list[list[str]] = []
    for row in (node for node in root.iter() if node.tag.endswith("}row")):
        cells: dict[int, str] = {}
        for cell in (node for node in row if node.tag.endswith("}c")):
            reference = cell.attrib.get("r", "")
            index = _column_index(reference)
            cell_type = cell.attrib.get("t")
            formula = next((node for node in cell if node.tag.endswith("}f")), None)
            if formula is not None:
                warnings.add("formula_ignored")
            raw_value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
            if cell_type == "s" and raw_value:
                try:
                    value = shared[int(raw_value)]
                except (ValueError, IndexError) as exc:
                    raise MaterialParseError("XLSX shared-string index is invalid") from exc
            elif cell_type == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
            elif cell_type == "b":
                value = "true" if raw_value == "1" else "false"
            else:
                value = raw_value
            cells[index] = value
        width = max(cells, default=-1) + 1
        rows.append([cells.get(index, "") for index in range(width)])
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def _parse_xlsx(payload: bytes, *, max_expanded_bytes: int) -> tuple[str, tuple[str, ...]]:
    files = _safe_archive(payload, max_expanded_bytes=max_expanded_bytes)
    workbook_raw = files.get("xl/workbook.xml")
    relationships_raw = files.get("xl/_rels/workbook.xml.rels")
    if workbook_raw is None or relationships_raw is None:
        raise MaterialParseError("XLSX package lacks workbook authority")
    workbook = _xml(workbook_raw, label="XLSX workbook")
    relationships = _relationship_targets(relationships_raw)
    shared = _shared_strings(files.get("xl/sharedStrings.xml"))
    warnings: set[str] = set()
    rendered: list[str] = []
    for sheet in (node for node in workbook.iter() if node.tag.endswith("}sheet")):
        name = sheet.attrib.get("name")
        relationship_id = next(
            (value for key, value in sheet.attrib.items() if key.endswith("}id")),
            None,
        )
        if not name or not relationship_id or relationship_id not in relationships:
            raise MaterialParseError("XLSX sheet binding is incomplete")
        target = relationships[relationship_id]
        raw = files.get(target)
        if raw is None:
            raise MaterialParseError("XLSX worksheet target is missing")
        rendered.append(_render_table(name, _xlsx_rows(raw, shared=shared, warnings=warnings)))
    return "\n\n".join(rendered), tuple(sorted(warnings))


def parse_planning_material(
    payload: bytes,
    source_format: MaterialSourceFormat,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> ParsedPlanningMaterial:
    """Render one planning document using the exact named parser."""

    if not isinstance(payload, bytes) or not payload:
        raise MaterialParseError("planning material bytes must be non-empty")
    if len(payload) > max_input_bytes:
        raise MaterialParseError("planning material exceeds input size limit")
    if max_expanded_bytes < 1:
        raise ValueError("max_expanded_bytes must be positive")

    warnings: tuple[str, ...] = ()
    if source_format in {"plain_text", "markdown"}:
        text = _newlines(_decode_utf8(payload))
    elif source_format == "html":
        parser = _SafeHtmlText()
        try:
            parser.feed(_decode_utf8(payload))
            parser.close()
        except Exception as exc:  # HTMLParser can expose malformed charrefs
            raise MaterialParseError("HTML material is malformed") from exc
        text = parser.rendered()
    elif source_format == "feishu_blocks_json":
        text = _parse_feishu(payload)
    elif source_format == "csv":
        text = _parse_csv(payload)
    elif source_format == "docx":
        text, warnings = _parse_docx(payload, max_expanded_bytes=max_expanded_bytes)
    elif source_format == "xlsx":
        text, warnings = _parse_xlsx(payload, max_expanded_bytes=max_expanded_bytes)
    else:  # pragma: no cover - Literal callers, retained for fail-closed runtime use
        raise MaterialParseError(f"unsupported planning material format: {source_format!r}")

    return ParsedPlanningMaterial(
        text=_require_text(text),
        parser_id=f"planning-material-{source_format}",
        parser_version="1",
        warnings=warnings,
    )


__all__ = [
    "DEFAULT_MAX_EXPANDED_BYTES",
    "DEFAULT_MAX_INPUT_BYTES",
    "MaterialParseError",
    "ParsedPlanningMaterial",
    "parse_planning_material",
]
