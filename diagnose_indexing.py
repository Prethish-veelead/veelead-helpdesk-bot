"""
diagnose_indexing.py — Compare what's in SharePoint vs what's in the index.
Run from project root: python diagnose_indexing.py
"""
import sys
sys.path.insert(0, '.')

from sources import get_source
from storage import search_index
from storage.search_index import _get_search_client
from collections import Counter

print("=" * 70)
print("  COMPARING SHAREPOINT vs INDEX")
print("=" * 70)

# 1. What's in SharePoint?
print("\n[1] Fetching from SharePoint...")
source = get_source()
sp_docs = source.list_documents(only_published=False)  # everything, including drafts
sp_published = [d for d in sp_docs if d.status in ("Published", "Approved")]

print(f"\n  Total in SharePoint: {len(sp_docs)}")
print(f"  Published/Approved:  {len(sp_published)}")
print(f"  Drafts/Other:        {len(sp_docs) - len(sp_published)}")

print(f"\n  All documents:")
for d in sp_docs:
    status_icon = "✅" if d.status in ("Published", "Approved") else "❌"
    print(f"    {status_icon} [{d.status or 'NO STATUS':12}] {d.filename}")
    print(f"        category={d.category!r}  sub_category={d.sub_category!r}")

# 2. What's in the search index?
print("\n" + "=" * 70)
print("  WHAT'S IN AZURE AI SEARCH INDEX")
print("=" * 70)

stats = search_index.get_index_stats()
print(f"\n  Total chunks in index: {stats['document_count']}")

# Get unique files in index
client = _get_search_client()
files_in_index = set()
chunks_per_file = Counter()
categories_per_file = {}

results = client.search(
    search_text="*",
    select=["filename", "category", "sub_category"],
    top=1000
)
for r in results:
    fn = r.get("filename") or ""
    files_in_index.add(fn)
    chunks_per_file[fn] += 1
    categories_per_file[fn] = (r.get("category"), r.get("sub_category"))

print(f"  Unique files in index: {len(files_in_index)}")
print()
for fn in sorted(files_in_index):
    cat, subcat = categories_per_file[fn]
    print(f"    {chunks_per_file[fn]:3} chunks  [{cat!r}/{subcat!r}]  {fn}")

# 3. Compare — find missing files
print("\n" + "=" * 70)
print("  DIFFERENCE: in SharePoint but NOT indexed")
print("=" * 70)

sp_published_names = {d.filename for d in sp_published}
missing = sp_published_names - files_in_index

if missing:
    print(f"\n  ⚠ These published docs in SharePoint are NOT in the index:")
    for fn in sorted(missing):
        # Find the doc to show its status
        d = next((x for x in sp_published if x.filename == fn), None)
        if d:
            print(f"    • {fn}")
            print(f"        Status: {d.status}, Category: {d.category}")
else:
    print("\n  ✅ All published SharePoint docs are indexed")

# 4. Category counts as seen by /categories endpoint
print("\n" + "=" * 70)
print("  WHAT /categories ENDPOINT WILL RETURN")
print("=" * 70)
cats = search_index.list_categories(only_published=True)
print()
for c in cats:
    print(f"    {c['display']:20} → {c['chunk_count']} chunks")

# Show categories that EXIST in SharePoint but have no chunks
sp_cats = set()
for d in sp_published:
    if d.category:
        sp_cats.add(d.category)

index_cats = {c["name"] for c in cats}
empty_in_index = sp_cats - index_cats

if empty_in_index:
    print(f"\n  ⚠ These categories exist in SharePoint but have NO indexed chunks:")
    for c in empty_in_index:
        print(f"    • {c}  (frontend won't show these)")
