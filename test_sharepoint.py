# """
# test_sharepoint.py — Standalone SharePoint connection test.
# Run from project root: python test_sharepoint.py
# """
# import sys
# from sources import get_source
# from config import settings, print_config_summary

# print_config_summary()

# print("\n" + "=" * 70)
# print("  TESTING SHAREPOINT CONNECTION")
# print("=" * 70)

# if not settings.is_sharepoint_mode:
#     print("❌ SOURCE_TYPE is not 'sharepoint' in .env — set it first")
#     sys.exit(1)

# try:
#     # 1. Initialize source (this triggers MSAL auth)
#     print("\n[1] Authenticating with Microsoft Graph...")
#     source = get_source()
#     print(f"    ✅ Source: {source.source_name()}")

#     # 2. List documents (tests Graph API + SharePoint permissions)
#     print("\n[2] Listing documents from SharePoint...")
#     docs = source.list_documents(only_published=True)
#     print(f"    ✅ Found {len(docs)} published document(s)")

#     if not docs:
#         print("\n    ⚠ No documents found. Possible reasons:")
#         print("       - All docs have Status != Published/Approved")
#         print("       - The library name is wrong (check SHAREPOINT_LIBRARY)")
#         print("       - The site URL is wrong (check SHAREPOINT_SITE_URL)")
#         sys.exit(1)

#     # 3. Show metadata of first 5 docs
#     print(f"\n[3] Sample of first 5 documents:")
#     for i, doc in enumerate(docs[:5], 1):
#         print(f"\n    {i}. {doc.filename}")
#         print(f"       file_id:        {doc.file_id[:30]}...")
#         print(f"       doc_type:       {doc.doc_type}")
#         print(f"       category:       {doc.category}")
#         print(f"       sub_category:   {doc.sub_category}")
#         print(f"       status:         {doc.status}")
#         print(f"       article_title:  {doc.article_title}")
#         print(f"       tags:           {doc.tags}")
#         print(f"       modified_at:    {doc.modified_at}")
#         print(f"       size_bytes:     {doc.size_bytes:,}")
#         print(f"       pdf_url:        {doc.pdf_url[:60]}..." if doc.pdf_url else "       pdf_url:        (none)")

#     # 4. Download a sample file to verify download works
#     print(f"\n[4] Downloading first file to verify content access...")
#     sample = docs[0]
#     data = source.download(sample)
#     if data.content_bytes:
#         print(f"    ✅ Downloaded {sample.filename}: {len(data.content_bytes):,} bytes")
#     elif data.pre_extracted_text:
#         print(f"    ✅ Got pre-extracted text from ArticleContent column: "
#               f"{len(data.pre_extracted_text):,} chars")
#     else:
#         print(f"    ❌ Download returned empty")
#         sys.exit(1)

#     # 5. Test delta sync (first call = full sync)
#     print(f"\n[5] Testing delta sync (first call returns ALL as updated)...")
#     changes = source.get_changes(delta_token=None)
#     print(f"    ✅ Added:   {len(changes.added)}")
#     print(f"    ✅ Updated: {len(changes.updated)}")
#     print(f"    ✅ Deleted: {len(changes.deleted)}")
#     print(f"    ✅ Got new delta token: {bool(changes.new_delta_token)}")

#     print("\n" + "=" * 70)
#     print("  🎉 SHAREPOINT CONNECTION WORKING")
#     print("=" * 70)
#     print(f"\n  Ready to run full bot:  uvicorn app:app --port 8000")

# except Exception as e:
#     print(f"\n❌ FAILED: {type(e).__name__}")
#     print(f"   {e}")
#     print(f"\n   Common fixes:")
#     print(f"   • 'invalid_client' → CLIENT_ID or CLIENT_SECRET is wrong")
#     print(f"   • 'AADSTS70011'    → TENANT_ID is wrong")
#     print(f"   • 'AADSTS65001'    → admin didn't click 'Grant admin consent'")
#     print(f"   • 'Forbidden 403'  → permissions Sites.Read.All not granted")
#     print(f"   • 'No drives found'→ SHAREPOINT_SITE_URL is wrong or bot has no site access")
#     print(f"   • '404 Not Found'  → SHAREPOINT_LIBRARY name doesn't match")
#     sys.exit(1)

"""
diagnose_sharepoint.py — Show EVERYTHING the bot sees in SharePoint.
This bypasses our filters so we can see raw data.
"""
import json
from sources.sharepoint import SharePointSource, _pick_field, COLUMN_MAP

src = SharePointSource()
src._resolve_ids()

print("=" * 70)
print("  RESOLVED IDs")
print("=" * 70)
print(f"  Site ID:    {src._site_id}")
print(f"  Drive ID:   {src._drive_id}")
print(f"  List ID:    {src._list_id}")

# 1. List ALL drives in the site
print("\n" + "=" * 70)
print("  ALL DRIVES (document libraries) IN THIS SITE")
print("=" * 70)
drives = src._http_get(f"https://graph.microsoft.com/v1.0/sites/{src._site_id}/drives").json()
for d in drives.get("value", []):
    print(f"  • Name: '{d.get('name')}'  |  ID: {d.get('id')[:25]}...")

# 2. List ALL lists in the site
print("\n" + "=" * 70)
print("  ALL LISTS IN THIS SITE")
print("=" * 70)
lists = src._http_get(f"https://graph.microsoft.com/v1.0/sites/{src._site_id}/lists").json()
for lst in lists.get("value", []):
    print(f"  • Display Name: '{lst.get('displayName')}'")
    print(f"    Internal Name: '{lst.get('name')}'")
    print(f"    ID: {lst.get('id')[:25]}...")
    print()

# 3. List ALL items in the current drive (no filtering)
print("=" * 70)
print("  ALL ITEMS IN CURRENT DRIVE")
print("=" * 70)
items = src._list_drive_items()
print(f"Total items: {len(items)}\n")
for item in items[:5]:
    print(f"  • {item.get('name', 'NO NAME')}")
    print(f"    file_id: {item.get('id', '')[:30]}...")
    print(f"    type: {'folder' if 'folder' in item else 'file'}")
    print(f"    size: {item.get('size', 0):,} bytes")
    print(f"    modified: {item.get('lastModifiedDateTime')}")
    print()

# 4. List ALL list-item metadata
print("=" * 70)
print("  ALL LIST-ITEM METADATA (custom columns)")
print("=" * 70)
meta = src._fetch_list_metadata()
print(f"Total items with metadata: {len(meta)}\n")
for filename, fields in list(meta.items())[:5]:
    print(f"  • {filename}")
    print(f"    Status field value: {_pick_field(fields, COLUMN_MAP['status'])!r}")
    print(f"    Category:           {_pick_field(fields, COLUMN_MAP['category'])!r}")
    print(f"    Article Title:      {_pick_field(fields, COLUMN_MAP['article_title'])!r}")
    print(f"    ALL field keys:     {list(fields.keys())[:15]}...")
    print()

# 5. Show ONE complete raw item record
print("=" * 70)
print("  RAW DATA OF FIRST FILE (full JSON)")
print("=" * 70)
if meta:
    first_filename = list(meta.keys())[0]
    print(f"Filename: {first_filename}")
    print(f"All fields and values:")
    for k, v in list(meta[first_filename].items())[:30]:
        print(f"  {k:40s} = {v!r}")