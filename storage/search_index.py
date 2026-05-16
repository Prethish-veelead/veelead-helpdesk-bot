# """
# storage/search_index.py — Azure AI Search wrapper.

# This is the bot's persistent memory. Stores document chunks (text +
# embedding + metadata) and serves hybrid queries (vector + keyword)
# filtered by SharePoint metadata.

# Public API:
#     ensure_index()                                # create if missing
#     upsert_chunks(chunks: list[dict])             # add/update chunks
#     delete_by_file_id(sharepoint_file_id)         # remove all chunks for a file
#     delete_by_filename(filename)                  # alternate delete (local-mode)
#     hybrid_search(query, ...)                     # main retrieval call
#     list_categories()                             # for /categories endpoint
#     get_index_stats()                             # for /health endpoint

# The schema mirrors SharePoint metadata 1:1, so when a SharePoint
# admin adds a tag or changes a Category, the next sync picks it up
# and search filters work immediately.
# """

# import logging
# from datetime import datetime
# from typing import List, Dict, Any, Optional

# from azure.core.credentials import AzureKeyCredential
# from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
# from azure.search.documents import SearchClient
# from azure.search.documents.indexes import SearchIndexClient
# from azure.search.documents.indexes.models import (
#     SearchIndex,
#     SimpleField,
#     SearchableField,
#     SearchField,
#     SearchFieldDataType,
#     VectorSearch,
#     HnswAlgorithmConfiguration,
#     VectorSearchProfile,
# )
# from azure.search.documents.models import VectorizedQuery

# from config import settings

# log = logging.getLogger(__name__)


# # ═══════════════════════════════════════════════════════════
# #  CLIENTS (lazy singletons)
# # ═══════════════════════════════════════════════════════════

# _credential: Optional[AzureKeyCredential] = None
# _search_client: Optional[SearchClient] = None
# _index_client: Optional[SearchIndexClient] = None


# def _get_credential() -> AzureKeyCredential:
#     global _credential
#     if _credential is None:
#         _credential = AzureKeyCredential(settings.search_api_key)
#     return _credential


# def _get_search_client() -> SearchClient:
#     global _search_client
#     if _search_client is None:
#         _search_client = SearchClient(
#             endpoint=settings.search_endpoint,
#             index_name=settings.search_index,
#             credential=_get_credential(),
#         )
#     return _search_client


# def _get_index_client() -> SearchIndexClient:
#     global _index_client
#     if _index_client is None:
#         _index_client = SearchIndexClient(
#             endpoint=settings.search_endpoint,
#             credential=_get_credential(),
#         )
#     return _index_client


# # ═══════════════════════════════════════════════════════════
# #  INDEX SCHEMA — matches SharePoint metadata + RAG fields
# # ═══════════════════════════════════════════════════════════

# def _build_index_definition() -> SearchIndex:
#     """
#     Define the index schema.

#     Field categories:
#       - Identity: chunk_id (key), sharepoint_file_id, filename
#       - Content:  text (analyzed), embedding (vector)
#       - SharePoint metadata: article_title, summary, category, sub_category,
#         tags, status, author, publish_date
#       - Quality signals: view_count, helpful_count, not_helpful_count,
#         ai_citation_count
#       - Tracking: source_ticket_id, last_indexed, chunk_index, total_chunks, doc_type
#     """
#     fields = [
#         # ── Identity ──
#         SimpleField(
#             name="chunk_id",
#             type=SearchFieldDataType.String,
#             key=True,
#             filterable=True,
#         ),
#         SimpleField(
#             name="sharepoint_file_id",
#             type=SearchFieldDataType.String,
#             filterable=True,
#         ),
#         SearchableField(
#             name="filename",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="doc_type",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             facetable=True,
#         ),

#         # ── Content (searchable text + vector) ──
#         SearchableField(
#             name="text",
#             type=SearchFieldDataType.String,
#             analyzer_name="en.microsoft",
#         ),
#         SearchField(
#             name="embedding",
#             type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
#             searchable=True,
#             vector_search_dimensions=settings.embed_dim,
#             vector_search_profile_name="vec-profile",
#         ),

