# """
# pipeline/extractors.py — Extract clean text from PDF, DOCX, and HTML files.

# Each extractor returns plain text suitable for chunking + embedding.
# Tables are preserved in a readable key:value format because tables
# contain critical structured information (leave days, schemas, policies).

# Public API:
#     extract(file_path: str | Path) -> str
#     extract_bytes(content: bytes, file_type: str) -> str

# The extractors are defensive: malformed pages or files don't crash the pipeline,
# they just produce warnings and skip the bad section.
# """

# import io
# import logging
# from pathlib import Path
# from typing import Union

# log = logging.getLogger(__name__)


# # ═══════════════════════════════════════════════════════════
# #  PDF EXTRACTION
# # ═══════════════════════════════════════════════════════════

# def extract_pdf_bytes(content: bytes) -> str:
#     """Extract text from PDF bytes. Returns plain text, page-separated."""
#     from pypdf import PdfReader

#     reader = PdfReader(io.BytesIO(content))
#     pages = []
#     for i, page in enumerate(reader.pages, start=1):
#         try:
#             text = page.extract_text() or ""
#             text = text.strip()
#             if text:
#                 pages.append(text)
#         except Exception as e:
#             log.warning(f"PDF page {i}: extraction failed — {e}")
#             continue

#     return "\n\n".join(pages)


# def extract_pdf(path: Union[str, Path]) -> str:
#     """Extract text from a PDF file on disk."""
#     with open(path, "rb") as f:
#         return extract_pdf_bytes(f.read())


# # ═══════════════════════════════════════════════════════════
# #  DOCX EXTRACTION
# # ═══════════════════════════════════════════════════════════

# def _table_to_text(table) -> str:
#     """
#     Convert a DOCX table to retrieval-friendly text.

#     Strategy:
#       - Wide tables (>= 5 cols): each row becomes a multi-line
#         "Header: value" block (better for embedding precision)
#       - Narrow tables: keep as single "Header: value | Header: value" lines

#     Wrapped in [TABLE]...[/TABLE] markers so chunker can recognize them.
#     """
#     rows = list(table.rows)
#     if not rows:
#         return ""

#     # Extract cells. Handle merged cells by deduping consecutive identical cells.
#     def cells_of(row):
#         seen, prev, out = [], None, []
#         for cell in row.cells:
#             text = cell.text.strip()
#             if text != prev:
#                 out.append(text)
#             prev = text
#         return out

#     header = cells_of(rows[0])
#     is_wide = len(header) >= 5

#     output = ["[TABLE]"]
#     if not is_wide:
#         # Compact format for narrow tables
#         output.append(" | ".join(header))
#         for row in rows[1:]:
#             cells = cells_of(row)
#             if len(cells) == len(header):
#                 output.append(" | ".join(
#                     f"{h}: {v}" for h, v in zip(header, cells) if v
#                 ))
#             else:
#                 output.append(" | ".join(cells))
#     else:
#         # Expanded format for wide tables (each row is a block)
#         for row_idx, row in enumerate(rows[1:], start=1):
#             cells = cells_of(row)
#             output.append(f"Row {row_idx}:")
#             for h, v in zip(header, cells):
#                 if v:
#                     output.append(f"  {h}: {v}")
#             output.append("")  # blank line between rows

#     output.append("[/TABLE]")
#     return "\n".join(output)


# def extract_docx_bytes(content: bytes) -> str:
#     """Extract text from DOCX bytes, preserving headings, paragraphs, and tables."""
#     from docx import Document
#     from docx.text.paragraph import Paragraph
#     from docx.table import Table

#     doc = Document(io.BytesIO(content))
#     sections = []

#     for child in doc.element.body:
#         tag = child.tag.split("}")[-1]

#         if tag == "p":
#             try:
#                 para = Paragraph(child, doc)
#             except Exception:
#                 continue
#             text = para.text.strip()
#             if not text:
#                 continue

#             style_name = para.style.name if para.style else ""
#             if "Heading" in style_name:
#                 level_str = style_name.replace("Heading ", "").strip()
#                 level = int(level_str) if level_str.isdigit() else 2
#                 prefix = "#" * level
#                 sections.append(f"\n{prefix} {text}\n")
#             else:
#                 sections.append(text)

