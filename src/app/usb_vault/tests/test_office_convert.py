"""office_convert 单测: 扩展名映射与"未装 Office"错误路径 (不真正调 COM)。

真转换路径不进自动化 (依赖本机 Office), 由 README 手动验收清单覆盖。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.app.usb_vault import office_convert
from src.app.usb_vault.office_convert import ConvertError


def test_prog_id_mapping() -> None:
    assert office_convert.PROG_ID_BY_EXT[".docx"] == "Word.Application"
    assert office_convert.PROG_ID_BY_EXT[".xls"] == "Excel.Application"
    assert office_convert.PROG_ID_BY_EXT[".pptx"] == "PowerPoint.Application"
    assert ".pdf" not in office_convert.PROG_ID_BY_EXT


def test_missing_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_convert, "is_app_available", lambda prog: False)
    files = [Path("a.docx"), Path("b.xlsx")]
    missing = office_convert.missing_apps(files)
    assert missing == {"Word.Application", "Excel.Application"}


def test_convert_batch_aborts_without_office(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(office_convert, "is_app_available", lambda prog: False)
    src = tmp_path / "合同.docx"
    src.write_bytes(b"PK")
    with pytest.raises(ConvertError, match="Word.Application"):
        office_convert.convert_batch([src], tmp_path / "out")


def test_convert_batch_names_offending_files(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(office_convert, "is_app_available", lambda prog: False)
    src = tmp_path / "报价表.docx"
    src.write_bytes(b"PK")
    with pytest.raises(ConvertError, match="报价表.docx"):
        office_convert.convert_batch([src], tmp_path / "out")


_MINIMAL_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"'
        ' Target="word/document.xml"/></Relationships>'
    ),
    "word/document.xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>usb_vault test</w:t></w:r></w:p></w:body></w:document>"
    ),
}


@pytest.mark.skipif(
    not office_convert.is_app_available("Word.Application"),
    reason="本机未装 Word, 真转换走 README 手动验收",
)
def test_real_word_conversion(tmp_path: Path) -> None:
    """装了 Word 才跑: 现造一个合法最小 docx, 断言能转出 PDF。"""
    import zipfile

    docx = tmp_path / "最小文档.docx"
    with zipfile.ZipFile(docx, "w") as zf:
        for name, content in _MINIMAL_DOCX_PARTS.items():
            zf.writestr(name, content)
    result = office_convert.convert_batch([docx], tmp_path / "out")
    pdf = result[docx]
    assert pdf.is_file() and pdf.read_bytes().startswith(b"%PDF")