#         # ── SharePoint metadata: filtering + ranking signals ──
#         SearchableField(
#             name="article_title",
#             type=SearchFieldDataType.String,
#             analyzer_name="en.microsoft",
#             sortable=True,
#         ),
#         SearchableField(
#             name="summary",
#             type=SearchFieldDataType.String,
#             analyzer_name="en.microsoft",
#         ),
#         SearchableField(
#             name="category",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             facetable=True,
#             sortable=True,
#         ),
#         SearchableField(
#             name="sub_category",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             facetable=True,
#         ),
#         SearchField(
#             name="tags",
#             type=SearchFieldDataType.Collection(SearchFieldDataType.String),
#             filterable=True,
#             facetable=True,
#             searchable=True,
#         ),
#         SimpleField(
#             name="status",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             facetable=True,
#         ),
#         SimpleField(
#             name="author",
#             type=SearchFieldDataType.String,
#             filterable=True,
#             facetable=True,
#         ),
#         SimpleField(
#             name="publish_date",
#             type=SearchFieldDataType.DateTimeOffset,
#             filterable=True,
#             sortable=True,
#         ),

#         # ── Quality signals (for ranking + analytics) ──
#         SimpleField(
#             name="view_count",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="helpful_count",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="not_helpful_count",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="ai_citation_count",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#             sortable=True,
#         ),

#         # ── Tracking ──
#         SimpleField(
#             name="source_ticket_id",
#             type=SearchFieldDataType.String,
#             filterable=True,
#         ),
#         SimpleField(
#             name="last_indexed",
#             type=SearchFieldDataType.DateTimeOffset,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="chunk_index",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#             sortable=True,
#         ),
#         SimpleField(
#             name="total_chunks",
#             type=SearchFieldDataType.Int32,
#             filterable=True,
#         ),
#     ]

#     # HNSW vector search config — good defaults for ≤10k vectors
#     vector_search = VectorSearch(
#         algorithms=[
#             HnswAlgorithmConfiguration(name="hnsw-config")
#         ],
#         profiles=[
#             VectorSearchProfile(
#                 name="vec-profile",
#                 algorithm_configuration_name="hnsw-config",
#             )
#         ],
#     )

#     return SearchIndex(
#         name=settings.search_index,
#         fields=fields,
#         vector_search=vector_search,
#     )


# def ensure_index() -> str:
#     """
#     Create the index if it doesn't exist. Idempotent.
#     Returns 'created', 'exists', or raises.
#     """
#     idx_client = _get_index_client()
#     try:
#         idx_client.get_index(settings.search_index)
#         log.info(f"Index '{settings.search_index}' already exists.")
#         return "exists"
#     except ResourceNotFoundError:
#         pass

#     log.info(f"Creating index '{settings.search_index}'...")
#     definition = _build_index_definition()
#     idx_client.create_index(definition)
#     log.info(f"✅ Index '{settings.search_index}' created.")
#     return "created"


# def delete_index() -> None:
#     """
#     Drop the index entirely. Used by tests and when changing schema.
#     """
#     idx_client = _get_index_client()
#     try:
#         idx_client.delete_index(settings.search_index)
#         log.warning(f"Index '{settings.search_index}' deleted.")
#     except ResourceNotFoundError:
#         pass


# # ═══════════════════════════════════════════════════════════
# #  WRITE: upsert + delete
# # ═══════════════════════════════════════════════════════════

# def _prepare_chunk_for_upload(chunk: Dict[str, Any]) -> Dict[str, Any]:
#     """
#     Convert a chunk dict (from chunk_document + embed_many) into the exact
#     shape Azure AI Search expects. Adds last_indexed timestamp and ensures
#     numeric defaults.
#     """
#     return {
#         "chunk_id": chunk["chunk_id"],
#         "sharepoint_file_id": chunk.get("sharepoint_file_id") or "",
#         "filename": chunk.get("filename") or "",
#         "doc_type": chunk.get("doc_type") or "",
#         "text": chunk["text"],
#         "embedding": chunk["embedding"],
#         "article_title": chunk.get("article_title") or "",
#         "summary": chunk.get("summary") or "",
#         "category": chunk.get("category") or "Uncategorized",
#         "sub_category": chunk.get("sub_category") or "",
#         "tags": chunk.get("tags") or [],
#         "status": chunk.get("status") or "",
#         "author": chunk.get("author") or "",
#         "publish_date": chunk.get("publish_date"),  # ISO 8601 or None
#         "view_count": int(chunk.get("view_count") or 0),
#         "helpful_count": int(chunk.get("helpful_count") or 0),
#         "not_helpful_count": int(chunk.get("not_helpful_count") or 0),
#         "ai_citation_count": int(chunk.get("ai_citation_count") or 0),
#         "source_ticket_id": chunk.get("source_ticket_id") or "",
#         "last_indexed": datetime.utcnow().isoformat() + "Z",
#         "chunk_index": int(chunk.get("chunk_index") or 0),
#         "total_chunks": int(chunk.get("total_chunks") or 0),
#     }


