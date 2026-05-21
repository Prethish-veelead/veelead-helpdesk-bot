# # # # """
# # # # app.py — Veelead Helpdesk RAG Bot, FastAPI server.

# # # # Endpoints:
# # # #     GET  /                       — health/info
# # # #     GET  /health                 — detailed status (stats, models, sync history)
# # # #     GET  /categories             — list categories with chunk counts (for frontend buttons)
# # # #     GET  /search.json?q=...      — main query endpoint
# # # #     POST /admin/reindex          — trigger sync now (background task)
# # # #     POST /admin/reset_sync       — force full re-sync next time

# # # # Authentication:
# # # #     All non-info endpoints require header: x-api-key: <API_KEY from .env>
# # # #     /health and / are public for monitoring.

# # # # Run locally:
# # # #     uvicorn app:app --reload --port 8000

# # # # Run in production (Azure App Service):
# # # #     uvicorn app:app --host 0.0.0.0 --port 8000
# # # # """

# # # # import logging
# # # # import re
# # # # from datetime import datetime, timezone
# # # # from typing import Optional, List, Dict, Any

# # # # from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
# # # # from fastapi.middleware.cors import CORSMiddleware
# # # # from pydantic import BaseModel, Field
# # # # from openai import AzureOpenAI

# # # # from config import settings, print_config_summary
# # # # from sources import get_source, DocumentRef, ChangeSet
# # # # from pipeline.extractors import extract_bytes
# # # # from pipeline.chunker import chunk_document
# # # # from pipeline.embedder import embed_one, embed_many, get_embedding_info
# # # # from pipeline.classifier import classify, get_classifier_info
# # # # from storage import search_index
# # # # from storage import cache
# # # # from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# # # # # ═══════════════════════════════════════════════════════════
# # # # #  LOGGING
# # # # # ═══════════════════════════════════════════════════════════
# # # # logging.basicConfig(
# # # #     level=getattr(logging, settings.log_level, logging.INFO),
# # # #     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
# # # # )
# # # # log = logging.getLogger("app")


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  LLM CLIENT (for answer generation)
# # # # # ═══════════════════════════════════════════════════════════
# # # # _gpt_client: Optional[AzureOpenAI] = None


# # # # def get_gpt_client() -> AzureOpenAI:
# # # #     global _gpt_client
# # # #     if _gpt_client is None:
# # # #         _gpt_client = AzureOpenAI(
# # # #             api_key=settings.gpt_api_key,
# # # #             api_version=settings.gpt_api_ver,
# # # #             azure_endpoint=settings.gpt_endpoint,
# # # #         )
# # # #     return _gpt_client


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  MODEL ROUTING — pick gpt-4o-mini vs gpt-4o
# # # # # ═══════════════════════════════════════════════════════════
# # # # COMPLEX_QUERY_RE = re.compile(
# # # #     r"\b(compare|difference|versus|vs|analy[sz]e|summari[sz]e|"
# # # #     r"explain why|step.by.step|multi(ple)?|across|between)\b", re.I
# # # # )
# # # # SMALL_TALK_RE = re.compile(
# # # #     r"^\s*(hi+|hello+|hey+|good\s+(morning|afternoon|evening)|"
# # # #     r"thanks|thank\s+you|how are you|what'?s up)\b[\s!.?]*$",
# # # #     re.I,
# # # # )


# # # # def pick_chat_model(question: str) -> str:
# # # #     """Choose gpt-4o-mini for typical queries, gpt-4o for complex ones."""
# # # #     if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
# # # #         return settings.gpt_large_deploy
# # # #     return settings.gpt_mini_deploy


# # # # def is_small_talk(question: str) -> bool:
# # # #     """Detect simple greetings and other short casual prompts."""
# # # #     q = question.strip().lower()
# # # #     return bool(SMALL_TALK_RE.match(q)) or q in {"hi", "hello", "hey"}


# # # # def generate_small_talk_response(question: str) -> tuple[str, str]:
# # # #     """
# # # #     Return a short friendly response for greetings and casual chat.
# # # #     Uses Azure OpenAI so the reply still feels natural.
# # # #     """
# # # #     try:
# # # #         client = get_gpt_client()
# # # #         resp = client.chat.completions.create(
# # # #             model=settings.gpt_mini_deploy,
# # # #             messages=[
# # # #                 {
# # # #                     "role": "system",
# # # #                     "content": (
# # # #                         "You are a friendly helpdesk assistant. "
# # # #                         "Respond naturally to greetings and brief casual chat. "
# # # #                         "Keep it short and invite the user to ask a work-related question."
# # # #                     ),
# # # #                 },
# # # #                 {"role": "user", "content": question},
# # # #             ],
# # # #             temperature=0.7,
# # # #             max_tokens=80,
# # # #             timeout=15,
# # # #         )
# # # #         return (resp.choices[0].message.content or "Hello! How can I help you today?", settings.gpt_mini_deploy)
# # # #     except Exception as e:
# # # #         log.warning(f"Small-talk LLM failed: {e}")
# # # #         return ("Hello! How can I help you today?", "fallback")


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  INDEXING PIPELINE
# # # # # ═══════════════════════════════════════════════════════════
# # # # def index_document(ref: DocumentRef) -> int:
# # # #     """
# # # #     Process one document: download → extract → chunk → embed → upload.
# # # #     If the doc already has chunks in the index, deletes them first.
# # # #     Returns count of chunks uploaded.
# # # #     """
# # # #     source = get_source()
# # # #     log.info(f"  → Indexing: {ref.filename}")

# # # #     # 1. Download
# # # #     data = source.download(ref)

# # # #     # 2. Extract text
# # # #     if data.pre_extracted_text:
# # # #         text = data.pre_extracted_text
# # # #     elif data.content_bytes:
# # # #         try:
# # # #             text = extract_bytes(data.content_bytes, ref.doc_type)
# # # #         except Exception as e:
# # # #             log.error(f"     ✗ Extraction failed for {ref.filename}: {e}")
# # # #             return 0
# # # #     else:
# # # #         log.error(f"     ✗ No content for {ref.filename}")
# # # #         return 0

# # # #     if not text or not text.strip():
# # # #         log.warning(f"     ⚠ Empty text for {ref.filename} — skipping")
# # # #         return 0

# # # #     # 3. Chunk + attach metadata
# # # #     doc_for_chunking = {**ref.to_doc_metadata(), "text": text}
# # # #     chunks = chunk_document(
# # # #         doc_for_chunking,
# # # #         chunk_size=settings.chunk_size,
# # # #         overlap=settings.chunk_overlap,
# # # #     )

# # # #     if not chunks:
# # # #         log.warning(f"     ⚠ No chunks produced for {ref.filename}")
# # # #         return 0

# # # #     # 4. Delete old chunks if any (idempotent — handles updates cleanly)
# # # #     if ref.file_id:
# # # #         search_index.delete_by_file_id(ref.file_id)

# # # #     # 5. Embed
# # # #     log.info(f"     • Embedding {len(chunks)} chunks...")
# # # #     texts = [c["text"] for c in chunks]
# # # #     vectors = embed_many(texts)
# # # #     for chunk, vec in zip(chunks, vectors):
# # # #         chunk["embedding"] = vec

# # # #     # 6. Upload to Azure AI Search
# # # #     result = search_index.upsert_chunks(chunks)
# # # #     log.info(f"     ✓ {result['uploaded']} uploaded, {result['failed']} failed")

# # # #     # 7. Invalidate cache for this file (in case of update/re-index)
# # # #     cache.cache_invalidate_by_file_id(ref.file_id)
# # # #     cache.cache_invalidate_by_filename(ref.filename)

# # # #     return result["uploaded"]


# # # # def run_sync(force_full: bool = False) -> Dict[str, Any]:
# # # #     """
# # # #     Run a sync cycle:
# # # #       - Get changes from source (delta sync, unless force_full)
# # # #       - Apply additions/updates/deletes
# # # #       - Save new delta token
# # # #       - Record audit log
# # # #     """
# # # #     started_at = datetime.now(timezone.utc)
# # # #     source = get_source()
# # # #     source_type_name = settings.source_type

# # # #     log.info("─" * 60)
# # # #     log.info(f"  SYNC START ({source.source_name()})")
# # # #     log.info("─" * 60)

# # # #     delta_token = None if force_full else cache.get_delta_token()
# # # #     try:
# # # #         changes: ChangeSet = source.get_changes(delta_token)
# # # #     except Exception as e:
# # # #         log.exception("Sync failed during get_changes()")
# # # #         cache.record_sync_run(
# # # #             source_type=source_type_name,
# # # #             started_at=started_at,
# # # #             finished_at=datetime.now(timezone.utc),
# # # #             status="failed",
# # # #             error_msg=str(e),
# # # #         )
# # # #         return {"status": "failed", "error": str(e)}

# # # #     if changes.is_empty():
# # # #         log.info("  No changes detected.")
# # # #         # Still save token so next sync knows where we are
# # # #         if changes.new_delta_token:
# # # #             cache.save_delta_token(changes.new_delta_token)
# # # #         finished_at = datetime.now(timezone.utc)
# # # #         run_id = cache.record_sync_run(
# # # #             source_type=source_type_name,
# # # #             started_at=started_at,
# # # #             finished_at=finished_at,
# # # #             added=0, updated=0, deleted=0,
# # # #             status="success",
# # # #         )
# # # #         return {
# # # #             "status": "success",
# # # #             "run_id": run_id,
# # # #             "added": 0, "updated": 0, "deleted": 0,
# # # #             "duration_sec": (finished_at - started_at).total_seconds(),
# # # #         }

# # # #     # ── DELETIONS first (clean state) ──
# # # #     deleted_count = 0
# # # #     for file_id in changes.deleted:
# # # #         try:
# # # #             n = search_index.delete_by_file_id(file_id)
# # # #             cache.cache_invalidate_by_file_id(file_id)
# # # #             deleted_count += 1
# # # #             log.info(f"  − Deleted file {file_id}: {n} chunks removed")
# # # #         except Exception as e:
# # # #             log.error(f"  ✗ Delete failed for {file_id}: {e}")

# # # #     # ── ADDITIONS + UPDATES ──
# # # #     added_count = 0
# # # #     updated_count = 0
# # # #     failed = 0

# # # #     for ref in changes.added:
# # # #         try:
# # # #             n = index_document(ref)
# # # #             if n > 0:
# # # #                 added_count += 1
# # # #         except Exception as e:
# # # #             log.error(f"  ✗ Add failed for {ref.filename}: {e}")
# # # #             failed += 1

# # # #     for ref in changes.updated:
# # # #         try:
# # # #             n = index_document(ref)
# # # #             if n > 0:
# # # #                 updated_count += 1
# # # #         except Exception as e:
# # # #             log.error(f"  ✗ Update failed for {ref.filename}: {e}")
# # # #             failed += 1

# # # #     # Save new delta token
# # # #     if changes.new_delta_token:
# # # #         cache.save_delta_token(changes.new_delta_token)

# # # #     finished_at = datetime.now(timezone.utc)
# # # #     status = "success" if failed == 0 else "partial"
# # # #     run_id = cache.record_sync_run(
# # # #         source_type=source_type_name,
# # # #         started_at=started_at,
# # # #         finished_at=finished_at,
# # # #         added=added_count,
# # # #         updated=updated_count,
# # # #         deleted=deleted_count,
# # # #         status=status,
# # # #         error_msg=f"{failed} items failed" if failed else None,
# # # #     )

# # # #     duration = (finished_at - started_at).total_seconds()
# # # #     log.info("─" * 60)
# # # #     log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
# # # #              f"({failed} failed, {duration:.1f}s)")
# # # #     log.info("─" * 60)

# # # #     return {
# # # #         "status": status,
# # # #         "run_id": run_id,
# # # #         "added": added_count,
# # # #         "updated": updated_count,
# # # #         "deleted": deleted_count,
# # # #         "failed": failed,
# # # #         "duration_sec": duration,
# # # #     }


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  ANSWER GENERATION
# # # # # ═══════════════════════════════════════════════════════════
# # # # SYSTEM_PROMPT_TEMPLATE = """You are an AI assistant for Veelead Solutions helpdesk.
# # # # Answer questions using ONLY the document context below.

# # # # Rules:
# # # # - Base your answer strictly on the context. Do not invent facts.
# # # # - If the answer is not in the context, say: "I could not find that in the available documents."
# # # # - Be concise and professional. Use bullet points for steps.
# # # # - When citing, reference the document name(s).

# # # # DOCUMENT CONTEXT:
# # # # {context}"""


# # # # def generate_answer(question: str, top_chunks: List[Dict[str, Any]]) -> tuple[str, str]:
# # # #     """
# # # #     Generate an answer using the LLM with retrieved context chunks.
# # # #     Returns (answer_text, model_used).
# # # #     """
# # # #     if not top_chunks:
# # # #         return ("I could not find that in the available documents.", "none")

# # # #     # Build context block
# # # #     context_parts = []
# # # #     for c in top_chunks:
# # # #         source_label = c.get("article_title") or c.get("filename") or "Unknown"
# # # #         context_parts.append(
# # # #             f"[Source: {source_label} | Category: {c.get('category', '?')} | "
# # # #             f"Relevance: {c.get('score', 0):.2f}]\n{c['text']}"
# # # #         )
# # # #     context = "\n\n---\n\n".join(context_parts)

# # # #     system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
# # # #     model = pick_chat_model(question)

# # # #     try:
# # # #         client = get_gpt_client()
# # # #         resp = client.chat.completions.create(
# # # #             model=model,
# # # #             messages=[
# # # #                 {"role": "system", "content": system_prompt},
# # # #                 {"role": "user", "content": question},
# # # #             ],
# # # #             temperature=0.2,
# # # #             max_tokens=800,
# # # #             timeout=30,
# # # #         )
# # # #         return (resp.choices[0].message.content or "", model)
# # # #     except Exception as e:
# # # #         log.error(f"LLM call failed: {e}")
# # # #         return (f"Sorry, I had trouble generating an answer. Error: {type(e).__name__}", model)


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  PYDANTIC RESPONSE MODELS
# # # # # ═══════════════════════════════════════════════════════════
# # # # class DocOut(BaseModel):
# # # #     rank: int
# # # #     chunk_id: str
# # # #     filename: Optional[str] = None
# # # #     article_title: Optional[str] = None
# # # #     category: Optional[str] = None
# # # #     sub_category: Optional[str] = None
# # # #     score: float
# # # #     text: str


# # # # class SearchResponse(BaseModel):
# # # #     q: str
# # # #     answer: str
# # # #     sources: List[str]
# # # #     numFound: int
# # # #     docs: List[DocOut]
# # # #     category_used: Optional[str] = None
# # # #     category_source: str = Field(
# # # #         ...,
# # # #         description="'user_selected' | 'ai_predicted' | 'fallback_all' | 'none'"
# # # #     )
# # # #     category_confidence: Optional[str] = None
# # # #     model_used: str
# # # #     cached: bool


# # # # class CategoryOut(BaseModel):
# # # #     name: str
# # # #     display: str
# # # #     chunk_count: int


# # # # class CategoriesResponse(BaseModel):
# # # #     categories: List[CategoryOut]


# # # # class HealthResponse(BaseModel):
# # # #     status: str
# # # #     source: str
# # # #     index: Dict[str, Any]
# # # #     cache: Dict[str, Any]
# # # #     sync: Dict[str, Any]
# # # #     scheduler: Dict[str, Any]
# # # #     embedding: Dict[str, Any]
# # # #     classifier: Dict[str, Any]


# # # # class AdminResponse(BaseModel):
# # # #     status: str
# # # #     message: str


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  FASTAPI APP
# # # # # ═══════════════════════════════════════════════════════════
# # # # app = FastAPI(
# # # #     title="Veelead Helpdesk RAG Bot",
# # # #     description="Cost-efficient SharePoint-aware helpdesk bot",
# # # #     version="1.0.0",
# # # # )

# # # # # CORS for frontend (lock down to specific origins in production if needed)
# # # # app.add_middleware(
# # # #     CORSMiddleware,
# # # #     allow_origins=["*"],
# # # #     allow_credentials=True,
# # # #     allow_methods=["*"],
# # # #     allow_headers=["*"],
# # # # )


# # # # def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
# # # #     """API key dependency. Allows blank header on default-key for local dev."""
# # # #     if not x_api_key:
# # # #         raise HTTPException(status_code=401, detail="Missing x-api-key header")
# # # #     if x_api_key != settings.api_key:
# # # #         raise HTTPException(status_code=401, detail="Invalid API key")
# # # #     return True


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  STARTUP
# # # # # ═══════════════════════════════════════════════════════════
# # # # @app.on_event("startup")
# # # # def startup():
# # # #     print_config_summary()
# # # #     log.info("═" * 60)
# # # #     log.info("  Veelead Helpdesk RAG Bot — starting")
# # # #     log.info("═" * 60)

# # # #     # Validate config
# # # #     issues = settings.validate()
# # # #     if issues:
# # # #         log.warning("Configuration issues found:")
# # # #         for issue in issues:
# # # #             log.warning(f"  ⚠ {issue}")

# # # #     # Init local DBs
# # # #     cache.init_db()

# # # #     # Ensure search index exists
# # # #     try:
# # # #         search_index.ensure_index()
# # # #     except Exception as e:
# # # #         log.error(f"Could not connect to Azure AI Search: {e}")
# # # #         log.error("API will start but searches will fail until this is fixed.")

# # # #     # Run an initial sync (non-blocking — only if delta token missing)
# # # #     if not cache.get_delta_token():
# # # #         log.info("No delta token found — running initial full sync...")
# # # #         try:
# # # #             run_sync(force_full=True)
# # # #         except Exception as e:
# # # #             log.exception("Initial sync failed (will retry on next scheduled run)")

# # # #     # Start background scheduler for periodic sync + cleanup
# # # #     try:
# # # #         start_scheduler()
# # # #     except Exception as e:
# # # #         log.exception("Failed to start scheduler — background sync disabled")

# # # #     log.info("═" * 60)
# # # #     log.info("  ✅ API READY")
# # # #     log.info(f"  Endpoint: {settings.embed_endpoint}")
# # # #     log.info(f"  Source: {settings.source_type}")
# # # #     log.info("═" * 60)


