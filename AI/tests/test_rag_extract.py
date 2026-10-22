"""Reading uploads safely: what is accepted, what is refused, and what text comes out."""
import io
import zipfile

import pytest

from app.rag import extract as extract_module
from app.rag.extract import ExtractionError, detect_kind, extract
from tests.helpers import make_docx, make_pdf


# ── what is accepted ─────────────────────────────────────────────────────
@pytest.mark.parametrize("name, data, kind", [
    ("report.pdf", make_pdf(["hi"]), "pdf"),
    ("REPORT.PDF", make_pdf(["hi"]), "pdf"),  # extension case does not matter
    ("notes.docx", make_docx([("p", "hi")]), "docx"),
    ("notes.txt", b"plain text", "text"),
    ("README.md", b"# Title", "markdown"),
    ("notes.markdown", b"# Title", "markdown"),
])
def test_supported_files_are_recognised(name, data, kind):
    assert detect_kind(name, data) == kind


# ── what is refused, and why ─────────────────────────────────────────────
@pytest.mark.parametrize("name", ["tool.exe", "image.png", "archive.zip", "page.html", "noextension", "script.py", "data.csv"])
def test_other_extensions_are_refused(name):
    with pytest.raises(ExtractionError, match="Unsupported"):
        detect_kind(name, b"data")


def test_old_word_files_get_a_helpful_message():
    with pytest.raises(ExtractionError, match=r"\.docx"):
        detect_kind("old.doc", b"\xd0\xcf\x11\xe0")


def test_an_empty_file_is_refused():
    with pytest.raises(ExtractionError, match="empty"):
        detect_kind("a.txt", b"")


def test_a_program_renamed_to_pdf_is_refused():
    with pytest.raises(ExtractionError, match="not a valid PDF"):
        detect_kind("invoice.pdf", b"MZ\x90\x00 this is an executable")


def test_a_zip_renamed_to_docx_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("hello.txt", "not a word document")
    with pytest.raises(ExtractionError, match="not a valid Word"):
        detect_kind("fake.docx", buffer.getvalue())


def test_garbage_renamed_to_docx_is_refused():
    with pytest.raises(ExtractionError, match="not a valid Word"):
        detect_kind("fake.docx", b"just some bytes, not even a zip")


def test_binary_data_renamed_to_txt_is_refused():
    with pytest.raises(ExtractionError, match="binary"):
        detect_kind("data.txt", b"abc\x00\x01\x02def")


def test_a_docx_that_expands_enormously_is_refused(monkeypatch):
    monkeypatch.setattr(extract_module, "MAX_DOCX_UNCOMPRESSED", 1000)  # a "zip bomb" check, at a testable size
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "A" * 50_000)  # tiny compressed, huge when expanded
    with pytest.raises(ExtractionError, match="too large"):
        detect_kind("bomb.docx", buffer.getvalue())


# ── PDF ──────────────────────────────────────────────────────────────────
def test_a_pdf_gives_one_page_per_page_with_numbers():
    result = extract("pdf", make_pdf(["first page text", "second page text"]))
    assert result.page_count == 2
    assert [(p.number, p.text) for p in result.pages] == [(1, "first page text"), (2, "second page text")]


def test_a_pdf_over_the_page_limit_is_refused():
    with pytest.raises(ExtractionError, match="limit is 2"):
        extract("pdf", make_pdf(["a", "b", "c"]), max_pages=2)


def test_a_pdf_with_no_text_asks_for_ocr():
    with pytest.raises(ExtractionError, match="OCR"):
        extract("pdf", make_pdf(["", ""]))


def test_a_password_protected_pdf_is_refused():
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf(["secret"]))).pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ExtractionError, match="password"):
        extract("pdf", buffer.getvalue())


def test_a_damaged_pdf_gives_a_plain_message_not_a_stack_trace():
    with pytest.raises(ExtractionError, match="could not be read"):
        extract("pdf", b"%PDF-1.4\nthis is not really a pdf at all")


# ── Word ─────────────────────────────────────────────────────────────────
def test_a_word_document_keeps_headings_and_tables_in_order():
    data = make_docx([("h1", "Refund policy"), ("p", "Refunds take five days."), ("h2", "Exceptions"), ("p", "None apply.")],
                     table=[["Plan", "Price"], ["Pro", "20"]])
    result = extract("docx", data)
    text = result.pages[0].text
    assert result.page_count is None and result.pages[0].number is None
    assert text.index("# Refund policy") < text.index("Refunds take five days.") < text.index("## Exceptions")
    assert "Plan | Price" in text and "Pro | 20" in text  # a table row becomes one readable line


# ── text and Markdown ────────────────────────────────────────────────────
def test_text_handles_a_utf8_byte_order_mark():
    assert extract("text", b"\xef\xbb\xbfhello").pages[0].text == "hello"


def test_old_windows_text_does_not_crash():
    assert "caf" in extract("text", "caf\xe9 au lait".encode("cp1252")).pages[0].text


def test_markdown_is_kept_as_written():
    assert extract("markdown", b"# Title\n\nSome *emphasis*.").pages[0].text.startswith("# Title")


def test_a_whitespace_only_file_has_no_readable_text():
    with pytest.raises(ExtractionError, match="No readable text"):
        extract("text", b"   \n\n  ")


def test_a_file_with_too_much_text_is_refused():
    with pytest.raises(ExtractionError, match="too much text"):
        extract("text", b"word " * 100, max_chars=100)
