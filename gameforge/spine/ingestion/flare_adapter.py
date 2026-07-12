"""FlareTxtAdapter — to_ir/from_ir between Flare's INI-like `.txt` config format
and Spec-IR (M1 Task 10, contract §12A.1 external-validity anchor).

Flare (flare-engine / flare-game, https://github.com/flareteam/flare-game) is
an open-source, data-driven action-RPG whose config files are plain-text
records of `key=value` lines: some files (`items.txt`) hold MANY records per
file, each starting with a `[item]` header and separated by blank lines;
others (one `enemies/<name>.txt` per monster) hold exactly ONE record per
file, keyed by filename rather than an in-record id. `#` comments are
allowed anywhere, and — critically — the SAME key legitimately repeats
within one record (`stat=`, `loot=`, `bonus=`, ...; even a scalar-looking key
like `quality=` can appear twice, the later line overriding the earlier one
in Flare's own parser, but BOTH lines must survive a round trip).

This mirrors the M0b `AureusCsvAdapter`'s losslessness technique exactly: a
record's FULL content is preserved verbatim (as an ORDERED list of typed line
entries, not a plain dict — a dict would silently drop repeated keys and
reorder them) in `entity.attrs["lines"]`. `from_ir` is a pure projection that
reconstructs `{pk, file, row, lines}` from `attrs` + `entity.id` +
`source_ref.row` — nothing is invented, nothing is lost, so the round trip is
lossless BY CONSTRUCTION.

Line kinds (each fully reversible on its own):
    blank   -- an empty line                                  -> ""
    comment -- a `#`-prefixed line (verbatim, incl. commented-
               out `#key=value` lines, which must NEVER be
               misread as a live kv pair)                      -> text
    section -- a bare `[name]` header line                     -> f"[{name}]"
    kv      -- a `key=value` line (split on the FIRST `=` only,
               so `key + "=" + value` always reconstructs the
               original line exactly, by construction of
               `str.partition`)                                -> f"{key}={value}"
    raw     -- anything else (opaque directive lines like
               `INCLUDE path/to/file.txt`, which have no `=`)   -> text
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation, SourceRef
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import IRGraph

_ADAPTER_ID = "flare"

# file-kind (workbook "sheet") -> Spec-IR NodeType (contract §12A.1 mapping).
SHEET_NODE_TYPE: dict[str, NodeType] = {
    "items": NodeType.ITEM,
    "enemies": NodeType.MONSTER,
}

_SECTION_RE = re.compile(r"^\[(\w+)\]$")


class FlareAdapterError(Exception):
    """Raised when Flare source text can't be parsed into a well-formed record
    (e.g. an `[item]` block missing its required `id=` field)."""


# --- line-level parse / render (the losslessness primitive) ---------------


def _parse_physical_line(line: str) -> dict[str, Any]:
    if line == "":
        return {"kind": "blank"}
    if line.lstrip().startswith("#"):
        return {"kind": "comment", "text": line}
    m = _SECTION_RE.match(line)
    if m is not None:
        return {"kind": "section", "name": m.group(1)}
    if "=" in line:
        key, _, value = line.partition("=")
        return {"kind": "kv", "key": key, "value": value}
    return {"kind": "raw", "text": line}


def _render_line(entry: dict[str, Any]) -> str:
    kind = entry["kind"]
    if kind == "blank":
        return ""
    if kind == "comment" or kind == "raw":
        return entry["text"]
    if kind == "section":
        return f"[{entry['name']}]"
    if kind == "kv":
        return f"{entry['key']}={entry['value']}"
    raise FlareAdapterError(f"unknown flare line kind: {kind!r}")


def parse_flare_text(content: str) -> list[dict[str, Any]]:
    """Parse raw Flare `.txt` content into an ORDERED list of typed line
    entries. `render_flare_lines` is its exact inverse for any input."""
    return [_parse_physical_line(line) for line in content.split("\n")]


def render_flare_lines(lines: list[dict[str, Any]]) -> str:
    """Inverse of `parse_flare_text`: reconstructs the exact original text."""
    return "\n".join(_render_line(e) for e in lines)


def render_records(records: list[dict[str, Any]]) -> str:
    """Reconstruct the exact original file text from an ordered list of Flare
    records belonging to the SAME source file (sorted by `row`). Each
    record's `lines` are a contiguous slice of the original file's lines, so
    concatenating them in row order and rendering reproduces the file
    byte-for-byte (contract §12A.1 round-trip anchor)."""
    ordered = sorted(records, key=lambda r: r["row"])
    all_lines = [entry for rec in ordered for entry in rec["lines"]]
    return render_flare_lines(all_lines)


def _find_id(lines: list[dict[str, Any]]) -> str:
    for entry in lines:
        if entry["kind"] == "kv" and entry["key"] == "id":
            return entry["value"]
    raise FlareAdapterError("Flare item record is missing its required 'id=' field")


def _segment_records(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a multi-record file's lines into per-record chunks at each
    `[section]` header. Any preamble before the first header (banner
    comments, etc.) is folded into the first record's chunk so that
    concatenating all chunks in order reproduces the whole file exactly. A
    file with no section headers at all is treated as a single record."""
    section_idxs = [i for i, e in enumerate(lines) if e["kind"] == "section"]
    if not section_idxs:
        return [lines]
    boundaries = section_idxs + [len(lines)]
    chunks = []
    for i, sec_i in enumerate(section_idxs):
        chunk_start = 0 if i == 0 else sec_i
        chunk_end = boundaries[i + 1]
        chunks.append(lines[chunk_start:chunk_end])
    return chunks