# # # # @app.on_event("shutdown")
# # # # def shutdown():
# # # #     """Gracefully stop the scheduler when FastAPI shuts down."""
# # # #     log.info("Shutting down...")
# # # #     try:
# # # #         stop_scheduler()
# # # #     except Exception:
# # # #         pass


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  ROOT + HEALTH
# # # # # ═══════════════════════════════════════════════════════════
# # # # @app.get("/")
# # # # def root():
# # # #     return {
# # # #         "service": "Veelead Helpdesk RAG Bot",
# # # #         "version": "1.0.0",
# # # #         "endpoints": {
# # # #             "GET /search.json?q=<question>": "main search endpoint (auth required)",
# # # #             "GET /categories": "list categories with counts (auth required)",
# # # #             "GET /health": "detailed status (public)",
# # # #             "POST /admin/reindex": "trigger sync now (auth required)",
# # # #             "POST /admin/reset_sync": "force full re-sync next time (auth required)",
# # # #         },
# # # #         "frontend": "Send 'x-api-key' header on every authenticated request",
# # # #     }


# # # # @app.get("/health", response_model=HealthResponse)
# # # # def health():
# # # #     try:
# # # #         src = get_source()
# # # #         src_name = src.source_name()
# # # #     except Exception as e:
# # # #         src_name = f"<error: {e}>"

# # # #     return HealthResponse(
# # # #         status="ok",
# # # #         source=src_name,
# # # #         index=search_index.get_index_stats(),
# # # #         cache=cache.cache_stats(),
# # # #         sync=cache.sync_state_stats(),
# # # #         scheduler=get_scheduler_status(),
# # # #         embedding=get_embedding_info(),
# # # #         classifier=get_classifier_info(),
# # # #     )


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  CATEGORIES (for frontend buttons)
# # # # # ═══════════════════════════════════════════════════════════
# # # # @app.get("/categories", response_model=CategoriesResponse)
# # # # def categories(_auth: bool = Depends(verify_api_key)):
# # # #     cats = search_index.list_categories(only_published=True)
# # # #     # Convert chunk_count to a more user-friendly count if needed
# # # #     return CategoriesResponse(
# # # #         categories=[
# # # #             CategoryOut(
# # # #                 name=c["name"],
# # # #                 display=c.get("display") or c["name"],
# # # #                 chunk_count=c["chunk_count"],
# # # #             )
# # # #             for c in cats
# # # #         ]
# # # #     )


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  MAIN SEARCH ENDPOINT
# # # # # ═══════════════════════════════════════════════════════════
# # # # @app.get("/search.json", response_model=SearchResponse)
# # # # def search(
# # # #     q: str = Query(..., description="User question", min_length=1),
# # # #     category: Optional[str] = Query(
# # # #         None,
# # # #         description="Optional category selected by user (HR, IT, Facilities, General)"
# # # #     ),
# # # #     _auth: bool = Depends(verify_api_key),
# # # # ):
# # # #     question = q.strip()
# # # #     if not question:
# # # #         raise HTTPException(status_code=400, detail="Query 'q' is required")

# # # #     # Short greetings and casual chat should not go through document retrieval.
# # # #     if is_small_talk(question):
# # # #         answer, model_used = generate_small_talk_response(question)
# # # #         return SearchResponse(
# # # #             q=question,
# # # #             answer=answer,
# # # #             sources=[],
# # # #             numFound=0,
# # # #             docs=[],
# # # #             category_used="Uncategorized",
# # # #             category_source="none",
# # # #             category_confidence="low",
# # # #             model_used=model_used,
# # # #             cached=False,
# # # #         )

# # # #     # ── 1. Cache lookup ──
# # # #     cached_resp = cache.cache_lookup(question)
# # # #     if cached_resp:
# # # #         log.info(f"💰 Cache HIT: {question[:60]}")
# # # #         cached_resp["cached"] = True
# # # #         # Backfill any new required fields the cached entry lacks
# # # #         cached_resp.setdefault("category_source", "cached")
# # # #         return SearchResponse(**cached_resp)

# # # #     # ── 2. Determine category ──
# # # #     # Get the live list of categories that actually exist in the index
# # # #     all_categories = [c["name"] for c in search_index.list_categories(only_published=True)]

# # # #     if category:
# # # #         # User-selected: validate it exists (case-insensitive)
# # # #         cat_match = next((c for c in all_categories
# # # #                           if c.lower() == category.lower()), None)
# # # #         if not cat_match:
# # # #             log.warning(f"User selected unknown category '{category}'. "
# # # #                         f"Available: {all_categories}")
# # # #             # Fall through to AI-predicted
# # # #             cat_match = None

# # # #         if cat_match:
# # # #             used_category = cat_match
# # # #             category_source = "user_selected"
# # # #             category_confidence = "high"
# # # #         else:
# # # #             classification = classify(question, all_categories)
# # # #             used_category = classification["category"]
# # # #             category_source = "ai_predicted"
# # # #             category_confidence = classification["confidence"]
# # # #     else:
# # # #         # No category specified — let the classifier decide
# # # #         classification = classify(question, all_categories)
# # # #         used_category = classification["category"]
# # # #         category_source = "ai_predicted"
# # # #         category_confidence = classification["confidence"]

# # # #     # If category is Uncategorized, search WITHOUT category filter
# # # #     # (Uncategorized is included automatically alongside any selected category)
# # # #     search_category = None if used_category == "Uncategorized" else used_category

# # # #     # ── 3. Embed the query + hybrid search ──
# # # #     try:
# # # #         query_vec = embed_one(question)
# # # #     except Exception as e:
# # # #         log.error(f"Query embedding failed: {e}")
# # # #         raise HTTPException(status_code=500, detail="Query embedding service unavailable")

# # # #     chunks = search_index.hybrid_search(
# # # #         query=question,
# # # #         query_vector=query_vec,
# # # #         top_k=settings.top_k_use,
# # # #         category=search_category,
# # # #         include_uncategorized=True,
# # # #         only_published=True,
# # # #     )

# # # #     # ── 4. Fallback: if no results and category was applied, retry without it ──
# # # #     if not chunks and search_category:
# # # #         log.info(f"  No results in {search_category}. Falling back to all categories.")
# # # #         chunks = search_index.hybrid_search(
# # # #             query=question,
# # # #             query_vector=query_vec,
# # # #             top_k=settings.top_k_use,
# # # #             category=None,
# # # #             only_published=True,
# # # #         )
# # # #         category_source = "fallback_all"

# # # #     # ── 5. Generate answer ──
# # # #     answer, model_used = generate_answer(question, chunks)

# # # #     # ── 6. Build response ──
# # # #     docs_out = [
# # # #         DocOut(
# # # #             rank=i + 1,
# # # #             chunk_id=c["chunk_id"],
# # # #             filename=c.get("filename"),
# # # #             article_title=c.get("article_title"),
# # # #             category=c.get("category"),
# # # #             sub_category=c.get("sub_category"),
# # # #             score=round(float(c.get("score", 0)), 6),
# # # #             text=c["text"],
# # # #         )
# # # #         for i, c in enumerate(chunks)
# # # #     ]

# # # #     # Deduplicate sources: prefer article_title, fallback to filename
# # # #     seen = set()
# # # #     sources: List[str] = []
# # # #     for c in chunks:
# # # #         label = c.get("article_title") or c.get("filename")
# # # #         if label and label not in seen:
# # # #             seen.add(label)
# # # #             sources.append(label)

# # # #     response = SearchResponse(
# # # #         q=question,
# # # #         answer=answer,
# # # #         sources=sources,
# # # #         numFound=len(docs_out),
# # # #         docs=docs_out,
# # # #         category_used=used_category,
# # # #         category_source=category_source,
# # # #         category_confidence=category_confidence,
# # # #         model_used=model_used,
# # # #         cached=False,
# # # #     )

# # # #     # ── 7. Cache the response (skips empty/no-answer answers automatically) ──
# # # #     cache.cache_store(question, response.model_dump())

# # # #     return response


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  ADMIN ENDPOINTS
# # # # # ═══════════════════════════════════════════════════════════
# # # # @app.post("/admin/reindex", response_model=AdminResponse)
# # # # def admin_reindex(
# # # #     background_tasks: BackgroundTasks,
# # # #     force_full: bool = Query(False, description="Force a full re-sync (ignore delta token)"),
# # # #     _auth: bool = Depends(verify_api_key),
# # # # ):
# # # #     """Trigger a sync now. Runs in background — endpoint returns immediately."""
# # # #     background_tasks.add_task(run_sync, force_full=force_full)
# # # #     msg = "full re-sync" if force_full else "delta sync"
# # # #     return AdminResponse(status="started", message=f"{msg} started in background")


# # # # @app.post("/admin/reset_sync", response_model=AdminResponse)
# # # # def admin_reset_sync(_auth: bool = Depends(verify_api_key)):
# # # #     """Clear the delta token. Next sync will be a full sync."""
# # # #     cache.reset_delta_token()
# # # #     return AdminResponse(
# # # #         status="ok",
# # # #         message="Delta token cleared. Next sync will be a full sync."
# # # #     )


# # # # # ═══════════════════════════════════════════════════════════
# # # # #  ENTRY POINT (for `python app.py`)
# # # # # ═══════════════════════════════════════════════════════════
# # # # if __name__ == "__main__":
# # # #     import uvicorn
# # # #     uvicorn.run(app, host="0.0.0.0", port=8000)


# # # """
# # # app.py — Veelead Helpdesk RAG Bot, FastAPI server.

# # # Endpoints:
# # #     GET  /                       — health/info
# # #     GET  /health                 — detailed status (stats, models, sync history)
# # #     GET  /categories             — list categories with chunk counts (for frontend buttons)
# # #     GET  /search.json?q=...      — main query endpoint
# # #     POST /admin/reindex          — trigger sync now (background task)
# # #     POST /admin/reset_sync       — force full re-sync next time

# # # Authentication:
# # #     All non-info endpoints require header: x-api-key: <API_KEY from .env>
# # #     /health and / are public for monitoring.

# # # Run locally:
# # #     uvicorn app:app --reload --port 8000

# # # Run in production (Azure App Service):
# # #     uvicorn app:app --host 0.0.0.0 --port 8000
# # # """

# # # import logging
# # # from datetime import datetime, timezone
# # # from typing import Optional, List, Dict, Any

# # # from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
# # # from fastapi.middleware.cors import CORSMiddleware
# # # from pydantic import BaseModel, Field
# # # from openai import AzureOpenAI

# # # from config import settings, print_config_summary
# # # from sources import get_source, DocumentRef, ChangeSet
# # # from pipeline.extractors import extract_bytes, extract_with_pages
# # # from pipeline.chunker import chunk_document
# # # from pipeline.embedder import embed_one, embed_many, get_embedding_info
# # # from pipeline.classifier import classify, get_classifier_info
# # # from storage import search_index
# # # from storage import cache
# # # from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# # # # ═══════════════════════════════════════════════════════════
# # # #  LOGGING
# # # # ═══════════════════════════════════════════════════════════
# # # logging.basicConfig(
# # #     level=getattr(logging, settings.log_level, logging.INFO),
# # #     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
# # # )
# # # log = logging.getLogger("app")


# # # # ═══════════════════════════════════════════════════════════
# # # #  LLM CLIENT (for answer generation)
# # # # ═══════════════════════════════════════════════════════════
# # # _gpt_client: Optional[AzureOpenAI] = None


# # # def get_gpt_client() -> AzureOpenAI:
# # #     global _gpt_client
# # #     if _gpt_client is None:
# # #         _gpt_client = AzureOpenAI(
# # #             api_key=settings.gpt_api_key,
# # #             api_version=settings.gpt_api_ver,
# # #             azure_endpoint=settings.gpt_endpoint,
# # #         )
# # #     return _gpt_client


# # # # ═══════════════════════════════════════════════════════════
# # # #  MODEL ROUTING — pick gpt-4o-mini vs gpt-4o
# # # # ═══════════════════════════════════════════════════════════
# # # import re
# # # COMPLEX_QUERY_RE = re.compile(
# # #     r"\b(compare|difference|versus|vs|analy[sz]e|summari[sz]e|"
# # #     r"explain why|step.by.step|multi(ple)?|across|between)\b", re.I
# # # )
# # # SMALL_TALK_RE = re.compile(
# # #     r"^\s*(hi+|hello+|hey+|good\s+(morning|afternoon|evening)|"
# # #     r"thanks|thank\s+you|how are you|what'?s up)\b[\s!.?]*$",
# # #     re.I,
# # # )


# # # def pick_chat_model(question: str) -> str:
# # #     """Choose gpt-4o-mini for typical queries, gpt-4o for complex ones."""
# # #     if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
# # #         return settings.gpt_large_deploy
# # #     return settings.gpt_mini_deploy


# # # def is_small_talk(question: str) -> bool:
# # #     """Detect simple greetings and other short casual prompts."""
# # #     q = question.strip().lower()
# # #     return bool(SMALL_TALK_RE.match(q)) or q in {"hi", "hello", "hey"}


# # # def generate_small_talk_response(question: str) -> tuple[str, str]:
# # #     """
# # #     Return a short friendly response for greetings and casual chat.
# # #     Uses Azure OpenAI so the reply still feels natural.
# # #     """
# # #     try:
# # #         client = get_gpt_client()
# # #         resp = client.chat.completions.create(
# # #             model=settings.gpt_mini_deploy,
# # #             messages=[
# # #                 {
# # #                     "role": "system",
# # #                     "content": (
# # #                         "You are a friendly helpdesk assistant. "
# # #                         "Respond naturally to greetings and brief casual chat. "
# # #                         "Keep it short and invite the user to ask a work-related question."
# # #                     ),
# # #                 },
# # #                 {"role": "user", "content": question},
# # #             ],
# # #             temperature=0.7,
# # #             max_tokens=80,
# # #             timeout=15,
# # #         )
# # #         return (
# # #             resp.choices[0].message.content or "Hello! How can I help you today?",
# # #             settings.gpt_mini_deploy,
# # #         )
# # #     except Exception as e:
# # #         log.warning(f"Small-talk LLM failed: {e}")
# # #         return ("Hello! How can I help you today?", "fallback")


# # # # ═══════════════════════════════════════════════════════════
# # # #  INDEXING PIPELINE
# # # # ═══════════════════════════════════════════════════════════
# # # def index_document(ref: DocumentRef) -> int:
# # #     """
# # #     Process one document: download → extract → chunk → embed → upload.
# # #     If the doc already has chunks in the index, deletes them first.
# # #     Returns count of chunks uploaded.
# # #     """
# # #     source = get_source()
# # #     log.info(f"  → Indexing: {ref.filename}")

# # #     # 1. Download
# # #     data = source.download(ref)

# # #     # 2. Extract text — page-aware when possible (PDF)
# # #     pages_data = None
# # #     if data.pre_extracted_text:
# # #         text = data.pre_extracted_text
# # #     elif data.content_bytes:
# # #         try:
# # #             pages_data = extract_with_pages(data.content_bytes, ref.doc_type)
# # #             text = "\n\n".join(p[0] for p in pages_data) if pages_data else ""
# # #         except Exception as e:
# # #             log.error(f"     ✗ Extraction failed for {ref.filename}: {e}")
# # #             return 0
# # #     else:
# # #         log.error(f"     ✗ No content for {ref.filename}")
# # #         return 0

# # #     if not text or not text.strip():
# # #         log.warning(f"     ⚠ Empty text for {ref.filename} — skipping")
# # #         return 0

# # #     # 3. Chunk + attach metadata (with page info if available)
# # #     doc_for_chunking = {
# # #         **ref.to_doc_metadata(),
# # #         "text": text,
# # #         "pages": pages_data,  # may be None or list of (text, page) tuples
# # #     }
# # #     chunks = chunk_document(
# # #         doc_for_chunking,
# # #         chunk_size=settings.chunk_size,
# # #         overlap=settings.chunk_overlap,
# # #     )

# # #     if not chunks:
# # #         log.warning(f"     ⚠ No chunks produced for {ref.filename}")
# # #         return 0

# # #     # 4. Delete old chunks if any (idempotent — handles updates cleanly)
# # #     if ref.file_id:
# # #         search_index.delete_by_file_id(ref.file_id)

# # #     # 5. Embed
# # #     log.info(f"     • Embedding {len(chunks)} chunks...")
# # #     texts = [c["text"] for c in chunks]
# # #     vectors = embed_many(texts)
# # #     for chunk, vec in zip(chunks, vectors):
# # #         chunk["embedding"] = vec

# # #     # 6. Upload to Azure AI Search
# # #     result = search_index.upsert_chunks(chunks)
# # #     log.info(f"     ✓ {result['uploaded']} uploaded, {result['failed']} failed")

# # #     # 7. Invalidate cache for this file (in case of update/re-index)
# # #     cache.cache_invalidate_by_file_id(ref.file_id)
# # #     cache.cache_invalidate_by_filename(ref.filename)

# # #     return result["uploaded"]


# # # def run_sync(force_full: bool = False) -> Dict[str, Any]:
# # #     """
# # #     Run a sync cycle:
# # #       - Get changes from source (delta sync, unless force_full)
# # #       - Apply additions/updates/deletes
# # #       - Save new delta token
# # #       - Record audit log
# # #     """
# # #     started_at = datetime.now(timezone.utc)
# # #     source = get_source()
# # #     source_type_name = settings.source_type

# # #     log.info("─" * 60)
# # #     log.info(f"  SYNC START ({source.source_name()})")
# # #     log.info("─" * 60)

# # #     delta_token = None if force_full else cache.get_delta_token()
# # #     try:
# # #         changes: ChangeSet = source.get_changes(delta_token)
# # #     except Exception as e:
# # #         log.exception("Sync failed during get_changes()")
# # #         cache.record_sync_run(
# # #             source_type=source_type_name,
# # #             started_at=started_at,
# # #             finished_at=datetime.now(timezone.utc),
# # #             status="failed",
# # #             error_msg=str(e),
# # #         )
# # #         return {"status": "failed", "error": str(e)}

# # #     if changes.is_empty():
# # #         log.info("  No changes detected.")
# # #         # Still save token so next sync knows where we are
# # #         if changes.new_delta_token:
# # #             cache.save_delta_token(changes.new_delta_token)
# # #         finished_at = datetime.now(timezone.utc)
# # #         run_id = cache.record_sync_run(
# # #             source_type=source_type_name,
# # #             started_at=started_at,
# # #             finished_at=finished_at,
# # #             added=0, updated=0, deleted=0,
# # #             status="success",
# # #         )
# # #         return {
# # #             "status": "success",
# # #             "run_id": run_id,
# # #             "added": 0, "updated": 0, "deleted": 0,
# # #             "duration_sec": (finished_at - started_at).total_seconds(),
# # #         }