# def upsert_chunks(chunks: List[Dict[str, Any]], batch_size: int = 100) -> Dict[str, int]:
#     """
#     Insert or update chunks. Each chunk must have an 'embedding' field already.
#     Returns counts: {'uploaded': N, 'failed': M}.
#     """
#     if not chunks:
#         return {"uploaded": 0, "failed": 0}

#     client = _get_search_client()
#     uploaded = 0
#     failed = 0

#     for start in range(0, len(chunks), batch_size):
#         batch_raw = chunks[start: start + batch_size]
#         batch = [_prepare_chunk_for_upload(c) for c in batch_raw]
#         try:
#             results = client.merge_or_upload_documents(documents=batch)
#             for r in results:
#                 if r.succeeded:
#                     uploaded += 1
#                 else:
#                     failed += 1
#                     log.warning(f"Upload failed for {r.key}: {r.error_message}")
#         except HttpResponseError as e:
#             log.error(f"Batch upload failed: {e}")
#             failed += len(batch)

#     log.info(f"Upsert complete: {uploaded} uploaded, {failed} failed")
#     return {"uploaded": uploaded, "failed": failed}


# def delete_by_file_id(sharepoint_file_id: str) -> int:
#     """
#     Delete all chunks for a given SharePoint file.
#     Called when a file is updated (delete old → upload new) or deleted.
#     Returns count of chunks deleted.
#     """
#     if not sharepoint_file_id:
#         return 0

#     client = _get_search_client()
#     # Find all chunks for this file
#     results = client.search(
#         search_text="*",
#         filter=f"sharepoint_file_id eq '{_escape_odata(sharepoint_file_id)}'",
#         select=["chunk_id"],
#         top=1000,
#     )
#     chunk_ids = [r["chunk_id"] for r in results]

#     if not chunk_ids:
#         return 0

#     docs = [{"chunk_id": cid} for cid in chunk_ids]
#     client.delete_documents(documents=docs)
#     log.info(f"Deleted {len(chunk_ids)} chunks for SharePoint file {sharepoint_file_id}")
#     return len(chunk_ids)


# def delete_by_filename(filename: str) -> int:
#     """
#     Delete all chunks by filename (used in local-folder mode where
#     there's no SharePoint file ID).
#     """
#     if not filename:
#         return 0

#     client = _get_search_client()
#     results = client.search(
#         search_text="*",
#         filter=f"filename eq '{_escape_odata(filename)}'",
#         select=["chunk_id"],
#         top=1000,
#     )
#     chunk_ids = [r["chunk_id"] for r in results]

#     if not chunk_ids:
#         return 0

#     docs = [{"chunk_id": cid} for cid in chunk_ids]
#     client.delete_documents(documents=docs)
#     log.info(f"Deleted {len(chunk_ids)} chunks for filename '{filename}'")
#     return len(chunk_ids)


# def _escape_odata(s: str) -> str:
#     """Escape single quotes for OData filter string literals."""
#     return s.replace("'", "''")


# # ═══════════════════════════════════════════════════════════
# #  READ: hybrid search
# # ═══════════════════════════════════════════════════════════

# def hybrid_search(
#     query: str,
#     query_vector: List[float],
#     top_k: int = 5,
#     category: Optional[str] = None,
#     include_uncategorized: bool = True,
#     only_published: bool = True,
#     extra_filter: Optional[str] = None,
# ) -> List[Dict[str, Any]]:
#     """
#     Hybrid search: combines vector similarity with keyword (BM25) matching.

#     Args:
#         query: User's question (for keyword search).
#         query_vector: Embedding of the query (for vector search).
#         top_k: How many chunks to return.
#         category: If set, restrict to this category (e.g. "IT", "HR").
#                   When include_uncategorized=True, also includes Uncategorized.
#         include_uncategorized: When filtering by category, also include
#                                Uncategorized items (they might be relevant
#                                but unlabeled).
#         only_published: If True, restrict to status='Published'.
#         extra_filter: Optional additional OData filter (e.g. exclude specific
#                       chunk_ids for "alternative method" feature).

#     Returns:
#         List of chunk dicts ordered by relevance. Each has all metadata + 'score'.
#     """
#     client = _get_search_client()

#     # Build OData filter
#     filters: List[str] = []

#     if only_published:
#         # Match Published OR Approved (case-insensitive friendly approach: include both)
#         filters.append("(status eq 'Published' or status eq 'Approved')")

