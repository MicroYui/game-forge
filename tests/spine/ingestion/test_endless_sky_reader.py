from __future__ import annotations

import pytest

from gameforge.spine.ingestion.endless_sky_reader import (
    EndlessSkyParseError,
    parse_data_file,
    read_source_tree,
    render_data_file,
    render_source_tree,
    top_level_chunks,
)


def test_reader_builds_token_tree_and_round_trips_exact_bytes() -> None:
    raw = (
        b"# preamble\r\n"
        b'mission "Quest Name"\r\n'
        b"\tto offer # comment\r\n"
        b"\t\thas `Quest Zero: done`\r\n"
        b"\r\n"
    )

    parsed = parse_data_file(raw, "data/missions.txt")

    mission = parsed.roots[0]
    assert [token.value for token in mission.tokens] == ["mission", "Quest Name"]
    assert [token.quote for token in mission.tokens] == ["bare", "double"]
    assert mission.children[0].tokens[0].value == "to"
    assert mission.children[0].children[0].tokens[1].value == "Quest Zero: done"
    assert mission.source_span.start_line == 2
    assert parsed.lines[0].kind == "comment"
    assert parsed.lines[-1].kind == "blank"
    assert render_data_file(parsed) == raw


def test_top_level_chunks_partition_every_input_byte_once() -> None:
    raw = b"# banner\nmission A\n\tsource X\n\nmission B\n\tdestination Y"
    chunks = top_level_chunks(parse_data_file(raw, "data/a.txt"))

    assert b"".join(chunk.raw for chunk in chunks) == raw
    assert [(chunk.kind, chunk.name) for chunk in chunks] == [
        ("mission", "A"),
        ("mission", "B"),
    ]
    assert chunks[0].raw.startswith(b"# banner\n")
    assert chunks[-1].raw.endswith(b"\tdestination Y")


@pytest.mark.parametrize("raw", [b"", b"# only comment", b"\n\r\n"])
def test_raw_only_files_still_form_one_lossless_chunk(raw: bytes) -> None:
    parsed = parse_data_file(raw, "data/raw.txt")
    chunks = top_level_chunks(parsed)

    assert len(chunks) == 1
    assert chunks[0].kind == "raw"
    assert chunks[0].raw == raw
    assert render_data_file(parsed) == raw


def test_reader_preserves_exact_token_spelling_and_source_offsets() -> None:
    raw = b'effect `star tail hit`\n\tsound "explosion small" # used here\n'
    parsed = parse_data_file(raw, "data/effects.txt")
    effect = parsed.roots[0]
    sound = effect.children[0]

    assert effect.tokens[1].raw == b"`star tail hit`"
    assert sound.tokens[1].raw == b'"explosion small"'
    assert sound.source_span.start_byte == raw.index(b"\tsound")
    assert sound.source_span.end_byte == len(raw)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b'mission "unterminated\n', "unterminated"),
        (b"mission \xff\n", "UTF-8"),
        (b"mission A\x00\n", "NUL"),
    ],
)
def test_reader_rejects_malformed_input_with_source_location(raw: bytes, reason: str) -> None:
    with pytest.raises(EndlessSkyParseError, match=reason) as exc:
        parse_data_file(raw, "data/bad.txt")

    assert exc.value.path == "data/bad.txt"
    assert exc.value.line >= 1


def test_source_tree_is_path_sorted_and_round_trips_every_file() -> None:
    source = {
        "data/z.txt": b"mission Z\n",
        "data/a.txt": b"mission A\r\n",
    }

    tree = read_source_tree(source)

    assert [file.path for file in tree.files] == ["data/a.txt", "data/z.txt"]
    assert render_source_tree(tree) == source


def test_source_tree_rejects_non_normalized_paths() -> None:
    with pytest.raises(ValueError, match="normalized"):
        read_source_tree({"data/../outside.txt": b"mission A\n"})