# # #     # ── DELETIONS first (clean state) ──
# # #     deleted_count = 0
# # #     for file_id in changes.deleted:
# # #         try:
# # #             n = search_index.delete_by_file_id(file_id)
# # #             cache.cache_invalidate_by_file_id(file_id)
# # #             deleted_count += 1
# # #             log.info(f"  − Deleted file {file_id}: {n} chunks removed")
# # #         except Exception as e:
# # #             log.error(f"  ✗ Delete failed for {file_id}: {e}")

# # #     # ── ADDITIONS + UPDATES ──
# # #     added_count = 0
# # #     updated_count = 0
# # #     failed = 0

# # #     for ref in changes.added:
# # #         try:
# # #             n = index_document(ref)
# # #             if n > 0:
# # #                 added_count += 1
# # #         except Exception as e:
# # #             log.error(f"  ✗ Add failed for {ref.filename}: {e}")
# # #             failed += 1

# # #     for ref in changes.updated:
# # #         try:
# # #             n = index_document(ref)
# # #             if n > 0:
# # #                 updated_count += 1
# # #         except Exception as e:
# # #             log.error(f"  ✗ Update failed for {ref.filename}: {e}")
# # #             failed += 1

# # #     # Save new delta token
# # #     if changes.new_delta_token:
# # #         cache.save_delta_token(changes.new_delta_token)

# # #     finished_at = datetime.now(timezone.utc)
# # #     status = "success" if failed == 0 else "partial"
# # #     run_id = cache.record_sync_run(
# # #         source_type=source_type_name,
# # #         started_at=started_at,
# # #         finished_at=finished_at,
# # #         added=added_count,
# # #         updated=updated_count,
# # #         deleted=deleted_count,
# # #         status=status,
# # #         error_msg=f"{failed} items failed" if failed else None,
# # #     )

# # #     duration = (finished_at - started_at).total_seconds()
# # #     log.info("─" * 60)
# # #     log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
# # #              f"({failed} failed, {duration:.1f}s)")
# # #     log.info("─" * 60)

# # #     return {
# # #         "status": status,
# # #         "run_id": run_id,
# # #         "added": added_count,
# # #         "updated": updated_count,
# # #         "deleted": deleted_count,
# # #         "failed": failed,
# # #         "duration_sec": duration,
# # #     }


# # # # ═══════════════════════════════════════════════════════════
# # # #  ANSWER GENERATION + FOLLOW-UPS (single LLM call)
# # # # ═══════════════════════════════════════════════════════════
# # # SYSTEM_PROMPT_TEMPLATE = """You are a Helpdesk Assistant for Veelead Solutions.
# # # Answer questions strictly from the document context provided.

# # # Return ONLY a JSON object with this exact shape (no markdown, no extra text):

# # # {{
# # #   "answer": "A clear, helpful answer in 2-5 sentences. Direct and actionable. Friendly and professional, written for non-technical employees.",
# # #   "suggested_followups": [
# # #     "Short specific follow-up question 1",
# # #     "Short specific follow-up question 2",
# # #     "Short specific follow-up question 3"
# # #   ]
# # # }}

# # # Rules:
# # # - Use only information from the context below. Never invent facts.
# # # - If the context doesn't contain the answer, set "answer" to:
# # #   "I could not find that in the available documents. Please check with your helpdesk or rephrase your question."
# # # - Keep "suggested_followups" to 2-3 short, specific questions related to the topic.
# # # - Do not include the document filename or source in the answer text — that goes separately.

# # # DOCUMENT CONTEXT:
# # # {context}"""


# # # def compute_confidence(chunks: List[Dict[str, Any]]) -> float:
# # #     """
# # #     Compute a 0.0-1.0 confidence score from chunk relevance scores.

# # #     Heuristic:
# # #       - Take top chunk's score, normalize to 0-1 (Azure hybrid scores ~0-5 typical)
# # #       - Boost if multiple chunks have reasonable scores
# # #       - Cap at 0.95 (never claim 100% certainty)
# # #     """
# # #     if not chunks:
# # #         return 0.0

# # #     scores = [float(c.get("score", 0)) for c in chunks]
# # #     top = max(scores)

# # #     # Azure AI Search hybrid scores: typical max ~3-5 for very good matches.
# # #     # Normalize: top=0.5 → 0.30; top=2.0 → 0.75; top=4.0 → 0.95
# # #     normalized = min(top / 4.5, 1.0)

# # #     # Boost slightly if 2+ chunks have decent scores (corroboration)
# # #     decent_count = sum(1 for s in scores if s > 0.5)
# # #     boost = 0.05 if decent_count >= 2 else 0.0

# # #     return round(min(normalized + boost, 0.95), 2)


# # # def generate_answer_and_followups(
# # #     question: str,
# # #     top_chunks: List[Dict[str, Any]]
# # # ) -> tuple[str, List[str], str]:
# # #     """
# # #     Call the LLM ONCE to produce both the answer text and 2-3 follow-up suggestions.
# # #     Returns (answer_text, followups_list, model_used).
# # #     """
# # #     if not top_chunks:
# # #         return (
# # #             "I could not find that in the available documents. "
# # #             "Please check with your helpdesk or rephrase your question.",
# # #             [],
# # #             "none",
# # #         )

# # #     # Build context block
# # #     context_parts = []
# # #     for c in top_chunks:
# # #         source_label = c.get("article_title") or c.get("filename") or "Unknown"
# # #         context_parts.append(
# # #             f"[Source: {source_label} | Category: {c.get('category', '?')} | "
# # #             f"Relevance: {c.get('score', 0):.2f}]\n{c['text']}"
# # #         )
# # #     context = "\n\n---\n\n".join(context_parts)

# # #     system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
# # #     model = pick_chat_model(question)

# # #     try:
# # #         client = get_gpt_client()
# # #         resp = client.chat.completions.create(
# # #             model=model,
# # #             messages=[
# # #                 {"role": "system", "content": system_prompt},
# # #                 {"role": "user", "content": question},
# # #             ],
# # #             temperature=0.2,
# # #             max_tokens=900,
# # #             response_format={"type": "json_object"},
# # #             timeout=30,
# # #         )
# # #         content = resp.choices[0].message.content or "{}"
# # #         import json as _json
# # #         parsed = _json.loads(content)
# # #         answer = (parsed.get("answer") or "").strip()
# # #         followups = parsed.get("suggested_followups") or []
# # #         # Validate followups
# # #         if not isinstance(followups, list):
# # #             followups = []
# # #         followups = [str(f).strip() for f in followups if f][:3]
# # #         if not answer:
# # #             answer = "I could not find that in the available documents."
# # #         return (answer, followups, model)
# # #     except Exception as e:
# # #         log.error(f"LLM call failed: {e}")
# # #         return (
# # #             f"Sorry, I had trouble generating an answer. ({type(e).__name__})",
# # #             [],
# # #             model,
# # #         )


# # # # ═══════════════════════════════════════════════════════════
# # # #  PYDANTIC RESPONSE MODELS
# # # # ═══════════════════════════════════════════════════════════
# # # class ChunkOut(BaseModel):
# # #     text: str
# # #     filename: str
# # #     pdf_url: Optional[str] = None
# # #     score: float
# # #     page: Optional[int] = None
# # #     # Bonus fields (frontend can ignore)
# # #     article_title: Optional[str] = None
# # #     category: Optional[str] = None
# # #     sub_category: Optional[str] = None
# # #     chunk_id: Optional[str] = None


# # # class SearchResponse(BaseModel):
# # #     answer: str
# # #     confidence: float = Field(..., ge=0.0, le=1.0)
# # #     model_used: str
# # #     cached: bool
# # #     numFound: int
# # #     sources: List[str]
# # #     chunks: List[ChunkOut]
# # #     suggested_followups: List[str]
# # #     # Bonus / debug fields (frontend can ignore — useful for category badge UI)
# # #     q: Optional[str] = None
# # #     category_used: Optional[str] = None
# # #     category_source: Optional[str] = None
# # #     category_confidence: Optional[str] = None


# # # class CategoryOut(BaseModel):
# # #     name: str
# # #     display: str
# # #     chunk_count: int


# # # class CategoriesResponse(BaseModel):
# # #     categories: List[CategoryOut]


# # # class HealthResponse(BaseModel):
# # #     status: str
# # #     source: str
# # #     index: Dict[str, Any]
# # #     cache: Dict[str, Any]
# # #     sync: Dict[str, Any]
# # #     scheduler: Dict[str, Any]
# # #     embedding: Dict[str, Any]
# # #     classifier: Dict[str, Any]


# # # class AdminResponse(BaseModel):
# # #     status: str
# # #     message: str


# # # # ═══════════════════════════════════════════════════════════
# # # #  FASTAPI APP
# # # # ═══════════════════════════════════════════════════════════
# # # app = FastAPI(
# # #     title="Veelead Helpdesk RAG Bot",
# # #     description="Cost-efficient SharePoint-aware helpdesk bot",
# # #     version="1.0.0",
# # # )

# # # # CORS for frontend (lock down to specific origins in production if needed)
# # # app.add_middleware(
# # #     CORSMiddleware,
# # #     allow_origins=["*"],
# # #     allow_credentials=True,
# # #     allow_methods=["*"],
# # #     allow_headers=["*"],
# # # )


# # # def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
# # #     """API key dependency. Allows blank header on default-key for local dev."""
# # #     if not x_api_key:
# # #         raise HTTPException(status_code=401, detail="Missing x-api-key header")
# # #     if x_api_key != settings.api_key:
# # #         raise HTTPException(status_code=401, detail="Invalid API key")
# # #     return True


# # # # ═══════════════════════════════════════════════════════════
# # # #  STARTUP
# # # # ═══════════════════════════════════════════════════════════
# # # @app.on_event("startup")
# # # def startup():
# # #     print_config_summary()
# # #     log.info("═" * 60)
# # #     log.info("  Veelead Helpdesk RAG Bot — starting")
# # #     log.info("═" * 60)

# # #     # Validate config
# # #     issues = settings.validate()
# # #     if issues:
# # #         log.warning("Configuration issues found:")
# # #         for issue in issues:
# # #             log.warning(f"  ⚠ {issue}")

# # #     # Init local DBs
# # #     cache.init_db()

# # #     # Ensure search index exists
# # #     try:
# # #         search_index.ensure_index()
# # #     except Exception as e:
# # #         log.error(f"Could not connect to Azure AI Search: {e}")
# # #         log.error("API will start but searches will fail until this is fixed.")

# # #     # Run an initial sync (non-blocking — only if delta token missing)
# # #     if not cache.get_delta_token():
# # #         log.info("No delta token found — running initial full sync...")
# # #         try:
# # #             run_sync(force_full=True)
# # #         except Exception as e:
# # #             log.exception("Initial sync failed (will retry on next scheduled run)")

# # #     # Start background scheduler for periodic sync + cleanup
# # #     try:
# # #         start_scheduler()
# # #     except Exception as e:
# # #         log.exception("Failed to start scheduler — background sync disabled")

# # #     log.info("═" * 60)
# # #     log.info("  ✅ API READY")
# # #     log.info(f"  Endpoint: {settings.embed_endpoint}")
# # #     log.info(f"  Source: {settings.source_type}")
# # #     log.info("═" * 60)


# # # @app.on_event("shutdown")
# # # def shutdown():
# # #     """Gracefully stop the scheduler when FastAPI shuts down."""
# # #     log.info("Shutting down...")
# # #     try:
# # #         stop_scheduler()
# # #     except Exception:
# # #         pass


# # # # ═══════════════════════════════════════════════════════════
# # # #  ROOT + HEALTH
# # # # ═══════════════════════════════════════════════════════════
# # # @app.get("/")
# # # def root():
# # #     return {
# # #         "service": "Veelead Helpdesk RAG Bot",
# # #         "version": "1.0.0",
# # #         "endpoints": {
# # #             "GET /search.json?q=<question>": "main search endpoint (auth required)",
# # #             "GET /categories": "list categories with counts (auth required)",
# # #             "GET /health": "detailed status (public)",
# # #             "POST /admin/reindex": "trigger sync now (auth required)",
# # #             "POST /admin/reset_sync": "force full re-sync next time (auth required)",
# # #         },
# # #         "frontend": "Send 'x-api-key' header on every authenticated request",
# # #     }


# # # @app.get("/health", response_model=HealthResponse)
# # # def health():
# # #     try:
# # #         src = get_source()
# # #         src_name = src.source_name()
# # #     except Exception as e:
# # #         src_name = f"<error: {e}>"

# # #     return HealthResponse(
# # #         status="ok",
# # #         source=src_name,
# # #         index=search_index.get_index_stats(),
# # #         cache=cache.cache_stats(),
# # #         sync=cache.sync_state_stats(),
# # #         scheduler=get_scheduler_status(),
# # #         embedding=get_embedding_info(),
# # #         classifier=get_classifier_info(),
# # #     )


# # # # ═══════════════════════════════════════════════════════════
# # # #  CATEGORIES (for frontend buttons)
# # # # ═══════════════════════════════════════════════════════════
# # # @app.get("/categories", response_model=CategoriesResponse)
# # # def categories(_auth: bool = Depends(verify_api_key)):
# # #     cats = search_index.list_categories(only_published=True)
# # #     # Convert chunk_count to a more user-friendly count if needed
# # #     return CategoriesResponse(
# # #         categories=[
# # #             CategoryOut(
# # #                 name=c["name"],
# # #                 display=c.get("display") or c["name"],
# # #                 chunk_count=c["chunk_count"],
# # #             )
# # #             for c in cats
# # #         ]
# # #     )


# # # # ═══════════════════════════════════════════════════════════
# # # #  MAIN SEARCH ENDPOINT
# # # # ═══════════════════════════════════════════════════════════
# # # @app.get("/search.json", response_model=SearchResponse)
# # # def search(
# # #     q: str = Query(..., description="User question", min_length=1),
# # #     category: Optional[str] = Query(
# # #         None,
# # #         description="Optional category selected by user (HR, IT, Facilities, General)"
# # #     ),
# # #     _auth: bool = Depends(verify_api_key),
# # # ):
# # #     question = q.strip()
# # #     if not question:
# # #         raise HTTPException(status_code=400, detail="Query 'q' is required")

# # #     # Short greetings and casual chat should not go through document retrieval.
# # #     if is_small_talk(question):
# # #         answer, model_used = generate_small_talk_response(question)
# # #         response = SearchResponse(
# # #             answer=answer,
# # #             confidence=0.0,
# # #             model_used=model_used,
# # #             cached=False,
# # #             numFound=0,
# # #             sources=[],
# # #             chunks=[],
# # #             suggested_followups=[
# # #                 "What does the leave policy say?",
# # #                 "What is the notice period after probation?",
# # #                 "What should I return when leaving the company?",
# # #             ],
# # #             q=question,
# # #             category_used="Uncategorized",
# # #             category_source="none",
# # #             category_confidence="low",
# # #         )
# # #         return response

# # #     # ── 1. Cache lookup ──
# # #     cached_resp = cache.cache_lookup(question)
# # #     if cached_resp:
# # #         log.info(f"💰 Cache HIT: {question[:60]}")
# # #         cached_resp["cached"] = True
# # #         # Backfill any new required fields the cached entry lacks
# # #         cached_resp.setdefault("category_source", "cached")
# # #         return SearchResponse(**cached_resp)

# # #     # ── 2. Determine category ──
# # #     # Get the live list of categories that actually exist in the index
# # #     all_categories = [c["name"] for c in search_index.list_categories(only_published=True)]

# # #     if category:
# # #         # User-selected: validate it exists (case-insensitive)
# # #         cat_match = next((c for c in all_categories
# # #                           if c.lower() == category.lower()), None)
# # #         if not cat_match:
# # #             log.warning(f"User selected unknown category '{category}'. "
# # #                         f"Available: {all_categories}")
# # #             # Fall through to AI-predicted
# # #             cat_match = None

# # #         if cat_match:
# # #             used_category = cat_match
# # #             category_source = "user_selected"
# # #             category_confidence = "high"
# # #         else:
# # #             classification = classify(question, all_categories)
# # #             used_category = classification["category"]
# # #             category_source = "ai_predicted"
# # #             category_confidence = classification["confidence"]
# # #     else:
# # #         # No category specified — let the classifier decide
# # #         classification = classify(question, all_categories)
# # #         used_category = classification["category"]
# # #         category_source = "ai_predicted"
# # #         category_confidence = classification["confidence"]

# # #     # If category is Uncategorized, search WITHOUT category filter
# # #     # (Uncategorized is included automatically alongside any selected category)
# # #     search_category = None if used_category == "Uncategorized" else used_category

# # #     # ── 3. Embed the query + hybrid search ──
# # #     try:
# # #         query_vec = embed_one(question)
# # #     except Exception as e:
# # #         log.error(f"Query embedding failed: {e}")
# # #         raise HTTPException(status_code=500, detail="Query embedding service unavailable")

# # #     chunks = search_index.hybrid_search(
# # #         query=question,
# # #         query_vector=query_vec,
# # #         top_k=settings.top_k_use,
# # #         category=search_category,
# # #         include_uncategorized=True,
# # #         only_published=True,
# # #     )

# # #     # ── 4. Fallback: if no results and category was applied, retry without it ──
# # #     if not chunks and search_category:
# # #         log.info(f"  No results in {search_category}. Falling back to all categories.")
# # #         chunks = search_index.hybrid_search(
# # #             query=question,
# # #             query_vector=query_vec,
# # #             top_k=settings.top_k_use,
# # #             category=None,
# # #             only_published=True,
# # #         )
# # #         category_source = "fallback_all"

# # #     # ── 5. Generate answer + follow-ups (single LLM call) ──
# # #     answer, followups, model_used = generate_answer_and_followups(question, chunks)

# # #     # ── 6. Compute confidence from chunk scores ──
# # #     confidence = compute_confidence(chunks) if chunks else 0.0

# # #     # If we have no chunks at all, force low confidence
# # #     if not chunks:
# # #         confidence = min(confidence, 0.30)

# # #     # ── 7. Build response in the new schema ──
# # #     chunks_out = [
# # #         ChunkOut(
# # #             text=c["text"],
# # #             filename=c.get("filename") or "",
# # #             pdf_url=c.get("pdf_url") or None,
# # #             score=round(float(c.get("score", 0)), 4),
# # #             page=c.get("page"),
# # #             article_title=c.get("article_title"),
# # #             category=c.get("category"),
# # #             sub_category=c.get("sub_category"),
# # #             chunk_id=c.get("chunk_id"),
# # #         )
# # #         for c in chunks
# # #     ]

