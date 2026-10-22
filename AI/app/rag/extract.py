"""Turn an uploaded file into plain text, safely.

Four formats are accepted: PDF, Word (.docx), plain text and Markdown. An upload is UNTRUSTED
input from the internet, so before any parser sees it we check that

  * the extension is on the allowlist (nothing else is ever opened),
  * the file's real CONTENT matches its extension (a program renamed to report.pdf is refused),
  * the size and the amount of text stay under limits (a "zip bomb" or a million-page PDF must
    not be able to exhaust memory).

Parsers get the bytes only after those checks, and any parser failure becomes a short, safe
message for the user (never a stack trace).
"""
from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

log = logging.getLogger("chat.rag.extract")

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".docx": "docx", ".txt": "text", ".md": "markdown", ".markdown": "markdown"}
MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024  # a .docx is a zip file: refuse one that expands to more than this


class ExtractionError(Exception):
    """The file cannot be used. `public_message` is safe to show the user."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


@dataclass(frozen=True)
class Page:
    number: int | None  # PDF page number; None for formats that have no pages
    text: str


@dataclass(frozen=True)
class Extracted:
    kind: str
    pages: list[Page]
    page_count: int | None


def detect_kind(filename: str, data: bytes) -> str:
    """The kind of file this really is ('pdf', 'docx', 'text', 'markdown'), or ExtractionError."""
    ext = PurePosixPath(filename.lower()).suffix
    if ext == ".doc":
        raise ExtractionError("Old Word .doc files are not supported. Save the file as .docx and upload that.")
    kind = ALLOWED_EXTENSIONS.get(ext)
    if kind is None:
        raise ExtractionError("Unsupported file type. Upload a PDF, a Word (.docx) file, or a text or Markdown file.")
    if not data:
        raise ExtractionError("The file is empty.")

    # The content must match the claimed type (the extension alone proves nothing).
    if kind == "pdf" and not data.lstrip()[:5] == b"%PDF-":
        raise ExtractionError("This file is not a valid PDF.")
    if kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" not in z.namelist():
                    raise ExtractionError("This file is not a valid Word (.docx) document.")
                if sum(i.file_size for i in z.infolist()) > MAX_DOCX_UNCOMPRESSED:
                    raise ExtractionError("This document is too large to process.")
        except zipfile.BadZipFile:
            raise ExtractionError("This file is not a valid Word (.docx) document.") from None
    if kind in ("text", "markdown") and b"\x00" in data[:8192]:
        raise ExtractionError("This looks like a binary file, not text.")
    return kind


def extract(kind: str, data: bytes, max_pages: int = 300, max_chars: int = 2_000_000) -> Extracted:
    """Read the text out of a file whose kind was already checked by detect_kind()."""
    try:
        if kind == "pdf":
            result = _pdf(data, max_pages)
        elif kind == "docx":
            result = _docx(data)
        else:
            result = _text(kind, data)
    except ExtractionError:
        raise
    except Exception as exc:  # a parser choking on a malformed file: say so plainly
        log.warning("extraction_failed kind=%s error_type=%s", kind, type(exc).__name__)
        raise ExtractionError("This file could not be read. It may be damaged.") from exc

    total = sum(len(p.text) for p in result.pages)
    if total == 0:
        raise ExtractionError("No readable text was found. A scanned document (an image) needs OCR first.")
    if total > max_chars:
        raise ExtractionError("This document contains too much text to process.")
    return result


def _pdf(data: bytes, max_pages: int) -> Extracted:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):  # many "protected" PDFs open with an empty password
                raise ExtractionError("This PDF is password-protected.")
        except ExtractionError:
            raise
        except Exception:
            raise ExtractionError("This PDF is password-protected.") from None
    count = len(reader.pages)
    if count > max_pages:
        raise ExtractionError(f"This PDF has {count} pages; the limit is {max_pages}.")
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # one bad page should not lose the whole document
            text = ""
        pages.append(Page(i, text))
    return Extracted("pdf", pages, count)


def _docx(data: bytes) -> Extracted:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    lines: list[str] = []
    # Walk the body in order, so a table stays where it is in the text. Headings become Markdown
    # "# " lines, so the chunker treats Word and Markdown the same way.
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            style = (paragraph.style.name or "") if paragraph.style is not None else ""
            if style.lower().startswith("heading") or style.lower() == "title":
                digits = "".join(ch for ch in style if ch.isdigit())
                lines.append(f"{'#' * min(int(digits or 1), 6)} {text}")
            else:
                lines.append(text)
        elif child.tag.endswith("}tbl"):
            for row in Table(child, document).rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
    return Extracted("docx", [Page(None, "\n\n".join(lines))], None)


def _text(kind: str, data: bytes) -> Extracted:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")  # old Windows text files
    return Extracted(kind, [Page(None, text.strip())], None)
