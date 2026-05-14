"""
sources/sharepoint.py — Read documents from SharePoint via Microsoft Graph API.

Used in production once IT admin provides app registration credentials.

Auth flow:
  - MSAL acquires an OAuth token using client_credentials flow
  - Token is cached in-memory and refreshed automatically when expiring
  - Uses Application permissions (Sites.Read.All + Files.Read.All)

Delta sync:
  - First call: full listing + returns a delta token
  - Subsequent calls: only changes since the token (new/updated/deleted)
  - Token persisted by caller (in storage/cache.py)

Metadata strategy:
  - File comes from /drives/{id}/items endpoint (gets file properties)
  - Custom columns (Category, Sub-Category, Tags, etc.) come from
    /sites/{id}/lists/{id}/items?expand=fields (gets list metadata)
  - We merge both into the DocumentRef
"""

import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

import msal
import requests

from sources.base import DocumentSource, DocumentRef, DocumentData, ChangeSet
from config import settings

log = logging.getLogger(__name__)


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
REQUEST_TIMEOUT = 60


# ═══════════════════════════════════════════════════════════
#  SHAREPOINT CUSTOM COLUMN → DocumentRef mapping
# ═══════════════════════════════════════════════════════════
# SharePoint column display names get internal names with spaces escaped.
# "Article Title" → "Article_x0020_Title"; "Sub-Category" → "Sub_x002d_Category"
# We try multiple variants of each because exact names depend on how the
# column was created.

COLUMN_MAP = {
    "article_title": ["Article_x0020_Title", "ArticleTitle", "Title0"],
    "category": ["Category"],
    "sub_category": ["Sub_x002d_Category", "SubCategory"],
    "tags": ["Tags"],
    "summary": ["Summary"],
    "status": ["Status"],
    "approved_by": ["Approved_x0020_By", "ApprovedBy"],
    "publish_date": ["Publish_x0020_Date", "PublishDate"],
    "view_count": ["View_x0020_Count", "ViewCount"],
    "helpful_count": ["Helpful_x0020_Count", "HelpfulCount"],
    "not_helpful_count": ["Not_x0020_Helpful_x0020_Count", "NotHelpfulCount"],
    "ai_citation_count": ["AI_x0020_Citation_x0020_Count", "AICitationCount"],
    "source_ticket_id": ["Source_x0020_TicketID", "SourceTicketID"],
    "article_content": ["ArticleContent", "Article_x0020_Content"],
    "last_indexed": ["Last_x0020_Indexed", "LastIndexed"],
}


def _pick_field(fields: dict, candidates: List[str]) -> Any:
    """Return the first non-None value found in fields under any candidate key."""
    for key in candidates:
        if key in fields and fields[key] not in (None, ""):
            return fields[key]
    return None


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string. Returns None on bad input."""
    if not s:
        return None
    try:
        # Graph returns "2026-04-10T00:00:00Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _parse_tags(value: Any) -> List[str]:
    """SharePoint 'Tags' column can be a list or a delimited string."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v]
    if isinstance(value, str):
        # Comma or semicolon separated
        parts = [p.strip() for p in value.replace(";", ",").split(",")]
        return [p for p in parts if p]
    return []


def _ext_to_doc_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {"pdf": "pdf", "docx": "docx", "html": "html", "htm": "html"}.get(ext, ext)