# # #     # Deduplicate sources: filename-based (matches new schema example)
# # #     seen = set()
# # #     sources: List[str] = []
# # #     for c in chunks:
# # #         fn = c.get("filename")
# # #         if fn and fn not in seen:
# # #             seen.add(fn)
# # #             sources.append(fn)

# # #     response = SearchResponse(
# # #         answer=answer,
# # #         confidence=confidence,
# # #         model_used=model_used,
# # #         cached=False,
# # #         numFound=len(chunks_out),
# # #         sources=sources,
# # #         chunks=chunks_out,
# # #         suggested_followups=followups,
# # #         q=question,
# # #         category_used=used_category,
# # #         category_source=category_source,
# # #         category_confidence=category_confidence,
# # #     )

# # #     # ── 8. Cache the response (skips empty/no-answer answers automatically) ──
# # #     cache.cache_store(question, response.model_dump())

# # #     return response


# # # # ═══════════════════════════════════════════════════════════
# # # #  ADMIN ENDPOINTS
# # # # ═══════════════════════════════════════════════════════════
# # # @app.post("/admin/reindex", response_model=AdminResponse)
# # # def admin_reindex(
# # #     background_tasks: BackgroundTasks,
# # #     force_full: bool = Query(False, description="Force a full re-sync (ignore delta token)"),
# # #     _auth: bool = Depends(verify_api_key),
# # # ):
# # #     """Trigger a sync now. Runs in background — endpoint returns immediately."""
# # #     background_tasks.add_task(run_sync, force_full=force_full)
# # #     msg = "full re-sync" if force_full else "delta sync"
# # #     return AdminResponse(status="started", message=f"{msg} started in background")


# # # @app.post("/admin/reset_sync", response_model=AdminResponse)
# # # def admin_reset_sync(_auth: bool = Depends(verify_api_key)):
# # #     """Clear the delta token. Next sync will be a full sync."""
# # #     cache.reset_delta_token()
# # #     return AdminResponse(
# # #         status="ok",
# # #         message="Delta token cleared. Next sync will be a full sync."
# # #     )


# # # # ═══════════════════════════════════════════════════════════
# # # #  ENTRY POINT (for `python app.py`)
# # # # ═══════════════════════════════════════════════════════════
# # # if __name__ == "__main__":
# # #     import uvicorn
# # #     uvicorn.run(app, host="0.0.0.0", port=8000)

# # """
# # app.py — Veelead Helpdesk RAG Bot, FastAPI server.

# # Endpoints:
# #     GET  /                       — health/info
# #     GET  /health                 — detailed status (stats, models, sync history)
# #     GET  /categories             — list categories with chunk counts (for frontend buttons)
# #     GET  /search.json?q=...      — main query endpoint
# #     POST /admin/reindex          — trigger sync now (background task)
# #     POST /admin/reset_sync       — force full re-sync next time

# # Authentication:
# #     All non-info endpoints require header: x-api-key: <API_KEY from .env>
# #     /health and / are public for monitoring.

# # Run locally:
# #     uvicorn app:app --reload --port 8000

# # Run in production (Azure App Service):
# #     uvicorn app:app --host 0.0.0.0 --port 8000
# # """

# # import logging
# # from datetime import datetime, timezone
# # from typing import Optional, List, Dict, Any

# # from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
# # from fastapi.middleware.cors import CORSMiddleware
# # from pydantic import BaseModel, Field
# # from openai import AzureOpenAI

# # from config import settings, print_config_summary
# # from sources import get_source, DocumentRef, ChangeSet
# # from pipeline.extractors import extract_bytes, extract_with_pages
# # from pipeline.chunker import chunk_document
# # from pipeline.embedder import embed_one, embed_many, get_embedding_info
# # from pipeline.classifier import classify, get_classifier_info
# # from storage import search_index
# # from storage import cache
# # from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# # # ═══════════════════════════════════════════════════════════
# # #  LOGGING
# # # ═══════════════════════════════════════════════════════════
# # logging.basicConfig(
# #     level=getattr(logging, settings.log_level, logging.INFO),
# #     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
# # )
# # log = logging.getLogger("app")


# # # ═══════════════════════════════════════════════════════════
# # #  LLM CLIENT (for answer generation)
# # # ═══════════════════════════════════════════════════════════
# # _gpt_client: Optional[AzureOpenAI] = None


# # def get_gpt_client() -> AzureOpenAI:
# #     global _gpt_client
# #     if _gpt_client is None:
# #         _gpt_client = AzureOpenAI(
# #             api_key=settings.gpt_api_key,
# #             api_version=settings.gpt_api_ver,
# #             azure_endpoint=settings.gpt_endpoint,
# #         )
# #     return _gpt_client


# # # ═══════════════════════════════════════════════════════════
# # #  MODEL ROUTING — pick gpt-4o-mini vs gpt-4o
# # # ═══════════════════════════════════════════════════════════
# # import re
# # COMPLEX_QUERY_RE = re.compile(
# #     r"\b(compare|difference|versus|vs|analy[sz]e|summari[sz]e|"
# #     r"explain why|step.by.step|multi(ple)?|across|between)\b", re.I
# # )
# # SMALL_TALK_RE = re.compile(
# #     r"^\s*(hi+|hello+|hey+|yo+|good\s+(morning|afternoon|evening)|"
# #     r"how are you|what'?s up|thanks|thank you)\b[\s!.?]*$",
# #     re.I,
# # )
# # CAPABILITY_RE = re.compile(
# #     r"^\s*(what can you help(?: me)? with|how can you help(?: me)?|"
# #     r"can you help with hr or it questions|what do you do|tell me what you can do)\b[\s!.?]*$",
# #     re.I,
# # )
# # SUMMARY_REQUEST_RE = re.compile(
# #     r"^\s*(hr policy|it policy|facilities policy|general policy|"
# #     r"tell me about (hr|it|facilities|general)( policy)?|"
# #     r"what does (the )?(hr|it|facilities|general) policy (cover|contain|include)|"
# #     r"what is (the )?(hr|it|facilities|general) policy)\b[\s!.?]*$",
# #     re.I,
# # )
# # ACTION_REQUEST_RE = re.compile(
# #     r"^\s*(how do i|how can i|can i|can you|what should i do|"
# #     r"leave balance|not working|problem|issue|error|stuck|reset|update|"
# #     r"carry forward|how to)\b",
# #     re.I,
# # )


# # def pick_chat_model(question: str) -> str:
# #     """Choose gpt-4o-mini for typical queries, gpt-4o for complex ones."""
# #     if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
# #         return settings.gpt_large_deploy
# #     return settings.gpt_mini_deploy


# # def is_small_talk(question: str) -> bool:
# #     """Detect greetings and short capability/meta prompts."""
# #     q = question.strip().lower()
# #     return (
# #         bool(SMALL_TALK_RE.match(q))
# #         or bool(CAPABILITY_RE.match(q))
# #         or q in {"hi", "hello", "hey", "yo"}
# #     )


# # def is_summary_request(question: str) -> bool:
# #     """Detect broad document-level questions that should get a summary."""
# #     q = question.strip().lower()
# #     return bool(SUMMARY_REQUEST_RE.match(q)) or (
# #         "policy" in q and len(q.split()) <= 5
# #     )


# # def is_action_request(question: str) -> bool:
# #     """Detect how-to, troubleshooting, and resolution-seeking questions."""
# #     q = question.strip().lower()
# #     return bool(ACTION_REQUEST_RE.match(q)) or any(
# #         token in q
# #         for token in [
# #             " not updated",
# #             " not working",
# #             " issue",
# #             " problem",
# #             " error",
# #             " help",
# #         ]
# #     )


# # def generate_small_talk_response(question: str) -> tuple[str, str]:
# #     """Return a short friendly Azure OpenAI response for greetings."""
# #     try:
# #         client = get_gpt_client()
# #         resp = client.chat.completions.create(
# #             model=settings.gpt_mini_deploy,
# #             messages=[
# #                 {
# #                     "role": "system",
# #                     "content": (
# #                         "You are a friendly helpdesk assistant. "
# #                         "Respond naturally to greetings, brief casual chat, and simple "
# #                         "meta questions about what you can help with. "
# #                         "Keep it short, helpful, and invite the user to ask a work-related question."
# #                     ),
# #                 },
# #                 {"role": "user", "content": question},
# #             ],
# #             temperature=0.7,
# #             max_tokens=80,
# #             timeout=15,
# #         )
# #         return (resp.choices[0].message.content or "Hello! How can I help you today?", settings.gpt_mini_deploy)
# #     except Exception as e:
# #         log.warning(f"Small-talk LLM failed: {e}")
# #         return ("Hello! How can I help you today?", "fallback")


# # # ═══════════════════════════════════════════════════════════
# # #  INDEXING PIPELINE
# # # ═══════════════════════════════════════════════════════════
# # def index_document(ref: DocumentRef) -> int:
# #     """
# #     Process one document: download → extract → chunk → embed → upload.
# #     If the doc already has chunks in the index, deletes them first.
# #     Returns count of chunks uploaded.
# #     """
# #     source = get_source()
# #     log.info(f"  → Indexing: {ref.filename}")

# #     # 1. Download
# #     data = source.download(ref)

# #     # 2. Extract text — page-aware when possible (PDF)
# #     pages_data = None
# #     if data.pre_extracted_text:
# #         text = data.pre_extracted_text
# #     elif data.content_bytes:
# #         try:
# #             pages_data = extract_with_pages(data.content_bytes, ref.doc_type)
# #             text = "\n\n".join(p[0] for p in pages_data) if pages_data else ""
# #         except Exception as e:
# #             log.error(f"     ✗ Extraction failed for {ref.filename}: {e}")
# #             return 0
# #     else:
# #         log.error(f"     ✗ No content for {ref.filename}")
# #         return 0

# #     if not text or not text.strip():
# #         log.warning(f"     ⚠ Empty text for {ref.filename} — skipping")
# #         return 0

# #     # 3. Chunk + attach metadata (with page info if available)
# #     doc_for_chunking = {
# #         **ref.to_doc_metadata(),
# #         "text": text,
# #         "pages": pages_data,  # may be None or list of (text, page) tuples
# #     }
# #     chunks = chunk_document(
# #         doc_for_chunking,
# #         chunk_size=settings.chunk_size,
# #         overlap=settings.chunk_overlap,
# #     )

# #     if not chunks:
# #         log.warning(f"     ⚠ No chunks produced for {ref.filename}")
# #         return 0

# #     # 4. Delete old chunks if any (idempotent — handles updates cleanly)
# #     if ref.file_id:
# #         search_index.delete_by_file_id(ref.file_id)

# #     # 5. Embed
# #     log.info(f"     • Embedding {len(chunks)} chunks...")
# #     texts = [c["text"] for c in chunks]
# #     vectors = embed_many(texts)
# #     for chunk, vec in zip(chunks, vectors):
# #         chunk["embedding"] = vec

# #     # 6. Upload to Azure AI Search
# #     result = search_index.upsert_chunks(chunks)
# #     log.info(f"     ✓ {result['uploaded']} uploaded, {result['failed']} failed")

# #     # 7. Invalidate cache for this file (in case of update/re-index)
# #     cache.cache_invalidate_by_file_id(ref.file_id)
# #     cache.cache_invalidate_by_filename(ref.filename)

# #     return result["uploaded"]


# # def run_sync(force_full: bool = False) -> Dict[str, Any]:
# #     """
# #     Run a sync cycle:
# #       - Get changes from source (delta sync, unless force_full)
# #       - Apply additions/updates/deletes
# #       - Save new delta token
# #       - Record audit log
# #     """
# #     started_at = datetime.now(timezone.utc)
# #     source = get_source()
# #     source_type_name = settings.source_type

# #     log.info("─" * 60)
# #     log.info(f"  SYNC START ({source.source_name()})")
# #     log.info("─" * 60)

# #     delta_token = None if force_full else cache.get_delta_token()
# #     try:
# #         changes: ChangeSet = source.get_changes(delta_token)
# #     except Exception as e:
# #         log.exception("Sync failed during get_changes()")
# #         cache.record_sync_run(
# #             source_type=source_type_name,
# #             started_at=started_at,
# #             finished_at=datetime.now(timezone.utc),
# #             status="failed",
# #             error_msg=str(e),
# #         )
# #         return {"status": "failed", "error": str(e)}

# #     if changes.is_empty():
# #         log.info("  No changes detected.")
# #         # Still save token so next sync knows where we are
# #         if changes.new_delta_token:
# #             cache.save_delta_token(changes.new_delta_token)
# #         finished_at = datetime.now(timezone.utc)
# #         run_id = cache.record_sync_run(
# #             source_type=source_type_name,
# #             started_at=started_at,
# #             finished_at=finished_at,
# #             added=0, updated=0, deleted=0,
# #             status="success",
# #         )
# #         return {
# #             "status": "success",
# #             "run_id": run_id,
# #             "added": 0, "updated": 0, "deleted": 0,
# #             "duration_sec": (finished_at - started_at).total_seconds(),
# #         }

# #     # ── DELETIONS first (clean state) ──
# #     deleted_count = 0
# #     for file_id in changes.deleted:
# #         try:
# #             n = search_index.delete_by_file_id(file_id)
# #             cache.cache_invalidate_by_file_id(file_id)
# #             deleted_count += 1
# #             log.info(f"  − Deleted file {file_id}: {n} chunks removed")
# #         except Exception as e:
# #             log.error(f"  ✗ Delete failed for {file_id}: {e}")

# #     # ── ADDITIONS + UPDATES ──
# #     added_count = 0
# #     updated_count = 0
# #     failed = 0

# #     for ref in changes.added:
# #         try:
# #             n = index_document(ref)
# #             if n > 0:
# #                 added_count += 1
# #         except Exception as e:
# #             log.error(f"  ✗ Add failed for {ref.filename}: {e}")
# #             failed += 1

# #     for ref in changes.updated:
# #         try:
# #             n = index_document(ref)
# #             if n > 0:
# #                 updated_count += 1
# #         except Exception as e:
# #             log.error(f"  ✗ Update failed for {ref.filename}: {e}")
# #             failed += 1

# #     # Save new delta token
# #     if changes.new_delta_token:
# #         cache.save_delta_token(changes.new_delta_token)

# #     finished_at = datetime.now(timezone.utc)
# #     status = "success" if failed == 0 else "partial"
# #     run_id = cache.record_sync_run(
# #         source_type=source_type_name,
# #         started_at=started_at,
# #         finished_at=finished_at,
# #         added=added_count,
# #         updated=updated_count,
# #         deleted=deleted_count,
# #         status=status,
# #         error_msg=f"{failed} items failed" if failed else None,
# #     )

# #     duration = (finished_at - started_at).total_seconds()
# #     log.info("─" * 60)
# #     log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
# #              f"({failed} failed, {duration:.1f}s)")
# #     log.info("─" * 60)

# #     return {
# #         "status": status,
# #         "run_id": run_id,
# #         "added": added_count,
# #         "updated": updated_count,
# #         "deleted": deleted_count,
# #         "failed": failed,
# #         "duration_sec": duration,
# #     }


# # # ═══════════════════════════════════════════════════════════
# # #  ANSWER GENERATION + FOLLOW-UPS (single LLM call)
# # # ═══════════════════════════════════════════════════════════
# # SYSTEM_PROMPT_TEMPLATE = """You are a Helpdesk Assistant for Veelead Solutions.
# # Answer questions strictly from the document context provided.

# # Return ONLY a JSON object with this exact shape (no markdown, no extra text):

# # {{
# #   "answer": "A clear, helpful answer in 2-5 sentences. Direct and actionable. Friendly and professional, written for non-technical employees.",
# #   "suggested_followups": [
# #     "Short specific follow-up question 1",
# #     "Short specific follow-up question 2",
# #     "Short specific follow-up question 3"
# #   ]
# # }}

# # Rules:
# # - Use only information from the context below. Never invent facts.
# # - If the context doesn't contain the answer, set "answer" to:
# #   "I could not find that in the available documents. Please check with your helpdesk or rephrase your question."
# # - Keep "suggested_followups" to 2-3 short, specific questions related to the topic.
# # - Do not include the document filename or source in the answer text — that goes separately.
# # - If QUESTION_FOCUS is "summary", answer at a document-summary level instead of a narrow clause-level answer.
# # - For summary questions like "HR policy", describe what the document covers using the retrieved context.
# # - Prefer a concise overview of the main topics, sections, or rules visible in the context.
# # - If QUESTION_FOCUS is "steps", start with a short friendly line such as "I found a solution!" and then present the answer as a few bullet points or numbered steps.
# # - For troubleshooting or how-to questions, favor practical steps, checks, and next actions over long paragraphs.

# # DOCUMENT CONTEXT:
# # {context}

# # QUESTION_FOCUS:
# # {question_focus}"""


# # def compute_confidence(chunks: List[Dict[str, Any]]) -> float:
# #     """
# #     Compute a 0.0-1.0 confidence score from chunk relevance scores.

# #     Heuristic:
# #       - Take top chunk's score, normalize to 0-1 (Azure hybrid scores ~0-5 typical)
# #       - Boost if multiple chunks have reasonable scores
# #       - Cap at 0.95 (never claim 100% certainty)
# #     """
# #     if not chunks:
# #         return 0.0

# #     scores = [float(c.get("score", 0)) for c in chunks]
# #     top = max(scores)

# #     # Azure AI Search hybrid scores: typical max ~3-5 for very good matches.
# #     # Normalize: top=0.5 → 0.30; top=2.0 → 0.75; top=4.0 → 0.95
# #     normalized = min(top / 4.5, 1.0)

# #     # Boost slightly if 2+ chunks have decent scores (corroboration)
# #     decent_count = sum(1 for s in scores if s > 0.5)
# #     boost = 0.05 if decent_count >= 2 else 0.0

# #     return round(min(normalized + boost, 0.95), 2)


# # def generate_answer_and_followups(
# #     question: str,
# #     top_chunks: List[Dict[str, Any]]
# # ) -> tuple[str, List[str], str]:
# #     """
# #     Call the LLM ONCE to produce both the answer text and 2-3 follow-up suggestions.
# #     Returns (answer_text, followups_list, model_used).
# #     """
# #     if not top_chunks:
# #         return (
# #             "I could not find that in the available documents. "
# #             "Please check with your helpdesk or rephrase your question.",
# #             [],
# #             "none",
# #         )

