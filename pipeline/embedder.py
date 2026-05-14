"""
pipeline/embedder.py — Convert text into vector embeddings via Azure OpenAI.

Single source of truth for "text → vector" in the whole codebase.
Used during indexing (one call per batch of chunks) and at query time
(one call per user question).

Public API:
    embed_one(text: str)     -> list[float]   # for queries
    embed_many(texts: list)  -> list[list[float]]  # for batch indexing

Design notes:
    - Batches up to 16 inputs per API call (Azure OpenAI sweet spot)
    - Retries on 429/5xx with exponential backoff + jitter
    - Caches recent query embeddings (LRU, 256 entries) — same user
      question asked twice → second call costs nothing
    - All Azure-specific config comes from config.settings
"""

import hashlib
import logging
import random
import time
from collections import OrderedDict
from typing import List, Optional

from openai import AzureOpenAI
from openai import APIConnectionError, APIError, RateLimitError, APITimeoutError

from config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  CLIENT
# ═══════════════════════════════════════════════════════════
# One module-level client. Created lazily so import doesn't fail
# if Azure credentials happen to be missing at module-load time.

_client: Optional[AzureOpenAI] = None


def _get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        _client = AzureOpenAI(
            api_key=settings.embed_api_key,
            api_version=settings.embed_api_ver,
            azure_endpoint=settings.embed_endpoint,
        )
    return _client


# ═══════════════════════════════════════════════════════════
#  QUERY CACHE (in-memory LRU, 256 entries)
# ═══════════════════════════════════════════════════════════
# Why: at query time, the same question is often asked many times
# ("how do I reset my password" — 50× a day). Caching the vector
# saves an API call without affecting freshness (embedding model
# is deterministic).

_QUERY_CACHE_SIZE = 256
_query_cache: "OrderedDict[str, List[float]]" = OrderedDict()


def _cache_key(text: str) -> str:
    """Stable hash of the input text for cache lookup."""
    # Normalize whitespace before hashing so trivial variations hit the cache
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _cache_get(text: str) -> Optional[List[float]]:
    k = _cache_key(text)
    if k in _query_cache:
        # Move to end (most-recently used)
        _query_cache.move_to_end(k)
        return _query_cache[k]
    return None


def _cache_put(text: str, vec: List[float]) -> None:
    k = _cache_key(text)
    _query_cache[k] = vec
    _query_cache.move_to_end(k)
    while len(_query_cache) > _QUERY_CACHE_SIZE:
        _query_cache.popitem(last=False)


def clear_query_cache() -> None:
    """Useful for tests or after a model change."""
    _query_cache.clear()


# ═══════════════════════════════════════════════════════════
#  CORE EMBEDDING WITH RETRY
# ═══════════════════════════════════════════════════════════

_MAX_RETRIES = 5
_BASE_BACKOFF_SEC = 1.0
_MAX_BATCH = 16
_REQUEST_TIMEOUT = 60


def _embed_batch_with_retry(texts: List[str]) -> List[List[float]]:
    """
    Call Azure OpenAI for one batch. Retry on transient errors.
    Returns vectors in the same order as input.
    """
    client = _get_client()
    last_err: Optional[Exception] = None

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.embeddings.create(
                model=settings.embed_deployment,
                input=texts,
                timeout=_REQUEST_TIMEOUT,
            )
            # resp.data is a list aligned with input order
            return [item.embedding for item in resp.data]

        except RateLimitError as e:
            # 429 → wait honoring Retry-After if provided, else exponential backoff
            last_err = e
            retry_after = None
            try:
                # Azure usually puts seconds in the response headers
                retry_after = float(e.response.headers.get("retry-after", 0))
            except Exception:
                pass
            wait = retry_after if retry_after else (_BASE_BACKOFF_SEC * (2 ** (attempt - 1)))
            wait += random.uniform(0, 0.5)  # jitter
            log.warning(f"  Rate-limited (attempt {attempt}/{_MAX_RETRIES}). "
                        f"Sleeping {wait:.1f}s")
            time.sleep(wait)

        except (APIConnectionError, APITimeoutError) as e:
            last_err = e
            wait = _BASE_BACKOFF_SEC * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(f"  Connection issue ({type(e).__name__}, attempt {attempt}). "
                        f"Sleeping {wait:.1f}s")
            time.sleep(wait)

        except APIError as e:
            # 5xx errors are retryable; 4xx (bad request) usually aren't
            status = getattr(e, "status_code", None)
            if status and 400 <= status < 500 and status != 429:
                log.error(f"  Non-retryable API error {status}: {e}")
                raise
            last_err = e
            wait = _BASE_BACKOFF_SEC * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(f"  API error (attempt {attempt}). Sleeping {wait:.1f}s — {e}")
            time.sleep(wait)

    # All retries exhausted
    raise RuntimeError(f"Embedding failed after {_MAX_RETRIES} retries: {last_err}")