#     if category:
#         cat_escaped = _escape_odata(category)
#         if include_uncategorized:
#             filters.append(
#                 f"(category eq '{cat_escaped}' or category eq 'Uncategorized')"
#             )
#         else:
#             filters.append(f"category eq '{cat_escaped}'")

#     if extra_filter:
#         filters.append(f"({extra_filter})")

#     filter_str = " and ".join(filters) if filters else None

#     # Vector query (the dominant signal)
#     vector_q = VectorizedQuery(
#         vector=query_vector,
#         k_nearest_neighbors=top_k * 3,  # over-fetch then re-rank
#         fields="embedding",
#     )

#     try:
#         results = client.search(
#             search_text=query,                # keyword side (BM25)
#             vector_queries=[vector_q],        # vector side
#             filter=filter_str,
#             top=top_k,
#             select=[
#                 "chunk_id", "sharepoint_file_id", "filename", "doc_type",
#                 "text", "article_title", "summary", "category", "sub_category",
#                 "tags", "status", "author", "ai_citation_count",
#                 "helpful_count", "not_helpful_count", "source_ticket_id",
#                 "chunk_index", "total_chunks",
#             ],
#         )
#     except HttpResponseError as e:
#         log.error(f"Search failed: {e}")
#         return []

#     output: List[Dict[str, Any]] = []
#     for r in results:
#         chunk = dict(r)  # SDK returns a dict-like; convert to plain dict
#         chunk["score"] = r["@search.score"]
#         # Remove the @-prefixed metadata fields the SDK adds
#         for k in list(chunk.keys()):
#             if k.startswith("@"):
#                 chunk.pop(k, None)
#         output.append(chunk)

#     return output


# # ═══════════════════════════════════════════════════════════
# #  FACETS / STATS — for /categories + /health endpoints
# # ═══════════════════════════════════════════════════════════

# def list_categories(only_published: bool = True) -> List[Dict[str, Any]]:
#     """
#     Return list of categories with document counts.
#     Used by GET /categories endpoint (frontend renders buttons).
#     """
#     client = _get_search_client()
#     filter_str = "(status eq 'Published' or status eq 'Approved')" if only_published else None

#     try:
#         results = client.search(
#             search_text="*",
#             filter=filter_str,
#             facets=["category,count:50"],
#             top=0,
#         )
#         # Force evaluation to get facets
#         list(results)
#         facets = results.get_facets() or {}
#         category_facets = facets.get("category", []) or []
#     except HttpResponseError as e:
#         log.error(f"Facet query failed: {e}")
#         return []

#     # Each facet is {'value': 'IT', 'count': 42}
#     categories = [
#         {
#             "name": f["value"],
#             "display": f["value"],   # frontend can override with nicer label
#             "chunk_count": f["count"],
#         }
#         for f in category_facets
#         if f["value"]  # skip empty
#     ]
#     return categories


# def get_index_stats() -> Dict[str, Any]:
#     """
#     Stats for /health endpoint.
#     """
#     idx_client = _get_index_client()
#     try:
#         stats = idx_client.get_index_statistics(settings.search_index)
#         return {
#             "index_name": settings.search_index,
#             "document_count": stats["document_count"],
#             "storage_size_bytes": stats["storage_size"],
#         }
#     except ResourceNotFoundError:
#         return {
#             "index_name": settings.search_index,
#             "document_count": 0,
#             "storage_size_bytes": 0,
#             "note": "Index does not exist yet — run ensure_index().",
#         }
#     except HttpResponseError as e:
#         log.error(f"Stats query failed: {e}")
#         return {"error": str(e)}


# def count_chunks_for_file(sharepoint_file_id: Optional[str] = None,
#                           filename: Optional[str] = None) -> int:
#     """
#     Count chunks for a given file. Used by sync to detect 'already indexed'.
#     """
#     client = _get_search_client()
#     if sharepoint_file_id:
#         filter_str = f"sharepoint_file_id eq '{_escape_odata(sharepoint_file_id)}'"
#     elif filename:
#         filter_str = f"filename eq '{_escape_odata(filename)}'"
#     else:
#         return 0

#     try:
#         results = client.search(
#             search_text="*",
#             filter=filter_str,
#             top=0,
#             include_total_count=True,
#         )
#         list(results)  # force evaluation
#         return results.get_count() or 0
#     except HttpResponseError as e:
#         log.error(f"Count query failed: {e}")
#         return 0


# # ═══════════════════════════════════════════════════════════
# #  CLI / quick test
# # ═══════════════════════════════════════════════════════════

