import io
import zipfile

import pytest

from app.core.config import REPO_ROOT
from app.core.errors import UnsafeFileError
from app.document_engine.samples import build_docx, build_pptx, build_xlsx
from app.services.uploads import clean_name, validate_upload

MB = 1024 * 1024


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    d = tmp_path_factory.mktemp("up")
    build_docx(d / "a.docx"); build_pptx(d / "a.pptx"); build_xlsx(d / "a.xlsx")
    return {e: (d / f"a.{e}").read_bytes() for e in ("docx", "pptx", "xlsx")}


def rewrite(data: bytes, mutate) -> bytes:
    """Rebuild a zip with a mutation hook mutate(zipfile.ZipFile out, name, bytes) -> None to add/replace."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i in src.infolist():
            mutate(z, i.filename, src.read(i.filename))
    return out.getvalue()


def test_valid_office_files_pass(good):
    for ext, data in good.items():
        v = validate_upload(f"My Report.{ext}", data, 25 * MB)
        assert v.format == ext and v.ext == ext and v.size == len(data)


def test_real_samples_pass():
    for rel in ("docx/sec_filing_sample.docx", "xlsx/sec_filing_sample.xls", "pdf/ael_investors_presentation.pdf"):
        p = REPO_ROOT / "sample_templates" / rel
        if p.exists():
            v = validate_upload(p.name, p.read_bytes(), 25 * MB)
            assert v.format in ("docx", "xlsx", "pdf")


@pytest.mark.parametrize("name", ["evil.docm", "x.XLSM", "deck.pptm", "t.dotm", "a.xlsb", "a.xlam"])
def test_macro_extensions_refused(name, good):
    with pytest.raises(UnsafeFileError, match="macro"):
        validate_upload(name, good["docx"], MB)


@pytest.mark.parametrize("name", ["run.exe", "a.js", "a.html", "a.svg", "noext", "a.docx.exe", "a.zip"])
def test_unsupported_types_refused(name, good):
    with pytest.raises(UnsafeFileError, match="not supported"):
        validate_upload(name, good["docx"], MB)


def test_extension_and_content_must_agree(good):
    with pytest.raises(UnsafeFileError, match="extension and content disagree"):
        validate_upload("report.docx", good["pptx"], 25 * MB)
    with pytest.raises(UnsafeFileError, match="extension and content disagree"):
        validate_upload("deck.pptx", good["xlsx"], 25 * MB)


def test_renamed_non_office_content_refused():
    with pytest.raises(UnsafeFileError, match="not a valid"):
        validate_upload("a.docx", b"MZ\x90\x00 this is an exe", MB)
    with pytest.raises(UnsafeFileError, match="not a valid"):
        validate_upload("a.xls", b"PK\x03\x04 zip pretending to be xls", MB)
    with pytest.raises(UnsafeFileError, match="not a valid PDF"):
        validate_upload("a.pdf", b"<html>hi</html>", MB)


def test_corrupt_zip_and_missing_content_types(good):
    with pytest.raises(UnsafeFileError, match="corrupt|bad zip"):
        validate_upload("a.docx", good["docx"][:200], MB)
    stripped = rewrite(good["docx"], lambda z, n, b: None if n == "[Content_Types].xml" else z.writestr(n, b))
    with pytest.raises(UnsafeFileError, match="Content_Types"):
        validate_upload("a.docx", stripped, MB)


def test_vba_project_inside_an_ooxml_package_is_refused(good):
    def add(z, n, b):
        z.writestr(n, b)
        if n == "[Content_Types].xml":
            z.writestr("word/vbaProject.bin", b"\x00fake")
    with pytest.raises(UnsafeFileError, match="macros"):
        validate_upload("a.docx", rewrite(good["docx"], add), MB)


def test_macro_enabled_content_type_is_refused_even_with_a_docx_name(good):
    def swap(z, n, b):
        z.writestr(n, b.replace(b"wordprocessingml.document.main+xml", b"wordprocessingml.document.macroEnabled.main+xml") if n == "[Content_Types].xml" else b)
    with pytest.raises(UnsafeFileError, match="macro"):
        validate_upload("a.docx", rewrite(good["docx"], swap), MB)


def test_xml_entity_declaration_is_refused(good):
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]><x>&xxe;</x>'
    def inject(z, n, b):
        z.writestr(n, evil if n == "word/styles.xml" else b)
    with pytest.raises(UnsafeFileError, match="DOCTYPE"):
        validate_upload("a.docx", rewrite(good["docx"], inject), MB)


def test_path_traversal_part_names_are_refused(good):
    def add(z, n, b):
        z.writestr(n, b)
        if n == "[Content_Types].xml":
            z.writestr("../../evil.txt", b"x")
    with pytest.raises(UnsafeFileError, match="unsafe part name"):
        validate_upload("a.docx", rewrite(good["docx"], add), MB)


def test_zip_bomb_is_refused(good):
    def add(z, n, b):
        z.writestr(n, b)
        if n == "[Content_Types].xml":
            z.writestr("word/media/bomb.bin", b"\x00" * (60 * MB))  # ~60 KB compressed -> ratio >> 200
    with pytest.raises(UnsafeFileError, match="zip bomb"):
        validate_upload("a.docx", rewrite(good["docx"], add), 25 * MB)


def test_too_many_parts_refused(good):
    def add(z, n, b):
        z.writestr(n, b)
        if n == "[Content_Types].xml":
            for k in range(5100):
                z.writestr(f"customXml/p{k}.txt", b"x")
    with pytest.raises(UnsafeFileError, match="too many parts"):
        validate_upload("a.docx", rewrite(good["docx"], add), 25 * MB)


def test_size_limit_and_empty(good):
    with pytest.raises(UnsafeFileError, match="larger than the 1 MB"):
        validate_upload("a.docx", good["docx"] + b"\x00" * (2 * MB), MB)
    with pytest.raises(UnsafeFileError, match="empty"):
        validate_upload("a.docx", b"", MB)


def test_xls_with_a_vba_stream_name_is_refused():
    ole = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 600 + "_VBA_PROJECT_CUR".encode("utf-16-le") + b"\x00" * 100
    with pytest.raises(UnsafeFileError, match="macros"):
        validate_upload("a.xls", ole, MB)
    clean_ole = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 600
    assert validate_upload("a.xls", clean_ole, MB).format == "xlsx"


def test_text_files():
    assert validate_upload("n.md", "héllo".encode(), MB).format == "txt"
    with pytest.raises(UnsafeFileError, match="UTF-8"):
        validate_upload("n.txt", "héllo".encode("latin-1"), MB)
    with pytest.raises(UnsafeFileError, match="text file"):
        validate_upload("n.txt", b"abc\x00def", MB)


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "passwd"), ("C:\\Users\\x\\a.docx", "a.docx"), ("a\x00b\x07.docx", "ab.docx"),
    ("...", "file"), ("", "file"), ("x" * 400 + ".docx", None)])
def test_display_name_cleaning(raw, expected):
    out = clean_name(raw)
    if expected is None:
        assert len(out) == 150
    else:
        assert out == expected