#         elif tag == "tbl":
#             try:
#                 table = Table(child, doc)
#                 table_text = _table_to_text(table)
#                 if table_text.strip():
#                     sections.append("\n" + table_text + "\n")
#             except Exception as e:
#                 log.warning(f"DOCX table extraction failed: {e}")
#                 continue

#     return "\n".join(sections)


# def extract_docx(path: Union[str, Path]) -> str:
#     """Extract text from a DOCX file on disk."""
#     with open(path, "rb") as f:
#         return extract_docx_bytes(f.read())


# # ═══════════════════════════════════════════════════════════
# #  HTML EXTRACTION
# # ═══════════════════════════════════════════════════════════

# def extract_html_bytes(content: bytes) -> str:
#     """
#     Extract text from HTML bytes.

#     Strips scripts, styles, and navigation. Preserves heading hierarchy
#     by converting <h1>..<h6> to markdown # headers, and lists to bullets.
#     """
#     from bs4 import BeautifulSoup

#     # Try lxml first (faster); fall back to built-in if lxml unhappy
#     try:
#         soup = BeautifulSoup(content, "lxml")
#     except Exception:
#         soup = BeautifulSoup(content, "html.parser")

#     # Drop noise tags
#     for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
#         tag.decompose()

#     # Convert headings to markdown
#     for level in range(1, 7):
#         for h in soup.find_all(f"h{level}"):
#             prefix = "#" * level
#             h.replace_with(f"\n{prefix} {h.get_text(strip=True)}\n")

#     # Convert list items to bullets
#     for li in soup.find_all("li"):
#         li.replace_with(f"- {li.get_text(' ', strip=True)}\n")

#     # Convert tables to simple pipe-delimited rows
#     for table in soup.find_all("table"):
#         rows_out = ["[TABLE]"]
#         for tr in table.find_all("tr"):
#             cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
#             if any(cells):
#                 rows_out.append(" | ".join(cells))
#         rows_out.append("[/TABLE]")
#         table.replace_with("\n" + "\n".join(rows_out) + "\n")

#     # Get remaining text
#     text = soup.get_text(separator="\n", strip=True)

#     # Collapse multiple blank lines
#     lines = [ln.strip() for ln in text.split("\n")]
#     cleaned = []
#     blank_count = 0
#     for ln in lines:
#         if not ln:
#             blank_count += 1
#             if blank_count <= 1:
#                 cleaned.append("")
#         else:
#             blank_count = 0
#             cleaned.append(ln)

#     return "\n".join(cleaned).strip()


# def extract_html(path: Union[str, Path]) -> str:
#     """Extract text from an HTML file on disk."""
#     with open(path, "rb") as f:
#         return extract_html_bytes(f.read())


# # ═══════════════════════════════════════════════════════════
# #  ROUTER
# # ═══════════════════════════════════════════════════════════

# SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}


# def extract(path: Union[str, Path]) -> str:
#     """
#     Extract text from a file based on its extension.

#     Raises ValueError if the file type isn't supported.
#     """
#     path = Path(path)
#     ext = path.suffix.lower()

#     if ext == ".pdf":
#         return extract_pdf(path)
#     elif ext == ".docx":
#         return extract_docx(path)
#     elif ext in (".html", ".htm"):
#         return extract_html(path)
#     else:
#         raise ValueError(f"Unsupported file type: {ext} (file: {path.name})")


# def extract_bytes(content: bytes, file_type: str) -> str:
#     """
#     Extract text from raw bytes given a file type hint.

#     file_type can be: 'pdf', 'docx', 'html', 'htm', or a filename/extension.
#     """
#     ft = file_type.lower().lstrip(".")
#     # If a filename was passed, get its extension
#     if "." in ft:
#         ft = ft.rsplit(".", 1)[-1]

#     if ft == "pdf":
#         return extract_pdf_bytes(content)
#     elif ft == "docx":
#         return extract_docx_bytes(content)
#     elif ft in ("html", "htm"):
#         return extract_html_bytes(content)
#     else:
#         raise ValueError(f"Unsupported file type: {file_type}")


# # ═══════════════════════════════════════════════════════════
# #  CLI / quick test
# # ═══════════════════════════════════════════════════════════

# if __name__ == "__main__":
#     import sys
#     if len(sys.argv) < 2:
#         print("Usage: python -m pipeline.extractors <path-to-file>")
#         sys.exit(1)

#     path = sys.argv[1]
#     print(f"\nExtracting: {path}\n" + "=" * 60)
#     text = extract(path)
#     print(text[:2000])
#     print("\n" + "=" * 60)
#     print(f"Total chars: {len(text):,}  |  Words: {len(text.split()):,}")

