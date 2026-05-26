# """
# debug_missing.py — Detect orphaned chunks that should have been deleted.

# This script finds STALE chunks — chunks in Azure AI Search that came from 
# files that no longer exist in SharePoint (or whose ArticleStatus is no 
# longer Published).

# If your bot is answering with content from old/deleted files, this script 
# will show you exactly which files are still in the index.

# Run from project root:
#     python debug_missing.py
# """
# import sys
# sys.path.insert(0, '.')

# from sources import get_source
# from storage import search_index
# from storage.search_index import _get_search_client
# from collections import Counter

# print("=" * 75)
# print("  ORPHANED CHUNK DETECTOR")
# print("  Finds chunks in Azure AI Search that should have been deleted")
# print("=" * 75)

# # ─── 1. Get everything currently in SharePoint ───
# print("\n[1] Reading current SharePoint state...")
# source = get_source()
# sp_docs = source.list_documents(only_published=False)

# # Build sets for fast comparison
# sp_filenames_all = {d.filename for d in sp_docs}
# sp_filenames_published = {
#     d.filename for d in sp_docs
#     if d.status in ("Published", "Approved")
# }
# sp_file_ids_all = {
#     d.file_id for d in sp_docs
#     if d.file_id
# }
# sp_file_ids_published = {
#     d.file_id for d in sp_docs
#     if d.status in ("Published", "Approved") and d.file_id
# }

# print(f"  SharePoint files (total):     {len(sp_filenames_all)}")
# print(f"  SharePoint files (published): {len(sp_filenames_published)}")

# # ─── 2. Get everything currently in the search index ───
# print("\n[2] Reading Azure AI Search index...")
# client = _get_search_client()

# chunks_per_file = Counter()
# chunks_per_file_id = Counter()
# file_to_status = {}
# file_to_category = {}
# file_id_to_filename = {}

# results = client.search(
#     search_text="*",
#     select=[
#         "filename", "category", "sub_category",
#         "article_status", "sharepoint_file_id"
#     ],
#     top=5000,
# )

# for r in results:
#     fn = r.get("filename") or "(no filename)"
#     fid = r.get("sharepoint_file_id") or "(no file_id)"
#     status = r.get("article_status") or "(no status)"
#     cat = r.get("category") or "(no category)"

#     chunks_per_file[fn] += 1
#     chunks_per_file_id[fid] += 1
#     file_to_status[fn] = status
#     file_to_category[fn] = cat
#     file_id_to_filename[fid] = fn

# print(f"  Indexed chunks (total):       {sum(chunks_per_file.values())}")
# print(f"  Unique files in index:        {len(chunks_per_file)}")

# # ─── 3. Find ORPHANED chunks ───
# print("\n" + "=" * 75)
# print("  ANALYSIS")
# print("=" * 75)

# # Files in index that no longer exist in SharePoint at all
# orphans_by_filename = set(chunks_per_file.keys()) - sp_filenames_all
# # Files in index that exist but are NOT published (status changed)
# unpublished_but_indexed = {
#     fn for fn in chunks_per_file.keys()
#     if fn in sp_filenames_all and fn not in sp_filenames_published
# }
# # Files in index whose file_id no longer exists in SharePoint
# orphans_by_file_id = set(chunks_per_file_id.keys()) - sp_file_ids_all - {"(no file_id)"}

# print(f"\n  ❌ STALE FILES (in index, NOT in SharePoint at all): {len(orphans_by_filename)}")
# if orphans_by_filename:
#     print("     These chunks should be deleted but weren't:")
#     for fn in sorted(orphans_by_filename):
#         cat = file_to_category.get(fn, "?")
#         print(f"       • [{cat}] {fn}  →  {chunks_per_file[fn]} chunks")

# print(f"\n  ⚠ UNPUBLISHED FILES (in index, but ArticleStatus changed): {len(unpublished_but_indexed)}")
# if unpublished_but_indexed:
#     print("     These chunks shouldn't be served (filter handles it at query time):")
#     for fn in sorted(unpublished_but_indexed):
#         status = file_to_status.get(fn, "?")
#         print(f"       • [{status}] {fn}  →  {chunks_per_file[fn]} chunks")

