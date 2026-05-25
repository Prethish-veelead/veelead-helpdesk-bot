# """
# storage/cache.py — SQLite-backed query cache + sync state tracker.

# Two small databases that live as files in DATA_DIR (default ./data, on
# Azure App Service /home/data — persistent across restarts):

#   1. cache.db        : query_cache table (Q&A memoization)
#   2. sync_state.db   : sync_meta + sync_runs tables (delta tokens, audit log)

# All configuration knobs come from config.settings so values can change
# without code edits (e.g. TTL, max entries, history retention).

# Public API:
#     --- Query cache ---
#     cache_lookup(question) -> dict | None
#     cache_store(question, response) -> None
#     cache_invalidate_by_filename(filename) -> int    # returns # cleared
#     cache_invalidate_by_file_id(file_id) -> int
#     cache_clear_expired() -> int                     # cleanup
#     cache_stats() -> dict

#     --- Sync state ---
#     get_delta_token() -> str | None
#     save_delta_token(token) -> None
#     record_sync_run(...) -> int                      # returns run_id
#     list_recent_sync_runs(limit=20) -> list[dict]
#     sync_state_stats() -> dict

#     --- Lifecycle ---
#     init_db() -> None                                # call once at startup
# """

# import hashlib
# import json
# import logging
# import re
# import sqlite3
# import threading
# from contextlib import contextmanager
# from datetime import datetime, timezone, timedelta
# from pathlib import Path
# from typing import Optional, Dict, Any, List, Iterator

# from config import settings

# log = logging.getLogger(__name__)


# # ═══════════════════════════════════════════════════════════
# #  PATHS + CONFIG (all driven by settings)
# # ═══════════════════════════════════════════════════════════

# DATA_DIR = Path(settings.data_dir)
# CACHE_DB_PATH = DATA_DIR / "cache.db"
# SYNC_DB_PATH = DATA_DIR / "sync_state.db"

# CACHE_TTL_HOURS = settings.cache_ttl_hours   # default 168 (7 days)
# SYNC_HISTORY_DAYS = 90                       # keep sync_runs for 90 days

# # Thread safety: SQLite connections aren't thread-safe by default.
# # We use per-thread connections via threading.local.
# _local = threading.local()


# # ═══════════════════════════════════════════════════════════
# #  CONNECTION HELPERS
# # ═══════════════════════════════════════════════════════════

# def _ensure_data_dir() -> None:
#     DATA_DIR.mkdir(parents=True, exist_ok=True)


# def _get_conn(db_path: Path) -> sqlite3.Connection:
#     """Get a thread-local SQLite connection (creates on first call per thread)."""
#     key = f"conn_{db_path.name}"
#     conn = getattr(_local, key, None)
#     if conn is None:
#         _ensure_data_dir()
#         conn = sqlite3.connect(
#             str(db_path),
#             timeout=10.0,
#             isolation_level=None,   # autocommit mode (we use explicit transactions)
#         )
#         conn.row_factory = sqlite3.Row
#         # WAL mode → readers don't block writers (better concurrency)
#         conn.execute("PRAGMA journal_mode=WAL")
#         conn.execute("PRAGMA synchronous=NORMAL")
#         conn.execute("PRAGMA foreign_keys=ON")
#         setattr(_local, key, conn)
#     return conn


# @contextmanager
# def _tx(db_path: Path) -> Iterator[sqlite3.Connection]:
#     """Transactional context manager for a database."""
#     conn = _get_conn(db_path)
#     conn.execute("BEGIN")
#     try:
#         yield conn
#         conn.execute("COMMIT")
#     except Exception:
#         conn.execute("ROLLBACK")
#         raise


# # ═══════════════════════════════════════════════════════════
# #  SCHEMA INITIALIZATION
# # ═══════════════════════════════════════════════════════════

# def init_db() -> None:
#     """
#     Create tables if missing. Idempotent — safe to call every startup.
#     Also runs lightweight maintenance (expired-entry cleanup).
#     """
#     _ensure_data_dir()