# #     # Build context block
# #     context_parts = []
# #     for c in top_chunks:
# #         source_label = c.get("article_title") or c.get("filename") or "Unknown"
# #         context_parts.append(
# #             f"[Source: {source_label} | Category: {c.get('category', '?')} | "
# #             f"Relevance: {c.get('score', 0):.2f}]\n{c['text']}"
# #         )
# #     context = "\n\n---\n\n".join(context_parts)

# #     if is_summary_request(question):
# #         question_focus = "summary"
# #     elif is_action_request(question):
# #         question_focus = "steps"
# #     else:
# #         question_focus = "specific"
# #     system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
# #         context=context,
# #         question_focus=question_focus,
# #     )
# #     model = pick_chat_model(question)

# #     try:
# #         client = get_gpt_client()
# #         resp = client.chat.completions.create(
# #             model=model,
# #             messages=[
# #                 {"role": "system", "content": system_prompt},
# #                 {"role": "user", "content": question},
# #             ],
# #             temperature=0.2,
# #             max_tokens=900,
# #             response_format={"type": "json_object"},
# #             timeout=30,
# #         )
# #         content = resp.choices[0].message.content or "{}"
# #         import json as _json
# #         parsed = _json.loads(content)
# #         answer = (parsed.get("answer") or "").strip()
# #         followups = parsed.get("suggested_followups") or []
# #         # Validate followups
# #         if not isinstance(followups, list):
# #             followups = []
# #         followups = [str(f).strip() for f in followups if f][:3]
# #         if not answer:
# #             answer = "I could not find that in the available documents."
# #         return (answer, followups, model)
# #     except Exception as e:
# #         log.error(f"LLM call failed: {e}")
# #         return (
# #             f"Sorry, I had trouble generating an answer. ({type(e).__name__})",
# #             [],
# #             model,
# #         )


# # # ═══════════════════════════════════════════════════════════
# # #  PYDANTIC RESPONSE MODELS
# # # ═══════════════════════════════════════════════════════════
# # class ChunkOut(BaseModel):
# #     text: str
# #     filename: str
# #     pdf_url: Optional[str] = None
# #     score: float
# #     page: Optional[int] = None
# #     # Bonus fields (frontend can ignore)
# #     article_title: Optional[str] = None
# #     category: Optional[str] = None
# #     sub_category: Optional[str] = None
# #     chunk_id: Optional[str] = None


# # class SearchResponse(BaseModel):
# #     answer: str
# #     confidence: float = Field(..., ge=0.0, le=1.0)
# #     model_used: str
# #     cached: bool
# #     numFound: int
# #     sources: List[str]
# #     chunks: List[ChunkOut]
# #     suggested_followups: List[str]
# #     # Bonus / debug fields (frontend can ignore — useful for category badge UI)
# #     q: Optional[str] = None
# #     category_used: Optional[str] = None
# #     category_source: Optional[str] = None
# #     category_confidence: Optional[str] = None


# # class CategoryOut(BaseModel):
# #     name: str
# #     display: str
# #     chunk_count: int


# # class CategoriesResponse(BaseModel):
# #     categories: List[CategoryOut]


# # class HealthResponse(BaseModel):
# #     status: str
# #     source: str
# #     index: Dict[str, Any]
# #     cache: Dict[str, Any]
# #     sync: Dict[str, Any]
# #     scheduler: Dict[str, Any]
# #     embedding: Dict[str, Any]
# #     classifier: Dict[str, Any]


# # class AdminResponse(BaseModel):
# #     status: str
# #     message: str


# # # ═══════════════════════════════════════════════════════════
# # #  FASTAPI APP
# # # ═══════════════════════════════════════════════════════════
# # app = FastAPI(
# #     title="Veelead Helpdesk RAG Bot",
# #     description="Cost-efficient SharePoint-aware helpdesk bot",
# #     version="1.0.0",
# # )

# # # CORS for frontend (lock down to specific origins in production if needed)
# # app.add_middleware(
# #     CORSMiddleware,
# #     allow_origins=["*"],
# #     allow_credentials=True,
# #     allow_methods=["*"],
# #     allow_headers=["*"],
# # )


# # def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
# #     """API key dependency. Allows blank header on default-key for local dev."""
# #     if not x_api_key:
# #         raise HTTPException(status_code=401, detail="Missing x-api-key header")
# #     if x_api_key != settings.api_key:
# #         raise HTTPException(status_code=401, detail="Invalid API key")
# #     return True


# # # ═══════════════════════════════════════════════════════════
# # #  STARTUP
# # # ═══════════════════════════════════════════════════════════
# # @app.on_event("startup")
# # def startup():
# #     print_config_summary()
# #     log.info("═" * 60)
# #     log.info("  Veelead Helpdesk RAG Bot — starting")
# #     log.info("═" * 60)

# #     # Validate config
# #     issues = settings.validate()
# #     if issues:
# #         log.warning("Configuration issues found:")
# #         for issue in issues:
# #             log.warning(f"  ⚠ {issue}")

# #     # Init local DBs
# #     cache.init_db()

# #     # Ensure search index exists
# #     try:
# #         search_index.ensure_index()
# #     except Exception as e:
# #         log.error(f"Could not connect to Azure AI Search: {e}")
# #         log.error("API will start but searches will fail until this is fixed.")

# #     # Run an initial sync (non-blocking — only if delta token missing)
# #     if not cache.get_delta_token():
# #         log.info("No delta token found — running initial full sync...")
# #         try:
# #             run_sync(force_full=True)
# #         except Exception as e:
# #             log.exception("Initial sync failed (will retry on next scheduled run)")

# #     # Start background scheduler for periodic sync + cleanup
# #     try:
# #         start_scheduler()
# #     except Exception as e:
# #         log.exception("Failed to start scheduler — background sync disabled")

# #     log.info("═" * 60)
# #     log.info("  ✅ API READY")
# #     log.info(f"  Endpoint: {settings.embed_endpoint}")
# #     log.info(f"  Source: {settings.source_type}")
# #     log.info("═" * 60)


# # @app.on_event("shutdown")
# # def shutdown():
# #     """Gracefully stop the scheduler when FastAPI shuts down."""
# #     log.info("Shutting down...")
# #     try:
# #         stop_scheduler()
# #     except Exception:
# #         pass


# # # ═══════════════════════════════════════════════════════════
# # #  ROOT + HEALTH
# # # ═══════════════════════════════════════════════════════════
# # @app.get("/")
# # def root():
# #     return {
# #         "service": "Veelead Helpdesk RAG Bot",
# #         "version": "1.0.0",
# #         "endpoints": {
# #             "GET /search.json?q=<question>": "main search endpoint (auth required)",
# #             "GET /categories": "list categories with counts (auth required)",
# #             "GET /health": "detailed status (public)",
# #             "POST /admin/reindex": "trigger sync now (auth required)",
# #             "POST /admin/reset_sync": "force full re-sync next time (auth required)",
# #         },
# #         "frontend": "Send 'x-api-key' header on every authenticated request",
# #     }


# # @app.get("/health", response_model=HealthResponse)
# # def health():
# #     try:
# #         src = get_source()
# #         src_name = src.source_name()
# #     except Exception as e:
# #         src_name = f"<error: {e}>"

# #     return HealthResponse(
# #         status="ok",
# #         source=src_name,
# #         index=search_index.get_index_stats(),
# #         cache=cache.cache_stats(),
# #         sync=cache.sync_state_stats(),
# #         scheduler=get_scheduler_status(),
# #         embedding=get_embedding_info(),
# #         classifier=get_classifier_info(),
# #     )


# # # ═══════════════════════════════════════════════════════════
# # #  CATEGORIES (for frontend buttons)
# # # ═══════════════════════════════════════════════════════════
# # @app.get("/categories", response_model=CategoriesResponse)
# # def categories(_auth: bool = Depends(verify_api_key)):
# #     cats = search_index.list_categories(only_published=True)
# #     # Convert chunk_count to a more user-friendly count if needed
# #     return CategoriesResponse(
# #         categories=[
# #             CategoryOut(
# #                 name=c["name"],
# #                 display=c.get("display") or c["name"],
# #                 chunk_count=c["chunk_count"],
# #             )
# #             for c in cats
# #         ]
# #     )


# # # ═══════════════════════════════════════════════════════════
# # #  MAIN SEARCH ENDPOINT
# # # ═══════════════════════════════════════════════════════════
# # @app.get("/search.json", response_model=SearchResponse)
# # def search(
# #     q: str = Query(..., description="User question", min_length=1),
# #     category: Optional[str] = Query(
# #         None,
# #         description="Optional category selected by user (HR, IT, Facilities, General)"
# #     ),
# #     _auth: bool = Depends(verify_api_key),
# # ):
# #     question = q.strip()
# #     if not question:
# #         raise HTTPException(status_code=400, detail="Query 'q' is required")

# #     if is_small_talk(question):
# #         answer, model_used = generate_small_talk_response(question)
# #         return SearchResponse(
# #             answer=answer,
# #             confidence=0.15,
# #             model_used=model_used,
# #             cached=False,
# #             numFound=0,
# #             sources=[],
# #             chunks=[],
# #             suggested_followups=[
# #                 "What can you help me with today?",
# #                 "Can you help with HR or IT questions?",
# #             ],
# #             q=question,
# #             category_used="General",
# #             category_source="small_talk",
# #             category_confidence="high",
# #         )

# #     # ── 1. Cache lookup ──
# #     cached_resp = cache.cache_lookup(question)
# #     if cached_resp:
# #         # Schema check: skip entries from older bot versions
# #         required_new = {"confidence", "chunks", "suggested_followups"}
# #         if all(k in cached_resp for k in required_new):
# #             log.info(f"💰 Cache HIT: {question[:60]}")
# #             cached_resp["cached"] = True
# #             cached_resp.setdefault("category_source", "cached")
# #             try:
# #                 return SearchResponse(**cached_resp)
# #             except Exception as e:
# #                 log.warning(f"Cached entry invalid, regenerating: {e}")
# #         else:
# #             log.info(f"Cache entry old-schema, regenerating: {question[:60]}")

# #     # ── 2. Determine category ──
# #     # Get the live list of categories that actually exist in the index
# #     all_categories = [c["name"] for c in search_index.list_categories(only_published=True)]

# #     if category:
# #         # User-selected: validate it exists (case-insensitive)
# #         cat_match = next((c for c in all_categories
# #                           if c.lower() == category.lower()), None)
# #         if not cat_match:
# #             log.warning(f"User selected unknown category '{category}'. "
# #                         f"Available: {all_categories}")
# #             # Fall through to AI-predicted
# #             cat_match = None

# #         if cat_match:
# #             used_category = cat_match
# #             category_source = "user_selected"
# #             category_confidence = "high"
# #         else:
# #             classification = classify(question, all_categories)
# #             used_category = classification["category"]
# #             category_source = "ai_predicted"
# #             category_confidence = classification["confidence"]
# #     else:
# #         # No category specified — let the classifier decide
# #         classification = classify(question, all_categories)
# #         used_category = classification["category"]
# #         category_source = "ai_predicted"
# #         category_confidence = classification["confidence"]

# #     # If category is Uncategorized, search WITHOUT category filter
# #     # (Uncategorized is included automatically alongside any selected category)
# #     search_category = None if used_category == "Uncategorized" else used_category

# #     # ── 3. Embed the query + hybrid search ──
# #     try:
# #         query_vec = embed_one(question)
# #     except Exception as e:
# #         log.error(f"Query embedding failed: {e}")
# #         raise HTTPException(status_code=500, detail="Query embedding service unavailable")

# #     chunks = search_index.hybrid_search(
# #         query=question,
# #         query_vector=query_vec,
# #         top_k=settings.top_k_use,
# #         category=search_category,
# #         include_uncategorized=True,
# #         only_published=True,
# #     )

# #     # ── 4. Fallback: if no results and category was applied, retry without it ──
# #     if not chunks and search_category:
# #         log.info(f"  No results in {search_category}. Falling back to all categories.")
# #         chunks = search_index.hybrid_search(
# #             query=question,
# #             query_vector=query_vec,
# #             top_k=settings.top_k_use,
# #             category=None,
# #             only_published=True,
# #         )
# #         category_source = "fallback_all"

# #     # ── 5. Generate answer + follow-ups (single LLM call) ──
# #     answer, followups, model_used = generate_answer_and_followups(question, chunks)

# #     # ── 6. Compute confidence from chunk scores ──
# #     confidence = compute_confidence(chunks) if chunks else 0.0

# #     # If we have no chunks at all, force low confidence
# #     if not chunks:
# #         confidence = min(confidence, 0.30)

# #     # ── 7. Build response in the new schema ──
# #     chunks_out = [
# #         ChunkOut(
# #             text=c["text"],
# #             filename=c.get("filename") or "",
# #             pdf_url=c.get("pdf_url") or None,
# #             score=round(float(c.get("score", 0)), 4),
# #             page=c.get("page"),
# #             article_title=c.get("article_title"),
# #             category=c.get("category"),
# #             sub_category=c.get("sub_category"),
# #             chunk_id=c.get("chunk_id"),
# #         )
# #         for c in chunks
# #     ]

# #     # Deduplicate sources: filename-based (matches new schema example)
# #     seen = set()
# #     sources: List[str] = []
# #     for c in chunks:
# #         fn = c.get("filename")
# #         if fn and fn not in seen:
# #             seen.add(fn)
# #             sources.append(fn)

# #     response = SearchResponse(
# #         answer=answer,
# #         confidence=confidence,
# #         model_used=model_used,
# #         cached=False,
# #         numFound=len(chunks_out),
# #         sources=sources,
# #         chunks=chunks_out,
# #         suggested_followups=followups,
# #         q=question,
# #         category_used=used_category,
# #         category_source=category_source,
# #         category_confidence=category_confidence,
# #     )

# #     # ── 8. Cache the response (skips empty/no-answer answers automatically) ──
# #     cache.cache_store(question, response.model_dump())

# #     return response


# # # ═══════════════════════════════════════════════════════════
# # #  ADMIN ENDPOINTS
# # # ═══════════════════════════════════════════════════════════
# # @app.post("/admin/reindex", response_model=AdminResponse)
# # def admin_reindex(
# #     background_tasks: BackgroundTasks,
# #     force_full: bool = Query(False, description="Force a full re-sync (ignore delta token)"),
# #     _auth: bool = Depends(verify_api_key),
# # ):
# #     """Trigger a sync now. Runs in background — endpoint returns immediately."""
# #     background_tasks.add_task(run_sync, force_full=force_full)
# #     msg = "full re-sync" if force_full else "delta sync"
# #     return AdminResponse(status="started", message=f"{msg} started in background")


# # @app.post("/admin/reset_sync", response_model=AdminResponse)
# # def admin_reset_sync(_auth: bool = Depends(verify_api_key)):
# #     """Clear the delta token. Next sync will be a full sync."""
# #     cache.reset_delta_token()
# #     return AdminResponse(
# #         status="ok",
# #         message="Delta token cleared. Next sync will be a full sync."
# #     )


# # # ═══════════════════════════════════════════════════════════
# # #  ENTRY POINT (for `python app.py`)
# # # ═══════════════════════════════════════════════════════════
# # if __name__ == "__main__":
# #     import uvicorn
# #     uvicorn.run(app, host="0.0.0.0", port=8000)


# """
# app.py — Veelead Helpdesk RAG Bot, FastAPI server.

# Endpoints:
#     GET  /                       — health/info
#     GET  /health                 — detailed status (stats, models, sync history)
#     GET  /categories             — list categories with chunk counts (for frontend buttons)
#     GET  /search.json?q=...      — main query endpoint
#     POST /admin/reindex          — trigger sync now (background task)
#     POST /admin/reset_sync       — force full re-sync next time

# Authentication:
#     All non-info endpoints require header: x-api-key: <API_KEY from .env>
#     /health and / are public for monitoring.

# Run locally:
#     uvicorn app:app --reload --port 8000

# Run in production (Azure App Service):
#     uvicorn app:app --host 0.0.0.0 --port 8000
# """

# import logging
# from datetime import datetime, timezone
# from typing import Optional, List, Dict, Any

# from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
# from fastapi.middleware.cors import CORSMiddleware
# from pydantic import BaseModel, Field
# from openai import AzureOpenAI

# from config import settings, print_config_summary
# from sources import get_source, DocumentRef, ChangeSet
# from pipeline.extractors import extract_bytes, extract_with_pages
# from pipeline.chunker import chunk_document
# from pipeline.embedder import embed_one, embed_many, get_embedding_info
# from pipeline.classifier import classify, get_classifier_info
# from storage import search_index
# from storage import cache
# from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# # ═══════════════════════════════════════════════════════════
# #  LOGGING
# # ═══════════════════════════════════════════════════════════
# logging.basicConfig(
#     level=getattr(logging, settings.log_level, logging.INFO),
#     format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
# )
# log = logging.getLogger("app")


# # ═══════════════════════════════════════════════════════════
# #  LLM CLIENT (for answer generation)
# # ═══════════════════════════════════════════════════════════
# _gpt_client: Optional[AzureOpenAI] = None


# def get_gpt_client() -> AzureOpenAI:
#     global _gpt_client
#     if _gpt_client is None:
#         _gpt_client = AzureOpenAI(
#             api_key=settings.gpt_api_key,
#             api_version=settings.gpt_api_ver,
#             azure_endpoint=settings.gpt_endpoint,
#         )
#     return _gpt_client


# # ═══════════════════════════════════════════════════════════
# #  MODEL ROUTING — pick gpt-4o-mini vs gpt-4o
# # ═══════════════════════════════════════════════════════════
# import re
# COMPLEX_QUERY_RE = re.compile(
#     r"\b(compare|difference|versus|vs|analy[sz]e|summari[sz]e|"
#     r"explain why|step.by.step|multi(ple)?|across|between)\b", re.I
# )
# SUMMARY_QUERY_RE = re.compile(r"\b(summar(?:y|ize|ise)|overview|cover(?:s|ed)?|what does)\b", re.I)
# SMALL_TALK_RE = re.compile(
#     r"^\s*(hi+|hello+|hey+|good\s+(morning|afternoon|evening)|"
#     r"thanks|thank\s+you|how are you|what'?s up|yo)\s*[!.?]*\s*$",
#     re.I,
# )
# CAPABILITY_RE = re.compile(
#     r"^\s*(can you help me|help|anyone there|i have (?:a )?doubt|"
#     r"one doubts?|i need help)\s*[!.?]*\s*$",
#     re.I,
# )