# if __name__ == "__main__":
#     """
#     Smoke test against your live Azure AI Search instance.

#     Usage:
#         python -m storage.search_index ensure       # create the index
#         python -m storage.search_index stats        # show index stats
#         python -m storage.search_index categories   # list categories
#         python -m storage.search_index drop         # delete index (careful!)
#     """
#     import sys

#     if len(sys.argv) < 2:
#         print(__doc__)
#         sys.exit(1)

#     cmd = sys.argv[1].lower()

#     if cmd == "ensure":
#         result = ensure_index()
#         print(f"  → {result}")
#     elif cmd == "stats":
#         import json
#         print(json.dumps(get_index_stats(), indent=2))
#     elif cmd == "categories":
#         cats = list_categories()
#         print(f"Categories ({len(cats)}):")
#         for c in cats:
#             print(f"  • {c['name']:20s} ({c['chunk_count']} chunks)")
#     elif cmd == "drop":
#         confirm = input(f"Delete index '{settings.search_index}'? Type 'yes': ")
#         if confirm == "yes":
#             delete_index()
#             print("Index deleted.")
#         else:
#             print("Cancelled.")
#     else:
#         print(f"Unknown command: {cmd}")
#         sys.exit(1)


"""
storage/search_index.py — Azure AI Search wrapper.

This is the bot's persistent memory. Stores document chunks (text +
embedding + metadata) and serves hybrid queries (vector + keyword)
filtered by SharePoint metadata.

Public API:
    ensure_index()                                # create if missing
    upsert_chunks(chunks: list[dict])             # add/update chunks
    delete_by_file_id(sharepoint_file_id)         # remove all chunks for a file
    delete_by_filename(filename)                  # alternate delete (local-mode)
    hybrid_search(query, ...)                     # main retrieval call
    list_categories()                             # for /categories endpoint
    get_index_stats()                             # for /health endpoint

The schema mirrors SharePoint metadata 1:1, so when a SharePoint
admin adds a tag or changes a Category, the next sync picks it up
and search filters work immediately.
"""

import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError, HttpResponseError
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

from config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  CLIENTS (lazy singletons)
# ═══════════════════════════════════════════════════════════

_credential: Optional[AzureKeyCredential] = None
_search_client: Optional[SearchClient] = None
_index_client: Optional[SearchIndexClient] = None


def _get_credential() -> AzureKeyCredential:
    global _credential
    if _credential is None:
        _credential = AzureKeyCredential(settings.search_api_key)
    return _credential


def _get_search_client() -> SearchClient:
    global _search_client
    if _search_client is None:
        _search_client = SearchClient(
            endpoint=settings.search_endpoint,
            index_name=settings.search_index,
            credential=_get_credential(),
        )
    return _search_client


def _get_index_client() -> SearchIndexClient:
    global _index_client
    if _index_client is None:
        _index_client = SearchIndexClient(
            endpoint=settings.search_endpoint,
            credential=_get_credential(),
        )
    return _index_client


# ═══════════════════════════════════════════════════════════
#  INDEX SCHEMA — matches SharePoint metadata + RAG fields
# ═══════════════════════════════════════════════════════════