#     # ── Query cache schema ──
#     with _tx(CACHE_DB_PATH) as conn:
#         conn.execute("""
#             CREATE TABLE IF NOT EXISTS query_cache (
#                 query_hash    TEXT PRIMARY KEY,
#                 question      TEXT NOT NULL,
#                 response_json TEXT NOT NULL,
#                 sources_json  TEXT NOT NULL,         -- list of filenames cited
#                 file_ids_json TEXT NOT NULL,         -- list of sharepoint_file_ids cited
#                 hit_count     INTEGER NOT NULL DEFAULT 1,
#                 created_at    TEXT NOT NULL,
#                 last_hit_at   TEXT NOT NULL
#             )
#         """)
#         conn.execute("""
#             CREATE INDEX IF NOT EXISTS idx_cache_created_at
#             ON query_cache(created_at)
#         """)
#         conn.execute("""
#             CREATE INDEX IF NOT EXISTS idx_cache_last_hit_at
#             ON query_cache(last_hit_at)
#         """)

#     # ── Sync state schema ──
#     with _tx(SYNC_DB_PATH) as conn:
#         conn.execute("""
#             CREATE TABLE IF NOT EXISTS sync_meta (
#                 key        TEXT PRIMARY KEY,
#                 value      TEXT NOT NULL,
#                 updated_at TEXT NOT NULL
#             )
#         """)
#         conn.execute("""
#             CREATE TABLE IF NOT EXISTS sync_runs (
#                 run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
#                 source_type  TEXT NOT NULL,
#                 started_at   TEXT NOT NULL,
#                 finished_at  TEXT,
#                 added        INTEGER DEFAULT 0,
#                 updated      INTEGER DEFAULT 0,
#                 deleted      INTEGER DEFAULT 0,
#                 status       TEXT NOT NULL,          -- 'success', 'partial', 'failed'
#                 error_msg    TEXT
#             )
#         """)
#         conn.execute("""
#             CREATE INDEX IF NOT EXISTS idx_sync_runs_started
#             ON sync_runs(started_at DESC)
#         """)

#     # Maintenance: clear expired cache + old sync runs
#     cleared_cache = cache_clear_expired()
#     cleared_runs = _clear_old_sync_runs()

#     log.info(
#         f"DB initialized at {DATA_DIR} "
#         f"(cleared {cleared_cache} expired cache entries, "
#         f"{cleared_runs} old sync runs)"
#     )


# # ═══════════════════════════════════════════════════════════
# #  QUERY CACHE
# # ═══════════════════════════════════════════════════════════

# def _normalize_question(q: str) -> str:
#     """Lowercase, strip, collapse whitespace, remove most punctuation."""
#     q = q.lower().strip()
#     q = re.sub(r"\s+", " ", q)
#     # Keep alphanumerics, spaces, and a few semantic chars; drop the rest
#     q = re.sub(r"[^a-z0-9\s\-]", "", q)
#     return q.strip()


# def _hash_question(q: str) -> str:
#     normalized = _normalize_question(q)
#     return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# def cache_lookup(question: str) -> Optional[Dict[str, Any]]:
#     """
#     Look up a cached response. Returns the response dict if found and fresh,
#     or None on miss/expiry.

#     Also increments hit_count + last_hit_at on hit (for analytics).
#     """
#     if not question or not question.strip():
#         return None

#     qhash = _hash_question(question)
#     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()

#     conn = _get_conn(CACHE_DB_PATH)
#     row = conn.execute(
#         "SELECT response_json, created_at FROM query_cache "
#         "WHERE query_hash = ? AND created_at >= ?",
#         (qhash, cutoff)
#     ).fetchone()

#     if not row:
#         return None

#     # Hit — update stats (don't fail caller if this errors)
#     try:
#         now_iso = datetime.now(timezone.utc).isoformat()
#         conn.execute(
#             "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
#             "WHERE query_hash = ?",
#             (now_iso, qhash)
#         )
#     except sqlite3.Error as e:
#         log.warning(f"Cache hit-count update failed (non-fatal): {e}")

#     return json.loads(row["response_json"])


# def cache_store(question: str, response: Dict[str, Any]) -> None:
#     """
#     Cache a response. Skips empty answers and follow-up dissatisfaction queries
#     (caller should pass is_alternative=False/empty answers separately).

#     Extracts source filenames + file IDs from the response so we can
#     invalidate later when those files change.
#     """
#     if not question or not question.strip():
#         return