# def pick_chat_model(question: str) -> str:
#     """Choose gpt-4o-mini for typical queries, gpt-4o for complex ones."""
#     if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
#         return settings.gpt_large_deploy
#     return settings.gpt_mini_deploy


# def is_small_talk(question: str) -> bool:
#     """Detect greetings and simple capability/help prompts."""
#     q = question.strip().lower()
#     return bool(SMALL_TALK_RE.match(q) or CAPABILITY_RE.match(q) or q in {"hi", "hello", "hey", "yo"})


# def generate_small_talk_response(question: str) -> tuple[str, str]:
#     """Return a short friendly response without running document search."""
#     q = question.strip().lower()
#     if q in {"hi", "hello", "hey", "yo"} or SMALL_TALK_RE.match(q):
#         return ("Hello! How can I help you today?", "small_talk")
#     return (
#         "Sure, I can help. Please share your IT, HR, Facilities, or policy-related question.",
#         "small_talk",
#     )


# def wants_combined_it_hr_summary(question: str) -> bool:
#     """Detect summary-style questions that explicitly mention both IT and HR."""
#     q = question.lower()
#     has_it = re.search(r"\bit\b", q) is not None
#     has_hr = re.search(r"\bhr\b", q) is not None
#     return has_it and has_hr and bool(SUMMARY_QUERY_RE.search(q))


# def _unique_source_labels(top_chunks: List[Dict[str, Any]], limit: int = 2) -> List[str]:
#     labels: List[str] = []
#     seen = set()
#     for c in top_chunks:
#         label = c.get("filename") or c.get("article_title")
#         if label and label not in seen:
#             seen.add(label)
#             labels.append(label)
#         if len(labels) >= limit:
#             break
#     return labels


# def _format_answer_with_intro_and_source(
#     answer: str,
#     top_chunks: List[Dict[str, Any]],
# ) -> str:
#     """Normalize the final answer style for short helpdesk responses."""
#     cleaned = (answer or "").strip()
#     if not cleaned:
#         return cleaned

#     lower = cleaned.lower()
#     if not lower.startswith("i could not find") and not lower.startswith("sorry"):
#         if not lower.startswith("i found a solution!"):
#             cleaned = f"I found a solution!\n\n{cleaned}"

#     source_labels = _unique_source_labels(top_chunks)
#     if source_labels and "source:" not in cleaned.lower():
#         cleaned = f"{cleaned}\n\n(Source: {', '.join(source_labels)})"
#     return cleaned


# # ═══════════════════════════════════════════════════════════
# #  INDEXING PIPELINE
# # ═══════════════════════════════════════════════════════════
# def index_document(ref: DocumentRef) -> int:
#     """
#     Process one document: download → extract → chunk → embed → upload.
#     If the doc already has chunks in the index, deletes them first.
#     Returns count of chunks uploaded.
#     """
#     source = get_source()
#     log.info(f"  → Indexing: {ref.filename}")

#     # 1. Download
#     data = source.download(ref)

#     # 2. Extract text — page-aware when possible (PDF)
#     pages_data = None
#     if data.pre_extracted_text:
#         text = data.pre_extracted_text
#     elif data.content_bytes:
#         try:
#             pages_data = extract_with_pages(data.content_bytes, ref.doc_type)
#             text = "\n\n".join(p[0] for p in pages_data) if pages_data else ""
#         except Exception as e:
#             log.error(f"     ✗ Extraction failed for {ref.filename}: {e}")
#             return 0
#     else:
#         log.error(f"     ✗ No content for {ref.filename}")
#         return 0

#     if not text or not text.strip():
#         log.warning(f"     ⚠ Empty text for {ref.filename} — skipping")
#         return 0

#     # 3. Chunk + attach metadata (with page info if available)
#     doc_for_chunking = {
#         **ref.to_doc_metadata(),
#         "text": text,
#         "pages": pages_data,  # may be None or list of (text, page) tuples
#     }
#     chunks = chunk_document(
#         doc_for_chunking,
#         chunk_size=settings.chunk_size,
#         overlap=settings.chunk_overlap,
#     )

#     if not chunks:
#         log.warning(f"     ⚠ No chunks produced for {ref.filename}")
#         return 0

#     # 4. Delete old chunks if any (idempotent — handles updates cleanly)
#     if ref.file_id:
#         search_index.delete_by_file_id(ref.file_id)

#     # 5. Embed
#     log.info(f"     • Embedding {len(chunks)} chunks...")
#     texts = [c["text"] for c in chunks]
#     vectors = embed_many(texts)
#     for chunk, vec in zip(chunks, vectors):
#         chunk["embedding"] = vec

#     # 6. Upload to Azure AI Search
#     result = search_index.upsert_chunks(chunks)
#     log.info(f"     ✓ {result['uploaded']} uploaded, {result['failed']} failed")

#     # 7. Invalidate cache for this file (in case of update/re-index)
#     cache.cache_invalidate_by_file_id(ref.file_id)
#     cache.cache_invalidate_by_filename(ref.filename)

#     return result["uploaded"]


# def run_sync(force_full: bool = False) -> Dict[str, Any]:
#     """
#     Run a sync cycle:
#       - Get changes from source (delta sync, unless force_full)
#       - Apply additions/updates/deletes
#       - Save new delta token
#       - Record audit log
#     """
#     started_at = datetime.now(timezone.utc)
#     source = get_source()
#     source_type_name = settings.source_type

#     log.info("─" * 60)
#     log.info(f"  SYNC START ({source.source_name()})")
#     log.info("─" * 60)

#     delta_token = None if force_full else cache.get_delta_token()
#     try:
#         changes: ChangeSet = source.get_changes(delta_token)
#     except Exception as e:
#         log.exception("Sync failed during get_changes()")
#         cache.record_sync_run(
#             source_type=source_type_name,
#             started_at=started_at,
#             finished_at=datetime.now(timezone.utc),
#             status="failed",
#             error_msg=str(e),
#         )
#         return {"status": "failed", "error": str(e)}

#     if changes.is_empty():
#         log.info("  No changes detected.")
#         # Still save token so next sync knows where we are
#         if changes.new_delta_token:
#             cache.save_delta_token(changes.new_delta_token)
#         finished_at = datetime.now(timezone.utc)
#         run_id = cache.record_sync_run(
#             source_type=source_type_name,
#             started_at=started_at,
#             finished_at=finished_at,
#             added=0, updated=0, deleted=0,
#             status="success",
#         )
#         return {
#             "status": "success",
#             "run_id": run_id,
#             "added": 0, "updated": 0, "deleted": 0,
#             "duration_sec": (finished_at - started_at).total_seconds(),
#         }

#     # ── DELETIONS first (clean state) ──
#     deleted_count = 0
#     for file_id in changes.deleted:
#         try:
#             n = search_index.delete_by_file_id(file_id)
#             cache.cache_invalidate_by_file_id(file_id)
#             deleted_count += 1
#             log.info(f"  − Deleted file {file_id}: {n} chunks removed")
#         except Exception as e:
#             log.error(f"  ✗ Delete failed for {file_id}: {e}")

#     # ── ADDITIONS + UPDATES ──
#     added_count = 0
#     updated_count = 0
#     failed = 0

#     for ref in changes.added:
#         try:
#             n = index_document(ref)
#             if n > 0:
#                 added_count += 1
#         except Exception as e:
#             log.error(f"  ✗ Add failed for {ref.filename}: {e}")
#             failed += 1

#     for ref in changes.updated:
#         try:
#             n = index_document(ref)
#             if n > 0:
#                 updated_count += 1
#         except Exception as e:
#             log.error(f"  ✗ Update failed for {ref.filename}: {e}")
#             failed += 1

#     # Save new delta token
#     if changes.new_delta_token:
#         cache.save_delta_token(changes.new_delta_token)

#     finished_at = datetime.now(timezone.utc)
#     status = "success" if failed == 0 else "partial"
#     run_id = cache.record_sync_run(
#         source_type=source_type_name,
#         started_at=started_at,
#         finished_at=finished_at,
#         added=added_count,
#         updated=updated_count,
#         deleted=deleted_count,
#         status=status,
#         error_msg=f"{failed} items failed" if failed else None,
#     )

#     duration = (finished_at - started_at).total_seconds()
#     log.info("─" * 60)
#     log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
#              f"({failed} failed, {duration:.1f}s)")
#     log.info("─" * 60)

#     return {
#         "status": status,
#         "run_id": run_id,
#         "added": added_count,
#         "updated": updated_count,
#         "deleted": deleted_count,
#         "failed": failed,
#         "duration_sec": duration,
#     }


# # ═══════════════════════════════════════════════════════════
# #  ANSWER GENERATION + FOLLOW-UPS (single LLM call)
# # ═══════════════════════════════════════════════════════════
# SYSTEM_PROMPT_TEMPLATE = """You are a Helpdesk Assistant for Veelead Solutions.
# Answer questions strictly from the document context provided.

# Return ONLY a JSON object with this exact shape (no markdown, no extra text):

# {{
#   "answer": "A clear, helpful answer in 2-5 sentences. Direct and actionable. Friendly and professional, written for non-technical employees.",
#   "suggested_followups": [
#     "Short specific follow-up question 1",
#     "Short specific follow-up question 2",
#     "Short specific follow-up question 3"
#   ]
# }}

# Rules:
# - Use only information from the context below. Never invent facts.
# - If the context doesn't contain the answer, set "answer" to:
#   "I could not find that in the available documents. Please check with your helpdesk or rephrase your question."
# - If the user asks for a summary of IT, HR, Facilities, or General documents, give a short document-based summary from the retrieved context.
# - When the answer contains multiple details, format them as 2-5 numbered points.
# - Start the answer in a friendly, human tone such as "I found a solution!" when the context supports an answer.
# - Keep "suggested_followups" to 2-3 short, specific questions related to the topic.
# - Do not include the document filename or source in the answer text — that goes separately.

# DOCUMENT CONTEXT:
# {context}"""


# def compute_confidence(chunks: List[Dict[str, Any]]) -> float:
#     """
#     Compute a 0.0-1.0 confidence score from chunk relevance scores.

#     Azure AI Search RRF (Reciprocal Rank Fusion) scores are typically:
#       - 0.030+ : strong match (good document for the query)
#       - 0.020-0.030 : moderate match
#       - 0.010-0.020 : weak match
#       - <0.010 : likely irrelevant

#     Strategy:
#       - Top score is the strongest signal
#       - Score spread tells us if the top chunk is clearly best or if results are noisy
#       - Number of chunks above threshold gives corroboration
#     """
#     if not chunks:
#         return 0.0

#     scores = [float(c.get("score", 0)) for c in chunks]
#     top = max(scores)

#     # Normalize top score: 0.04+ → 0.95 (excellent), 0.025 → 0.65, 0.015 → 0.35
#     # Calibrated against real RRF score distribution
#     if top >= 0.035:
#         base = 0.90
#     elif top >= 0.025:
#         base = 0.65 + (top - 0.025) * 25      # 0.025→0.65, 0.035→0.90
#     elif top >= 0.015:
#         base = 0.40 + (top - 0.015) * 25      # 0.015→0.40, 0.025→0.65
#     elif top >= 0.008:
#         base = 0.15 + (top - 0.008) * (25 / 7)  # 0.008→0.15, 0.015→0.40
#     else:
#         base = top * (0.15 / 0.008)            # near zero → very low

#     # Corroboration boost: more chunks above ~75% of top → more confidence
#     threshold = top * 0.75
#     decent_count = sum(1 for s in scores if s >= threshold)
#     if decent_count >= 3:
#         base += 0.05

#     # Quality penalty: if top is clearly above the rest (big spread = top is uniquely relevant)
#     # vs. all scores clustered (noisy, less confident pick)
#     if len(scores) >= 2:
#         spread = top - scores[1]
#         if spread < top * 0.05:  # all very close together → less unique
#             base -= 0.05

#     return round(max(0.0, min(base, 0.95)), 2)


# def generate_answer_and_followups(
#     question: str,
#     top_chunks: List[Dict[str, Any]]
# ) -> tuple[str, List[str], str]:
#     """
#     Call the LLM ONCE to produce both the answer text and 2-3 follow-up suggestions.
#     Returns (answer_text, followups_list, model_used).
#     """
#     if not top_chunks:
#         return (
#             "I could not find that in the available documents. "
#             "Please check with your helpdesk or rephrase your question.",
#             [],
#             "none",
#         )

#     # Build context block
#     context_parts = []
#     for c in top_chunks:
#         source_label = c.get("article_title") or c.get("filename") or "Unknown"
#         context_parts.append(
#             f"[Source: {source_label} | Category: {c.get('category', '?')} | "
#             f"Relevance: {c.get('score', 0):.2f}]\n{c['text']}"
#         )
#     context = "\n\n---\n\n".join(context_parts)

#     system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
#     model = pick_chat_model(question)

#     try:
#         client = get_gpt_client()
#         resp = client.chat.completions.create(
#             model=model,
#             messages=[
#                 {"role": "system", "content": system_prompt},
#                 {"role": "user", "content": question},
#             ],
#             temperature=0.2,
#             max_tokens=900,
#             response_format={"type": "json_object"},
#             timeout=30,
#         )
#         content = resp.choices[0].message.content or "{}"
#         import json as _json
#         parsed = _json.loads(content)
#         answer = (parsed.get("answer") or "").strip()
#         followups = parsed.get("suggested_followups") or []
#         # Validate followups
#         if not isinstance(followups, list):
#             followups = []
#         followups = [str(f).strip() for f in followups if f][:3]
#         if not answer:
#             answer = "I could not find that in the available documents."
#         answer = _format_answer_with_intro_and_source(answer, top_chunks)
#         return (answer, followups, model)
#     except Exception as e:
#         log.error(f"LLM call failed: {e}")
#         return (
#             f"Sorry, I had trouble generating an answer. ({type(e).__name__})",
#             [],
#             model,
#         )


# # ═══════════════════════════════════════════════════════════
# #  PYDANTIC RESPONSE MODELS
# # ═══════════════════════════════════════════════════════════
# class ChunkOut(BaseModel):
#     text: str
#     filename: str
#     pdf_url: Optional[str] = None
#     score: float
#     page: Optional[int] = None
#     # Bonus fields (frontend can ignore)
#     article_title: Optional[str] = None
#     category: Optional[str] = None
#     sub_category: Optional[str] = None
#     chunk_id: Optional[str] = None


# class SearchResponse(BaseModel):
#     answer: str
#     confidence: float = Field(..., ge=0.0, le=1.0)
#     model_used: str
#     cached: bool
#     numFound: int
#     sources: List[str]
#     chunks: List[ChunkOut]
#     suggested_followups: List[str]
#     # Bonus / debug fields (frontend can ignore — useful for category badge UI)
#     q: Optional[str] = None
#     category_used: Optional[str] = None
#     category_source: Optional[str] = None
#     category_confidence: Optional[str] = None


# class CategoryOut(BaseModel):
#     name: str
#     display: str
#     chunk_count: int


# class CategoriesResponse(BaseModel):
#     categories: List[CategoryOut]


# class HealthResponse(BaseModel):
#     status: str
#     source: str
#     index: Dict[str, Any]
#     cache: Dict[str, Any]
#     sync: Dict[str, Any]
#     scheduler: Dict[str, Any]
#     embedding: Dict[str, Any]
#     classifier: Dict[str, Any]


# class AdminResponse(BaseModel):
#     status: str
#     message: str


# # ═══════════════════════════════════════════════════════════
# #  FASTAPI APP
# # ═══════════════════════════════════════════════════════════
# app = FastAPI(
#     title="Veelead Helpdesk RAG Bot",
#     description="Cost-efficient SharePoint-aware helpdesk bot",
#     version="1.0.0",
# )

# # CORS for frontend (lock down to specific origins in production if needed)
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
#     """API key dependency. Allows blank header on default-key for local dev."""
#     if not x_api_key:
#         raise HTTPException(status_code=401, detail="Missing x-api-key header")
#     if x_api_key != settings.api_key:
#         raise HTTPException(status_code=401, detail="Invalid API key")
#     return True


# # ═══════════════════════════════════════════════════════════
# #  STARTUP
# # ═══════════════════════════════════════════════════════════
# @app.on_event("startup")
# def startup():
#     print_config_summary()
#     log.info("═" * 60)
#     log.info("  Veelead Helpdesk RAG Bot — starting")
#     log.info("═" * 60)

#     # Validate config
#     issues = settings.validate()
#     if issues:
#         log.warning("Configuration issues found:")
#         for issue in issues:
#             log.warning(f"  ⚠ {issue}")

#     # Init local DBs
#     cache.init_db()

#     # Ensure search index exists
#     try:
#         search_index.ensure_index()
#     except Exception as e:
#         log.error(f"Could not connect to Azure AI Search: {e}")
#         log.error("API will start but searches will fail until this is fixed.")

#     # Run an initial sync (non-blocking — only if delta token missing)
#     if not cache.get_delta_token():
#         log.info("No delta token found — running initial full sync...")
#         try:
#             run_sync(force_full=True)
#         except Exception as e:
#             log.exception("Initial sync failed (will retry on next scheduled run)")

#     # Start background scheduler for periodic sync + cleanup
#     try:
#         start_scheduler()
#     except Exception as e:
#         log.exception("Failed to start scheduler — background sync disabled")

#     log.info("═" * 60)
#     log.info("  ✅ API READY")
#     log.info(f"  Endpoint: {settings.embed_endpoint}")
#     log.info(f"  Source: {settings.source_type}")
#     log.info("═" * 60)


# @app.on_event("shutdown")
# def shutdown():
#     """Gracefully stop the scheduler when FastAPI shuts down."""
#     log.info("Shutting down...")
#     try:
#         stop_scheduler()
#     except Exception:
#         pass


# # ═══════════════════════════════════════════════════════════
# #  ROOT + HEALTH
# # ═══════════════════════════════════════════════════════════
# @app.get("/")
# def root():
#     return {
#         "service": "Veelead Helpdesk RAG Bot",
#         "version": "1.0.0",
#         "endpoints": {
#             "GET /search.json?q=<question>": "main search endpoint (auth required)",
#             "GET /categories": "list categories with counts (auth required)",
#             "GET /health": "detailed status (public)",
#             "POST /admin/reindex": "trigger sync now (auth required)",
#             "POST /admin/reset_sync": "force full re-sync next time (auth required)",
#         },
#         "frontend": "Send 'x-api-key' header on every authenticated request",
#     }


