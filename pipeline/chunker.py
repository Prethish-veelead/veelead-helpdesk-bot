"""
pipeline/chunker.py — Split extracted text into RAG-ready chunks.

A "chunk" is a piece of text small enough to embed efficiently but big
enough to contain coherent meaning. Typical size: ~400 words with ~60 word
overlap between consecutive chunks.

Design priorities (in order):
  1. NEVER split a [TABLE]...[/TABLE] block in half — tables are atomic
  2. Prefer breaking on heading boundaries (preserves topic continuity)
  3. Prefer breaking on paragraph boundaries (don't slice mid-sentence)
  4. Maintain word-count overlap between chunks (context continuity)
  5. Filter out fragments too small to be useful (<20 words)

Public API:
    chunk_text(text: str, chunk_size: int = 400, overlap: int = 60) -> list[str]
    chunk_document(doc: dict, ...) -> list[dict]
"""

import re
from typing import List, Dict, Any


# Patterns to recognize structural elements
TABLE_OPEN = "[TABLE]"
TABLE_CLOSE = "[/TABLE]"
HEADING_RE = re.compile(r"^#{1,6}\s+\S")  # # Heading, ## Heading, etc.


def _split_into_units(text: str) -> List[Dict[str, Any]]:
    """
    Split text into atomic units, each tagged with its type:
      - 'table'   : full [TABLE]...[/TABLE] block (cannot be split further)
      - 'heading' : a single heading line (e.g. "# Section")
      - 'para'    : a paragraph (one or more lines, no headings or tables)

    Each unit has: {type, text, word_count}
    """
    units: List[Dict[str, Any]] = []

    # Split into lines for easier scanning
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Detect table block ──
        if stripped == TABLE_OPEN:
            # Collect until [/TABLE]
            table_lines = [line]
            i += 1
            while i < len(lines):
                table_lines.append(lines[i])
                if lines[i].strip() == TABLE_CLOSE:
                    i += 1
                    break
                i += 1
            table_text = "\n".join(table_lines)
            units.append({
                "type": "table",
                "text": table_text,
                "word_count": len(table_text.split()),
            })
            continue

        # ── Detect heading ──
        if HEADING_RE.match(stripped):
            units.append({
                "type": "heading",
                "text": stripped,
                "word_count": len(stripped.split()),
            })
            i += 1
            continue

        # ── Skip blank lines ──
        if not stripped:
            i += 1
            continue

        # ── Collect a paragraph (consecutive non-blank, non-special lines) ──
        para_lines = []
        while i < len(lines):
            cur = lines[i]
            cur_strip = cur.strip()
            if not cur_strip:
                break
            if cur_strip == TABLE_OPEN or HEADING_RE.match(cur_strip):
                break
            para_lines.append(cur_strip)
            i += 1

        if para_lines:
            para_text = " ".join(para_lines)  # flatten paragraph into single line
            units.append({
                "type": "para",
                "text": para_text,
                "word_count": len(para_text.split()),
            })

    return units


def _flush_chunk(buffer_units: List[Dict[str, Any]]) -> str:
    """Convert a buffer of units back into a single text chunk."""
    parts: List[str] = []
    for unit in buffer_units:
        parts.append(unit["text"])
    return "\n\n".join(parts).strip()


def _last_n_words_for_overlap(buffer_units: List[Dict[str, Any]],
                              overlap_words: int) -> List[Dict[str, Any]]:
    """
    Build a small "carry-over" list of units totaling ~overlap_words.
    Tables are NEVER included in overlap (they're already atomic chunks
    of their own and we don't want them duplicated across many chunks).
    Headings ARE included so the next chunk knows its context.
    """
    if overlap_words <= 0:
        return []

    carry: List[Dict[str, Any]] = []
    total = 0
    # Walk from the END of the buffer backwards
    for unit in reversed(buffer_units):
        if unit["type"] == "table":
            # Skip tables in overlap — they're large and self-contained
            continue
        carry.insert(0, unit)
        total += unit["word_count"]
        if total >= overlap_words:
            break

    return carry