#     answer = response.get("answer", "").strip()
#     if not answer or answer.startswith("I could not find"):
#         return  # Don't cache "couldn't find" answers — they may become findable

#     sources = response.get("sources") or []
#     docs = response.get("docs") or []
#     file_ids = sorted({
#         d.get("sharepoint_file_id")
#         for d in docs
#         if d.get("sharepoint_file_id")
#     })

#     qhash = _hash_question(question)
#     now_iso = datetime.now(timezone.utc).isoformat()

#     conn = _get_conn(CACHE_DB_PATH)
#     try:
#         conn.execute(
#             """INSERT OR REPLACE INTO query_cache
#                (query_hash, question, response_json, sources_json, file_ids_json,
#                 hit_count, created_at, last_hit_at)
#                VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
#             (
#                 qhash,
#                 question,
#                 json.dumps(response),
#                 json.dumps(sources),
#                 json.dumps(list(file_ids)),
#                 now_iso,
#                 now_iso,
#             )
#         )
#     except sqlite3.Error as e:
#         log.error(f"Cache write failed: {e}")


# def cache_invalidate_by_filename(filename: str) -> int:
#     """
#     Clear cache entries that cited this filename.
#     Called when a SharePoint file is updated or deleted.
#     Returns count of entries cleared.
#     """
#     if not filename:
#         return 0

#     conn = _get_conn(CACHE_DB_PATH)
#     # Use ESCAPE clause so % and _ in the filename are literal.
#     # The JSON form "filename" is what's stored, so we search for that.
#     pattern = "%" + _escape_like(filename) + "%"
#     cursor = conn.execute(
#         "DELETE FROM query_cache WHERE sources_json LIKE ? ESCAPE '\\'",
#         (pattern,)
#     )
#     n = cursor.rowcount
#     if n > 0:
#         log.info(f"Cache: cleared {n} entries citing '{filename}'")
#     return n


# def cache_invalidate_by_file_id(file_id: str) -> int:
#     """
#     Clear cache entries that cited this SharePoint file ID.
#     More reliable than filename when files get renamed.
#     """
#     if not file_id:
#         return 0

#     conn = _get_conn(CACHE_DB_PATH)
#     pattern = "%" + _escape_like(file_id) + "%"
#     cursor = conn.execute(
#         "DELETE FROM query_cache WHERE file_ids_json LIKE ? ESCAPE '\\'",
#         (pattern,)
#     )
#     n = cursor.rowcount
#     if n > 0:
#         log.info(f"Cache: cleared {n} entries citing file_id '{file_id}'")
#     return n


# def cache_clear_expired() -> int:
#     """Delete cache entries older than TTL. Called by init_db() + scheduler."""
#     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
#     conn = _get_conn(CACHE_DB_PATH)
#     cursor = conn.execute(
#         "DELETE FROM query_cache WHERE created_at < ?",
#         (cutoff,)
#     )
#     return cursor.rowcount


# def cache_stats() -> Dict[str, Any]:
#     """Stats for /health endpoint."""
#     conn = _get_conn(CACHE_DB_PATH)
#     row = conn.execute(
#         "SELECT COUNT(*) AS n, "
#         "COALESCE(SUM(hit_count), 0) AS total_hits, "
#         "COALESCE(MAX(last_hit_at), '') AS last_hit "
#         "FROM query_cache"
#     ).fetchone()

#     top = conn.execute(
#         "SELECT question, hit_count FROM query_cache "
#         "ORDER BY hit_count DESC LIMIT 5"
#     ).fetchall()

#     return {
#         "entries": row["n"],
#         "total_hits": row["total_hits"],
#         "last_hit_at": row["last_hit"] or None,
#         "ttl_hours": CACHE_TTL_HOURS,
#         "top_questions": [
#             {"question": r["question"], "hits": r["hit_count"]} for r in top
#         ],
#     }


# def _escape_like(s: str) -> str:
#     """Escape % and _ for SQL LIKE pattern."""
#     return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# # ═══════════════════════════════════════════════════════════
# #  SYNC STATE
# # ═══════════════════════════════════════════════════════════

# DELTA_TOKEN_KEY = "delta_token"
# LAST_FULL_SYNC_KEY = "last_full_sync"


