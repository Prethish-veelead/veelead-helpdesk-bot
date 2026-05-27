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
import threading
from concurrent.futures import ThreadPoolExecutor
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
#  LOGGING — with per-request IDs for traceability
# ═══════════════════════════════════════════════════════════
import contextvars as _ctxv

# Holds the current request's unique ID. Set by middleware on each
# request, read by the log filter to inject `[req-xxxx]` into every
# log line emitted while handling that request.
# _request_id_var: _ctxv.ContextVar[str] = _ctxv.ContextVar("request_id", default="")


# class _RequestIdFilter(logging.Filter):
#     """Adds request_id from contextvar to every log record."""
#     def filter(self, record: logging.LogRecord) -> bool:
#         rid = _request_id_var.get()
#         record.request_id = f"[req-{rid}] " if rid else ""
#         return True

_request_id_var: _ctxv.ContextVar[str] = _ctxv.ContextVar("request_id", default="")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rid = _request_id_var.get()
        record.request_id = f"[req-{rid}] " if rid else ""
        return True


# Sensitive substrings we never want in logs. If a log message contains
# any of these (e.g. someone accidentally logged the whole .env), the
# filter replaces them with [REDACTED].
_SENSITIVE_PATTERNS = (
    "api_key=", "api-key:", "x-api-key", "Authorization:", "Bearer ",
    "AZURE_OPENAI_API_KEY", "AZURE_SEARCH_KEY",
    "CLIENT_SECRET", "client_secret",
)


class _RedactSensitiveFilter(logging.Filter):
    """Redact accidentally-logged secrets. Cheap defence in depth."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for pat in _SENSITIVE_PATTERNS:
            if pat in msg:
                # Replace value portion with [REDACTED]
                # Naive but adequate — these strings shouldn't appear in normal logs
                record.msg = msg.replace(pat, f"{pat}[REDACTED]")
                record.args = ()  # we already inlined the message
                break
        return True


# logging.basicConfig(
#     level=getattr(logging, settings.log_level, logging.INFO),
#     format="%(asctime)s [%(levelname)s] %(request_id)s%(name)s: %(message)s",
# )
# # Attach filters to the root logger so they apply to everyone
# _root = logging.getLogger()
# _root.addFilter(_RequestIdFilter())
# _root.addFilter(_RedactSensitiveFilter())
# Build handler first, attach filters to it BEFORE basicConfig
_handler = logging.StreamHandler()
_handler.addFilter(_RequestIdFilter())
_handler.addFilter(_RedactSensitiveFilter())
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(request_id)s%(name)s: %(message)s"
))

# Use basicConfig only to set level; don't let it create its own handler
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    handlers=[_handler],   # <-- pass OUR handler, not the default StreamHandler
)


log = logging.getLogger("app")


def _new_request_id() -> str:
    """Generate a short, log-friendly request ID (8 hex chars)."""
    import secrets
    return secrets.token_hex(4)


# Shared thread pool for running independent I/O calls in parallel
# during each request. 4 workers is plenty for our needs.
_io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bot-io")


# ═══════════════════════════════════════════════════════════
#  CACHE VALIDATION — drop stale entries citing deleted files
# ═══════════════════════════════════════════════════════════

# In-memory cache of "filenames currently in the search index".
# Refreshed at most once per CACHE_VALIDATE_TTL_SEC to avoid hammering
# the index on every cache hit.
_indexed_filenames: Dict[str, Any] = {"value": None, "expires_at": 0.0}
_CACHE_VALIDATE_TTL_SEC = 60


def _get_indexed_filenames() -> Optional[set]:
    """
    Return the set of filenames currently in the Azure AI Search index,
    cached in-process for 60s. Returns None if the lookup fails — in
    that case the caller should ASSUME VALID (don't punish users for a
    transient Azure outage).
    """
    import time as _time
    now = _time.time()

    if _indexed_filenames["value"] is not None and _indexed_filenames["expires_at"] > now:
        return _indexed_filenames["value"]

    try:
        from storage.search_index import _get_search_client
        client = _get_search_client()
        results = client.search(
            search_text="*",
            select=["filename"],
            top=5000,
        )
        names = set()
        for r in results:
            fn = r.get("filename")
            if fn:
                names.add(fn)

        _indexed_filenames["value"] = names
        _indexed_filenames["expires_at"] = now + _CACHE_VALIDATE_TTL_SEC
        return names
    except Exception as e:
        log.warning(f"Could not fetch indexed filenames for cache validation: {e}")
        return None


def _is_cached_entry_still_valid(cached_resp: Dict[str, Any]) -> bool:
    """
    True if every source filename in the cached response is still in the
    current search index. False if any source file is missing (deleted/
    renamed since the entry was cached).

    Returns True (optimistic) if we couldn't fetch the indexed filenames —
    we don't punish users for a transient lookup failure.
    """
    sources = cache.cache_entry_sources(cached_resp)
    if not sources:
        # No sources cited (e.g. small-talk or no-answer) → always valid
        return True

    current = _get_indexed_filenames()
    if current is None:
        # Couldn't verify → assume valid (fail open)
        return True

    missing = [s for s in sources if s not in current]
    if missing:
        log.info(f"🗑️  Cache validation: sources missing from index: {missing}")
        return False
    return True


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

# The exact "no-answer" phrase the LLM should produce when the documents
# don't contain the answer. Kept as a single string so app.py and the
# detector stay in sync.
NO_ANSWER_RESPONSE = (
    "Veelead Helpdesk here.\n\n"
    "I couldn't find this in our knowledge base. 🤔\n\n"
    "Would you like me to help you raise a ticket with the IT team?\n\n"
    "In the meantime, you can try asking me about any other issue if you are "
    "facing laptop issues, VPN access, email setup, or other IT policies."
)

# Markers used to DETECT a no-answer response (whether produced by the LLM or
# generated locally). Used to cap confidence low and skip caching.
NO_ANSWER_MARKERS = (
    "couldn't find this in our knowledge base",
    "could not find this in our knowledge base",
    "could not find that in the available documents",
    "couldn't find that in the available documents",
    "couldn't find",
    "could not find",
    "cannot find",
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
        return (
            "Veelead Helpdesk here. 👋\n\n"
            "How can I help you today? Ask me anything about IT, HR, "
            "Facilities, or company policies.",
            "small_talk",
        )
    return (
        "Veelead Helpdesk here.\n\n"
        "Sure, I can help. Please share your IT, HR, Facilities, or "
        "policy-related question and I'll find the answer for you.",
        "small_talk",
    )


def wants_combined_it_hr_summary(question: str) -> bool:
    """Detect summary-style questions that explicitly mention both IT and HR."""
    q = question.lower()
    has_it = re.search(r"\bit\b", q) is not None
    has_hr = re.search(r"\bhr\b", q) is not None
    return has_it and has_hr and bool(SUMMARY_QUERY_RE.search(q))


def _is_no_answer(answer: str) -> bool:
    """True if the answer indicates the bot couldn't find content."""
    if not answer:
        return True
    a = answer.lower()
    return any(marker in a for marker in NO_ANSWER_MARKERS)