# print(f"\n  🔍 ORPHANED FILE_IDS (file_id in index, NOT in SharePoint): {len(orphans_by_file_id)}")
# if orphans_by_file_id:
#     print("     These are likely from files that were re-uploaded with NEW file_ids:")
#     for fid in sorted(orphans_by_file_id):
#         fn = file_id_to_filename.get(fid, "?")
#         print(f"       • {fid[:25]}...  →  {fn}  ({chunks_per_file_id[fid]} chunks)")

# # ─── 4. Summary ───
# print("\n" + "=" * 75)
# print("  SUMMARY")
# print("=" * 75)

# total_orphan_chunks = sum(chunks_per_file[fn] for fn in orphans_by_filename)
# total_unpublished_chunks = sum(chunks_per_file[fn] for fn in unpublished_but_indexed)

# if orphans_by_filename or unpublished_but_indexed or orphans_by_file_id:
#     print(f"\n  ⚠ Problems found:")
#     if orphans_by_filename:
#         print(f"     - {len(orphans_by_filename)} stale files = {total_orphan_chunks} stale chunks")
#     if unpublished_but_indexed:
#         print(f"     - {len(unpublished_but_indexed)} unpublished files = {total_unpublished_chunks} chunks (filtered at query time)")
#     if orphans_by_file_id:
#         print(f"     - {len(orphans_by_file_id)} orphan file_ids in index")
#     print()
#     print("  📋 To fix, you need to:")
#     print("     1. Trigger a full re-sync (clears delta token and rebuilds)")
#     print("     2. OR run `delete_orphans()` to remove just the stale chunks")
#     print()
#     print("  Run this to delete orphans:")
#     print()
#     print("     python -c \"")
#     print("     import sys; sys.path.insert(0, '.')")
#     print("     from storage import search_index")
#     print("     from sources import get_source")
#     print("     sp = {d.filename for d in get_source().list_documents(only_published=False)}")
#     print("     from storage.search_index import _get_search_client")
#     print("     client = _get_search_client()")
#     print("     results = client.search(search_text='*', select=['filename','chunk_id'], top=5000)")
#     print("     to_delete = [r['chunk_id'] for r in results if r.get('filename') not in sp]")
#     print("     print(f'Will delete {len(to_delete)} stale chunks')")
#     print("     client.delete_documents([{'chunk_id': cid} for cid in to_delete])")
#     print("     print('Done')")
#     print("     \"")
# else:
#     print("\n  ✅ No orphans found — index matches SharePoint state")


"""
debug_missing.py — Detect orphaned chunks that should have been deleted.

This script finds STALE chunks — chunks in Azure AI Search that came from 
files that no longer exist in SharePoint (or whose ArticleStatus is no 
longer Published).

If your bot is answering with content from old/deleted files, this script 
will show you exactly which files are still in the index.

Run from project root:
    python debug_missing.py
"""
import sys
sys.path.insert(0, '.')

from sources import get_source
from storage import search_index
from storage.search_index import _get_search_client
from collections import Counter

print("=" * 75)
print("  ORPHANED CHUNK DETECTOR")
print("  Finds chunks in Azure AI Search that should have been deleted")
print("=" * 75)

# ─── 1. Get everything currently in SharePoint ───
print("\n[1] Reading current SharePoint state...")
source = get_source()
sp_docs = source.list_documents(only_published=False)

# Build sets for fast comparison
sp_filenames_all = {d.filename for d in sp_docs}
sp_filenames_published = {
    d.filename for d in sp_docs
    if d.status in ("Published", "Approved")
}
sp_file_ids_all = {
    d.file_id for d in sp_docs
    if d.file_id
}
sp_file_ids_published = {
    d.file_id for d in sp_docs
    if d.status in ("Published", "Approved") and d.file_id
}

print(f"  SharePoint files (total):     {len(sp_filenames_all)}")
print(f"  SharePoint files (published): {len(sp_filenames_published)}")

# ─── 2. Get everything currently in the search index ───
print("\n[2] Reading Azure AI Search index...")
client = _get_search_client()

chunks_per_file = Counter()
chunks_per_file_id = Counter()
file_to_status = {}
file_to_category = {}
file_id_to_filename = {}

results = client.search(
    search_text="*",
    select=[
        "filename", "category", "sub_category",
        "status", "sharepoint_file_id"
    ],
    top=5000,
)