# def get_delta_token() -> Optional[str]:
#     """Return the saved delta token, or None on first run."""
#     conn = _get_conn(SYNC_DB_PATH)
#     row = conn.execute(
#         "SELECT value FROM sync_meta WHERE key = ?",
#         (DELTA_TOKEN_KEY,)
#     ).fetchone()
#     return row["value"] if row else None


# def save_delta_token(token: Optional[str]) -> None:
#     """Persist the delta token from SharePoint Graph API."""
#     if not token:
#         return
#     now_iso = datetime.now(timezone.utc).isoformat()
#     conn = _get_conn(SYNC_DB_PATH)
#     conn.execute(
#         """INSERT INTO sync_meta (key, value, updated_at)
#            VALUES (?, ?, ?)
#            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
#                                           updated_at=excluded.updated_at""",
#         (DELTA_TOKEN_KEY, token, now_iso)
#     )


# def reset_delta_token() -> None:
#     """Force the next sync to be a full sync. Used by /admin/reset_sync."""
#     conn = _get_conn(SYNC_DB_PATH)
#     conn.execute("DELETE FROM sync_meta WHERE key = ?", (DELTA_TOKEN_KEY,))
#     log.info("Delta token reset — next sync will be full")


# def record_sync_run(
#     source_type: str,
#     started_at: datetime,
#     finished_at: Optional[datetime] = None,
#     added: int = 0,
#     updated: int = 0,
#     deleted: int = 0,
#     status: str = "success",
#     error_msg: Optional[str] = None,
# ) -> int:
#     """
#     Record a sync run in the audit log. Returns the run_id.
#     """
#     conn = _get_conn(SYNC_DB_PATH)
#     cursor = conn.execute(
#         """INSERT INTO sync_runs
#            (source_type, started_at, finished_at, added, updated, deleted,
#             status, error_msg)
#            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
#         (
#             source_type,
#             started_at.isoformat(),
#             finished_at.isoformat() if finished_at else None,
#             added,
#             updated,
#             deleted,
#             status,
#             error_msg,
#         )
#     )
#     return cursor.lastrowid


# def update_last_full_sync() -> None:
#     """Mark that a full sync (no delta token) just completed."""
#     now_iso = datetime.now(timezone.utc).isoformat()
#     conn = _get_conn(SYNC_DB_PATH)
#     conn.execute(
#         """INSERT INTO sync_meta (key, value, updated_at)
#            VALUES (?, ?, ?)
#            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
#                                           updated_at=excluded.updated_at""",
#         (LAST_FULL_SYNC_KEY, now_iso, now_iso)
#     )


# def list_recent_sync_runs(limit: int = 20) -> List[Dict[str, Any]]:
#     """Return the N most recent sync runs."""
#     conn = _get_conn(SYNC_DB_PATH)
#     rows = conn.execute(
#         "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
#         (limit,)
#     ).fetchall()
#     return [dict(r) for r in rows]


# def sync_state_stats() -> Dict[str, Any]:
#     """Stats for /health endpoint."""
#     conn = _get_conn(SYNC_DB_PATH)
#     total = conn.execute("SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
#     last = conn.execute(
#         "SELECT started_at, finished_at, status, added, updated, deleted "
#         "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
#     ).fetchone()
#     has_token = bool(get_delta_token())

#     return {
#         "total_sync_runs": total,
#         "has_delta_token": has_token,
#         "last_sync": dict(last) if last else None,
#     }


# def _clear_old_sync_runs() -> int:
#     """Delete sync_runs older than SYNC_HISTORY_DAYS."""
#     cutoff = (
#         datetime.now(timezone.utc) - timedelta(days=SYNC_HISTORY_DAYS)
#     ).isoformat()
#     conn = _get_conn(SYNC_DB_PATH)
#     cursor = conn.execute(
#         "DELETE FROM sync_runs WHERE started_at < ?",
#         (cutoff,)
#     )
#     return cursor.rowcount


# # ═══════════════════════════════════════════════════════════
# #  CLI / quick test
# # ═══════════════════════════════════════════════════════════

# if __name__ == "__main__":
#     """
#     Quick CLI for inspecting the cache.

#     Usage:
#         python -m storage.cache init     # create tables
#         python -m storage.cache stats    # show stats
#         python -m storage.cache clear    # clear cache (keeps sync state)
#     """
#     import sys