def chunk_text(text: str, chunk_size: int = 400, overlap: int = 60,
               min_chunk_words: int = 20) -> List[str]:
    """
    Split text into chunks of approximately `chunk_size` words.

    Args:
        text: The extracted document text.
        chunk_size: Target words per chunk (soft limit).
        overlap: Word overlap between consecutive chunks.
        min_chunk_words: Drop chunks smaller than this.

    Returns:
        List of chunk strings, ready to embed.

    Rules:
        - [TABLE]...[/TABLE] blocks are never split.
        - A table larger than chunk_size becomes its own chunk (oversized OK).
        - Headings stay attached to the content that follows them.
    """
    if not text or not text.strip():
        return []

    units = _split_into_units(text)
    if not units:
        return []

    chunks: List[str] = []
    buffer: List[Dict[str, Any]] = []
    buffer_word_count = 0
    pending_heading: List[Dict[str, Any]] = []  # heading waiting to attach to next content

    for unit in units:
        # Headings buffer separately — they always attach to following content
        if unit["type"] == "heading":
            # If buffer is non-empty AND heading would push us over → flush first
            if buffer and buffer_word_count >= chunk_size * 0.7:
                chunk = _flush_chunk(buffer)
                if len(chunk.split()) >= min_chunk_words:
                    chunks.append(chunk)
                # Start new buffer with overlap from previous + the pending heading
                carry = _last_n_words_for_overlap(buffer, overlap)
                buffer = carry[:]
                buffer_word_count = sum(u["word_count"] for u in buffer)
            pending_heading.append(unit)
            continue

        # If a heading is pending, add it to the buffer along with this content
        if pending_heading:
            for h in pending_heading:
                buffer.append(h)
                buffer_word_count += h["word_count"]
            pending_heading.clear()

        # ── Handle tables: never split, but may need to flush first ──
        if unit["type"] == "table":
            # If adding this table would significantly exceed chunk_size AND
            # buffer already has reasonable content → flush buffer first
            if buffer and buffer_word_count + unit["word_count"] > chunk_size * 1.5:
                chunk = _flush_chunk(buffer)
                if len(chunk.split()) >= min_chunk_words:
                    chunks.append(chunk)
                carry = _last_n_words_for_overlap(buffer, overlap)
                buffer = carry[:]
                buffer_word_count = sum(u["word_count"] for u in buffer)

            # Append the table atomically
            buffer.append(unit)
            buffer_word_count += unit["word_count"]

            # If buffer is now well over chunk_size, flush
            if buffer_word_count >= chunk_size:
                chunk = _flush_chunk(buffer)
                if len(chunk.split()) >= min_chunk_words:
                    chunks.append(chunk)
                carry = _last_n_words_for_overlap(buffer, overlap)
                buffer = carry[:]
                buffer_word_count = sum(u["word_count"] for u in buffer)
            continue

        # ── Handle paragraphs ──
        if unit["type"] == "para":
            # If paragraph itself is huge (>chunk_size), split it on word boundaries
            if unit["word_count"] > chunk_size:
                # First flush whatever is in buffer
                if buffer:
                    chunk = _flush_chunk(buffer)
                    if len(chunk.split()) >= min_chunk_words:
                        chunks.append(chunk)
                    buffer = []
                    buffer_word_count = 0
                # Hard-split the giant paragraph
                words = unit["text"].split()
                step = chunk_size - overlap
                for i in range(0, len(words), step):
                    piece = " ".join(words[i:i + chunk_size])
                    if len(piece.split()) >= min_chunk_words:
                        chunks.append(piece)
                continue

            # Normal paragraph — append, then check if we should flush
            buffer.append(unit)
            buffer_word_count += unit["word_count"]

            if buffer_word_count >= chunk_size:
                chunk = _flush_chunk(buffer)
                if len(chunk.split()) >= min_chunk_words:
                    chunks.append(chunk)
                carry = _last_n_words_for_overlap(buffer, overlap)
                buffer = carry[:]
                buffer_word_count = sum(u["word_count"] for u in buffer)

    # Flush remaining buffer
    if buffer:
        # Don't flush if it's only an unattached heading
        non_heading_count = sum(u["word_count"] for u in buffer if u["type"] != "heading")
        if non_heading_count >= min_chunk_words:
            chunk = _flush_chunk(buffer)
            if len(chunk.split()) >= min_chunk_words:
                chunks.append(chunk)

    return chunks


