"""
debug_missing.py — Try indexing each missing file individually with verbose error output.
Run: python debug_missing.py
"""
import sys
import traceback
sys.path.insert(0, '.')

from sources import get_source
from pipeline.extractors import extract_with_pages
from pipeline.chunker import chunk_document
from pipeline.embedder import embed_many
from storage import search_index

MISSING_FILES = [
    "Facilities_Event_Management.docx",
    "Facilities_Sustainability_Green.docx",
    "IT_Incident_Response.docx",
    "testing-1779020100389.html",
    "testing-1779176358180.html",
    "testing-1779176393511.html",
    "testing-for-two-scenario-1779174028595.html",
    "vpn-fix-guide-1778999363169.html",
]

print("=" * 70)
print("  DEBUGGING MISSING FILES")
print("=" * 70)

source = get_source()
all_docs = source.list_documents(only_published=False)

for filename in MISSING_FILES:
    print(f"\n{'=' * 70}")
    print(f"  📄 {filename}")
    print(f"{'=' * 70}")

    # Find the doc in SharePoint
    ref = next((d for d in all_docs if d.filename == filename), None)
    if not ref:
        print(f"  ❌ NOT FOUND in SharePoint listing")
        continue

    print(f"  Found in SharePoint:")
    print(f"    file_id:    {ref.file_id[:40]}...")
    print(f"    doc_type:   {ref.doc_type}")
    print(f"    status:     {ref.status!r}")
    print(f"    category:   {ref.category!r}")
    print(f"    sub_cat:    {ref.sub_category!r}")
    print(f"    size:       {ref.size_bytes:,} bytes")
    print(f"    modified:   {ref.modified_at}")

    # Try downloading
    print(f"\n  [1] Downloading from SharePoint...")
    try:
        data = source.download(ref)
        if data.pre_extracted_text:
            print(f"      ✅ Got pre-extracted text: {len(data.pre_extracted_text):,} chars")
            text = data.pre_extracted_text
            pages_data = None
        elif data.content_bytes:
            print(f"      ✅ Downloaded: {len(data.content_bytes):,} bytes")
            # Try extracting
            print(f"  [2] Extracting text...")
            try:
                pages_data = extract_with_pages(data.content_bytes, ref.doc_type)
                text = "\n\n".join(p[0] for p in pages_data) if pages_data else ""
                print(f"      ✅ Extracted {len(text):,} chars from {len(pages_data)} page/entry(ies)")
                if len(text) < 100:
                    print(f"      ⚠ EXTRACTED TEXT IS VERY SHORT — likely the problem")
                    print(f"      Content: {text!r}")
            except Exception as e:
                print(f"      ❌ EXTRACTION FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()
                continue
        else:
            print(f"      ❌ Empty download — no content!")
            continue
    except Exception as e:
        print(f"      ❌ DOWNLOAD FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        continue

    if not text or len(text.strip()) < 20:
        print(f"\n      ⚠ TEXT IS TOO SHORT TO CHUNK — this is why it was skipped")
        print(f"      Text content: {text[:200]!r}")
        continue

    # Try chunking
    print(f"  [3] Chunking...")
    try:
        doc_for_chunking = {
            **ref.to_doc_metadata(),
            "text": text,
            "pages": pages_data,
        }
        chunks = chunk_document(doc_for_chunking, chunk_size=400, overlap=60)
        if chunks:
            print(f"      ✅ Produced {len(chunks)} chunk(s)")
            print(f"      First chunk preview: {chunks[0]['text'][:150]}...")
        else:
            print(f"      ❌ NO CHUNKS produced — text may be all filler/whitespace")
    except Exception as e:
        print(f"      ❌ CHUNKING FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()

print(f"\n{'=' * 70}")
print("  SUMMARY")
print("=" * 70)
print("\n  For files showing 'TEXT IS TOO SHORT':")
print("    → The file has very little extractable text (maybe just images,")
print("      or empty body, or 'min_chunk_words=20' filter is rejecting it)")
print("\n  For files showing 'EXTRACTION FAILED':")
print("    → File format is corrupt or our extractor has a bug")
print("\n  For files showing 'NO CHUNKS produced':")
print("    → All extracted text was filler (headers/footers/whitespace)")