# @app.get("/health", response_model=HealthResponse)
# def health():
#     try:
#         src = get_source()
#         src_name = src.source_name()
#     except Exception as e:
#         src_name = f"<error: {e}>"

#     return HealthResponse(
#         status="ok",
#         source=src_name,
#         index=search_index.get_index_stats(),
#         cache=cache.cache_stats(),
#         sync=cache.sync_state_stats(),
#         scheduler=get_scheduler_status(),
#         embedding=get_embedding_info(),
#         classifier=get_classifier_info(),
#     )


# # ═══════════════════════════════════════════════════════════
# #  CATEGORIES (for frontend buttons)
# # ═══════════════════════════════════════════════════════════
# @app.get("/categories", response_model=CategoriesResponse)
# def categories(_auth: bool = Depends(verify_api_key)):
#     cats = search_index.list_categories(only_published=True)
#     # Convert chunk_count to a more user-friendly count if needed
#     return CategoriesResponse(
#         categories=[
#             CategoryOut(
#                 name=c["name"],
#                 display=c.get("display") or c["name"],
#                 chunk_count=c["chunk_count"],
#             )
#             for c in cats
#         ]
#     )


# # ═══════════════════════════════════════════════════════════
# #  MAIN SEARCH ENDPOINT
# # ═══════════════════════════════════════════════════════════
# @app.get("/search.json", response_model=SearchResponse)
# def search(
#     q: str = Query(..., description="User question", min_length=1),
#     category: Optional[str] = Query(
#         None,
#         description="Optional category selected by user (HR, IT, Facilities, General)"
#     ),
#     _auth: bool = Depends(verify_api_key),
# ):
#     question = q.strip()
#     if not question:
#         raise HTTPException(status_code=400, detail="Query 'q' is required")

#     if is_small_talk(question):
#         answer, model_used = generate_small_talk_response(question)
#         return SearchResponse(
#             answer=answer,
#             confidence=0.15,
#             model_used=model_used,
#             cached=False,
#             numFound=0,
#             sources=[],
#             chunks=[],
#             suggested_followups=[
#                 "What can you help me with today?",
#                 "Can you help with IT or HR questions?",
#                 "Do you want a policy summary?",
#             ],
#             q=question,
#             category_used="General",
#             category_source="small_talk",
#             category_confidence="high",
#         )

#     # ── 1. Cache lookup ──
#     cached_resp = cache.cache_lookup(question)
#     if cached_resp:
#         # Schema check: skip entries from older bot versions
#         required_new = {"confidence", "chunks", "suggested_followups"}
#         if all(k in cached_resp for k in required_new):
#             log.info(f"💰 Cache HIT: {question[:60]}")
#             cached_resp["cached"] = True
#             cached_resp.setdefault("category_source", "cached")
#             try:
#                 return SearchResponse(**cached_resp)
#             except Exception as e:
#                 log.warning(f"Cached entry invalid, regenerating: {e}")
#         else:
#             log.info(f"Cache entry old-schema, regenerating: {question[:60]}")

#     # ── 2. Determine category ──
#     # Get the live list of categories that actually exist in the index
#     all_categories = [c["name"] for c in search_index.list_categories(only_published=True)]

#     if category:
#         # User-selected: validate it exists (case-insensitive)
#         cat_match = next((c for c in all_categories
#                           if c.lower() == category.lower()), None)
#         if not cat_match:
#             log.warning(f"User selected unknown category '{category}'. "
#                         f"Available: {all_categories}")
#             # Fall through to AI-predicted
#             cat_match = None

#         if cat_match:
#             used_category = cat_match
#             category_source = "user_selected"
#             category_confidence = "high"
#         else:
#             classification = classify(question, all_categories)
#             used_category = classification["category"]
#             category_source = "ai_predicted"
#             category_confidence = classification["confidence"]
#     else:
#         # No category specified — let the classifier decide
#         classification = classify(question, all_categories)
#         used_category = classification["category"]
#         category_source = "ai_predicted"
#         category_confidence = classification["confidence"]

#     # ── 3. Embed the query + hybrid search ──
#     try:
#         query_vec = embed_one(question)
#     except Exception as e:
#         log.error(f"Query embedding failed: {e}")
#         raise HTTPException(status_code=500, detail="Query embedding service unavailable")

#     chunks: List[Dict[str, Any]] = []

#     if wants_combined_it_hr_summary(question):
#         requested_categories = [c for c in ("IT", "HR") if c in all_categories]
#         per_category_k = max(3, settings.top_k_use // max(1, len(requested_categories)))
#         for cat in requested_categories:
#             cat_chunks = search_index.hybrid_search(
#                 query=question,
#                 query_vector=query_vec,
#                 top_k=per_category_k,
#                 category=cat,
#                 include_uncategorized=False,
#                 only_published=True,
#             )
#             chunks.extend(cat_chunks)

#         # De-duplicate by chunk_id when available, otherwise by filename+text prefix.
#         deduped: List[Dict[str, Any]] = []
#         seen_keys = set()
#         for c in chunks:
#             key = c.get("chunk_id") or f"{c.get('filename','')}::{c.get('text','')[:120]}"
#             if key not in seen_keys:
#                 seen_keys.add(key)
#                 deduped.append(c)
#         chunks = deduped
#         used_category = "IT + HR"
#         category_source = "multi_category"
#         category_confidence = "high"
#     else:
#         # If category is Uncategorized, search WITHOUT category filter
#         # (Uncategorized is included automatically alongside any selected category)
#         search_category = None if used_category == "Uncategorized" else used_category

#         chunks = search_index.hybrid_search(
#             query=question,
#             query_vector=query_vec,
#             top_k=settings.top_k_use,
#             category=search_category,
#             include_uncategorized=True,
#             only_published=True,
#         )

#         # ── 4. Fallback: if no results and category was applied, retry without it ──
#         if not chunks and search_category:
#             log.info(f"  No results in {search_category}. Falling back to all categories.")
#             chunks = search_index.hybrid_search(
#                 query=question,
#                 query_vector=query_vec,
#                 top_k=settings.top_k_use,
#                 category=None,
#                 only_published=True,
#             )
#             category_source = "fallback_all"

#     # ── 5. Generate answer + follow-ups (single LLM call) ──
#     answer, followups, model_used = generate_answer_and_followups(question, chunks)

#     # ── 6. Compute confidence from chunk scores ──
#     confidence = compute_confidence(chunks) if chunks else 0.0

#     # If we have no chunks at all, force low confidence
#     if not chunks:
#         confidence = min(confidence, 0.30)

#     # ── 7. Build response in the new schema ──
#     chunks_out = [
#         ChunkOut(
#             text=c["text"],
#             filename=c.get("filename") or "",
#             pdf_url=c.get("pdf_url") or None,
#             score=round(float(c.get("score", 0)), 4),
#             page=c.get("page"),
#             article_title=c.get("article_title"),
#             category=c.get("category"),
#             sub_category=c.get("sub_category"),
#             chunk_id=c.get("chunk_id"),
#         )
#         for c in chunks
#     ]

#     # Deduplicate sources: filename-based (matches new schema example)
#     seen = set()
#     sources: List[str] = []
#     for c in chunks:
#         fn = c.get("filename")
#         if fn and fn not in seen:
#             seen.add(fn)
#             sources.append(fn)

#     response = SearchResponse(
#         answer=answer,
#         confidence=confidence,
#         model_used=model_used,
#         cached=False,
#         numFound=len(chunks_out),
#         sources=sources,
#         chunks=chunks_out,
#         suggested_followups=followups,
#         q=question,
#         category_used=used_category,
#         category_source=category_source,
#         category_confidence=category_confidence,
#     )

#     # ── 8. Cache the response (skips empty/no-answer answers automatically) ──
#     cache.cache_store(question, response.model_dump())

#     return response


# # ═══════════════════════════════════════════════════════════
# #  ADMIN ENDPOINTS
# # ═══════════════════════════════════════════════════════════
# @app.post("/admin/reindex", response_model=AdminResponse)
# def admin_reindex(
#     background_tasks: BackgroundTasks,
#     force_full: bool = Query(False, description="Force a full re-sync (ignore delta token)"),
#     _auth: bool = Depends(verify_api_key),
# ):
#     """Trigger a sync now. Runs in background — endpoint returns immediately."""
#     background_tasks.add_task(run_sync, force_full=force_full)
#     msg = "full re-sync" if force_full else "delta sync"
#     return AdminResponse(status="started", message=f"{msg} started in background")


# @app.post("/admin/reset_sync", response_model=AdminResponse)
# def admin_reset_sync(_auth: bool = Depends(verify_api_key)):
#     """Clear the delta token. Next sync will be a full sync."""
#     cache.reset_delta_token()
#     return AdminResponse(
#         status="ok",
#         message="Delta token cleared. Next sync will be a full sync."
#     )


# # ═══════════════════════════════════════════════════════════
# #  ENTRY POINT (for `python app.py`)
# # ═══════════════════════════════════════════════════════════
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)


"""
app.py — Veelead Helpdesk RAG Bot, FastAPI server.

Endpoints:
    GET  /                       — health/info
    GET  /health                 — detailed status (stats, models, sync history)
    GET  /categories             — list categories with chunk counts (for frontend buttons)
    GET  /search.json?q=...      — main query endpoint
    POST /admin/reindex          — trigger sync now (background task)
    POST /admin/reset_sync       — force full re-sync next time

Authentication:
    All non-info endpoints require header: x-api-key: <API_KEY from .env>
    /health and / are public for monitoring.

Run locally:
    uvicorn app:app --reload --port 8000

Run in production (Azure App Service):
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import AzureOpenAI

from config import settings, print_config_summary
from sources import get_source, DocumentRef, ChangeSet
from pipeline.extractors import extract_bytes, extract_with_pages
from pipeline.chunker import chunk_document
from pipeline.embedder import embed_one, embed_many, get_embedding_info
from pipeline.classifier import classify, get_classifier_info
from storage import search_index
from storage import cache
from scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")


# ═══════════════════════════════════════════════════════════
#  LLM CLIENT (for answer generation)
# ═══════════════════════════════════════════════════════════
_gpt_client: Optional[AzureOpenAI] = None


def get_gpt_client() -> AzureOpenAI:
    global _gpt_client
    if _gpt_client is None:
        _gpt_client = AzureOpenAI(
            api_key=settings.gpt_api_key,
            api_version=settings.gpt_api_ver,
            azure_endpoint=settings.gpt_endpoint,
        )
    return _gpt_client


# ═══════════════════════════════════════════════════════════
#  QUERY DETECTION HELPERS
# ═══════════════════════════════════════════════════════════
import re
COMPLEX_QUERY_RE = re.compile(
    r"\b(compare|difference|versus|vs|analy[sz]e|summari[sz]e|"
    r"explain why|step.by.step|multi(ple)?|across|between)\b", re.I
)
SUMMARY_QUERY_RE = re.compile(r"\b(summar(?:y|ize|ise)|overview|cover(?:s|ed)?|what does)\b", re.I)
SMALL_TALK_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|good\s+(morning|afternoon|evening)|"
    r"thanks|thank\s+you|how are you|what'?s up|yo)\s*[!.?]*\s*$",
    re.I,
)
CAPABILITY_RE = re.compile(
    r"^\s*(can you help me|help|anyone there|i have (?:a )?doubt|"
    r"one doubts?|i need help)\s*[!.?]*\s*$",
    re.I,
)

# Phrases that indicate the LLM said "could not find" — used to cap confidence low
# and prevent caching of negative responses.
NO_ANSWER_MARKERS = (
    "could not find",
    "couldn't find",
    "cannot find",
    "no information",
    "not available in",
    "not mentioned in",
    "not found in",
)


def pick_chat_model(question: str) -> str:
    """Choose gpt-4o-mini for typical queries, gpt-4o for complex ones."""
    if len(question.split()) > 30 or COMPLEX_QUERY_RE.search(question):
        return settings.gpt_large_deploy
    return settings.gpt_mini_deploy


def is_small_talk(question: str) -> bool:
    """Detect greetings and simple capability/help prompts."""
    q = question.strip().lower()
    return bool(SMALL_TALK_RE.match(q) or CAPABILITY_RE.match(q) or q in {"hi", "hello", "hey", "yo"})


def generate_small_talk_response(question: str) -> tuple[str, str]:
    """Return a short friendly response without running document search."""
    q = question.strip().lower()
    if q in {"hi", "hello", "hey", "yo"} or SMALL_TALK_RE.match(q):
        return ("Hello! How can I help you today?", "small_talk")
    return (
        "Sure, I can help. Please share your IT, HR, Facilities, or policy-related question.",
        "small_talk",
    )


def wants_combined_it_hr_summary(question: str) -> bool:
    """Detect summary-style questions that explicitly mention both IT and HR."""
    q = question.lower()
    has_it = re.search(r"\bit\b", q) is not None
    has_hr = re.search(r"\bhr\b", q) is not None
    return has_it and has_hr and bool(SUMMARY_QUERY_RE.search(q))


def _is_no_answer(answer: str) -> bool:
    """True if the LLM admits it could not find an answer in the documents."""
    if not answer:
        return True
    a = answer.lower()
    return any(marker in a for marker in NO_ANSWER_MARKERS)


def _unique_source_labels(top_chunks: List[Dict[str, Any]], limit: int = 2) -> List[str]:
    labels: List[str] = []
    seen = set()
    for c in top_chunks:
        label = c.get("filename") or c.get("article_title")
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _format_answer_with_intro_and_source(
    answer: str,
    top_chunks: List[Dict[str, Any]],
) -> str:
    """
    Normalize the final answer style for short helpdesk responses.

    - Adds "I found a solution!" prefix ONLY for affirmative answers
    - Appends "(Source: filename.pdf)" ONLY for affirmative answers
    - Leaves "could not find" / "sorry" answers UNTOUCHED (no fake source citation)
    """
    cleaned = (answer or "").strip()
    if not cleaned:
        return cleaned

    # Don't prefix or add source for negative answers — citing a source for
    # "I could not find" would be misleading
    if _is_no_answer(cleaned):
        return cleaned

    # Add "I found a solution!" prefix only if the LLM didn't already write
    # a friendly opener
    lower = cleaned.lower()
    friendly_openers = ("i found", "sure", "here", "great question",
                        "absolutely", "yes,", "yes.")
    if not lower.startswith(friendly_openers):
        cleaned = f"I found a solution!\n\n{cleaned}"

    # Append source citation
    source_labels = _unique_source_labels(top_chunks)
    if source_labels and "source:" not in cleaned.lower():
        cleaned = f"{cleaned}\n\n(Source: {', '.join(source_labels)})"
    return cleaned


# ═══════════════════════════════════════════════════════════
#  INDEXING PIPELINE
# ═══════════════════════════════════════════════════════════
def index_document(ref: DocumentRef) -> int:
    """
    Process one document: download → extract → chunk → embed → upload.
    If the doc already has chunks in the index, deletes them first.
    Returns count of chunks uploaded.
    """
    source = get_source()
    log.info(f"  → Indexing: {ref.filename}")

    # 1. Download
    data = source.download(ref)

    # 2. Extract text — page-aware when possible (PDF)
    pages_data = None
    if data.pre_extracted_text:
        text = data.pre_extracted_text
    elif data.content_bytes:
        try:
            pages_data = extract_with_pages(data.content_bytes, ref.doc_type)
            text = "\n\n".join(p[0] for p in pages_data) if pages_data else ""
        except Exception as e:
            log.error(f"     ✗ Extraction failed for {ref.filename}: {e}")
            return 0
    else:
        log.error(f"     ✗ No content for {ref.filename}")
        return 0

    if not text or not text.strip():
        log.warning(f"     ⚠ Empty text for {ref.filename} — skipping")
        return 0

    # 3. Chunk + attach metadata
    doc_for_chunking = {
        **ref.to_doc_metadata(),
        "text": text,
        "pages": pages_data,
    }
    chunks = chunk_document(
        doc_for_chunking,
        chunk_size=settings.chunk_size,
        overlap=settings.chunk_overlap,
    )

    if not chunks:
        log.warning(f"     ⚠ No chunks produced for {ref.filename}")
        return 0

    # 4. Delete old chunks if any (idempotent — handles updates cleanly)
    if ref.file_id:
        search_index.delete_by_file_id(ref.file_id)

    # 5. Embed
    log.info(f"     • Embedding {len(chunks)} chunks...")
    texts = [c["text"] for c in chunks]
    vectors = embed_many(texts)
    for chunk, vec in zip(chunks, vectors):
        chunk["embedding"] = vec

    # 6. Upload to Azure AI Search
    result = search_index.upsert_chunks(chunks)
    log.info(f"     ✓ {result['uploaded']} uploaded, {result['failed']} failed")

    # 7. Invalidate cache for this file
    cache.cache_invalidate_by_file_id(ref.file_id)
    cache.cache_invalidate_by_filename(ref.filename)

    return result["uploaded"]


def run_sync(force_full: bool = False) -> Dict[str, Any]:
    """
    Run a sync cycle:
      - Get changes from source (delta sync, unless force_full)
      - Apply additions/updates/deletes
      - Save new delta token
      - Record audit log
    """
    started_at = datetime.now(timezone.utc)
    source = get_source()
    source_type_name = settings.source_type

    log.info("─" * 60)
    log.info(f"  SYNC START ({source.source_name()})")
    log.info("─" * 60)

    delta_token = None if force_full else cache.get_delta_token()
    try:
        changes: ChangeSet = source.get_changes(delta_token)
    except Exception as e:
        log.exception("Sync failed during get_changes()")
        cache.record_sync_run(
            source_type=source_type_name,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            status="failed",
            error_msg=str(e),
        )
        return {"status": "failed", "error": str(e)}

    if changes.is_empty():
        log.info("  No changes detected.")
        if changes.new_delta_token:
            cache.save_delta_token(changes.new_delta_token)
        finished_at = datetime.now(timezone.utc)
        run_id = cache.record_sync_run(
            source_type=source_type_name,
            started_at=started_at,
            finished_at=finished_at,
            added=0, updated=0, deleted=0,
            status="success",
        )
        return {
            "status": "success",
            "run_id": run_id,
            "added": 0, "updated": 0, "deleted": 0,
            "duration_sec": (finished_at - started_at).total_seconds(),
        }

    # ── DELETIONS first (clean state) ──
    deleted_count = 0
    for file_id in changes.deleted:
        try:
            n = search_index.delete_by_file_id(file_id)
            cache.cache_invalidate_by_file_id(file_id)
            deleted_count += 1
            log.info(f"  − Deleted file {file_id}: {n} chunks removed")
        except Exception as e:
            log.error(f"  ✗ Delete failed for {file_id}: {e}")

    # ── ADDITIONS + UPDATES ──
    added_count = 0
    updated_count = 0
    failed = 0

    for ref in changes.added:
        try:
            n = index_document(ref)
            if n > 0:
                added_count += 1
        except Exception as e:
            log.error(f"  ✗ Add failed for {ref.filename}: {e}")
            failed += 1

    for ref in changes.updated:
        try:
            n = index_document(ref)
            if n > 0:
                updated_count += 1
        except Exception as e:
            log.error(f"  ✗ Update failed for {ref.filename}: {e}")
            failed += 1

    # Save new delta token
    if changes.new_delta_token:
        cache.save_delta_token(changes.new_delta_token)

    finished_at = datetime.now(timezone.utc)
    status = "success" if failed == 0 else "partial"
    run_id = cache.record_sync_run(
        source_type=source_type_name,
        started_at=started_at,
        finished_at=finished_at,
        added=added_count,
        updated=updated_count,
        deleted=deleted_count,
        status=status,
        error_msg=f"{failed} items failed" if failed else None,
    )

    duration = (finished_at - started_at).total_seconds()
    log.info("─" * 60)
    log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
             f"({failed} failed, {duration:.1f}s)")
    log.info("─" * 60)

    return {
        "status": status,
        "run_id": run_id,
        "added": added_count,
        "updated": updated_count,
        "deleted": deleted_count,
        "failed": failed,
        "duration_sec": duration,
    }


# ═══════════════════════════════════════════════════════════
#  ANSWER GENERATION + FOLLOW-UPS (single LLM call)
# ═══════════════════════════════════════════════════════════
SYSTEM_PROMPT_TEMPLATE = """You are a Helpdesk Assistant for Veelead Solutions.
Answer questions strictly from the document context provided.