def _build_index_definition() -> SearchIndex:
    """
    Define the index schema.

    Field categories:
      - Identity: chunk_id (key), sharepoint_file_id, filename
      - Content:  text (analyzed), embedding (vector)
      - SharePoint metadata: article_title, summary, category, sub_category,
        tags, status, author, publish_date
      - Quality signals: view_count, helpful_count, not_helpful_count,
        ai_citation_count
      - Tracking: source_ticket_id, last_indexed, chunk_index, total_chunks, doc_type
    """
    fields = [
        # ── Identity ──
        SimpleField(
            name="chunk_id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=True,
        ),
        SimpleField(
            name="sharepoint_file_id",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SearchableField(
            name="filename",
            type=SearchFieldDataType.String,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="doc_type",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),

        # ── Content (searchable text + vector) ──
        SearchableField(
            name="text",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
        ),
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=settings.embed_dim,
            vector_search_profile_name="vec-profile",
        ),

        # ── SharePoint metadata: filtering + ranking signals ──
        SearchableField(
            name="article_title",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
            sortable=True,
        ),
        SearchableField(
            name="summary",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft",
        ),
        SearchableField(
            name="category",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
            sortable=True,
        ),
        SearchableField(
            name="sub_category",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SearchField(
            name="tags",
            type=SearchFieldDataType.Collection(SearchFieldDataType.String),
            filterable=True,
            facetable=True,
            searchable=True,
        ),
        SimpleField(
            name="status",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="author",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
        ),
        SimpleField(
            name="publish_date",
            type=SearchFieldDataType.DateTimeOffset,
            filterable=True,
            sortable=True,
        ),

        # ── Quality signals (for ranking + analytics) ──
        SimpleField(
            name="view_count",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="helpful_count",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="not_helpful_count",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="ai_citation_count",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),

        # ── Tracking ──
        SimpleField(
            name="source_ticket_id",
            type=SearchFieldDataType.String,
            filterable=True,
        ),
        SimpleField(
            name="last_indexed",
            type=SearchFieldDataType.DateTimeOffset,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="total_chunks",
            type=SearchFieldDataType.Int32,
            filterable=True,
        ),
        SimpleField(
            name="page",
            type=SearchFieldDataType.Int32,
            filterable=True,
            sortable=True,
        ),
        SimpleField(
            name="pdf_url",
            type=SearchFieldDataType.String,
            filterable=False,
            sortable=False,
        ),
    ]

    # HNSW vector search config — good defaults for ≤10k vectors
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="hnsw-config")
        ],
        profiles=[
            VectorSearchProfile(
                name="vec-profile",
                algorithm_configuration_name="hnsw-config",
            )
        ],
    )

    return SearchIndex(
        name=settings.search_index,
        fields=fields,
        vector_search=vector_search,
    )


def ensure_index() -> str:
    """
    Create the index if it doesn't exist. Idempotent.
    Returns 'created', 'exists', or raises.
    """
    idx_client = _get_index_client()
    try:
        idx_client.get_index(settings.search_index)
        log.info(f"Index '{settings.search_index}' already exists.")
        return "exists"
    except ResourceNotFoundError:
        pass

    log.info(f"Creating index '{settings.search_index}'...")
    definition = _build_index_definition()
    idx_client.create_index(definition)
    log.info(f"✅ Index '{settings.search_index}' created.")
    return "created"


def delete_index() -> None:
    """
    Drop the index entirely. Used by tests and when changing schema.
    """
    idx_client = _get_index_client()
    try:
        idx_client.delete_index(settings.search_index)
        log.warning(f"Index '{settings.search_index}' deleted.")
    except ResourceNotFoundError:
        pass


# ═══════════════════════════════════════════════════════════
#  WRITE: upsert + delete
# ═══════════════════════════════════════════════════════════