# ═══════════════════════════════════════════════════════════
#  QUERY REWRITE — fix typos & grammar before searching
# ═══════════════════════════════════════════════════════════

# Domain-specific terms commonly misspelled — preserved by the rewriter
DOMAIN_TERMS = [
    "payslip", "payroll", "reimbursement", "appraisal",
    "PF", "TDS", "ESI", "PAN", "HRA", "LTA", "NOC", "CTC", "POSH",
    "VPN", "BSOD", "BitLocker", "MFA", "MDM", "BYOD",
    "OneDrive", "SharePoint", "Outlook", "Microsoft 365", "M365",
    "Veelead",
]

QUERY_REWRITE_PROMPT = (
    "You are a query corrector for a corporate helpdesk bot.\n\n"
    "Rules:\n"
    "- Fix spelling mistakes (e.g. \"aply\" → \"apply\", \"playslip\" → \"payslip\", "
    "\"leve\" → \"leave\", \"reimbersement\" → \"reimbursement\")\n"
    "- Fix grammar (e.g. \"how take leve i can\" → \"how can I take leave\")\n"
    "- Preserve domain-specific terms exactly: " + ", ".join(DOMAIN_TERMS) + "\n"
    "- DO NOT change the meaning of the question\n"
    "- DO NOT add information not in the original\n"
    "- DO NOT translate to another language\n"
    "- DO NOT answer the question, only rewrite it\n"
    "- If the query is already correct, return it unchanged\n\n"
    "Return ONLY a JSON object with this exact shape:\n"
    "{\"corrected\": \"the corrected query\", \"was_corrected\": true_or_false}"
)