def chunk_document(doc: Dict[str, Any], chunk_size: int = 400,
                   overlap: int = 60) -> List[Dict[str, Any]]:
    """
    Chunk a document and attach metadata to each chunk.

    Args:
        doc: dict with at least 'filename' and 'text' keys.
             May also have: 'sharepoint_file_id', 'article_title', 'category',
             'sub_category', 'tags', 'summary', 'status', 'doc_type', etc.
        chunk_size: target words per chunk
        overlap: overlap words between chunks

    Returns:
        List of dicts, each one a chunk with full doc metadata + chunk-specific fields.
    """
    chunks = chunk_text(doc["text"], chunk_size=chunk_size, overlap=overlap)

    # Stable chunk ID prefix: prefer SharePoint file ID if available, else filename
    id_base = doc.get("sharepoint_file_id") or doc["filename"]
    # Sanitize: Azure AI Search keys allow letters, digits, _, -, =
    id_base = re.sub(r"[^A-Za-z0-9_\-=]", "_", id_base)

    output = []
    for i, chunk in enumerate(chunks):
        # Build chunk dict — start with all doc metadata, then add chunk-specific
        chunk_doc = {
            "chunk_id": f"{id_base}__c{i:04d}",
            "filename": doc.get("filename"),
            "doc_type": doc.get("doc_type"),
            "sharepoint_file_id": doc.get("sharepoint_file_id"),
            "article_title": doc.get("article_title"),
            "category": doc.get("category") or "Uncategorized",
            "sub_category": doc.get("sub_category"),
            "tags": doc.get("tags") or [],
            "summary": doc.get("summary"),
            "status": doc.get("status"),
            "author": doc.get("author"),
            "publish_date": doc.get("publish_date"),
            "view_count": doc.get("view_count", 0),
            "helpful_count": doc.get("helpful_count", 0),
            "not_helpful_count": doc.get("not_helpful_count", 0),
            "ai_citation_count": doc.get("ai_citation_count", 0),
            "source_ticket_id": doc.get("source_ticket_id"),
            "text": chunk,
            "chunk_index": i,
            "total_chunks": len(chunks),
        }
        output.append(chunk_doc)

    return output


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    from pipeline.extractors import extract

    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.chunker <path-to-file>")
        sys.exit(1)

    path = sys.argv[1]
    print(f"Extracting: {path}")
    text = extract(path)
    print(f"  → {len(text):,} chars, {len(text.split()):,} words")

    chunks = chunk_text(text, chunk_size=400, overlap=60)
    print(f"\nChunked into {len(chunks)} pieces:")
    word_counts = [len(c.split()) for c in chunks]
    print(f"  Min words:  {min(word_counts)}")
    print(f"  Max words:  {max(word_counts)}")
    print(f"  Avg words:  {sum(word_counts) / len(word_counts):.0f}")
    print(f"  Total words: {sum(word_counts):,}")

    # Show first 2 chunks as samples
    for i, c in enumerate(chunks[:2], start=1):
        print(f"\n── Chunk {i} ({len(c.split())} words) ──")
        print(c[:500])
        if len(c) > 500:
            print(f"... ({len(c) - 500} more chars)")