#     if len(sys.argv) < 2:
#         print(__doc__)
#         sys.exit(1)

#     cmd = sys.argv[1].lower()
#     if cmd == "init":
#         init_db()
#         print(f"✅ Initialized DBs at {DATA_DIR}")
#     elif cmd == "stats":
#         init_db()
#         print("\n── Query Cache Stats ──")
#         for k, v in cache_stats().items():
#             print(f"  {k}: {v}")
#         print("\n── Sync State Stats ──")
#         for k, v in sync_state_stats().items():
#             print(f"  {k}: {v}")
#     elif cmd == "clear":
#         init_db()
#         conn = _get_conn(CACHE_DB_PATH)
#         cursor = conn.execute("DELETE FROM query_cache")
#         print(f"✅ Cleared {cursor.rowcount} cache entries")
#     else:
#         print(f"Unknown command: {cmd}")
#         sys.exit(1)
"""
storage/cache.py — SQLite-backed query cache + sync state tracker.

Two small databases that live as files in DATA_DIR (default ./data, on
Azure App Service /home/data — persistent across restarts):

  1. cache.db        : query_cache table (Q&A memoization)
  2. sync_state.db   : sync_meta + sync_runs tables (delta tokens, audit log)

All configuration knobs come from config.settings so values can change
without code edits (e.g. TTL, max entries, history retention).

Public API:
    --- Query cache ---
    cache_lookup(question) -> dict | None
    cache_store(question, response) -> None
    cache_invalidate_by_filename(filename) -> int    # returns # cleared
    cache_invalidate_by_file_id(file_id) -> int
    cache_clear_expired() -> int                     # cleanup
    cache_stats() -> dict

    --- Sync state ---
    get_delta_token() -> str | None
    save_delta_token(token) -> None
    record_sync_run(...) -> int                      # returns run_id
    list_recent_sync_runs(limit=20) -> list[dict]
    sync_state_stats() -> dict

    --- Lifecycle ---
    init_db() -> None                                # call once at startup
"""

import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Iterator

from config import settings

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
#  PATHS + CONFIG (all driven by settings)
# ═══════════════════════════════════════════════════════════

DATA_DIR = Path(settings.data_dir)
CACHE_DB_PATH = DATA_DIR / "cache.db"
SYNC_DB_PATH = DATA_DIR / "sync_state.db"

CACHE_TTL_HOURS = settings.cache_ttl_hours   # default 168 (7 days)
SYNC_HISTORY_DAYS = 90                       # keep sync_runs for 90 days

# Thread safety: SQLite connections aren't thread-safe by default.
# We use per-thread connections via threading.local.
_local = threading.local()


# ═══════════════════════════════════════════════════════════
#  CONNECTION HELPERS
# ═══════════════════════════════════════════════════════════

def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Get a thread-local SQLite connection (creates on first call per thread)."""
    key = f"conn_{db_path.name}"
    conn = getattr(_local, key, None)
    if conn is None:
        _ensure_data_dir()
        conn = sqlite3.connect(
            str(db_path),
            timeout=10.0,
            isolation_level=None,   # autocommit mode (we use explicit transactions)
        )
        conn.row_factory = sqlite3.Row
        # WAL mode → readers don't block writers (better concurrency)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        setattr(_local, key, conn)
    return conn


@contextmanager
def _tx(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Transactional context manager for a database."""
    conn = _get_conn(db_path)
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ═══════════════════════════════════════════════════════════
#  SCHEMA INITIALIZATION
# ═══════════════════════════════════════════════════════════