Return ONLY a JSON object with this exact shape (no markdown, no extra text):

{{
  "answer": "A clear, helpful answer in 2-5 sentences. Direct and actionable. Friendly and professional, written for non-technical employees.",
  "suggested_followups": [
    "Short specific follow-up question 1",
    "Short specific follow-up question 2",
    "Short specific follow-up question 3"
  ]
}}

Rules:
- Use only information from the context below. Never invent facts.
- If the context doesn't contain the answer, set "answer" to EXACTLY:
  "I could not find that in the available documents. Please check with your helpdesk or rephrase your question."
- If the user asks for a summary of IT, HR, Facilities, or General documents, give a short document-based summary from the retrieved context.
- When the answer contains multiple details, format them as 2-5 numbered points.
- Keep "suggested_followups" to 2-3 short, specific questions related to the topic.
- Do not include the document filename or source in the answer text — that goes separately.

DOCUMENT CONTEXT:
{context}"""


def compute_confidence(chunks: List[Dict[str, Any]]) -> float:
    """
    Compute a 0.0-1.0 confidence score from Azure AI Search RRF scores.

    RRF scores are typically:
      - 0.030+       : strong match
      - 0.020-0.030  : moderate
      - 0.010-0.020  : weak
      - <0.010       : likely irrelevant
    """
    if not chunks:
        return 0.0

    scores = [float(c.get("score", 0)) for c in chunks]
    top = max(scores)

    # Normalize top score: 0.035+ → 0.90, 0.025 → 0.65, 0.015 → 0.40
    if top >= 0.035:
        base = 0.90
    elif top >= 0.025:
        base = 0.65 + (top - 0.025) * 25
    elif top >= 0.015:
        base = 0.40 + (top - 0.015) * 25
    elif top >= 0.008:
        base = 0.15 + (top - 0.008) * (25 / 7)
    else:
        base = top * (0.15 / 0.008)

    # Corroboration boost
    threshold = top * 0.75
    decent_count = sum(1 for s in scores if s >= threshold)
    if decent_count >= 3:
        base += 0.05

    # Quality penalty
    if len(scores) >= 2:
        spread = top - scores[1]
        if spread < top * 0.05:
            base -= 0.05

    return round(max(0.0, min(base, 0.95)), 2)


def generate_answer_and_followups(
    question: str,
    top_chunks: List[Dict[str, Any]]
) -> tuple[str, List[str], str]:
    """
    Call the LLM ONCE to produce both the answer text and 2-3 follow-up suggestions.
    Returns (answer_text, followups_list, model_used).
    """
    if not top_chunks:
        return (
            "I could not find that in the available documents. "
            "Please check with your helpdesk or rephrase your question.",
            [],
            "none",
        )

    # Build context block
    context_parts = []
    for c in top_chunks:
        source_label = c.get("article_title") or c.get("filename") or "Unknown"
        context_parts.append(
            f"[Source: {source_label} | Category: {c.get('category', '?')} | "
            f"Relevance: {c.get('score', 0):.2f}]\n{c['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context)
    model = pick_chat_model(question)

    try:
        client = get_gpt_client()
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.2,
            max_tokens=900,
            response_format={"type": "json_object"},
            timeout=30,
        )
        content = resp.choices[0].message.content or "{}"
        import json as _json
        parsed = _json.loads(content)
        answer = (parsed.get("answer") or "").strip()
        followups = parsed.get("suggested_followups") or []
        if not isinstance(followups, list):
            followups = []
        followups = [str(f).strip() for f in followups if f][:3]
        if not answer:
            answer = "I could not find that in the available documents."
        # Apply friendly formatting (skipped for "could not find" answers)
        answer = _format_answer_with_intro_and_source(answer, top_chunks)
        return (answer, followups, model)
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        return (
            f"Sorry, I had trouble generating an answer. ({type(e).__name__})",
            [],
            model,
        )


# ═══════════════════════════════════════════════════════════
#  PYDANTIC RESPONSE MODELS
# ═══════════════════════════════════════════════════════════
class ChunkOut(BaseModel):
    text: str
    filename: str
    pdf_url: Optional[str] = None
    score: float
    page: Optional[int] = None
    article_title: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    chunk_id: Optional[str] = None


class SearchResponse(BaseModel):
    answer: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_used: str
    cached: bool
    numFound: int
    sources: List[str]
    chunks: List[ChunkOut]
    suggested_followups: List[str]
    q: Optional[str] = None
    category_used: Optional[str] = None
    category_source: Optional[str] = None
    category_confidence: Optional[str] = None


class CategoryOut(BaseModel):
    name: str
    display: str
    chunk_count: int


class CategoriesResponse(BaseModel):
    categories: List[CategoryOut]


class HealthResponse(BaseModel):
    status: str
    source: str
    index: Dict[str, Any]
    cache: Dict[str, Any]
    sync: Dict[str, Any]
    scheduler: Dict[str, Any]
    embedding: Dict[str, Any]
    classifier: Dict[str, Any]


class AdminResponse(BaseModel):
    status: str
    message: str


# ═══════════════════════════════════════════════════════════
#  FASTAPI APP
# ═══════════════════════════════════════════════════════════
app = FastAPI(
    title="Veelead Helpdesk RAG Bot",
    description="Cost-efficient SharePoint-aware helpdesk bot",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing x-api-key header")
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# ═══════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════
# @app.on_event("startup")
# def startup():
#     print_config_summary()
#     log.info("═" * 60)
#     log.info("  Veelead Helpdesk RAG Bot — starting")
#     log.info("═" * 60)

#     issues = settings.validate()
#     if issues:
#         log.warning("Configuration issues found:")
#         for issue in issues:
#             log.warning(f"  ⚠ {issue}")

#     cache.init_db()

#     try:
#         search_index.ensure_index()
#     except Exception as e:
#         log.error(f"Could not connect to Azure AI Search: {e}")
#         log.error("API will start but searches will fail until this is fixed.")

#     if not cache.get_delta_token():
#         log.info("No delta token found — running initial full sync...")
#         try:
#             run_sync(force_full=True)
#         except Exception as e:
#             log.exception("Initial sync failed (will retry on next scheduled run)")

#     try:
#         start_scheduler()
#     except Exception as e:
#         log.exception("Failed to start scheduler — background sync disabled")

#     log.info("═" * 60)
#     log.info("  ✅ API READY")
#     log.info(f"  Endpoint: {settings.embed_endpoint}")
#     log.info(f"  Source: {settings.source_type}")
#     log.info("═" * 60)

@app.on_event("startup")
def startup():
    print_config_summary()
    log.info("═" * 60)
    log.info("  Veelead Helpdesk RAG Bot — starting")
    log.info("═" * 60)

    issues = settings.validate()
    if issues:
        log.warning("Configuration issues found:")
        for issue in issues:
            log.warning(f"  ⚠ {issue}")

    cache.init_db()

    try:
        search_index.ensure_index()
    except Exception as e:
        log.error(f"Could not connect to Azure AI Search: {e}")
        log.error("API will start but searches will fail until this is fixed.")

    # ✅ Run sync in background — don't block startup
    if not cache.get_delta_token():
        log.info("No delta token found — scheduling initial full sync in background...")
        import threading
        threading.Thread(
            target=_background_sync,
            args=(True,),
            daemon=True,
            name="initial-sync"
        ).start()

    try:
        start_scheduler()
    except Exception as e:
        log.exception("Failed to start scheduler — background sync disabled")

    log.info("═" * 60)
    log.info("  ✅ API READY")
    log.info(f"  Endpoint: {settings.embed_endpoint}")
    log.info(f"  Source: {settings.source_type}")
    log.info("═" * 60)


def _background_sync(force_full: bool = False):
    """Wrapper for run_sync that catches all exceptions safely."""
    try:
        run_sync(force_full=force_full)
    except Exception:
        log.exception("Background sync failed")
        
@app.on_event("shutdown")
def shutdown():
    log.info("Shutting down...")
    try:
        stop_scheduler()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  ROOT + HEALTH
# ═══════════════════════════════════════════════════════════
@app.get("/")
def root():
    return {
        "service": "Veelead Helpdesk RAG Bot",
        "version": "1.0.0",
        "endpoints": {
            "GET /search.json?q=<question>": "main search endpoint (auth required)",
            "GET /categories": "list categories with counts (auth required)",
            "GET /health": "detailed status (public)",
            "POST /admin/reindex": "trigger sync now (auth required)",
            "POST /admin/reset_sync": "force full re-sync next time (auth required)",
        },
        "frontend": "Send 'x-api-key' header on every authenticated request",
    }


@app.get("/health", response_model=HealthResponse)
def health():
    try:
        src = get_source()
        src_name = src.source_name()
    except Exception as e:
        src_name = f"<error: {e}>"

    return HealthResponse(
        status="ok",
        source=src_name,
        index=search_index.get_index_stats(),
        cache=cache.cache_stats(),
        sync=cache.sync_state_stats(),
        scheduler=get_scheduler_status(),
        embedding=get_embedding_info(),
        classifier=get_classifier_info(),
    )


# ═══════════════════════════════════════════════════════════
#  CATEGORIES (for frontend buttons)
# ═══════════════════════════════════════════════════════════
@app.get("/categories", response_model=CategoriesResponse)
def categories(_auth: bool = Depends(verify_api_key)):
    cats = search_index.list_categories(only_published=True)
    return CategoriesResponse(
        categories=[
            CategoryOut(
                name=c["name"],
                display=c.get("display") or c["name"],
                chunk_count=c["chunk_count"],
            )
            for c in cats
        ]
    )


# ═══════════════════════════════════════════════════════════
#  MAIN SEARCH ENDPOINT
# ═══════════════════════════════════════════════════════════
@app.get("/search.json", response_model=SearchResponse)
def search(
    q: str = Query(..., description="User question", min_length=1),
    category: Optional[str] = Query(
        None,
        description="Optional category selected by user (HR, IT, Facilities, General)"
    ),
    _auth: bool = Depends(verify_api_key),
):
    question = q.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Query 'q' is required")

    # ── 0. Small-talk fast path ──
    if is_small_talk(question):
        answer, model_used = generate_small_talk_response(question)
        return SearchResponse(
            answer=answer,
            confidence=0.15,
            model_used=model_used,
            cached=False,
            numFound=0,
            sources=[],
            chunks=[],
            suggested_followups=[
                "What can you help me with today?",
                "Can you help with IT or HR questions?",
                "Do you want a policy summary?",
            ],
            q=question,
            category_used="General",
            category_source="small_talk",
            category_confidence="high",
        )

    # ── 1. Cache lookup ──
    cached_resp = cache.cache_lookup(question)
    if cached_resp:
        required_new = {"confidence", "chunks", "suggested_followups"}
        if all(k in cached_resp for k in required_new):
            log.info(f"💰 Cache HIT: {question[:60]}")
            cached_resp["cached"] = True
            cached_resp.setdefault("category_source", "cached")
            try:
                return SearchResponse(**cached_resp)
            except Exception as e:
                log.warning(f"Cached entry invalid, regenerating: {e}")
        else:
            log.info(f"Cache entry old-schema, regenerating: {question[:60]}")

    # ── 2. Determine category ──
    all_categories = [c["name"] for c in search_index.list_categories(only_published=True)]

    if category:
        cat_match = next((c for c in all_categories
                          if c.lower() == category.lower()), None)
        if not cat_match:
            log.warning(f"User selected unknown category '{category}'. "
                        f"Available: {all_categories}")
            cat_match = None

        if cat_match:
            used_category = cat_match
            category_source = "user_selected"
            category_confidence = "high"
        else:
            classification = classify(question, all_categories)
            used_category = classification["category"]
            category_source = "ai_predicted"
            category_confidence = classification["confidence"]
    else:
        classification = classify(question, all_categories)
        used_category = classification["category"]
        category_source = "ai_predicted"
        category_confidence = classification["confidence"]

    # ── 3. Embed the query + hybrid search ──
    try:
        query_vec = embed_one(question)
    except Exception as e:
        log.error(f"Query embedding failed: {e}")
        raise HTTPException(status_code=500, detail="Query embedding service unavailable")

    chunks: List[Dict[str, Any]] = []

    if wants_combined_it_hr_summary(question):
        requested_categories = [c for c in ("IT", "HR") if c in all_categories]
        per_category_k = max(3, settings.top_k_use // max(1, len(requested_categories)))
        for cat in requested_categories:
            cat_chunks = search_index.hybrid_search(
                query=question,
                query_vector=query_vec,
                top_k=per_category_k,
                category=cat,
                include_uncategorized=False,
                only_published=True,
            )
            chunks.extend(cat_chunks)

        # Deduplicate by chunk_id or filename+text-prefix
        deduped: List[Dict[str, Any]] = []
        seen_keys = set()
        for c in chunks:
            key = c.get("chunk_id") or f"{c.get('filename','')}::{c.get('text','')[:120]}"
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(c)
        chunks = deduped
        used_category = "IT + HR"
        category_source = "multi_category"
        category_confidence = "high"
    else:
        search_category = None if used_category == "Uncategorized" else used_category

        chunks = search_index.hybrid_search(
            query=question,
            query_vector=query_vec,
            top_k=settings.top_k_use,
            category=search_category,
            include_uncategorized=True,
            only_published=True,
        )

        # ── 4. Fallback: retry without category if empty ──
        if not chunks and search_category:
            log.info(f"  No results in {search_category}. Falling back to all categories.")
            chunks = search_index.hybrid_search(
                query=question,
                query_vector=query_vec,
                top_k=settings.top_k_use,
                category=None,
                only_published=True,
            )
            category_source = "fallback_all"

    # ── 5. Generate answer + follow-ups (single LLM call) ──
    answer, followups, model_used = generate_answer_and_followups(question, chunks)

    # ── 6. Compute confidence from chunk scores ──
    confidence = compute_confidence(chunks) if chunks else 0.0

    # If no chunks, force low confidence
    if not chunks:
        confidence = min(confidence, 0.30)

    # If the LLM admits it could not find the answer, cap confidence at 0.30
    # (chunk scores may have been high but the answer is effectively "no answer")
    if _is_no_answer(answer):
        confidence = min(confidence, 0.30)

    # ── 7. Build response ──
    chunks_out = [
        ChunkOut(
            text=c["text"],
            filename=c.get("filename") or "",
            pdf_url=c.get("pdf_url") or None,
            score=round(float(c.get("score", 0)), 4),
            page=c.get("page"),
            article_title=c.get("article_title"),
            category=c.get("category"),
            sub_category=c.get("sub_category"),
            chunk_id=c.get("chunk_id"),
        )
        for c in chunks
    ]

    # Deduplicate sources
    seen = set()
    sources: List[str] = []
    for c in chunks:
        fn = c.get("filename")
        if fn and fn not in seen:
            seen.add(fn)
            sources.append(fn)

    response = SearchResponse(
        answer=answer,
        confidence=confidence,
        model_used=model_used,
        cached=False,
        numFound=len(chunks_out),
        sources=sources,
        chunks=chunks_out,
        suggested_followups=followups,
        q=question,
        category_used=used_category,
        category_source=category_source,
        category_confidence=category_confidence,
    )

    # ── 8. Cache the response (skip caching "no answer" responses) ──
    # Don't cache negative results — next sync may have indexed the answer
    if not _is_no_answer(answer):
        cache.cache_store(question, response.model_dump())

    return response


# ═══════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════
@app.post("/admin/reindex", response_model=AdminResponse)
def admin_reindex(
    background_tasks: BackgroundTasks,
    force_full: bool = Query(False, description="Force a full re-sync (ignore delta token)"),
    _auth: bool = Depends(verify_api_key),
):
    """Trigger a sync now. Runs in background — endpoint returns immediately."""
    background_tasks.add_task(run_sync, force_full=force_full)
    msg = "full re-sync" if force_full else "delta sync"
    return AdminResponse(status="started", message=f"{msg} started in background")


@app.post("/admin/reset_sync", response_model=AdminResponse)
def admin_reset_sync(_auth: bool = Depends(verify_api_key)):
    """Clear the delta token. Next sync will be a full sync."""
    cache.reset_delta_token()
    return AdminResponse(
        status="ok",
        message="Delta token cleared. Next sync will be a full sync."
    )


# ═══════════════════════════════════════════════════════════
#  ENTRY POINT (for `python app.py`)
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)