"""
pipeline/extractors.py — Extract clean text from PDF, DOCX, and HTML files.

Each extractor returns plain text suitable for chunking + embedding.
Tables are preserved in a readable key:value format because tables
contain critical structured information (leave days, schemas, policies).

Public API:
    extract(file_path: str | Path) -> str
    extract_bytes(content: bytes, file_type: str) -> str

The extractors are defensive: malformed pages or files don't crash the pipeline,
they just produce warnings and skip the bad section.
"""

import io
import logging
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  PDF EXTRACTION
# ═══════════════════════════════════════════════════════════

def extract_pdf_bytes(content: bytes) -> str:
    """Extract text from PDF bytes. Returns plain text, page-separated."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                pages.append(text)
        except Exception as e:
            log.warning(f"PDF page {i}: extraction failed — {e}")
            continue

    return "\n\n".join(pages)


def extract_pdf_bytes_with_pages(content: bytes) -> list:
    """
    Extract text from PDF bytes, preserving page boundaries.
    Returns list of (text, page_num) tuples, one per page with text.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    out = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                out.append((text, i))
        except Exception as e:
            log.warning(f"PDF page {i}: extraction failed — {e}")
            continue
    return out


def extract_pdf(path: Union[str, Path]) -> str:
    """Extract text from a PDF file on disk."""
    with open(path, "rb") as f:
        return extract_pdf_bytes(f.read())


def extract_pdf_with_pages(path: Union[str, Path]) -> list:
    """Extract text from a PDF file with page boundaries preserved."""
    with open(path, "rb") as f:
        return extract_pdf_bytes_with_pages(f.read())


# ═══════════════════════════════════════════════════════════
#  DOCX EXTRACTION
# ═══════════════════════════════════════════════════════════

def _table_to_text(table) -> str:
    """
    Convert a DOCX table to retrieval-friendly text.

    Strategy:
      - Wide tables (>= 5 cols): each row becomes a multi-line
        "Header: value" block (better for embedding precision)
      - Narrow tables: keep as single "Header: value | Header: value" lines

    Wrapped in [TABLE]...[/TABLE] markers so chunker can recognize them.
    """
    rows = list(table.rows)
    if not rows:
        return ""

    # Extract cells. Handle merged cells by deduping consecutive identical cells.
    def cells_of(row):
        seen, prev, out = [], None, []
        for cell in row.cells:
            text = cell.text.strip()
            if text != prev:
                out.append(text)
            prev = text
        return out

    header = cells_of(rows[0])
    is_wide = len(header) >= 5

    output = ["[TABLE]"]
    if not is_wide:
        # Compact format for narrow tables
        output.append(" | ".join(header))
        for row in rows[1:]:
            cells = cells_of(row)
            if len(cells) == len(header):
                output.append(" | ".join(
                    f"{h}: {v}" for h, v in zip(header, cells) if v
                ))
            else:
                output.append(" | ".join(cells))
    else:
        # Expanded format for wide tables (each row is a block)
        for row_idx, row in enumerate(rows[1:], start=1):
            cells = cells_of(row)
            output.append(f"Row {row_idx}:")
            for h, v in zip(header, cells):
                if v:
                    output.append(f"  {h}: {v}")
            output.append("")  # blank line between rows

    output.append("[/TABLE]")
    return "\n".join(output)


def extract_docx_bytes(content: bytes) -> str:
    """Extract text from DOCX bytes, preserving headings, paragraphs, and tables."""
    from docx import Document
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    doc = Document(io.BytesIO(content))
    sections = []

    for child in doc.element.body:
        tag = child.tag.split("}")[-1]

        if tag == "p":
            try:
                para = Paragraph(child, doc)
            except Exception:
                continue
            text = para.text.strip()
            if not text:
                continue

            style_name = para.style.name if para.style else ""
            if "Heading" in style_name:
                level_str = style_name.replace("Heading ", "").strip()
                level = int(level_str) if level_str.isdigit() else 2
                prefix = "#" * level
                sections.append(f"\n{prefix} {text}\n")
            else:
                sections.append(text)

        elif tag == "tbl":
            try:
                table = Table(child, doc)
                table_text = _table_to_text(table)
                if table_text.strip():
                    sections.append("\n" + table_text + "\n")
            except Exception as e:
                log.warning(f"DOCX table extraction failed: {e}")
                continue

    return "\n".join(sections)