def init_db() -> None:
    """
    Create tables if missing. Idempotent — safe to call every startup.
    Also runs lightweight maintenance (expired-entry cleanup).
    """
    _ensure_data_dir()

    # ── Query cache schema ──
    with _tx(CACHE_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_cache (
                query_hash    TEXT PRIMARY KEY,
                question      TEXT NOT NULL,
                response_json TEXT NOT NULL,
                sources_json  TEXT NOT NULL,         -- list of filenames cited
                file_ids_json TEXT NOT NULL,         -- list of sharepoint_file_ids cited
                hit_count     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                last_hit_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cache_created_at
            ON query_cache(created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cache_last_hit_at
            ON query_cache(last_hit_at)
        """)

        # Migration: add embedding column if it doesn't exist (safe to run repeatedly).
        # Stores the embedding as a JSON-encoded float list. Used for semantic match.
        try:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(query_cache)").fetchall()}
            if "embedding_json" not in existing:
                conn.execute("ALTER TABLE query_cache ADD COLUMN embedding_json TEXT")
                log.info("Cache migration: added embedding_json column for semantic matching")
        except sqlite3.Error as e:
            log.warning(f"Cache column migration check failed (non-fatal): {e}")

    # ── Sync state schema ──
    with _tx(SYNC_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type  TEXT NOT NULL,
                started_at   TEXT NOT NULL,
                finished_at  TEXT,
                added        INTEGER DEFAULT 0,
                updated      INTEGER DEFAULT 0,
                deleted      INTEGER DEFAULT 0,
                status       TEXT NOT NULL,          -- 'success', 'partial', 'failed'
                error_msg    TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sync_runs_started
            ON sync_runs(started_at DESC)
        """)

    # Maintenance: clear expired cache + old sync runs
    cleared_cache = cache_clear_expired()
    cleared_runs = _clear_old_sync_runs()

    log.info(
        f"DB initialized at {DATA_DIR} "
        f"(cleared {cleared_cache} expired cache entries, "
        f"{cleared_runs} old sync runs)"
    )


# ═══════════════════════════════════════════════════════════
#  QUERY CACHE
# ═══════════════════════════════════════════════════════════

def _normalize_question(q: str) -> str:
    """Lowercase, strip, collapse whitespace, remove most punctuation."""
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    # Keep alphanumerics, spaces, and a few semantic chars; drop the rest
    q = re.sub(r"[^a-z0-9\s\-]", "", q)
    return q.strip()


def _hash_question(q: str) -> str:
    normalized = _normalize_question(q)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def cache_lookup(question: str) -> Optional[Dict[str, Any]]:
    """
    Look up a cached response. Returns the response dict if found and fresh,
    or None on miss/expiry.

    Also increments hit_count + last_hit_at on hit (for analytics).
    """
    if not question or not question.strip():
        return None

    qhash = _hash_question(question)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()

    conn = _get_conn(CACHE_DB_PATH)
    row = conn.execute(
        "SELECT response_json, created_at FROM query_cache "
        "WHERE query_hash = ? AND created_at >= ?",
        (qhash, cutoff)
    ).fetchone()

    if not row:
        return None

    # Hit — update stats (don't fail caller if this errors)
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
            "WHERE query_hash = ?",
            (now_iso, qhash)
        )
    except sqlite3.Error as e:
        log.warning(f"Cache hit-count update failed (non-fatal): {e}")

    return json.loads(row["response_json"])


def cache_store(
    question: str,
    response: Dict[str, Any],
    embedding: Optional[List[float]] = None,
) -> None:
    """
    Cache a response. Skips empty answers and "couldn't find" responses.

    Extracts source filenames + file IDs from the response so we can
    invalidate later when those files change.

    If `embedding` is provided, it's stored for semantic ("related question")
    matching by cache_lookup_semantic().
    """
    if not question or not question.strip():
        return

    answer = response.get("answer", "").strip()
    if not answer or answer.startswith("I could not find"):
        return  # Don't cache "couldn't find" answers — they may become findable

    # Detect Veelead's no-answer phrase (used by new prompt style)
    if "couldn't find this in our knowledge base" in answer.lower():
        return

    sources = response.get("sources") or []
    docs = response.get("docs") or []
    file_ids = sorted({
        d.get("sharepoint_file_id")
        for d in docs
        if d.get("sharepoint_file_id")
    })

    qhash = _hash_question(question)
    now_iso = datetime.now(timezone.utc).isoformat()
    embedding_json = json.dumps(embedding) if embedding else None

    conn = _get_conn(CACHE_DB_PATH)
    try:
        conn.execute(
            """INSERT OR REPLACE INTO query_cache
               (query_hash, question, response_json, sources_json, file_ids_json,
                embedding_json, hit_count, created_at, last_hit_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                qhash,
                question,
                json.dumps(response),
                json.dumps(sources),
                json.dumps(list(file_ids)),
                embedding_json,
                now_iso,
                now_iso,
            )
        )
    except sqlite3.Error as e:
        log.error(f"Cache write failed: {e}")


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two equal-length float vectors (0.0 to 1.0)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def cache_lookup_semantic(
    query_embedding: List[float],
    threshold: float = 0.92,
) -> Optional[Dict[str, Any]]:
    """
    Find a cached response whose stored embedding is semantically similar
    to `query_embedding` above `threshold` (cosine similarity).

    Returns a dict like:
        {
            "response": <cached response>,
            "similarity": 0.94,
            "matched_question": "the original cached question text",
        }
    or None if no entry above threshold is found.

    This is called AFTER exact-match cache_lookup() misses. It walks every
    fresh cached entry, computes similarity, and returns the best match
    if it crosses the threshold. For < 5000 cached entries this is fast
    enough (each comparison is ~20 microseconds).
    """
    if not query_embedding:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    conn = _get_conn(CACHE_DB_PATH)

    rows = conn.execute(
        """SELECT query_hash, question, response_json, embedding_json
           FROM query_cache
           WHERE created_at >= ?
           AND embedding_json IS NOT NULL""",
        (cutoff,)
    ).fetchall()

    best_similarity = 0.0
    best_row = None

    for row in rows:
        try:
            cached_emb = json.loads(row["embedding_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        sim = _cosine_similarity(query_embedding, cached_emb)
        if sim > best_similarity:
            best_similarity = sim
            best_row = row

    if not best_row or best_similarity < threshold:
        return None

    # Hit — update stats (don't fail caller if this errors)
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
            "WHERE query_hash = ?",
            (now_iso, best_row["query_hash"])
        )
    except sqlite3.Error as e:
        log.warning(f"Cache hit-count update failed (non-fatal): {e}")

    return {
        "response": json.loads(best_row["response_json"]),
        "similarity": round(float(best_similarity), 4),
        "matched_question": best_row["question"],
    }


def cache_invalidate_by_filename(filename: str) -> int:
    """
    Clear cache entries that cited this filename.
    Called when a SharePoint file is updated or deleted.
    Returns count of entries cleared.
    """
    if not filename:
        return 0

    conn = _get_conn(CACHE_DB_PATH)
    # Use ESCAPE clause so % and _ in the filename are literal.
    # The JSON form "filename" is what's stored, so we search for that.
    pattern = "%" + _escape_like(filename) + "%"
    cursor = conn.execute(
        "DELETE FROM query_cache WHERE sources_json LIKE ? ESCAPE '\\'",
        (pattern,)
    )
    n = cursor.rowcount
    if n > 0:
        log.info(f"Cache: cleared {n} entries citing '{filename}'")
    return n


def cache_invalidate_by_file_id(file_id: str) -> int:
    """
    Clear cache entries that cited this SharePoint file ID.
    More reliable than filename when files get renamed.
    """
    if not file_id:
        return 0

    conn = _get_conn(CACHE_DB_PATH)
    pattern = "%" + _escape_like(file_id) + "%"
    cursor = conn.execute(
        "DELETE FROM query_cache WHERE file_ids_json LIKE ? ESCAPE '\\'",
        (pattern,)
    )
    n = cursor.rowcount
    if n > 0:
        log.info(f"Cache: cleared {n} entries citing file_id '{file_id}'")
    return n


def cache_clear_expired() -> int:
    """Delete cache entries older than TTL. Called by init_db() + scheduler."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
    conn = _get_conn(CACHE_DB_PATH)
    cursor = conn.execute(
        "DELETE FROM query_cache WHERE created_at < ?",
        (cutoff,)
    )
    return cursor.rowcount


def cache_stats() -> Dict[str, Any]:
    """Stats for /health endpoint."""
    conn = _get_conn(CACHE_DB_PATH)
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(hit_count), 0) AS total_hits, "
        "COALESCE(MAX(last_hit_at), '') AS last_hit "
        "FROM query_cache"
    ).fetchone()

    top = conn.execute(
        "SELECT question, hit_count FROM query_cache "
        "ORDER BY hit_count DESC LIMIT 5"
    ).fetchall()

    return {
        "entries": row["n"],
        "total_hits": row["total_hits"],
        "last_hit_at": row["last_hit"] or None,
        "ttl_hours": CACHE_TTL_HOURS,
        "top_questions": [
            {"question": r["question"], "hits": r["hit_count"]} for r in top
        ],
    }


