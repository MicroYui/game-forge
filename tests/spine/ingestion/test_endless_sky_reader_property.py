from __future__ import annotations

from hypothesis import given, settings, strategies as st

from gameforge.spine.ingestion.endless_sky_reader import (
    parse_data_file,
    render_data_file,
    top_level_chunks,
)


_BARE = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="_-.:/<>+=",
    ),
    min_size=1,
    max_size=18,
).filter(lambda value: "#" not in value)
_QUOTED = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd", "Zs"),
        whitelist_characters="_-.:/<>+=#'",
    ),
    max_size=24,
).filter(lambda value: '"' not in value and "\n" not in value and "\r" not in value)
_BACKTICK = _QUOTED.filter(lambda value: "`" not in value)


@st.composite
def _node_line(draw) -> bytes:
    depth = draw(st.integers(min_value=0, max_value=4))
    indent_char = draw(st.sampled_from([b"\t", b" "]))
    indent = indent_char * depth
    first = draw(_BARE).encode("utf-8")
    rest = draw(
        st.lists(
            st.one_of(
                _BARE.map(lambda value: value.encode("utf-8")),
                _QUOTED.map(lambda value: b'"' + value.encode("utf-8") + b'"'),
                _BACKTICK.map(lambda value: b"`" + value.encode("utf-8") + b"`"),
            ),
            max_size=3,
        )
    )
    comment = draw(
        st.one_of(
            st.just(b""),
            _BARE.map(lambda value: b" # " + value.encode("utf-8")),
        )
    )
    newline = draw(st.sampled_from([b"\n", b"\r\n"]))
    return indent + b" ".join([first, *rest]) + comment + newline


@st.composite
def _source_files(draw) -> bytes:
    entries = draw(
        st.lists(
            st.one_of(
                _node_line(),
                st.sampled_from([b"\n", b"\r\n", b"# comment\n", b"\t# indented\r\n"]),
            ),
            max_size=35,
        )
    )
    raw = b"".join(entries)
    if raw and draw(st.booleans()):
        raw = raw.removesuffix(b"\r\n").removesuffix(b"\n")
    return raw


@settings(max_examples=300, deadline=None)
@given(_source_files())
def test_render_parse_is_byte_exact(raw: bytes) -> None:
    parsed = parse_data_file(raw, "data/property.txt")

    assert render_data_file(parsed) == raw
    assert b"".join(chunk.raw for chunk in top_level_chunks(parsed)) == raw