def extract_docx(path: Union[str, Path]) -> str:
    """Extract text from a DOCX file on disk."""
    with open(path, "rb") as f:
        return extract_docx_bytes(f.read())


# ═══════════════════════════════════════════════════════════
#  HTML EXTRACTION
# ═══════════════════════════════════════════════════════════

def extract_html_bytes(content: bytes) -> str:
    """
    Extract text from HTML bytes.

    Strips scripts, styles, and navigation. Preserves heading hierarchy
    by converting <h1>..<h6> to markdown # headers, and lists to bullets.
    """
    from bs4 import BeautifulSoup

    # Try lxml first (faster); fall back to built-in if lxml unhappy
    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    # Drop noise tags
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    # Convert headings to markdown
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            prefix = "#" * level
            h.replace_with(f"\n{prefix} {h.get_text(strip=True)}\n")

    # Convert list items to bullets
    for li in soup.find_all("li"):
        li.replace_with(f"- {li.get_text(' ', strip=True)}\n")

    # Convert tables to simple pipe-delimited rows
    for table in soup.find_all("table"):
        rows_out = ["[TABLE]"]
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if any(cells):
                rows_out.append(" | ".join(cells))
        rows_out.append("[/TABLE]")
        table.replace_with("\n" + "\n".join(rows_out) + "\n")

    # Get remaining text
    text = soup.get_text(separator="\n", strip=True)

    # Collapse multiple blank lines
    lines = [ln.strip() for ln in text.split("\n")]
    cleaned = []
    blank_count = 0
    for ln in lines:
        if not ln:
            blank_count += 1
            if blank_count <= 1:
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(ln)

    return "\n".join(cleaned).strip()


def extract_html(path: Union[str, Path]) -> str:
    """Extract text from an HTML file on disk."""
    with open(path, "rb") as f:
        return extract_html_bytes(f.read())


# ═══════════════════════════════════════════════════════════
#  ROUTER
# ═══════════════════════════════════════════════════════════

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}


def extract(path: Union[str, Path]) -> str:
    """
    Extract text from a file based on its extension.

    Raises ValueError if the file type isn't supported.
    """
    path = Path(path)
    ext = path.suffix.lower()

    if ext == ".pdf":
        return extract_pdf(path)
    elif ext == ".docx":
        return extract_docx(path)
    elif ext in (".html", ".htm"):
        return extract_html(path)
    else:
        raise ValueError(f"Unsupported file type: {ext} (file: {path.name})")


def extract_bytes(content: bytes, file_type: str) -> str:
    """
    Extract text from raw bytes given a file type hint.

    file_type can be: 'pdf', 'docx', 'html', 'htm', or a filename/extension.
    """
    ft = file_type.lower().lstrip(".")
    # If a filename was passed, get its extension
    if "." in ft:
        ft = ft.rsplit(".", 1)[-1]

    if ft == "pdf":
        return extract_pdf_bytes(content)
    elif ft == "docx":
        return extract_docx_bytes(content)
    elif ft in ("html", "htm"):
        return extract_html_bytes(content)
    else:
        raise ValueError(f"Unsupported file type: {file_type}")


def extract_with_pages(content: bytes, file_type: str):
    """
    Extract text WITH page tracking when applicable (PDF only).

    Returns:
        For PDF: list of (text, page_num) tuples
        For DOCX/HTML: list with single (text, None) tuple — no page concept

    Use this when downstream code needs to associate chunks with pages
    (for citation in the chatbot response).
    """
    ft = file_type.lower().lstrip(".")
    if "." in ft:
        ft = ft.rsplit(".", 1)[-1]

    if ft == "pdf":
        return extract_pdf_bytes_with_pages(content)
    elif ft == "docx":
        text = extract_docx_bytes(content)
        return [(text, None)] if text else []
    elif ft in ("html", "htm"):
        text = extract_html_bytes(content)
        return [(text, None)] if text else []
    else:
        raise ValueError(f"Unsupported file type: {file_type}")


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.extractors <path-to-file>")
        sys.exit(1)

    path = sys.argv[1]
    print(f"\nExtracting: {path}\n" + "=" * 60)
    text = extract(path)
    print(text[:2000])
    print("\n" + "=" * 60)
    print(f"Total chars: {len(text):,}  |  Words: {len(text.split()):,}")