def _escape_like(s: str) -> str:
    """Escape % and _ for SQL LIKE pattern."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ═══════════════════════════════════════════════════════════
#  SYNC STATE
# ═══════════════════════════════════════════════════════════

DELTA_TOKEN_KEY = "delta_token"
LAST_FULL_SYNC_KEY = "last_full_sync"


def get_delta_token() -> Optional[str]:
    """Return the saved delta token, or None on first run."""
    conn = _get_conn(SYNC_DB_PATH)
    row = conn.execute(
        "SELECT value FROM sync_meta WHERE key = ?",
        (DELTA_TOKEN_KEY,)
    ).fetchone()
    return row["value"] if row else None


def save_delta_token(token: Optional[str]) -> None:
    """Persist the delta token from SharePoint Graph API."""
    if not token:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = _get_conn(SYNC_DB_PATH)
    conn.execute(
        """INSERT INTO sync_meta (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        (DELTA_TOKEN_KEY, token, now_iso)
    )


def reset_delta_token() -> None:
    """Force the next sync to be a full sync. Used by /admin/reset_sync."""
    conn = _get_conn(SYNC_DB_PATH)
    conn.execute("DELETE FROM sync_meta WHERE key = ?", (DELTA_TOKEN_KEY,))
    log.info("Delta token reset — next sync will be full")