# --- directory -> workbook (records with pk/file/row/lines) ----------------


def read_flare_dir(directory: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Parse a Flare sample directory into `{"items": [...], "enemies": [...]}`
    records, each `{"pk": str, "file": str, "row": int, "lines": [...]}`.

    Convention (matches the vendored `scenarios/flare_sample` layout):
      - `<directory>/items.txt`    -- multi-record file, one `[item]` block
                                      per record, pk = its `id=` field.
      - `<directory>/enemies/*.txt` -- one record per file, pk = filename
                                      stem (Flare enemies have no in-record
                                      id; the file *is* the entity's identity).
    """
    base = Path(directory)
    workbook: dict[str, list[dict[str, Any]]] = {}

    items_path = base / "items.txt"
    if items_path.is_file():
        lines = parse_flare_text(items_path.read_text(encoding="utf-8"))
        records = []
        for row, chunk in enumerate(_segment_records(lines)):
            records.append({"pk": _find_id(chunk), "file": "items.txt", "row": row, "lines": chunk})
        workbook["items"] = records

    enemies_dir = base / "enemies"
    if enemies_dir.is_dir():
        records = []
        for row, path in enumerate(sorted(enemies_dir.glob("*.txt"))):
            lines = parse_flare_text(path.read_text(encoding="utf-8"))
            records.append({"pk": path.stem, "file": f"enemies/{path.name}", "row": row, "lines": lines})
        workbook["enemies"] = records

    return workbook


class _RelIds:
    """Deterministic `rel:<TYPE>:<src>-><dst>:<n>` ids (same scheme as `AureusCsvAdapter`)."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, etype: EdgeType, src: str, dst: str) -> str:
        rid = f"rel:{etype.value}:{src}->{dst}:{self._n}"
        self._n += 1
        return rid


class FlareTxtAdapter:
    """Adapter for the Flare open-source-game `.txt` config format (contract §12A.1)."""

    format_id = "flare"

    def to_ir(self, workbook: dict[str, list[dict[str, Any]]], file_ref: str) -> Snapshot:
        g = IRGraph()

        # --- pass 1: every record -> one typed Entity, full line content verbatim in attrs ---
        for sheet, node_type in SHEET_NODE_TYPE.items():
            for rec in workbook.get(sheet, []):
                sref = SourceRef(
                    adapter=_ADAPTER_ID, file=rec.get("file", file_ref), sheet=sheet, row=rec["row"]
                )
                g.add_entity(Entity(
                    id=f"{sheet}:{rec['pk']}", type=node_type,
                    attrs={"lines": rec["lines"]}, source_ref=sref,
                ))

        # --- pass 2: derived DROPS_FROM edges (enemy -> item) via
        # `loot=<item_id>,<chance>` ---
        # A `loot=` value is an item reference only when its first comma-separated
        # token is a KNOWN item pk (e.g. `loot=32,5`); `loot=loot/leveled_low.txt`
        # (a loot-TABLE pointer, not a direct item drop) never matches and is
        # correctly left with no derived edge.
        item_pks = {rec["pk"] for rec in workbook.get("items", [])}
        rid = _RelIds()
        for rec in workbook.get("enemies", []):
            monster_entity_id = f"enemies:{rec['pk']}"
            for entry in rec["lines"]:
                if entry["kind"] != "kv" or entry["key"] != "loot":
                    continue
                item_pk = entry["value"].split(",", 1)[0].strip()
                if item_pk not in item_pks:
                    continue
                item_entity_id = f"items:{item_pk}"
                g.add_relation(Relation(
                    id=rid.next(
                        EdgeType.DROPS_FROM, monster_entity_id, item_entity_id
                    ),
                    type=EdgeType.DROPS_FROM,
                    src_id=monster_entity_id,
                    dst_id=item_entity_id,
                    source_ref=SourceRef(
                        adapter=_ADAPTER_ID, file=rec.get("file", file_ref), sheet="enemies", row=rec["row"]
                    ),
                ))

        return Snapshot.from_graph(g)

    def from_ir(self, snapshot: Snapshot) -> dict[str, list[dict[str, Any]]]:
        g = snapshot.to_graph()
        workbook: dict[str, list[dict[str, Any]]] = {}
        for sheet, node_type in SHEET_NODE_TYPE.items():
            entities = g.nodes_of_type(node_type)
            if not entities:
                continue  # emit ONLY sheets that have entities
            entities.sort(
                key=lambda e: e.source_ref.row
                if e.source_ref is not None and e.source_ref.row is not None
                else 0
            )
            workbook[sheet] = [
                {
                    "pk": e.id.split(":", 1)[1],
                    "file": e.source_ref.file if e.source_ref is not None else None,
                    "row": e.source_ref.row if e.source_ref is not None else None,
                    "lines": e.attrs["lines"],
                }
                for e in entities
            ]
        return workbook
