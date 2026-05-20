"""Inspect HD_Categories and HD_SubCategories lookup tables."""
from sources.sharepoint import SharePointSource

src = SharePointSource()
src._resolve_ids()

# Get all lists
lists = src._http_get(f"https://graph.microsoft.com/v1.0/sites/{src._site_id}/lists").json()

# Find HD_Categories and HD_SubCategories
for lst in lists.get("value", []):
    display = lst.get("displayName") or ""
    if "categor" in display.lower():
        list_id = lst["id"]
        print(f"\n{'='*70}")
        print(f"  LIST: {display}")
        print(f"{'='*70}")

        # Fetch all items with full fields
        url = f"https://graph.microsoft.com/v1.0/sites/{src._site_id}/lists/{list_id}/items?expand=fields&$top=200"
        data = src._http_get(url).json()
        items = data.get("value", [])
        print(f"  Total items: {len(items)}\n")

        for item in items:
            fields = item.get("fields") or {}
            print(f"  Item id={fields.get('id')}")
            # Show all fields except the etag
            for k, v in fields.items():
                if k.startswith("@") or k.startswith("_"):
                    continue
                print(f"    {k:40s} = {v!r}")
            print()