# ═══════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════

def embed_one(text: str, use_cache: bool = True) -> List[float]:
    """
    Embed a single text (typically a user query).
    Uses the in-memory LRU cache when use_cache=True.
    """
    if not text or not text.strip():
        raise ValueError("Cannot embed empty text")

    if use_cache:
        cached = _cache_get(text)
        if cached is not None:
            return cached

    vectors = _embed_batch_with_retry([text])
    vec = vectors[0]

    if use_cache:
        _cache_put(text, vec)

    return vec


def embed_many(texts: List[str], batch_size: int = _MAX_BATCH,
               show_progress: bool = False) -> List[List[float]]:
    """
    Embed a list of texts (typically chunks during indexing).
    Splits into batches of up to `batch_size`. Returns vectors in input order.
    """
    if not texts:
        return []

    # Reject empties / whitespace-only to avoid 400s from the API
    for i, t in enumerate(texts):
        if not t or not t.strip():
            raise ValueError(f"Empty text at index {i} — cannot embed")

    results: List[List[float]] = []
    total_batches = (len(texts) + batch_size - 1) // batch_size

    for batch_idx, start in enumerate(range(0, len(texts), batch_size), start=1):
        batch = texts[start: start + batch_size]
        if show_progress:
            log.info(f"  Embedding batch {batch_idx}/{total_batches} "
                     f"({len(batch)} items)...")
        vectors = _embed_batch_with_retry(batch)
        results.extend(vectors)

    return results


def get_embedding_info() -> dict:
    """Diagnostic info for /health endpoint."""
    return {
        "deployment": settings.embed_deployment,
        "dimensions": settings.embed_dim,
        "endpoint": settings.embed_endpoint,
        "query_cache_size": len(_query_cache),
        "query_cache_max": _QUERY_CACHE_SIZE,
    }


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Quick smoke test.

    Usage:
        python -m pipeline.embedder "your test query here"

    Requires valid Azure OpenAI credentials in .env.
    """
    import sys

    if len(sys.argv) < 2:
        print('Usage: python -m pipeline.embedder "your test query"')
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    print(f"\nEmbedding test for: {query!r}")
    print("─" * 60)
    print(f"  Endpoint:   {settings.embed_endpoint}")
    print(f"  Deployment: {settings.embed_deployment}")
    print(f"  Expected dim: {settings.embed_dim}")
    print("─" * 60)

    t0 = time.time()
    vec = embed_one(query)
    elapsed = time.time() - t0

    print(f"  ✅ Returned vector of dim {len(vec)} in {elapsed:.2f}s")
    print(f"  First 5 values: {[round(x, 4) for x in vec[:5]]}")

    # Cache hit test
    t0 = time.time()
    vec2 = embed_one(query)
    elapsed = time.time() - t0
    print(f"  Cache test: same query took {elapsed*1000:.1f}ms (should be <1ms)")
    assert vec == vec2, "Cached vector mismatch!"
    print(f"  ✅ Cache works")