for r in results:
    fn = r.get("filename") or "(no filename)"
    fid = r.get("sharepoint_file_id") or "(no file_id)"
    status = r.get("status") or "(no status)"
    cat = r.get("category") or "(no category)"

    chunks_per_file[fn] += 1
    chunks_per_file_id[fid] += 1
    file_to_status[fn] = status
    file_to_category[fn] = cat
    file_id_to_filename[fid] = fn

print(f"  Indexed chunks (total):       {sum(chunks_per_file.values())}")
print(f"  Unique files in index:        {len(chunks_per_file)}")

# ─── 3. Find ORPHANED chunks ───
print("\n" + "=" * 75)
print("  ANALYSIS")
print("=" * 75)

# Files in index that no longer exist in SharePoint at all
orphans_by_filename = set(chunks_per_file.keys()) - sp_filenames_all
# Files in index that exist but are NOT published (status changed)
unpublished_but_indexed = {
    fn for fn in chunks_per_file.keys()
    if fn in sp_filenames_all and fn not in sp_filenames_published
}
# Files in index whose file_id no longer exists in SharePoint
orphans_by_file_id = set(chunks_per_file_id.keys()) - sp_file_ids_all - {"(no file_id)"}

print(f"\n  ❌ STALE FILES (in index, NOT in SharePoint at all): {len(orphans_by_filename)}")
if orphans_by_filename:
    print("     These chunks should be deleted but weren't:")
    for fn in sorted(orphans_by_filename):
        cat = file_to_category.get(fn, "?")
        print(f"       • [{cat}] {fn}  →  {chunks_per_file[fn]} chunks")

print(f"\n  ⚠ UNPUBLISHED FILES (in index, but ArticleStatus changed): {len(unpublished_but_indexed)}")
if unpublished_but_indexed:
    print("     These chunks shouldn't be served (filter handles it at query time):")
    for fn in sorted(unpublished_but_indexed):
        status = file_to_status.get(fn, "?")
        print(f"       • [{status}] {fn}  →  {chunks_per_file[fn]} chunks")

print(f"\n  🔍 ORPHANED FILE_IDS (file_id in index, NOT in SharePoint): {len(orphans_by_file_id)}")
if orphans_by_file_id:
    print("     These are likely from files that were re-uploaded with NEW file_ids:")
    for fid in sorted(orphans_by_file_id):
        fn = file_id_to_filename.get(fid, "?")
        print(f"       • {fid[:25]}...  →  {fn}  ({chunks_per_file_id[fid]} chunks)")

# ─── 4. Summary ───
print("\n" + "=" * 75)
print("  SUMMARY")
print("=" * 75)

total_orphan_chunks = sum(chunks_per_file[fn] for fn in orphans_by_filename)
total_unpublished_chunks = sum(chunks_per_file[fn] for fn in unpublished_but_indexed)

if orphans_by_filename or unpublished_but_indexed or orphans_by_file_id:
    print(f"\n  ⚠ Problems found:")
    if orphans_by_filename:
        print(f"     - {len(orphans_by_filename)} stale files = {total_orphan_chunks} stale chunks")
    if unpublished_but_indexed:
        print(f"     - {len(unpublished_but_indexed)} unpublished files = {total_unpublished_chunks} chunks (filtered at query time)")
    if orphans_by_file_id:
        print(f"     - {len(orphans_by_file_id)} orphan file_ids in index")
    print()
    print("  📋 To fix, you need to:")
    print("     1. Trigger a full re-sync (clears delta token and rebuilds)")
    print("     2. OR run `delete_orphans()` to remove just the stale chunks")
    print()
    print("  Run this to delete orphans:")
    print()
    print("     python -c \"")
    print("     import sys; sys.path.insert(0, '.')")
    print("     from storage import search_index")
    print("     from sources import get_source")
    print("     sp = {d.filename for d in get_source().list_documents(only_published=False)}")
    print("     from storage.search_index import _get_search_client")
    print("     client = _get_search_client()")
    print("     results = client.search(search_text='*', select=['filename','chunk_id'], top=5000)")
    print("     to_delete = [r['chunk_id'] for r in results if r.get('filename') not in sp]")
    print("     print(f'Will delete {len(to_delete)} stale chunks')")
    print("     client.delete_documents([{'chunk_id': cid} for cid in to_delete])")
    print("     print('Done')")
    print("     \"")
else:
    print("\n  ✅ No orphans found — index matches SharePoint state")