def rewrite_query(question: str) -> tuple[str, bool]:
    """
    Use a tiny LLM call to fix spelling/grammar in user queries before searching.
    Returns (corrected_query, was_corrected_flag).

    Falls back to the original on any error. Cheap (~$0.00005 per call,
    adds ~200ms latency).

    Skips:
      - Very short queries (1-2 words) — risk of meaning loss
      - Small-talk (handled separately before this is called)
    """
    if not question or len(question.split()) < 3:
        return (question, False)

    try:
        client = get_gpt_client()
        resp = client.chat.completions.create(
            model=settings.gpt_mini_deploy,
            messages=[
                {"role": "system", "content": QUERY_REWRITE_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0,           # deterministic — same typo → same correction
            max_tokens=120,
            response_format={"type": "json_object"},
            timeout=10,
        )
        import json as _json
        parsed = _json.loads(resp.choices[0].message.content or "{}")
        corrected = (parsed.get("corrected") or "").strip()
        was_corrected = bool(parsed.get("was_corrected"))

        # Sanity checks: empty, much longer than original, or unchanged
        if not corrected:
            return (question, False)
        if len(corrected) > len(question) * 3:
            log.warning(f"Query rewrite suspiciously long, using original: {corrected!r}")
            return (question, False)
        if corrected.lower().strip() == question.lower().strip():
            return (question, False)

        if was_corrected:
            log.info(f"  📝 Query corrected: {question!r} → {corrected!r}")
        return (corrected, was_corrected)

    except Exception as e:
        log.warning(f"Query rewrite failed, using original: {e}")
        return (question, False)


# ═══════════════════════════════════════════════════════════
#  CONVERSATION MEMORY — disambiguate follow-up questions
# ═══════════════════════════════════════════════════════════

# How many previous questions to consider when contextualizing a follow-up.
# Older questions are dropped (FIFO). Higher = more context but more tokens.
MAX_HISTORY_QUESTIONS = 5

CONTEXTUALIZE_PROMPT = """You are a query rewriter for a helpdesk chatbot.

The user is in a multi-turn conversation. Their CURRENT question may be a
follow-up that depends on earlier questions for meaning (e.g. "where can I
apply?" — apply for what?).

Your job: rewrite the CURRENT question into a STANDALONE question that can
be understood without the previous context. Use the previous questions ONLY
to disambiguate the current one.

CRITICAL rules:
- If the current question is already complete and clear → return it UNCHANGED.
- If the current question clearly starts a NEW topic → return it UNCHANGED.
- If the current question has pronouns ("it", "that", "this") or implicit
  references ("where", "how", "when", "what about it") that refer to the
  previous topic → expand them.
- Keep the rewritten question SHORT and NATURAL (under 20 words).
- Do NOT answer the question.
- Do NOT add information not implied by the previous questions.

Return ONLY this JSON:
{"rewritten": "the standalone question", "was_rewritten": true_or_false}

EXAMPLES:

Previous: ["How many casual leaves do I get?"]
Current:  "Where can I apply?"
Output:   {"rewritten": "Where can I apply for casual leave?", "was_rewritten": true}

Previous: ["How many casual leaves do I get?", "Where can I apply?"]
Current:  "How does approval work?"
Output:   {"rewritten": "How does casual leave approval work?", "was_rewritten": true}

Previous: ["How many leaves do I get?"]
Current:  "How do I reset my Windows password?"
Output:   {"rewritten": "How do I reset my Windows password?", "was_rewritten": false}

Previous: ["What is the salary structure?"]
Current:  "What about bonuses?"
Output:   {"rewritten": "What is the bonus structure?", "was_rewritten": true}
"""


def contextualize_query(current: str, previous_questions: List[str]) -> tuple[str, bool]:
    """
    Rewrite an ambiguous follow-up question into a standalone question using
    the user's previous questions as context.

    Returns (contextualized_query, was_rewritten_flag).

    Skips:
      - Empty history (first turn) — no context to use
      - Long current questions (≥10 words) — usually already self-contained
      - Small-talk (handled separately before this is called)

    Falls back to the original on any error. ~$0.00005 per call, ~200ms.
    """
    if not previous_questions or not current.strip():
        return (current, False)

    # If the question is already long and well-formed, skip — likely self-contained
    if len(current.split()) >= 10:
        return (current, False)

    # Use only the most recent N questions (FIFO)
    history = previous_questions[-MAX_HISTORY_QUESTIONS:]
    prev_text = "\n".join(f"- {q}" for q in history)
    user_msg = f'Previous questions:\n{prev_text}\n\nCurrent question: "{current}"'

    try:
        client = get_gpt_client()
        resp = client.chat.completions.create(
            model=settings.gpt_mini_deploy,
            messages=[
                {"role": "system", "content": CONTEXTUALIZE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
            timeout=10,
        )
        import json as _json
        parsed = _json.loads(resp.choices[0].message.content or "{}")
        rewritten = (parsed.get("rewritten") or "").strip()
        was_rewritten = bool(parsed.get("was_rewritten"))

        # Sanity checks
        if not rewritten:
            return (current, False)
        if len(rewritten) > len(current) * 5:
            log.warning(f"Contextualize output suspiciously long, using original: {rewritten!r}")
            return (current, False)
        if rewritten.lower().strip() == current.lower().strip():
            return (current, False)

        if was_rewritten:
            log.info(f"  🧠 Query contextualized: {current!r} → {rewritten!r}")
        return (rewritten, was_rewritten)

    except Exception as e:
        log.warning(f"Contextualize failed, using original: {e}")
        return (current, False)


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


def _format_answer_response(
    answer: str,
    top_chunks: List[Dict[str, Any]],
) -> str:
    """
    Final answer cleanup.

    - For no-answer responses: return the standard Veelead phrase unchanged
      (already friendly, no source citation needed)
    - For affirmative answers: trust the LLM's markdown formatting; do NOT
      add a source line in the answer text (the chunks panel shows sources)
    """
    cleaned = (answer or "").strip()
    if not cleaned:
        return NO_ANSWER_RESPONSE

    # If LLM said "couldn't find", replace with our standard friendly phrase
    if _is_no_answer(cleaned):
        return NO_ANSWER_RESPONSE

    # Otherwise leave the markdown-formatted answer as the LLM produced it.
    # Source is shown separately in the chunks panel in the UI.
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

    # If anything was deleted, bust the in-process filename cache so the
    # next user query gets a fresh look at the index.
    if deleted_count > 0:
        _indexed_filenames["value"] = None
        _indexed_filenames["expires_at"] = 0.0

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

    # ── SELF-HEALING ORPHAN CLEANUP ──
    # Detect chunks in Azure Search whose source file is no longer in SharePoint
    # (or whose ArticleStatus is no longer Published). The delta API sometimes
    # misses delete events, especially when files are deleted between sync
    # cycles. This step runs every sync and self-corrects any drift.
    orphan_count = 0
    try:
        current_sp_files: set = set()
        try:
            all_sp_docs = source.list_documents(only_published=False)
            current_sp_files = {d.filename for d in all_sp_docs if d.filename}
        except Exception as e:
            log.warning(f"Orphan cleanup: failed to list SharePoint state: {e}")

        if current_sp_files:
            from storage.search_index import _get_search_client
            client = _get_search_client()
            indexed = list(client.search(
                search_text="*",
                select=["filename", "chunk_id"],
                top=5000,
            ))
            orphans = [
                r for r in indexed
                if r.get("filename") and r.get("filename") not in current_sp_files
            ]
            if orphans:
                # Group by filename for nicer logging
                from collections import Counter
                orphan_files = Counter(r["filename"] for r in orphans)
                log.info(f"  🧹 Orphan cleanup: found {len(orphans)} stale chunks "
                         f"across {len(orphan_files)} files")
                for fn, count in orphan_files.most_common():
                    log.info(f"     - {fn}: {count} chunks")

                # Delete the orphaned chunks from Azure AI Search
                batch_size = 100
                for i in range(0, len(orphans), batch_size):
                    batch = [{"chunk_id": r["chunk_id"]} for r in orphans[i:i+batch_size]]
                    client.delete_documents(batch)
                orphan_count = len(orphans)
                log.info(f"  ✅ Orphan cleanup: removed {orphan_count} stale chunks")

                # ALSO invalidate any cached responses that cited these
                # orphaned files. Without this, the cache continues to
                # serve old answers pointing to now-deleted documents.
                cache_invalidated = 0
                for orphan_fn in orphan_files.keys():
                    cache_invalidated += cache.cache_invalidate_by_filename(orphan_fn)
                if cache_invalidated > 0:
                    log.info(f"  🗑️  Cache: invalidated {cache_invalidated} entries "
                             f"citing orphaned files")

                # Also drop the in-process "indexed filenames" cache so the
                # next user query rebuilds it freshly (otherwise stale entries
                # might pass validation for up to 60s).
                _indexed_filenames["value"] = None
                _indexed_filenames["expires_at"] = 0.0
    except Exception as e:
        log.warning(f"Orphan cleanup failed (non-fatal): {e}")

    finished_at = datetime.now(timezone.utc)
    status = "success" if failed == 0 else "partial"
    run_id = cache.record_sync_run(
        source_type=source_type_name,
        started_at=started_at,
        finished_at=finished_at,
        added=added_count,
        updated=updated_count,
        deleted=deleted_count + orphan_count,   # count orphans as deletes too
        status=status,
        error_msg=f"{failed} items failed" if failed else None,
    )

    duration = (finished_at - started_at).total_seconds()
    log.info("─" * 60)
    log.info(f"  SYNC COMPLETE  +{added_count}  ~{updated_count}  -{deleted_count}  "
             f"(orphans cleaned: {orphan_count}, {failed} failed, {duration:.1f}s)")
    log.info("─" * 60)

    return {
        "status": status,
        "run_id": run_id,
        "added": added_count,
        "updated": updated_count,
        "deleted": deleted_count,
        "orphans_cleaned": orphan_count,
        "failed": failed,
        "duration_sec": duration,
    }


# ═══════════════════════════════════════════════════════════
#  ANSWER GENERATION + FOLLOW-UPS (single LLM call)
# ═══════════════════════════════════════════════════════════
# SYSTEM_PROMPT_TEMPLATE = """You are the Veelead Solutions Helpdesk AI Assistant.
# Your job is to answer employee questions clearly and professionally,
# strictly using the document context provided below.

# ═══════════════════════════════════════════════════════════
# KNOWLEDGE BASE — DOCUMENT MAP (for reasoning, not for fetching)
# ═══════════════════════════════════════════════════════════

# The bot's knowledge spans these categories. Before answering, mentally
# check that the chunks below come from the document family that matches
# the user's question. If the chunks look mis-routed (e.g. user asked
# about salary advance but chunks are from performance policy), favour
# chunks whose article_title clearly matches the topic, and ignore
# unrelated chunks.

# HR        → Leave & Time-Off Master Policy
#             Performance Management Policy
#             Recruitment & Onboarding Policy
#             HR FAQ Quick Reference

# IT        → IT Security Master Policy
#             IT Equipment & Asset Policy
#             Remote Work & VPN Policy
#             IT Helpdesk FAQ Quick Reference

# Payroll   → Compensation & Salary Master Policy
#             Reimbursement & Expense Policy
#             Payroll FAQ Quick Reference

# Facilities→ Office Facilities & Workspace Policy
#             Health, Safety & Security Policy
#             Facilities FAQ Quick Reference

# General   → Employee Handbook
#             New Employee Quick Start Guide

# ═══════════════════════════════════════════════════════════
# TONE & VOICE
# ═══════════════════════════════════════════════════════════

# - Always begin the answer with: "Veelead Helpdesk here."  (followed by a blank line)
# - Use clear, professional Indian English (rupees, lakh, cheque, queries, etc.)
# - Friendly but business-appropriate. No slang, no excessive enthusiasm.
# - Talk to the employee like a helpful colleague, not a manual.
# - Address the employee as "you".

# ═══════════════════════════════════════════════════════════
# FORMATTING RULES — strict
# ═══════════════════════════════════════════════════════════

# The "answer" field must be clean Markdown:

# 1. Start with "Veelead Helpdesk here." then a blank line, then a short
#    one-line intro of what you're answering.
# 2. Use **bold** for step titles, key terms, and warnings.
# 3. Use numbered lists (1. 2. 3.) for sequential steps. Each step:

#    **Step Title** (emoji optional)
#    One short sentence describing the action.

# 4. Use bullet points (-) for non-sequential items.
# 5. Use blank lines generously between steps and sections.
# 6. Use `inline code` for filenames, commands, ticket numbers, or
#    specific values like `MEMORY_MANAGEMENT`.
# 7. Use visual cue emojis sparingly but consistently:
#    - ✅ success / approval / done
#    - ⚠️ warning / caution / important
#    - 💡 tip / helpful suggestion
#    - 📝 note / write down / document
#    - 🔄 restart / try again
#    - 🎫 ticket / escalation
#    - 🔍 check / investigate
#    - 🧾 receipts / forms / paperwork
#    - 💰 money / payment / reimbursement
# 8. Keep total length under 300 words unless the question explicitly
#    asks for a detailed explanation.
# 9. Do NOT include the document filename or "(Source: ...)" in the
#    answer text — that's shown separately in the UI.

# ═══════════════════════════════════════════════════════════
# CONTENT RULES
# ═══════════════════════════════════════════════════════════

# - Use ONLY information from the context chunks below. Never invent
#   facts, contact details, phone numbers, email addresses, or amounts.
# - If you see chunks from MULTIPLE different documents, prefer the one
#   whose article_title most directly matches the user's question.
# - If the context does NOT contain the answer, set "answer" to EXACTLY:
#   "I couldn't find this in our knowledge base."
# - For summary questions, give a brief structured overview using bullet
#   points or a short numbered list.

# ═══════════════════════════════════════════════════════════
# OUTPUT FORMAT
# ═══════════════════════════════════════════════════════════

# Return ONLY this JSON object (no markdown fences, no extra text):

# {{
#   "subject": "Short topic title in 3-8 words (like an email subject)",
#   "description": "1-2 sentence factual summary of what the user wanted to know, in third person.",
#   "answer": "Markdown answer following all formatting rules above",
#   "suggested_followups": [
#     "Short specific follow-up question 1",
#     "Short specific follow-up question 2",
#     "Short specific follow-up question 3"
#   ]
# }}

# ═══════════════════════════════════════════════════════════
# ABOUT subject AND description
# ═══════════════════════════════════════════════════════════

# ALWAYS populate "subject" and "description" — required on every response.

# - "subject" — short and clear (3-8 words). Title-case it. Examples:
#     • "Salary Advance Request"
#     • "Blue Screen Error on Laptop"
#     • "Leave Application Process"
#     • "Reimbursement Claim Steps"
#     • "VPN Connection Issue"

# - "description" — 1-2 short sentences in third person. Example:
#     "User asked about the salary advance process. Provides eligibility,
#     limits, and application steps."

# If the bot cannot answer, use:
#   subject:     "Question Not Answered"
#   description: "User asked a question that is not covered by the available documents."

# ═══════════════════════════════════════════════════════════
# EXAMPLE — full answer for "what should I do if I get a blue screen"
# ═══════════════════════════════════════════════════════════

# {{
#   "subject": "Blue Screen Error Troubleshooting",
#   "description": "User asked how to handle a blue screen error on their laptop. Provides step-by-step troubleshooting and escalation guidance.",
#   "answer": "Veelead Helpdesk here.\n\nHere are the steps to handle a blue screen error on your laptop:\n\n**1. Note the error code** 📝\nWrite down the error code shown on the blue screen (for example, `MEMORY_MANAGEMENT`). This helps the IT team diagnose faster.\n\n**2. Restart your laptop** 🔄\nHold the power button for 10 seconds, wait 30 seconds, then turn it back on.\n\n**3. Check for recent changes** 🔍\nThink about any new software installations or Windows updates from the last 24 hours.\n\n**4. Raise an IT ticket** 🎫\nIf the problem returns, raise a ticket through the IT portal with the error code from step 1.\n\n⚠️ **If the blue screen appears more than twice in a day**, stop using the laptop and contact your IT team immediately — this may indicate a hardware issue.",
#   "suggested_followups": [
#     "How do I raise an IT ticket?",
#     "What if my laptop won\'t restart?",
#     "How do I check Windows update history?"
#   ]
# }}

# ═══════════════════════════════════════════════════════════
# DOCUMENT CONTEXT (chunks retrieved from the knowledge base)
# ═══════════════════════════════════════════════════════════

# {context}"""

SYSTEM_PROMPT_TEMPLATE = """You are the Veelead Solutions Helpdesk AI Assistant.
Your job is to act as an expert interactive diagnostic agent to resolve employee questions regarding IT hardware, system connections, HR rules, leave requests, and payroll/reimbursement procedures using the provided document context.

═══════════════════════════════════════════════════════════
KNOWLEDGE BASE — DOCUMENT MAP (for reasoning, not for fetching)
═══════════════════════════════════════════════════════════
The bot's knowledge spans these categories. Before answering, mentally check that the chunks below come from the document family that matches the user's question.

HR         → Leave & Time-Off Master Policy | Performance Management Policy | Recruitment & Onboarding Policy
IT         → IT Security Master Policy | IT Equipment & Asset Policy | Remote Work & VPN Policy
Payroll    → Compensation & Salary Master Policy | Reimbursement & Expense Policy
Facilities → Office Facilities & Workspace Policy | Health, Safety & Security Policy
General    → Employee Handbook | New Employee Quick Start Guide

═══════════════════════════════════════════════════════════
TONE & DIAGNOSTIC INTERACTION STYLE (Crucial)
═══════════════════════════════════════════════════════════
- Always begin the answer with: "Veelead Helpdesk here." (followed by a blank line)
- DO NOT just spit out a flat list of steps. Act like a live support engineer.
- Use clear, professional Indian English (rupees, lakh, cheque, queries, etc.)
- Speak directly to the employee using "you".

═══════════════════════════════════════════════════════════
RESPONSE STRUCTURAL MANDATES (The Q1/Q2 Style)
═══════════════════════════════════════════════════════════
Every resolution response MUST contain these 4 distinct structural blocks:

1. DIAGNOSTIC INTERACTION QUESTIONS: 
   Ask the employee 3 to 5 highly specific clarifying questions about their current situation or system state to narrow down the problem (e.g., "Does it turn on?", "Is there an error message?", "Whose name is on the bill?").
   
2. IMMEDIATE QUICK CHECKS / RULE CHECKS:
   Provide an immediate troubleshooting bullet list titled "Meanwhile, please try these quick checks:" or "Please verify these policy rules first:". Use clear, practical, standalone actions.

3. RE-ROUTING OR ACTION STEPS:
   Provide deep, sequential, bolded steps with structural sub-steps (e.g., Settings → System → Troubleshoot) for the actual fix or document filing process.

4. FALLBACK ESCALATION:
   Conclude with explicit, conditional troubleshooting questions or specific helpdesk contact details if the self-service steps fail.

═══════════════════════════════════════════════════════════
FORMATTING RULES — strict
═══════════════════════════════════════════════════════════
The "answer" field must be clean Markdown:
- Use **bold** for section headers, step titles, sub-steps, and vital limits.
- Use arrows (→) to define system paths (e.g., **Open Settings → Privacy & Security → Microphone**).
- Use blank lines generously between different blocks and steps to keep the UI legible.
- Use `inline code` for filenames, system URLs, commands, or values (e.g., `vpn.company.com`, `fast.com`).
- Use visual cue emojis consistently: 🔍, ⚠️, 💡, 🔄, 🎫, 💰, 🎨.
- Do NOT include the document filename or text like "(Source: ...)" in the answer field.

═══════════════════════════════════════════════════════════
CONTENT RULES
═══════════════════════════════════════════════════════════
- Use ONLY facts directly stated in the context chunks below. Never invent URLs, emails, phone numbers, or numeric policy thresholds.
- If the context does NOT contain the details needed to help the user, set "answer" to EXACTLY: "I couldn't find this in our knowledge base."

═══════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════
Return ONLY this JSON object (no markdown fences, no extra text):

{{
  "subject": "Short topic title in 3-8 words (Title-Case)",
  "description": "1-2 sentence factual summary of what the user wanted to know, in third person.",
  "answer": "Markdown answer following the exact Diagnostic Interaction structure",
  "suggested_followups": [
    "Short specific follow-up question 1",
    "Short specific follow-up question 2",
    "Short specific follow-up question 3"
  ]
}}

═══════════════════════════════════════════════════════════
EXAMPLE — Expected Output Structure for VPN Failure
═══════════════════════════════════════════════════════════
{{
  "subject": "VPN Connection Issue Troubleshooting",
  "description": "User is experiencing errors while attempting to connect to the corporate VPN network. Provides deep troubleshooting, network requirements, and helpdesk contact details.",
  "answer": "Veelead Helpdesk here.\n\nLet's troubleshoot your VPN connection issue right away. To help me pinpoint the exact cause, please tell me:\n- Does the GlobalProtect client display an explicit error code (like 'Connection Timed Out')?\n- Are you stuck indefinitely on the 'Connecting' progress wheel?\n- Is your Microsoft Authenticator multi-factor authentication (MFA) prompt failing to show up on your mobile phone?\n\nMeanwhile, please verify these vital checks:\n- **Verify your Gateway Address** 🔍: Ensure your gateway is set exactly to `vpn.company.com` without extra letters or blank trailing spaces.\n- **Verify your Credentials** 🔑: Confirm that you are typing your updated corporate email account password correctly.\n- **Check network parameters** 🌐: Open a browser and test your speed via `fast.com`. The policy requires a minimum of 50 Mbps download and 10 Mbps upload for stable operation.\n\nIf the quick checks look fine, try these action steps to reset the client:\n\n**1. Clear active sessions** 🔄\nRight-click the GlobalProtect icon in your taskbar system tray, click **Disconnect**, wait 15 seconds, and click connect again.\n\n**2. Authenticate cleanly** ⏳\nWhen the login window appears, input your corporate credentials and ensure you submit your active mobile MFA token code within 30 seconds of generation before it expires.\n\n**3. Switch networks** 📱\nIf your home broadband network continues to drop, turn on your smartphone's mobile hotspot (4G/5G), connect your laptop to it, and attempt a clean connection sequence.\n\nIf the VPN connection still fails to initialize, tell me your current Windows version, what internet service provider (ISP) you are using, and copy-paste the exact error code so we can solve this immediately. Alternatively, you can drop a line to `it-helpdesk@company.com`.",
  "suggested_followups": [
    "How do I update my GlobalProtect client?",
    "What are the core working hours for remote work?",
    "Where do I raise an IT support ticket?"
  ]
}}

═══════════════════════════════════════════════════════════
DOCUMENT CONTEXT (chunks retrieved from the knowledge base)
═══════════════════════════════════════════════════════════
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
) -> tuple[str, List[str], str, str, str]:
    """
    Call the LLM ONCE to produce:
      - the answer text (markdown)
      - 2-3 follow-up suggestions
      - the model used
      - a short subject (3-8 words)
      - a 1-2 sentence description

    Returns (answer, followups, model_used, subject, description).
    """
    if not top_chunks:
        return (
            NO_ANSWER_RESPONSE,
            [],
            "none",
            "Question Not Answered",
            "User asked a question that is not covered by the available documents.",
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

        # Extract subject + description (with sensible fallbacks)
        subject = (parsed.get("subject") or "").strip()[:120]
        description = (parsed.get("description") or "").strip()[:500]
        if not subject:
            # Fallback: use the question itself, truncated
            subject = question[:80] + ("..." if len(question) > 80 else "")
        if not description:
            description = "User asked a helpdesk question."

        if not answer:
            answer = NO_ANSWER_RESPONSE

        # If the bot couldn't answer, override subject/description
        if _is_no_answer(answer):
            subject = "Question Not Answered"
            description = "User asked a question that is not covered by the available documents."

        # Final cleanup — replaces no-answer text with standard phrase
        answer = _format_answer_response(answer, top_chunks)
        return (answer, followups, model, subject, description)
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        return (
            f"Veelead Helpdesk here.\n\nSorry, I'm having trouble right now. "
            f"Please try again in a moment. ({type(e).__name__})",
            [],
            model,
            "Bot Error",
            f"The helpdesk bot encountered an error: {type(e).__name__}",
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
    # Always present
    subject: str                                       # short topic title (3-8 words)
    description: str                                   # 1-2 sentence factual summary
    answer: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    model_used: str
    cached: bool
    numFound: int
    sources: List[str]
    chunks: List[ChunkOut]
    suggested_followups: List[str]

    # Optional / context fields
    q: Optional[str] = None
    corrected_query: Optional[str] = None              # populated only when typos/grammar were fixed
    contextualized_query: Optional[str] = None         # populated only when follow-up was disambiguated from history
    category_used: Optional[str] = None
    category_source: Optional[str] = None
    category_confidence: Optional[str] = None

    # Cache match debug fields — populated only on cache hits
    cache_match_type: Optional[str] = None             # "exact" | "semantic" | None
    cache_similarity: Optional[float] = None           # cosine similarity (only when semantic)


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


@app.middleware("http")
async def _request_id_and_timing_middleware(request, call_next):
    """
    Per-request observability:
      1. Assign a short request ID (e.g. "a3f2c901")
      2. Make it available to every log call in this request via contextvar
      3. Echo it back in the response header X-Request-ID (helps client correlate)
      4. Log a one-line summary at the end with status + duration

    Cost: a few microseconds per request. No external dependencies.
    """
    import time as _t
    rid = _new_request_id()
    token = _request_id_var.set(rid)
    started = _t.time()

    # Log the inbound request (path + query string, but NOT headers/body)
    try:
        method = request.method
        path = request.url.path
        qs = str(request.url.query) if request.url.query else ""
        # Truncate query string in logs (some queries can be long)
        if len(qs) > 120:
            qs = qs[:120] + "..."
        log.info(f"→ {method} {path}{'?' + qs if qs else ''}")
    except Exception:
        pass  # logging must never break a request

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        # Echo request ID back to client so they can quote it in bug reports
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        duration_ms = int((_t.time() - started) * 1000)
        # One-line summary: easy to grep for slow requests
        log.info(f"← {status_code} in {duration_ms}ms")
        _request_id_var.reset(token)


def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing x-api-key header")
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# ═══════════════════════════════════════════════════════════
#  STARTUP — background sync so API doesn't block
# ═══════════════════════════════════════════════════════════
def _background_sync(force_full: bool = False):
    """Wrapper for run_sync that catches all exceptions safely."""
    try:
        run_sync(force_full=force_full)
    except Exception:
        log.exception("Background sync failed")


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

    # Run sync in background — don't block startup (important for Azure App Service)
    if not cache.get_delta_token():
        log.info("No delta token found — scheduling initial full sync in background...")
        threading.Thread(
            target=_background_sync,
            args=(True,),
            daemon=True,
            name="initial-sync",
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
    previous: Optional[List[str]] = Query(
        None,
        description=(
            "Previous user questions in this chat session, oldest first. "
            "Used to disambiguate follow-up questions like 'where can I apply?'. "
            "Browser should send up to 5 most recent questions."
        ),
    ),
    _auth: bool = Depends(verify_api_key),
):
    question = q.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Query 'q' is required")

    # Normalise history: drop empties, trim, dedupe consecutive, cap to max
    history: List[str] = []
    if previous:
        for p in previous:
            if isinstance(p, str):
                p = p.strip()
                if p and (not history or history[-1].lower() != p.lower()):
                    history.append(p)
        history = history[-MAX_HISTORY_QUESTIONS:]

    # ── 0. Small-talk fast path ──
    if is_small_talk(question):
        answer, model_used = generate_small_talk_response(question)
        return SearchResponse(
            subject="Greeting",
            description="User greeted the bot or asked for general help.",
            answer=answer,
            confidence=0.15,
            model_used=model_used,
            cached=False,
            numFound=0,
            sources=[],
            chunks=[],
            suggested_followups=[
                "How do I apply for leave?",
                "What should I do if my laptop crashes?",
                "How do I claim a reimbursement?",
            ],
            q=question,
            category_used="General",
            category_source="small_talk",
            category_confidence="high",
        )

    # ── 1. Rewrite query (fix typos/grammar before search & cache lookup) ──
    # We rewrite BEFORE the cache check so that "how to aply for leve" and
    # "how to apply for leave" hit the same cached entry.
    original_question = question
    question, was_corrected = rewrite_query(question)

    # ── 1b. Contextualise follow-up questions using conversation history ──
    # If user previously asked "How many leaves?" and now asks "Where can I apply?",
    # we rewrite the current question to "Where can I apply for leave?" so the
    # search has enough context to find the right docs.
    was_contextualised = False
    contextualised_query: Optional[str] = None
    if history:
        new_q, was_contextualised = contextualize_query(question, history)
        if was_contextualised:
            contextualised_query = new_q
            question = new_q

    # ── 2. Cache lookup — EXACT MATCH ──
    cached_resp = cache.cache_lookup(question)
    if cached_resp:
        required_new = {"confidence", "chunks", "suggested_followups", "subject", "description"}
        if all(k in cached_resp for k in required_new):
            # Validate: are the cited source files still in the index?
            # If any source is missing (deleted/renamed), drop this entry
            # and regenerate fresh. This prevents serving stale answers
            # with broken pdf_urls.
            if not _is_cached_entry_still_valid(cached_resp):
                cache.cache_delete_by_question(question)
                log.info(f"🗑️  Dropped stale cache entry for: {question[:60]}")
                # Fall through to fresh generation
            else:
                log.info(f"💰 Cache HIT (exact): {question[:60]}")
                cached_resp["cached"] = True
                cached_resp["cache_match_type"] = "exact"
                cached_resp["cache_similarity"] = None
                cached_resp.setdefault("category_source", "cached")
                # Make sure the response reflects the actual user query
                cached_resp["q"] = original_question
                cached_resp["corrected_query"] = question if was_corrected else None
                cached_resp["contextualized_query"] = contextualised_query if was_contextualised else None
                try:
                    return SearchResponse(**cached_resp)
                except Exception as e:
                    log.warning(f"Cached entry invalid, regenerating: {e}")
        else:
            log.info(f"Cache entry old-schema, regenerating: {question[:60]}")

    # ── 2b. PARALLEL: embed query + fetch categories list ──
    # These two I/O calls are independent — run them concurrently to save
    # ~200-400ms per non-cached request.
    fut_embed = _io_pool.submit(embed_one, question)
    fut_cats = _io_pool.submit(search_index.list_categories, True)

    # Block on embed (needed for semantic cache check next)
    try:
        query_vec = fut_embed.result(timeout=15)
    except Exception as e:
        log.error(f"Query embedding failed: {e}")
        raise HTTPException(status_code=500, detail="Query embedding service unavailable")

    # ── 2c. Cache lookup — SEMANTIC MATCH (related question) ──
    try:
        semantic_hit = cache.cache_lookup_semantic(
            query_vec, threshold=settings.cache_semantic_threshold
        )
    except Exception as e:
        log.warning(f"Semantic cache lookup failed (non-fatal): {e}")
        semantic_hit = None

    if semantic_hit:
        cached_resp = semantic_hit["response"]
        similarity = semantic_hit["similarity"]
        matched_q = semantic_hit["matched_question"]
        required_new = {"confidence", "chunks", "suggested_followups", "subject", "description"}
        if all(k in cached_resp for k in required_new):
            # Validate: cited sources still in index? (same rule as exact hit)
            if not _is_cached_entry_still_valid(cached_resp):
                # The matched entry is stale — delete it and fall through to
                # fresh generation. Use the matched question (the original
                # cached question) to delete the right entry.
                cache.cache_delete_by_question(matched_q)
                log.info(f"🗑️  Dropped stale semantic-cache entry: {matched_q[:60]}")
                # Fall through to fresh generation
            else:
                log.info(
                    f"💰 Cache HIT (semantic, sim={similarity:.3f}): "
                    f"{question[:50]} ~ {matched_q[:50]}"
                )
                cached_resp["cached"] = True
                cached_resp["cache_match_type"] = "semantic"
                cached_resp["cache_similarity"] = similarity
                cached_resp.setdefault("category_source", "cached")
                cached_resp["q"] = original_question
                cached_resp["corrected_query"] = question if was_corrected else None
                cached_resp["contextualized_query"] = contextualised_query if was_contextualised else None
                try:
                    return SearchResponse(**cached_resp)
                except Exception as e:
                    log.warning(f"Semantic cached entry invalid, regenerating: {e}")

    # ── 3. Determine category (await parallel categories list) ──
    try:
        all_categories = [c["name"] for c in fut_cats.result(timeout=10)]
    except Exception as e:
        log.warning(f"Category listing failed, using defaults: {e}")
        all_categories = ["HR", "IT", "Payroll", "Facilities", "General"]

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

    # ── 4. Hybrid search (query_vec already computed in step 2b for semantic cache) ──
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

        # Fallback: retry without category if empty
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

    # ── 5. Generate answer + follow-ups + subject + description (single LLM call) ──
    answer, followups, model_used, subject, description = generate_answer_and_followups(
        question, chunks
    )

    # ── 6. Compute confidence from chunk scores ──
    confidence = compute_confidence(chunks) if chunks else 0.0

    # If no chunks, force low confidence
    if not chunks:
        confidence = min(confidence, 0.30)

    # If answer is a no-answer response, cap confidence
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
        subject=subject,
        description=description,
        answer=answer,
        confidence=confidence,
        model_used=model_used,
        cached=False,
        numFound=len(chunks_out),
        sources=sources,
        chunks=chunks_out,
        suggested_followups=followups,
        q=original_question,                                           # original user query (for display)
        corrected_query=question if was_corrected else None,           # corrected version (None if no change)
        contextualized_query=contextualised_query if was_contextualised else None,  # disambiguated form (None if no change)
        category_used=used_category,
        category_source=category_source,
        category_confidence=category_confidence,
        cache_match_type=None,                                         # this is a fresh response
        cache_similarity=None,
    )

    # ── 8. Cache the response with embedding for semantic match later ──
    # Key uses corrected query for better hit rate. Embedding enables
    # "related question" matching for future queries.
    if not _is_no_answer(answer):
        cache.cache_store(question, response.model_dump(), embedding=query_vec)

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