def _is_supported_filename(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in {"pdf", "docx", "html", "htm"}


# ═══════════════════════════════════════════════════════════
#  SHAREPOINT SOURCE
# ═══════════════════════════════════════════════════════════

class SharePointSource(DocumentSource):
    """
    Read from SharePoint via Microsoft Graph.

    Required settings (from .env):
        TENANT_ID, CLIENT_ID, CLIENT_SECRET
        SHAREPOINT_SITE_URL    (e.g. https://veeleaddev.sharepoint.com/sites/Helpdesk)
        SHAREPOINT_LIBRARY     (display name, e.g. "HD_KnowledgeDocuments")
    """

    def __init__(self):
        if not (settings.tenant_id and settings.client_id and settings.client_secret):
            raise RuntimeError(
                "SharePoint mode requires TENANT_ID, CLIENT_ID, CLIENT_SECRET in .env"
            )
        if not settings.sharepoint_site_url:
            raise RuntimeError("SHAREPOINT_SITE_URL is required")

        self._msal_app = msal.ConfidentialClientApplication(
            client_id=settings.client_id,
            client_credential=settings.client_secret,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
        )
        self._site_url = settings.sharepoint_site_url
        self._library_name = settings.sharepoint_library
        # Cached after first resolve
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None
        self._list_id: Optional[str] = None

    # ── DocumentSource interface ──

    def source_name(self) -> str:
        return f"SharePoint({self._site_url} / {self._library_name})"

    def list_documents(self, only_published: bool = True) -> List[DocumentRef]:
        """List all files in the library, with full metadata."""
        self._resolve_ids()
        items = self._list_drive_items()

        # Also fetch list-item metadata (custom columns) — join by name
        meta_by_name = self._fetch_list_metadata()

        refs: List[DocumentRef] = []
        for item in items:
            filename = item.get("name", "")
            if not _is_supported_filename(filename):
                continue

            meta = meta_by_name.get(filename, {})
            ref = self._build_ref(item, meta)

            if only_published and ref.status not in ("Published", "Approved"):
                log.debug(f"  ⏭ Skipping (status={ref.status!r}): {filename}")
                continue

            refs.append(ref)

        log.info(f"SharePoint: {len(refs)} published document(s) in {self._library_name}")
        return refs

    def download(self, ref: DocumentRef) -> DocumentData:
        """
        Fetch file bytes from SharePoint.

        Optimization: if SharePoint has ArticleContent column populated for
        this file, we use that as pre-extracted text and skip the download —
        saves bandwidth and avoids re-parsing HTML.
        """
        # Fast path: pre-extracted text already on the ref
        if ref.article_content:
            log.debug(f"  Using ArticleContent column for {ref.filename}")
            return DocumentData(ref=ref, pre_extracted_text=ref.article_content)

        self._resolve_ids()
        url = f"{GRAPH_BASE}/drives/{self._drive_id}/items/{ref.file_id}/content"
        resp = self._http_get(url, raw=True)
        return DocumentData(ref=ref, content_bytes=resp.content)

    def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
        """
        Use Graph API delta query to get changes since the last token.

        Notes:
          - The 'delta' endpoint returns added + updated files inline
            but only file IDs for deleted ones (marked with @removed)
          - We re-fetch list metadata for changed items to pick up
            Category/Tag/Status changes (not just file content changes)
        """
        self._resolve_ids()

        if delta_token:
            url = delta_token  # the token IS a full URL with $deltaToken
            log.info("SharePoint delta: resuming from saved token")
        else:
            url = f"{GRAPH_BASE}/drives/{self._drive_id}/root/delta"
            log.info("SharePoint delta: first call (full sync)")

        # Drive-item changes
        added: List[DocumentRef] = []
        updated: List[DocumentRef] = []
        deleted: List[str] = []
        new_token: Optional[str] = None

        while True:
            data = self._http_get(url).json()
            for item in data.get("value", []):
                # Folders / non-files skipped
                if "file" not in item and "@microsoft.graph.downloadUrl" not in item:
                    # Could be folder or root — skip
                    if not item.get("name"):
                        continue
                # Deleted
                if "deleted" in item:
                    file_id = item.get("id")
                    if file_id:
                        deleted.append(file_id)
                    continue
                # Added/updated — Graph delta doesn't distinguish so we treat all as "updated"
                # (our pipeline does delete-old → insert-new for both anyway)
                filename = item.get("name", "")
                if not _is_supported_filename(filename):
                    continue
                # Need full metadata — fetch list-item metadata for this item
                meta = self._fetch_list_metadata_for_item(filename)
                ref = self._build_ref(item, meta)
                if ref.status not in ("Published", "Approved"):
                    continue
                updated.append(ref)

            # Paginate
            if "@odata.nextLink" in data:
                url = data["@odata.nextLink"]
                continue
            # Final page contains the delta link for next sync
            new_token = data.get("@odata.deltaLink")
            break

        log.info(
            f"SharePoint delta: +{len(added)} +/~{len(updated)} updated, "
            f"-{len(deleted)} deleted"
        )

        return ChangeSet(
            added=added,
            updated=updated,
            deleted=deleted,
            new_delta_token=new_token,
        )

    # ════════════════════════════════════════════════════════
    #  PRIVATE — Graph plumbing
    # ════════════════════════════════════════════════════════

    def _get_token(self) -> str:
        """Acquire (or refresh) the OAuth access token via MSAL."""
        # MSAL handles in-memory token caching automatically
        result = self._msal_app.acquire_token_silent(GRAPH_SCOPE, account=None)
        if not result:
            result = self._msal_app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or result
            raise RuntimeError(f"MSAL auth failed: {err}")
        return result["access_token"]

    def _http_get(self, url: str, raw: bool = False) -> requests.Response:
        """GET helper with auth header + error handling. Returns Response."""
        token = self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        if not raw:
            headers["Accept"] = "application/json"
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Graph API error {resp.status_code} on {url}: "
                f"{resp.text[:500]}"
            )
        return resp

    def _resolve_ids(self) -> None:
        """Resolve site_id, drive_id, and list_id from the configured site URL."""
        if self._site_id and self._drive_id and self._list_id:
            return

        # Parse hostname + site path from URL
        # https://veeleaddev.sharepoint.com/sites/Helpdesk → hostname, /sites/Helpdesk
        parsed = urlparse(self._site_url)
        hostname = parsed.hostname
        site_path = parsed.path  # /sites/Helpdesk

        # Resolve site
        url = f"{GRAPH_BASE}/sites/{hostname}:{site_path}"
        site = self._http_get(url).json()
        self._site_id = site["id"]
        log.info(f"Resolved SharePoint site: {self._site_id}")

        # Resolve drive (find by name matching library, or use default 'Documents')
        drives = self._http_get(f"{GRAPH_BASE}/sites/{self._site_id}/drives").json()
        drive_id = None
        for d in drives.get("value", []):
            if d.get("name", "").lower() == self._library_name.lower():
                drive_id = d["id"]
                break
        if not drive_id and drives.get("value"):
            # Fallback: use the first drive
            drive_id = drives["value"][0]["id"]
            log.warning(
                f"Library '{self._library_name}' not found; using first drive "
                f"'{drives['value'][0].get('name')}'"
            )
        if not drive_id:
            raise RuntimeError(f"No drives found in site {self._site_url}")
        self._drive_id = drive_id

        # Resolve list (same as library, for accessing custom columns)
        lists = self._http_get(f"{GRAPH_BASE}/sites/{self._site_id}/lists").json()
        list_id = None
        for lst in lists.get("value", []):
            if lst.get("displayName", "").lower() == self._library_name.lower():
                list_id = lst["id"]
                break
        if not list_id:
            log.warning(
                f"List '{self._library_name}' not found — custom columns "
                f"(Category, Tags, etc.) will be missing."
            )
        self._list_id = list_id

    def _list_drive_items(self) -> List[dict]:
        """List all items in the drive (files + folders)."""
        items: List[dict] = []
        url = f"{GRAPH_BASE}/drives/{self._drive_id}/root/children?$top=200"
        while url:
            data = self._http_get(url).json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def _fetch_list_metadata(self) -> Dict[str, dict]:
        """
        Return dict of {filename: fields} for all items in the list.
        'fields' contains all custom columns.
        """
        if not self._list_id:
            return {}

        meta: Dict[str, dict] = {}
        url = (
            f"{GRAPH_BASE}/sites/{self._site_id}/lists/{self._list_id}/items"
            f"?expand=fields&$top=200"
        )
        while url:
            data = self._http_get(url).json()
            for item in data.get("value", []):
                fields = item.get("fields") or {}
                filename = fields.get("FileLeafRef") or fields.get("LinkFilename")
                if filename:
                    meta[filename] = fields
            url = data.get("@odata.nextLink")
        return meta

    def _fetch_list_metadata_for_item(self, filename: str) -> dict:
        """Fetch metadata for a single item by filename. Used during delta sync."""
        if not self._list_id:
            return {}
        # Filter by FileLeafRef
        from urllib.parse import quote
        filter_str = quote(f"fields/FileLeafRef eq '{filename}'")
        url = (
            f"{GRAPH_BASE}/sites/{self._site_id}/lists/{self._list_id}/items"
            f"?expand=fields&$filter={filter_str}"
        )
        try:
            data = self._http_get(url).json()
            items = data.get("value", [])
            if items:
                return items[0].get("fields") or {}
        except Exception as e:
            log.warning(f"Could not fetch metadata for {filename}: {e}")
        return {}

    def _build_ref(self, drive_item: dict, list_meta: dict) -> DocumentRef:
        """Combine drive-item info + list-item metadata into a DocumentRef."""
        filename = drive_item.get("name", "")
        return DocumentRef(
            file_id=drive_item.get("id", ""),
            filename=filename,
            doc_type=_ext_to_doc_type(filename),
            modified_at=(
                _parse_iso(drive_item.get("lastModifiedDateTime"))
                or datetime.utcnow()
            ),
            size_bytes=int(drive_item.get("size") or 0),
            # SharePoint custom columns (any may be missing/None)
            article_title=_pick_field(list_meta, COLUMN_MAP["article_title"]),
            category=_pick_field(list_meta, COLUMN_MAP["category"]) or "Uncategorized",
            sub_category=_pick_field(list_meta, COLUMN_MAP["sub_category"]),
            tags=_parse_tags(_pick_field(list_meta, COLUMN_MAP["tags"])),
            summary=_pick_field(list_meta, COLUMN_MAP["summary"]),
            status=_pick_field(list_meta, COLUMN_MAP["status"]),
            author=(
                _pick_field(list_meta, COLUMN_MAP["approved_by"])
                or (drive_item.get("createdBy", {}).get("user", {}) or {}).get("displayName")
            ),
            publish_date=_parse_iso(_pick_field(list_meta, COLUMN_MAP["publish_date"])),
            view_count=int(_pick_field(list_meta, COLUMN_MAP["view_count"]) or 0),
            helpful_count=int(_pick_field(list_meta, COLUMN_MAP["helpful_count"]) or 0),
            not_helpful_count=int(
                _pick_field(list_meta, COLUMN_MAP["not_helpful_count"]) or 0
            ),
            ai_citation_count=int(
                _pick_field(list_meta, COLUMN_MAP["ai_citation_count"]) or 0
            ),
            source_ticket_id=_pick_field(list_meta, COLUMN_MAP["source_ticket_id"]),
            article_content=_pick_field(list_meta, COLUMN_MAP["article_content"]),
        )
