from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from gameforge.spine.ingestion.planning_materials import (
    MaterialParseError,
    parse_planning_material,
)


def _zip(entries: dict[str, str]) -> bytes:
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return out.getvalue()


def test_plain_markdown_html_and_csv_render_canonical_text() -> None:
    assert parse_planning_material(b"A\r\nB\r\n", "plain_text").text == "A\nB\n"
    assert parse_planning_material("# 标题\r\n内容".encode(), "markdown").text == "# 标题\n内容"
    html = b"<h1>Sky</h1><script>steal()</script><p>Air &amp; Water</p>"
    rendered = parse_planning_material(html, "html").text
    assert rendered == "Sky\nAir & Water"
    assert "steal" not in rendered
    assert parse_planning_material(b"id,name\r\nnpc:a,Alice\r\n", "csv").text == (
        "# Sheet: CSV\n\nid | name\n--- | ---\nnpc:a | Alice"
    )


def test_feishu_block_json_extracts_semantic_text_in_document_order() -> None:
    payload = (
        '{"blocks":['
        '{"block_type":3,"heading1":{"elements":[{"text_run":{"content":"世界观"}}]}},'
        '{"block_type":2,"text":{"elements":[{"text_run":{"content":"天空港"}},'
        '{"text_run":{"content":"依靠风核。"}}]}},'
        '{"block_type":12,"bullet":{"elements":[{"text_run":{"content":"空气质量 air.quality"}}]}}'
        "]}"
    ).encode()
    result = parse_planning_material(payload, "feishu_blocks_json")
    assert result.text == "# 世界观\n\n天空港依靠风核。\n\n- 空气质量 air.quality"


def test_docx_and_xlsx_exports_are_parsed_without_executing_formulas() -> None:
    docx = _zip(
        {
            "[Content_Types].xml": "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
            "word/document.xml": (
                "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
                "<w:body><w:p><w:r><w:t>天空港</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>空气质量</w:t></w:r></w:p></w:body></w:document>"
            ),
        }
    )
    assert parse_planning_material(docx, "docx").text == "天空港\n\n空气质量"

    xlsx = _zip(
        {
            "[Content_Types].xml": "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'/>",
            "xl/workbook.xml": (
                "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main' "
                "xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>"
                "<sheets><sheet name='NPC' sheetId='1' r:id='rId1'/></sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
                "<Relationship Id='rId1' Target='worksheets/sheet1.xml' "
                "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet'/>"
                "</Relationships>"
            ),
            "xl/sharedStrings.xml": (
                "<sst xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
                "<si><t>id</t></si><si><t>name</t></si><si><t>npc:a</t></si><si><t>港务员</t></si></sst>"
            ),
            "xl/worksheets/sheet1.xml": (
                "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
                "<sheetData><row r='1'><c r='A1' t='s'><v>0</v></c><c r='B1' t='s'><v>1</v></c></row>"
                "<row r='2'><c r='A2' t='s'><v>2</v></c><c r='B2' t='s'><f>EVIL()</f><v>3</v></c></row>"
                "</sheetData></worksheet>"
            ),
        }
    )
    rendered = parse_planning_material(xlsx, "xlsx")
    assert rendered.text == "# Sheet: NPC\n\nid | name\n--- | ---\nnpc:a | 港务员"
    assert "EVIL" not in rendered.text
    assert "formula_ignored" in rendered.warnings


def test_zip_slip_encrypted_or_oversized_archives_fail_closed() -> None:
    with pytest.raises(MaterialParseError, match="unsafe archive path"):
        parse_planning_material(_zip({"../word/document.xml": "bad"}), "docx")

    oversized = _zip({"word/document.xml": "x" * 2_100_000})
    with pytest.raises(MaterialParseError, match="expanded size"):
        parse_planning_material(oversized, "docx", max_expanded_bytes=2_000_000)