def record_sync_run(
    source_type: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    added: int = 0,
    updated: int = 0,
    deleted: int = 0,
    status: str = "success",
    error_msg: Optional[str] = None,
) -> int:
    """
    Record a sync run in the audit log. Returns the run_id.
    """
    conn = _get_conn(SYNC_DB_PATH)
    cursor = conn.execute(
        """INSERT INTO sync_runs
           (source_type, started_at, finished_at, added, updated, deleted,
            status, error_msg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_type,
            started_at.isoformat(),
            finished_at.isoformat() if finished_at else None,
            added,
            updated,
            deleted,
            status,
            error_msg,
        )
    )
    return cursor.lastrowid


def update_last_full_sync() -> None:
    """Mark that a full sync (no delta token) just completed."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = _get_conn(SYNC_DB_PATH)
    conn.execute(
        """INSERT INTO sync_meta (key, value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        (LAST_FULL_SYNC_KEY, now_iso, now_iso)
    )


def list_recent_sync_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """Return the N most recent sync runs."""
    conn = _get_conn(SYNC_DB_PATH)
    rows = conn.execute(
        "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def sync_state_stats() -> Dict[str, Any]:
    """Stats for /health endpoint."""
    conn = _get_conn(SYNC_DB_PATH)
    total = conn.execute("SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
    last = conn.execute(
        "SELECT started_at, finished_at, status, added, updated, deleted "
        "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    has_token = bool(get_delta_token())

    return {
        "total_sync_runs": total,
        "has_delta_token": has_token,
        "last_sync": dict(last) if last else None,
    }


def _clear_old_sync_runs() -> int:
    """Delete sync_runs older than SYNC_HISTORY_DAYS."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=SYNC_HISTORY_DAYS)
    ).isoformat()
    conn = _get_conn(SYNC_DB_PATH)
    cursor = conn.execute(
        "DELETE FROM sync_runs WHERE started_at < ?",
        (cutoff,)
    )
    return cursor.rowcount


# ═══════════════════════════════════════════════════════════
#  CLI / quick test
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Quick CLI for inspecting the cache.

    Usage:
        python -m storage.cache init     # create tables
        python -m storage.cache stats    # show stats
        python -m storage.cache clear    # clear cache (keeps sync state)
    """
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "init":
        init_db()
        print(f"✅ Initialized DBs at {DATA_DIR}")
    elif cmd == "stats":
        init_db()
        print("\n── Query Cache Stats ──")
        for k, v in cache_stats().items():
            print(f"  {k}: {v}")
        print("\n── Sync State Stats ──")
        for k, v in sync_state_stats().items():
            print(f"  {k}: {v}")
    elif cmd == "clear":
        init_db()
        conn = _get_conn(CACHE_DB_PATH)
        cursor = conn.execute("DELETE FROM query_cache")
        print(f"✅ Cleared {cursor.rowcount} cache entries")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)