def _prepare_chunk_for_upload(chunk: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a chunk dict (from chunk_document + embed_many) into the exact
    shape Azure AI Search expects. Adds last_indexed timestamp and ensures
    numeric defaults.
    """
    return {
        "chunk_id": chunk["chunk_id"],
        "sharepoint_file_id": chunk.get("sharepoint_file_id") or "",
        "filename": chunk.get("filename") or "",
        "doc_type": chunk.get("doc_type") or "",
        "text": chunk["text"],
        "embedding": chunk["embedding"],
        "article_title": chunk.get("article_title") or "",
        "summary": chunk.get("summary") or "",
        "category": chunk.get("category") or "Uncategorized",
        "sub_category": chunk.get("sub_category") or "",
        "tags": chunk.get("tags") or [],
        "status": chunk.get("status") or "",
        "author": chunk.get("author") or "",
        "publish_date": chunk.get("publish_date"),  # ISO 8601 or None
        "view_count": int(chunk.get("view_count") or 0),
        "helpful_count": int(chunk.get("helpful_count") or 0),
        "not_helpful_count": int(chunk.get("not_helpful_count") or 0),
        "ai_citation_count": int(chunk.get("ai_citation_count") or 0),
        "source_ticket_id": chunk.get("source_ticket_id") or "",
        "last_indexed": datetime.utcnow().isoformat() + "Z",
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "total_chunks": int(chunk.get("total_chunks") or 0),
        "page": int(chunk["page"]) if chunk.get("page") is not None else None,
        "pdf_url": chunk.get("pdf_url") or "",
    }


def upsert_chunks(chunks: List[Dict[str, Any]], batch_size: int = 100) -> Dict[str, int]:
    """
    Insert or update chunks. Each chunk must have an 'embedding' field already.
    Returns counts: {'uploaded': N, 'failed': M}.
    """
    if not chunks:
        return {"uploaded": 0, "failed": 0}

    client = _get_search_client()
    uploaded = 0
    failed = 0

    for start in range(0, len(chunks), batch_size):
        batch_raw = chunks[start: start + batch_size]
        batch = [_prepare_chunk_for_upload(c) for c in batch_raw]
        try:
            results = client.merge_or_upload_documents(documents=batch)
            for r in results:
                if r.succeeded:
                    uploaded += 1
                else:
                    failed += 1
                    log.warning(f"Upload failed for {r.key}: {r.error_message}")
        except HttpResponseError as e:
            log.error(f"Batch upload failed: {e}")
            failed += len(batch)

    log.info(f"Upsert complete: {uploaded} uploaded, {failed} failed")
    return {"uploaded": uploaded, "failed": failed}


def delete_by_file_id(sharepoint_file_id: str) -> int:
    """
    Delete all chunks for a given SharePoint file.
    Called when a file is updated (delete old → upload new) or deleted.
    Returns count of chunks deleted.
    """
    if not sharepoint_file_id:
        return 0

    client = _get_search_client()
    # Find all chunks for this file
    results = client.search(
        search_text="*",
        filter=f"sharepoint_file_id eq '{_escape_odata(sharepoint_file_id)}'",
        select=["chunk_id"],
        top=1000,
    )
    chunk_ids = [r["chunk_id"] for r in results]

    if not chunk_ids:
        return 0

    docs = [{"chunk_id": cid} for cid in chunk_ids]
    client.delete_documents(documents=docs)
    log.info(f"Deleted {len(chunk_ids)} chunks for SharePoint file {sharepoint_file_id}")
    return len(chunk_ids)


def delete_by_filename(filename: str) -> int:
    """
    Delete all chunks by filename (used in local-folder mode where
    there's no SharePoint file ID).
    """
    if not filename:
        return 0

    client = _get_search_client()
    results = client.search(
        search_text="*",
        filter=f"filename eq '{_escape_odata(filename)}'",
        select=["chunk_id"],
        top=1000,
    )
    chunk_ids = [r["chunk_id"] for r in results]

    if not chunk_ids:
        return 0

    docs = [{"chunk_id": cid} for cid in chunk_ids]
    client.delete_documents(documents=docs)
    log.info(f"Deleted {len(chunk_ids)} chunks for filename '{filename}'")
    return len(chunk_ids)


def _escape_odata(s: str) -> str:
    """Escape single quotes for OData filter string literals."""
    return s.replace("'", "''")


# ═══════════════════════════════════════════════════════════
#  READ: hybrid search
# ═══════════════════════════════════════════════════════════

def hybrid_search(
    query: str,
    query_vector: List[float],
    top_k: int = 5,
    category: Optional[str] = None,
    include_uncategorized: bool = True,
    only_published: bool = True,
    extra_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Hybrid search: combines vector similarity with keyword (BM25) matching.

    Args:
        query: User's question (for keyword search).
        query_vector: Embedding of the query (for vector search).
        top_k: How many chunks to return.
        category: If set, restrict to this category (e.g. "IT", "HR").
                  When include_uncategorized=True, also includes Uncategorized.
        include_uncategorized: When filtering by category, also include
                               Uncategorized items (they might be relevant
                               but unlabeled).
        only_published: If True, restrict to status='Published'.
        extra_filter: Optional additional OData filter (e.g. exclude specific
                      chunk_ids for "alternative method" feature).

    Returns:
        List of chunk dicts ordered by relevance. Each has all metadata + 'score'.
    """
    client = _get_search_client()

    # Build OData filter
    filters: List[str] = []

    if only_published:
        # Match Published OR Approved (case-insensitive friendly approach: include both)
        filters.append("(status eq 'Published' or status eq 'Approved')")

    if category:
        cat_escaped = _escape_odata(category)
        if include_uncategorized:
            filters.append(
                f"(category eq '{cat_escaped}' or category eq 'Uncategorized')"
            )
        else:
            filters.append(f"category eq '{cat_escaped}'")

    if extra_filter:
        filters.append(f"({extra_filter})")

    filter_str = " and ".join(filters) if filters else None

    # Vector query (the dominant signal)
    vector_q = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=top_k * 3,  # over-fetch then re-rank
        fields="embedding",
    )

    base_select = [
        "chunk_id", "sharepoint_file_id", "filename", "doc_type",
        "text", "article_title", "summary", "category", "sub_category",
        "tags", "status", "author", "ai_citation_count",
        "helpful_count", "not_helpful_count", "source_ticket_id",
        "chunk_index", "total_chunks",
    ]
    full_select = base_select + ["page", "pdf_url"]

    try:
        results = client.search(
            search_text=query,                # keyword side (BM25)
            vector_queries=[vector_q],        # vector side
            filter=filter_str,
            top=top_k,
            select=full_select,
        )
    except HttpResponseError as e:
        message = str(e)
        if "Could not find a property named 'page'" in message or \
           "Could not find a property named 'pdf_url'" in message:
            log.warning(
                "Search index is missing page/pdf_url fields; retrying without them."
            )
            try:
                results = client.search(
                    search_text=query,
                    vector_queries=[vector_q],
                    filter=filter_str,
                    top=top_k,
                    select=base_select,
                )
            except HttpResponseError as retry_err:
                log.error(f"Search failed: {retry_err}")
                return []
        else:
            log.error(f"Search failed: {e}")
            return []

    output: List[Dict[str, Any]] = []
    for r in results:
        chunk = dict(r)  # SDK returns a dict-like; convert to plain dict
        chunk["score"] = r["@search.score"]
        # Remove the @-prefixed metadata fields the SDK adds
        for k in list(chunk.keys()):
            if k.startswith("@"):
                chunk.pop(k, None)
        output.append(chunk)

    return output


# ═══════════════════════════════════════════════════════════
#  FACETS / STATS — for /categories + /health endpoints
# ═══════════════════════════════════════════════════════════

def list_categories(only_published: bool = True) -> List[Dict[str, Any]]:
    """
    Return list of categories with document counts.
    Used by GET /categories endpoint (frontend renders buttons).
    """
    client = _get_search_client()
    filter_str = "(status eq 'Published' or status eq 'Approved')" if only_published else None

    try:
        results = client.search(
            search_text="*",
            filter=filter_str,
            facets=["category,count:50"],
            top=0,
        )
        # Force evaluation to get facets
        list(results)
        facets = results.get_facets() or {}
        category_facets = facets.get("category", []) or []
    except HttpResponseError as e:
        log.error(f"Facet query failed: {e}")
        return []

    # Each facet is {'value': 'IT', 'count': 42}
    categories = [
        {
            "name": f["value"],
            "display": f["value"],   # frontend can override with nicer label
            "chunk_count": f["count"],
        }
        for f in category_facets
        if f["value"]  # skip empty
    ]
    return categories


def get_index_stats() -> Dict[str, Any]:
    """
    Stats for /health endpoint.
    """
    idx_client = _get_index_client()
    try:
        stats = idx_client.get_index_statistics(settings.search_index)
        return {
            "index_name": settings.search_index,
            "document_count": stats["document_count"],
            "storage_size_bytes": stats["storage_size"],
        }
    except ResourceNotFoundError:
        return {
            "index_name": settings.search_index,
            "document_count": 0,
            "storage_size_bytes": 0,
            "note": "Index does not exist yet — run ensure_index().",
        }
    except HttpResponseError as e:
        log.error(f"Stats query failed: {e}")
        return {"error": str(e)}


def count_chunks_for_file(sharepoint_file_id: Optional[str] = None,
                          filename: Optional[str] = None) -> int:
    """
    Count chunks for a given file. Used by sync to detect 'already indexed'.
    """
    client = _get_search_client()
    if sharepoint_file_id:
        filter_str = f"sharepoint_file_id eq '{_escape_odata(sharepoint_file_id)}'"
    elif filename:
        filter_str = f"filename eq '{_escape_odata(filename)}'"
    else:
        return 0

    try:
        results = client.search(
            search_text="*",
            filter=filter_str,
            top=0,
            include_total_count=True,
        )
        list(results)  # force evaluation
        return results.get_count() or 0
    except HttpResponseError as e:
        log.error(f"Count query failed: {e}")
        return 0


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Smoke test against your live Azure AI Search instance.

    Usage:
        python -m storage.search_index ensure       # create the index
        python -m storage.search_index stats        # show index stats
        python -m storage.search_index categories   # list categories
        python -m storage.search_index drop         # delete index (careful!)
    """
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "ensure":
        result = ensure_index()
        print(f"  → {result}")
    elif cmd == "stats":
        import json
        print(json.dumps(get_index_stats(), indent=2))
    elif cmd == "categories":
        cats = list_categories()
        print(f"Categories ({len(cats)}):")
        for c in cats:
            print(f"  • {c['name']:20s} ({c['chunk_count']} chunks)")
    elif cmd == "drop":
        confirm = input(f"Delete index '{settings.search_index}'? Type 'yes': ")
        if confirm == "yes":
            delete_index()
            print("Index deleted.")
        else:
            print("Cancelled.")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
