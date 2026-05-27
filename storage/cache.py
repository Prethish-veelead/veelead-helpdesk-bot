# # # """
# # # storage/cache.py — SQLite-backed query cache + sync state tracker.

# # # Two small databases that live as files in DATA_DIR (default ./data, on
# # # Azure App Service /home/data — persistent across restarts):

# # #   1. cache.db        : query_cache table (Q&A memoization)
# # #   2. sync_state.db   : sync_meta + sync_runs tables (delta tokens, audit log)

# # # All configuration knobs come from config.settings so values can change
# # # without code edits (e.g. TTL, max entries, history retention).

# # # Public API:
# # #     --- Query cache ---
# # #     cache_lookup(question) -> dict | None
# # #     cache_store(question, response) -> None
# # #     cache_invalidate_by_filename(filename) -> int    # returns # cleared
# # #     cache_invalidate_by_file_id(file_id) -> int
# # #     cache_clear_expired() -> int                     # cleanup
# # #     cache_stats() -> dict

# # #     --- Sync state ---
# # #     get_delta_token() -> str | None
# # #     save_delta_token(token) -> None
# # #     record_sync_run(...) -> int                      # returns run_id
# # #     list_recent_sync_runs(limit=20) -> list[dict]
# # #     sync_state_stats() -> dict

# # #     --- Lifecycle ---
# # #     init_db() -> None                                # call once at startup
# # # """

# # # import hashlib
# # # import json
# # # import logging
# # # import re
# # # import sqlite3
# # # import threading
# # # from contextlib import contextmanager
# # # from datetime import datetime, timezone, timedelta
# # # from pathlib import Path
# # # from typing import Optional, Dict, Any, List, Iterator

# # # from config import settings

# # # log = logging.getLogger(__name__)


# # # # ═══════════════════════════════════════════════════════════
# # # #  PATHS + CONFIG (all driven by settings)
# # # # ═══════════════════════════════════════════════════════════

# # # DATA_DIR = Path(settings.data_dir)
# # # CACHE_DB_PATH = DATA_DIR / "cache.db"
# # # SYNC_DB_PATH = DATA_DIR / "sync_state.db"

# # # CACHE_TTL_HOURS = settings.cache_ttl_hours   # default 168 (7 days)
# # # SYNC_HISTORY_DAYS = 90                       # keep sync_runs for 90 days

# # # # Thread safety: SQLite connections aren't thread-safe by default.
# # # # We use per-thread connections via threading.local.
# # # _local = threading.local()


# # # # ═══════════════════════════════════════════════════════════
# # # #  CONNECTION HELPERS
# # # # ═══════════════════════════════════════════════════════════

# # # def _ensure_data_dir() -> None:
# # #     DATA_DIR.mkdir(parents=True, exist_ok=True)


# # # def _get_conn(db_path: Path) -> sqlite3.Connection:
# # #     """Get a thread-local SQLite connection (creates on first call per thread)."""
# # #     key = f"conn_{db_path.name}"
# # #     conn = getattr(_local, key, None)
# # #     if conn is None:
# # #         _ensure_data_dir()
# # #         conn = sqlite3.connect(
# # #             str(db_path),
# # #             timeout=10.0,
# # #             isolation_level=None,   # autocommit mode (we use explicit transactions)
# # #         )
# # #         conn.row_factory = sqlite3.Row
# # #         # WAL mode → readers don't block writers (better concurrency)
# # #         conn.execute("PRAGMA journal_mode=WAL")
# # #         conn.execute("PRAGMA synchronous=NORMAL")
# # #         conn.execute("PRAGMA foreign_keys=ON")
# # #         setattr(_local, key, conn)
# # #     return conn


# # # @contextmanager
# # # def _tx(db_path: Path) -> Iterator[sqlite3.Connection]:
# # #     """Transactional context manager for a database."""
# # #     conn = _get_conn(db_path)
# # #     conn.execute("BEGIN")
# # #     try:
# # #         yield conn
# # #         conn.execute("COMMIT")
# # #     except Exception:
# # #         conn.execute("ROLLBACK")
# # #         raise


# # # # ═══════════════════════════════════════════════════════════
# # # #  SCHEMA INITIALIZATION
# # # # ═══════════════════════════════════════════════════════════

# # # def init_db() -> None:
# # #     """
# # #     Create tables if missing. Idempotent — safe to call every startup.
# # #     Also runs lightweight maintenance (expired-entry cleanup).
# # #     """
# # #     _ensure_data_dir()

# # #     # ── Query cache schema ──
# # #     with _tx(CACHE_DB_PATH) as conn:
# # #         conn.execute("""
# # #             CREATE TABLE IF NOT EXISTS query_cache (
# # #                 query_hash    TEXT PRIMARY KEY,
# # #                 question      TEXT NOT NULL,
# # #                 response_json TEXT NOT NULL,
# # #                 sources_json  TEXT NOT NULL,         -- list of filenames cited
# # #                 file_ids_json TEXT NOT NULL,         -- list of sharepoint_file_ids cited
# # #                 hit_count     INTEGER NOT NULL DEFAULT 1,
# # #                 created_at    TEXT NOT NULL,
# # #                 last_hit_at   TEXT NOT NULL
# # #             )
# # #         """)
# # #         conn.execute("""
# # #             CREATE INDEX IF NOT EXISTS idx_cache_created_at
# # #             ON query_cache(created_at)
# # #         """)
# # #         conn.execute("""
# # #             CREATE INDEX IF NOT EXISTS idx_cache_last_hit_at
# # #             ON query_cache(last_hit_at)
# # #         """)

# # #     # ── Sync state schema ──
# # #     with _tx(SYNC_DB_PATH) as conn:
# # #         conn.execute("""
# # #             CREATE TABLE IF NOT EXISTS sync_meta (
# # #                 key        TEXT PRIMARY KEY,
# # #                 value      TEXT NOT NULL,
# # #                 updated_at TEXT NOT NULL
# # #             )
# # #         """)
# # #         conn.execute("""
# # #             CREATE TABLE IF NOT EXISTS sync_runs (
# # #                 run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
# # #                 source_type  TEXT NOT NULL,
# # #                 started_at   TEXT NOT NULL,
# # #                 finished_at  TEXT,
# # #                 added        INTEGER DEFAULT 0,
# # #                 updated      INTEGER DEFAULT 0,
# # #                 deleted      INTEGER DEFAULT 0,
# # #                 status       TEXT NOT NULL,          -- 'success', 'partial', 'failed'
# # #                 error_msg    TEXT
# # #             )
# # #         """)
# # #         conn.execute("""
# # #             CREATE INDEX IF NOT EXISTS idx_sync_runs_started
# # #             ON sync_runs(started_at DESC)
# # #         """)

# # #     # Maintenance: clear expired cache + old sync runs
# # #     cleared_cache = cache_clear_expired()
# # #     cleared_runs = _clear_old_sync_runs()

# # #     log.info(
# # #         f"DB initialized at {DATA_DIR} "
# # #         f"(cleared {cleared_cache} expired cache entries, "
# # #         f"{cleared_runs} old sync runs)"
# # #     )


# # # # ═══════════════════════════════════════════════════════════
# # # #  QUERY CACHE
# # # # ═══════════════════════════════════════════════════════════

# # # def _normalize_question(q: str) -> str:
# # #     """Lowercase, strip, collapse whitespace, remove most punctuation."""
# # #     q = q.lower().strip()
# # #     q = re.sub(r"\s+", " ", q)
# # #     # Keep alphanumerics, spaces, and a few semantic chars; drop the rest
# # #     q = re.sub(r"[^a-z0-9\s\-]", "", q)
# # #     return q.strip()


# # # def _hash_question(q: str) -> str:
# # #     normalized = _normalize_question(q)
# # #     return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# # # def cache_lookup(question: str) -> Optional[Dict[str, Any]]:
# # #     """
# # #     Look up a cached response. Returns the response dict if found and fresh,
# # #     or None on miss/expiry.

# # #     Also increments hit_count + last_hit_at on hit (for analytics).
# # #     """
# # #     if not question or not question.strip():
# # #         return None

# # #     qhash = _hash_question(question)
# # #     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()

# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     row = conn.execute(
# # #         "SELECT response_json, created_at FROM query_cache "
# # #         "WHERE query_hash = ? AND created_at >= ?",
# # #         (qhash, cutoff)
# # #     ).fetchone()

# # #     if not row:
# # #         return None

# # #     # Hit — update stats (don't fail caller if this errors)
# # #     try:
# # #         now_iso = datetime.now(timezone.utc).isoformat()
# # #         conn.execute(
# # #             "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
# # #             "WHERE query_hash = ?",
# # #             (now_iso, qhash)
# # #         )
# # #     except sqlite3.Error as e:
# # #         log.warning(f"Cache hit-count update failed (non-fatal): {e}")

# # #     return json.loads(row["response_json"])


# # # def cache_store(question: str, response: Dict[str, Any]) -> None:
# # #     """
# # #     Cache a response. Skips empty answers and follow-up dissatisfaction queries
# # #     (caller should pass is_alternative=False/empty answers separately).

# # #     Extracts source filenames + file IDs from the response so we can
# # #     invalidate later when those files change.
# # #     """
# # #     if not question or not question.strip():
# # #         return

# # #     answer = response.get("answer", "").strip()
# # #     if not answer or answer.startswith("I could not find"):
# # #         return  # Don't cache "couldn't find" answers — they may become findable

# # #     sources = response.get("sources") or []
# # #     docs = response.get("docs") or []
# # #     file_ids = sorted({
# # #         d.get("sharepoint_file_id")
# # #         for d in docs
# # #         if d.get("sharepoint_file_id")
# # #     })

# # #     qhash = _hash_question(question)
# # #     now_iso = datetime.now(timezone.utc).isoformat()

# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     try:
# # #         conn.execute(
# # #             """INSERT OR REPLACE INTO query_cache
# # #                (query_hash, question, response_json, sources_json, file_ids_json,
# # #                 hit_count, created_at, last_hit_at)
# # #                VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
# # #             (
# # #                 qhash,
# # #                 question,
# # #                 json.dumps(response),
# # #                 json.dumps(sources),
# # #                 json.dumps(list(file_ids)),
# # #                 now_iso,
# # #                 now_iso,
# # #             )
# # #         )
# # #     except sqlite3.Error as e:
# # #         log.error(f"Cache write failed: {e}")


# # # def cache_invalidate_by_filename(filename: str) -> int:
# # #     """
# # #     Clear cache entries that cited this filename.
# # #     Called when a SharePoint file is updated or deleted.
# # #     Returns count of entries cleared.
# # #     """
# # #     if not filename:
# # #         return 0

# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     # Use ESCAPE clause so % and _ in the filename are literal.
# # #     # The JSON form "filename" is what's stored, so we search for that.
# # #     pattern = "%" + _escape_like(filename) + "%"
# # #     cursor = conn.execute(
# # #         "DELETE FROM query_cache WHERE sources_json LIKE ? ESCAPE '\\'",
# # #         (pattern,)
# # #     )
# # #     n = cursor.rowcount
# # #     if n > 0:
# # #         log.info(f"Cache: cleared {n} entries citing '{filename}'")
# # #     return n


# # # def cache_invalidate_by_file_id(file_id: str) -> int:
# # #     """
# # #     Clear cache entries that cited this SharePoint file ID.
# # #     More reliable than filename when files get renamed.
# # #     """
# # #     if not file_id:
# # #         return 0

# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     pattern = "%" + _escape_like(file_id) + "%"
# # #     cursor = conn.execute(
# # #         "DELETE FROM query_cache WHERE file_ids_json LIKE ? ESCAPE '\\'",
# # #         (pattern,)
# # #     )
# # #     n = cursor.rowcount
# # #     if n > 0:
# # #         log.info(f"Cache: cleared {n} entries citing file_id '{file_id}'")
# # #     return n


# # # def cache_clear_expired() -> int:
# # #     """Delete cache entries older than TTL. Called by init_db() + scheduler."""
# # #     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     cursor = conn.execute(
# # #         "DELETE FROM query_cache WHERE created_at < ?",
# # #         (cutoff,)
# # #     )
# # #     return cursor.rowcount


# # # def cache_stats() -> Dict[str, Any]:
# # #     """Stats for /health endpoint."""
# # #     conn = _get_conn(CACHE_DB_PATH)
# # #     row = conn.execute(
# # #         "SELECT COUNT(*) AS n, "
# # #         "COALESCE(SUM(hit_count), 0) AS total_hits, "
# # #         "COALESCE(MAX(last_hit_at), '') AS last_hit "
# # #         "FROM query_cache"
# # #     ).fetchone()

# # #     top = conn.execute(
# # #         "SELECT question, hit_count FROM query_cache "
# # #         "ORDER BY hit_count DESC LIMIT 5"
# # #     ).fetchall()

# # #     return {
# # #         "entries": row["n"],
# # #         "total_hits": row["total_hits"],
# # #         "last_hit_at": row["last_hit"] or None,
# # #         "ttl_hours": CACHE_TTL_HOURS,
# # #         "top_questions": [
# # #             {"question": r["question"], "hits": r["hit_count"]} for r in top
# # #         ],
# # #     }


# # # def _escape_like(s: str) -> str:
# # #     """Escape % and _ for SQL LIKE pattern."""
# # #     return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# # # # ═══════════════════════════════════════════════════════════
# # # #  SYNC STATE
# # # # ═══════════════════════════════════════════════════════════

# # # DELTA_TOKEN_KEY = "delta_token"
# # # LAST_FULL_SYNC_KEY = "last_full_sync"


# # # def get_delta_token() -> Optional[str]:
# # #     """Return the saved delta token, or None on first run."""
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     row = conn.execute(
# # #         "SELECT value FROM sync_meta WHERE key = ?",
# # #         (DELTA_TOKEN_KEY,)
# # #     ).fetchone()
# # #     return row["value"] if row else None


# # # def save_delta_token(token: Optional[str]) -> None:
# # #     """Persist the delta token from SharePoint Graph API."""
# # #     if not token:
# # #         return
# # #     now_iso = datetime.now(timezone.utc).isoformat()
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     conn.execute(
# # #         """INSERT INTO sync_meta (key, value, updated_at)
# # #            VALUES (?, ?, ?)
# # #            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
# # #                                           updated_at=excluded.updated_at""",
# # #         (DELTA_TOKEN_KEY, token, now_iso)
# # #     )


# # # def reset_delta_token() -> None:
# # #     """Force the next sync to be a full sync. Used by /admin/reset_sync."""
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     conn.execute("DELETE FROM sync_meta WHERE key = ?", (DELTA_TOKEN_KEY,))
# # #     log.info("Delta token reset — next sync will be full")


# # # def record_sync_run(
# # #     source_type: str,
# # #     started_at: datetime,
# # #     finished_at: Optional[datetime] = None,
# # #     added: int = 0,
# # #     updated: int = 0,
# # #     deleted: int = 0,
# # #     status: str = "success",
# # #     error_msg: Optional[str] = None,
# # # ) -> int:
# # #     """
# # #     Record a sync run in the audit log. Returns the run_id.
# # #     """
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     cursor = conn.execute(
# # #         """INSERT INTO sync_runs
# # #            (source_type, started_at, finished_at, added, updated, deleted,
# # #             status, error_msg)
# # #            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
# # #         (
# # #             source_type,
# # #             started_at.isoformat(),
# # #             finished_at.isoformat() if finished_at else None,
# # #             added,
# # #             updated,
# # #             deleted,
# # #             status,
# # #             error_msg,
# # #         )
# # #     )
# # #     return cursor.lastrowid


# # # def update_last_full_sync() -> None:
# # #     """Mark that a full sync (no delta token) just completed."""
# # #     now_iso = datetime.now(timezone.utc).isoformat()
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     conn.execute(
# # #         """INSERT INTO sync_meta (key, value, updated_at)
# # #            VALUES (?, ?, ?)
# # #            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
# # #                                           updated_at=excluded.updated_at""",
# # #         (LAST_FULL_SYNC_KEY, now_iso, now_iso)
# # #     )


# # # def list_recent_sync_runs(limit: int = 20) -> List[Dict[str, Any]]:
# # #     """Return the N most recent sync runs."""
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     rows = conn.execute(
# # #         "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
# # #         (limit,)
# # #     ).fetchall()
# # #     return [dict(r) for r in rows]


# # # def sync_state_stats() -> Dict[str, Any]:
# # #     """Stats for /health endpoint."""
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     total = conn.execute("SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
# # #     last = conn.execute(
# # #         "SELECT started_at, finished_at, status, added, updated, deleted "
# # #         "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
# # #     ).fetchone()
# # #     has_token = bool(get_delta_token())

# # #     return {
# # #         "total_sync_runs": total,
# # #         "has_delta_token": has_token,
# # #         "last_sync": dict(last) if last else None,
# # #     }


# # # def _clear_old_sync_runs() -> int:
# # #     """Delete sync_runs older than SYNC_HISTORY_DAYS."""
# # #     cutoff = (
# # #         datetime.now(timezone.utc) - timedelta(days=SYNC_HISTORY_DAYS)
# # #     ).isoformat()
# # #     conn = _get_conn(SYNC_DB_PATH)
# # #     cursor = conn.execute(
# # #         "DELETE FROM sync_runs WHERE started_at < ?",
# # #         (cutoff,)
# # #     )
# # #     return cursor.rowcount


# # # # ═══════════════════════════════════════════════════════════
# # # #  CLI / quick test
# # # # ═══════════════════════════════════════════════════════════

# # # if __name__ == "__main__":
# # #     """
# # #     Quick CLI for inspecting the cache.

# # #     Usage:
# # #         python -m storage.cache init     # create tables
# # #         python -m storage.cache stats    # show stats
# # #         python -m storage.cache clear    # clear cache (keeps sync state)
# # #     """
# # #     import sys

# # #     if len(sys.argv) < 2:
# # #         print(__doc__)
# # #         sys.exit(1)

# # #     cmd = sys.argv[1].lower()
# # #     if cmd == "init":
# # #         init_db()
# # #         print(f"✅ Initialized DBs at {DATA_DIR}")
# # #     elif cmd == "stats":
# # #         init_db()
# # #         print("\n── Query Cache Stats ──")
# # #         for k, v in cache_stats().items():
# # #             print(f"  {k}: {v}")
# # #         print("\n── Sync State Stats ──")
# # #         for k, v in sync_state_stats().items():
# # #             print(f"  {k}: {v}")
# # #     elif cmd == "clear":
# # #         init_db()
# # #         conn = _get_conn(CACHE_DB_PATH)
# # #         cursor = conn.execute("DELETE FROM query_cache")
# # #         print(f"✅ Cleared {cursor.rowcount} cache entries")
# # #     else:
# # #         print(f"Unknown command: {cmd}")
# # #         sys.exit(1)
# # """
# # storage/cache.py — SQLite-backed query cache + sync state tracker.

# # Two small databases that live as files in DATA_DIR (default ./data, on
# # Azure App Service /home/data — persistent across restarts):

# #   1. cache.db        : query_cache table (Q&A memoization)
# #   2. sync_state.db   : sync_meta + sync_runs tables (delta tokens, audit log)

# # All configuration knobs come from config.settings so values can change
# # without code edits (e.g. TTL, max entries, history retention).

# # Public API:
# #     --- Query cache ---
# #     cache_lookup(question) -> dict | None
# #     cache_store(question, response) -> None
# #     cache_invalidate_by_filename(filename) -> int    # returns # cleared
# #     cache_invalidate_by_file_id(file_id) -> int
# #     cache_clear_expired() -> int                     # cleanup
# #     cache_stats() -> dict

# #     --- Sync state ---
# #     get_delta_token() -> str | None
# #     save_delta_token(token) -> None
# #     record_sync_run(...) -> int                      # returns run_id
# #     list_recent_sync_runs(limit=20) -> list[dict]
# #     sync_state_stats() -> dict

# #     --- Lifecycle ---
# #     init_db() -> None                                # call once at startup
# # """

# # import hashlib
# # import json
# # import logging
# # import re
# # import sqlite3
# # import threading
# # from contextlib import contextmanager
# # from datetime import datetime, timezone, timedelta
# # from pathlib import Path
# # from typing import Optional, Dict, Any, List, Iterator

# # from config import settings

# # log = logging.getLogger(__name__)


# # # ═══════════════════════════════════════════════════════════
# # #  PATHS + CONFIG (all driven by settings)
# # # ═══════════════════════════════════════════════════════════

# # DATA_DIR = Path(settings.data_dir)
# # CACHE_DB_PATH = DATA_DIR / "cache.db"
# # SYNC_DB_PATH = DATA_DIR / "sync_state.db"

# # CACHE_TTL_HOURS = settings.cache_ttl_hours   # default 168 (7 days)
# # SYNC_HISTORY_DAYS = 90                       # keep sync_runs for 90 days

# # # Thread safety: SQLite connections aren't thread-safe by default.
# # # We use per-thread connections via threading.local.
# # _local = threading.local()


# # # ═══════════════════════════════════════════════════════════
# # #  CONNECTION HELPERS
# # # ═══════════════════════════════════════════════════════════

# # def _ensure_data_dir() -> None:
# #     DATA_DIR.mkdir(parents=True, exist_ok=True)


# # def _get_conn(db_path: Path) -> sqlite3.Connection:
# #     """Get a thread-local SQLite connection (creates on first call per thread)."""
# #     key = f"conn_{db_path.name}"
# #     conn = getattr(_local, key, None)
# #     if conn is None:
# #         _ensure_data_dir()
# #         conn = sqlite3.connect(
# #             str(db_path),
# #             timeout=10.0,
# #             isolation_level=None,   # autocommit mode (we use explicit transactions)
# #         )
# #         conn.row_factory = sqlite3.Row
# #         # WAL mode → readers don't block writers (better concurrency)
# #         conn.execute("PRAGMA journal_mode=WAL")
# #         conn.execute("PRAGMA synchronous=NORMAL")
# #         conn.execute("PRAGMA foreign_keys=ON")
# #         setattr(_local, key, conn)
# #     return conn


# # @contextmanager
# # def _tx(db_path: Path) -> Iterator[sqlite3.Connection]:
# #     """Transactional context manager for a database."""
# #     conn = _get_conn(db_path)
# #     conn.execute("BEGIN")
# #     try:
# #         yield conn
# #         conn.execute("COMMIT")
# #     except Exception:
# #         conn.execute("ROLLBACK")
# #         raise


# # # ═══════════════════════════════════════════════════════════
# # #  SCHEMA INITIALIZATION
# # # ═══════════════════════════════════════════════════════════

# # def init_db() -> None:
# #     """
# #     Create tables if missing. Idempotent — safe to call every startup.
# #     Also runs lightweight maintenance (expired-entry cleanup).
# #     """
# #     _ensure_data_dir()

# #     # ── Query cache schema ──
# #     with _tx(CACHE_DB_PATH) as conn:
# #         conn.execute("""
# #             CREATE TABLE IF NOT EXISTS query_cache (
# #                 query_hash    TEXT PRIMARY KEY,
# #                 question      TEXT NOT NULL,
# #                 response_json TEXT NOT NULL,
# #                 sources_json  TEXT NOT NULL,         -- list of filenames cited
# #                 file_ids_json TEXT NOT NULL,         -- list of sharepoint_file_ids cited
# #                 hit_count     INTEGER NOT NULL DEFAULT 1,
# #                 created_at    TEXT NOT NULL,
# #                 last_hit_at   TEXT NOT NULL
# #             )
# #         """)
# #         conn.execute("""
# #             CREATE INDEX IF NOT EXISTS idx_cache_created_at
# #             ON query_cache(created_at)
# #         """)
# #         conn.execute("""
# #             CREATE INDEX IF NOT EXISTS idx_cache_last_hit_at
# #             ON query_cache(last_hit_at)
# #         """)

# #         # Migration: add embedding column if it doesn't exist (safe to run repeatedly).
# #         # Stores the embedding as a JSON-encoded float list. Used for semantic match.
# #         try:
# #             existing = {row[1] for row in conn.execute("PRAGMA table_info(query_cache)").fetchall()}
# #             if "embedding_json" not in existing:
# #                 conn.execute("ALTER TABLE query_cache ADD COLUMN embedding_json TEXT")
# #                 log.info("Cache migration: added embedding_json column for semantic matching")
# #         except sqlite3.Error as e:
# #             log.warning(f"Cache column migration check failed (non-fatal): {e}")

# #     # ── Sync state schema ──
# #     with _tx(SYNC_DB_PATH) as conn:
# #         conn.execute("""
# #             CREATE TABLE IF NOT EXISTS sync_meta (
# #                 key        TEXT PRIMARY KEY,
# #                 value      TEXT NOT NULL,
# #                 updated_at TEXT NOT NULL
# #             )
# #         """)
# #         conn.execute("""
# #             CREATE TABLE IF NOT EXISTS sync_runs (
# #                 run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
# #                 source_type  TEXT NOT NULL,
# #                 started_at   TEXT NOT NULL,
# #                 finished_at  TEXT,
# #                 added        INTEGER DEFAULT 0,
# #                 updated      INTEGER DEFAULT 0,
# #                 deleted      INTEGER DEFAULT 0,
# #                 status       TEXT NOT NULL,          -- 'success', 'partial', 'failed'
# #                 error_msg    TEXT
# #             )
# #         """)
# #         conn.execute("""
# #             CREATE INDEX IF NOT EXISTS idx_sync_runs_started
# #             ON sync_runs(started_at DESC)
# #         """)

# #     # Maintenance: clear expired cache + old sync runs
# #     cleared_cache = cache_clear_expired()
# #     cleared_runs = _clear_old_sync_runs()

# #     log.info(
# #         f"DB initialized at {DATA_DIR} "
# #         f"(cleared {cleared_cache} expired cache entries, "
# #         f"{cleared_runs} old sync runs)"
# #     )


# # # ═══════════════════════════════════════════════════════════
# # #  QUERY CACHE
# # # ═══════════════════════════════════════════════════════════

# # def _normalize_question(q: str) -> str:
# #     """Lowercase, strip, collapse whitespace, remove most punctuation."""
# #     q = q.lower().strip()
# #     q = re.sub(r"\s+", " ", q)
# #     # Keep alphanumerics, spaces, and a few semantic chars; drop the rest
# #     q = re.sub(r"[^a-z0-9\s\-]", "", q)
# #     return q.strip()


# # def _hash_question(q: str) -> str:
# #     normalized = _normalize_question(q)
# #     return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# # def cache_lookup(question: str) -> Optional[Dict[str, Any]]:
# #     """
# #     Look up a cached response. Returns the response dict if found and fresh,
# #     or None on miss/expiry.

# #     Also increments hit_count + last_hit_at on hit (for analytics).
# #     """
# #     if not question or not question.strip():
# #         return None

# #     qhash = _hash_question(question)
# #     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()

# #     conn = _get_conn(CACHE_DB_PATH)
# #     row = conn.execute(
# #         "SELECT response_json, created_at FROM query_cache "
# #         "WHERE query_hash = ? AND created_at >= ?",
# #         (qhash, cutoff)
# #     ).fetchone()

# #     if not row:
# #         return None

# #     # Hit — update stats (don't fail caller if this errors)
# #     try:
# #         now_iso = datetime.now(timezone.utc).isoformat()
# #         conn.execute(
# #             "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
# #             "WHERE query_hash = ?",
# #             (now_iso, qhash)
# #         )
# #     except sqlite3.Error as e:
# #         log.warning(f"Cache hit-count update failed (non-fatal): {e}")

# #     return json.loads(row["response_json"])


# # def cache_store(
# #     question: str,
# #     response: Dict[str, Any],
# #     embedding: Optional[List[float]] = None,
# # ) -> None:
# #     """
# #     Cache a response. Skips empty answers and "couldn't find" responses.

# #     Extracts source filenames + file IDs from the response so we can
# #     invalidate later when those files change.

# #     If `embedding` is provided, it's stored for semantic ("related question")
# #     matching by cache_lookup_semantic().
# #     """
# #     if not question or not question.strip():
# #         return

# #     answer = response.get("answer", "").strip()
# #     if not answer or answer.startswith("I could not find"):
# #         return  # Don't cache "couldn't find" answers — they may become findable

# #     # Detect Veelead's no-answer phrase (used by new prompt style)
# #     if "couldn't find this in our knowledge base" in answer.lower():
# #         return

# #     sources = response.get("sources") or []
# #     docs = response.get("docs") or []
# #     file_ids = sorted({
# #         d.get("sharepoint_file_id")
# #         for d in docs
# #         if d.get("sharepoint_file_id")
# #     })

# #     qhash = _hash_question(question)
# #     now_iso = datetime.now(timezone.utc).isoformat()
# #     embedding_json = json.dumps(embedding) if embedding else None

# #     conn = _get_conn(CACHE_DB_PATH)
# #     try:
# #         conn.execute(
# #             """INSERT OR REPLACE INTO query_cache
# #                (query_hash, question, response_json, sources_json, file_ids_json,
# #                 embedding_json, hit_count, created_at, last_hit_at)
# #                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
# #             (
# #                 qhash,
# #                 question,
# #                 json.dumps(response),
# #                 json.dumps(sources),
# #                 json.dumps(list(file_ids)),
# #                 embedding_json,
# #                 now_iso,
# #                 now_iso,
# #             )
# #         )
# #     except sqlite3.Error as e:
# #         log.error(f"Cache write failed: {e}")


# # def _cosine_similarity(a: List[float], b: List[float]) -> float:
# #     """Cosine similarity between two equal-length float vectors (0.0 to 1.0)."""
# #     if not a or not b or len(a) != len(b):
# #         return 0.0
# #     dot = 0.0
# #     norm_a = 0.0
# #     norm_b = 0.0
# #     for x, y in zip(a, b):
# #         dot += x * y
# #         norm_a += x * x
# #         norm_b += y * y
# #     if norm_a == 0.0 or norm_b == 0.0:
# #         return 0.0
# #     return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


# # def cache_lookup_semantic(
# #     query_embedding: List[float],
# #     threshold: float = 0.92,
# # ) -> Optional[Dict[str, Any]]:
# #     """
# #     Find a cached response whose stored embedding is semantically similar
# #     to `query_embedding` above `threshold` (cosine similarity).

# #     Returns a dict like:
# #         {
# #             "response": <cached response>,
# #             "similarity": 0.94,
# #             "matched_question": "the original cached question text",
# #         }
# #     or None if no entry above threshold is found.

# #     This is called AFTER exact-match cache_lookup() misses. It walks every
# #     fresh cached entry, computes similarity, and returns the best match
# #     if it crosses the threshold. For < 5000 cached entries this is fast
# #     enough (each comparison is ~20 microseconds).
# #     """
# #     if not query_embedding:
# #         return None

# #     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
# #     conn = _get_conn(CACHE_DB_PATH)

# #     rows = conn.execute(
# #         """SELECT query_hash, question, response_json, embedding_json
# #            FROM query_cache
# #            WHERE created_at >= ?
# #            AND embedding_json IS NOT NULL""",
# #         (cutoff,)
# #     ).fetchall()

# #     best_similarity = 0.0
# #     best_row = None

# #     for row in rows:
# #         try:
# #             cached_emb = json.loads(row["embedding_json"])
# #         except (json.JSONDecodeError, TypeError):
# #             continue

# #         sim = _cosine_similarity(query_embedding, cached_emb)
# #         if sim > best_similarity:
# #             best_similarity = sim
# #             best_row = row

# #     if not best_row or best_similarity < threshold:
# #         return None

# #     # Hit — update stats (don't fail caller if this errors)
# #     try:
# #         now_iso = datetime.now(timezone.utc).isoformat()
# #         conn.execute(
# #             "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
# #             "WHERE query_hash = ?",
# #             (now_iso, best_row["query_hash"])
# #         )
# #     except sqlite3.Error as e:
# #         log.warning(f"Cache hit-count update failed (non-fatal): {e}")

# #     return {
# #         "response": json.loads(best_row["response_json"]),
# #         "similarity": round(float(best_similarity), 4),
# #         "matched_question": best_row["question"],
# #     }


# # def cache_invalidate_by_filename(filename: str) -> int:
# #     """
# #     Clear cache entries that cited this filename.
# #     Called when a SharePoint file is updated or deleted.
# #     Returns count of entries cleared.
# #     """
# #     if not filename:
# #         return 0

# #     conn = _get_conn(CACHE_DB_PATH)
# #     # Use ESCAPE clause so % and _ in the filename are literal.
# #     # The JSON form "filename" is what's stored, so we search for that.
# #     pattern = "%" + _escape_like(filename) + "%"
# #     cursor = conn.execute(
# #         "DELETE FROM query_cache WHERE sources_json LIKE ? ESCAPE '\\'",
# #         (pattern,)
# #     )
# #     n = cursor.rowcount
# #     if n > 0:
# #         log.info(f"Cache: cleared {n} entries citing '{filename}'")
# #     return n


# # def cache_invalidate_by_file_id(file_id: str) -> int:
# #     """
# #     Clear cache entries that cited this SharePoint file ID.
# #     More reliable than filename when files get renamed.
# #     """
# #     if not file_id:
# #         return 0

# #     conn = _get_conn(CACHE_DB_PATH)
# #     pattern = "%" + _escape_like(file_id) + "%"
# #     cursor = conn.execute(
# #         "DELETE FROM query_cache WHERE file_ids_json LIKE ? ESCAPE '\\'",
# #         (pattern,)
# #     )
# #     n = cursor.rowcount
# #     if n > 0:
# #         log.info(f"Cache: cleared {n} entries citing file_id '{file_id}'")
# #     return n


# # def cache_clear_expired() -> int:
# #     """Delete cache entries older than TTL. Called by init_db() + scheduler."""
# #     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
# #     conn = _get_conn(CACHE_DB_PATH)
# #     cursor = conn.execute(
# #         "DELETE FROM query_cache WHERE created_at < ?",
# #         (cutoff,)
# #     )
# #     return cursor.rowcount


# # def cache_stats() -> Dict[str, Any]:
# #     """Stats for /health endpoint."""
# #     conn = _get_conn(CACHE_DB_PATH)
# #     row = conn.execute(
# #         "SELECT COUNT(*) AS n, "
# #         "COALESCE(SUM(hit_count), 0) AS total_hits, "
# #         "COALESCE(MAX(last_hit_at), '') AS last_hit "
# #         "FROM query_cache"
# #     ).fetchone()

# #     top = conn.execute(
# #         "SELECT question, hit_count FROM query_cache "
# #         "ORDER BY hit_count DESC LIMIT 5"
# #     ).fetchall()

# #     return {
# #         "entries": row["n"],
# #         "total_hits": row["total_hits"],
# #         "last_hit_at": row["last_hit"] or None,
# #         "ttl_hours": CACHE_TTL_HOURS,
# #         "top_questions": [
# #             {"question": r["question"], "hits": r["hit_count"]} for r in top
# #         ],
# #     }


# # def _escape_like(s: str) -> str:
# #     """Escape % and _ for SQL LIKE pattern."""
# #     return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# # # ═══════════════════════════════════════════════════════════
# # #  SYNC STATE
# # # ═══════════════════════════════════════════════════════════

# # DELTA_TOKEN_KEY = "delta_token"
# # LAST_FULL_SYNC_KEY = "last_full_sync"


# # def get_delta_token() -> Optional[str]:
# #     """Return the saved delta token, or None on first run."""
# #     conn = _get_conn(SYNC_DB_PATH)
# #     row = conn.execute(
# #         "SELECT value FROM sync_meta WHERE key = ?",
# #         (DELTA_TOKEN_KEY,)
# #     ).fetchone()
# #     return row["value"] if row else None


# # def save_delta_token(token: Optional[str]) -> None:
# #     """Persist the delta token from SharePoint Graph API."""
# #     if not token:
# #         return
# #     now_iso = datetime.now(timezone.utc).isoformat()
# #     conn = _get_conn(SYNC_DB_PATH)
# #     conn.execute(
# #         """INSERT INTO sync_meta (key, value, updated_at)
# #            VALUES (?, ?, ?)
# #            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
# #                                           updated_at=excluded.updated_at""",
# #         (DELTA_TOKEN_KEY, token, now_iso)
# #     )


# # def reset_delta_token() -> None:
# #     """Force the next sync to be a full sync. Used by /admin/reset_sync."""
# #     conn = _get_conn(SYNC_DB_PATH)
# #     conn.execute("DELETE FROM sync_meta WHERE key = ?", (DELTA_TOKEN_KEY,))
# #     log.info("Delta token reset — next sync will be full")


# # def record_sync_run(
# #     source_type: str,
# #     started_at: datetime,
# #     finished_at: Optional[datetime] = None,
# #     added: int = 0,
# #     updated: int = 0,
# #     deleted: int = 0,
# #     status: str = "success",
# #     error_msg: Optional[str] = None,
# # ) -> int:
# #     """
# #     Record a sync run in the audit log. Returns the run_id.
# #     """
# #     conn = _get_conn(SYNC_DB_PATH)
# #     cursor = conn.execute(
# #         """INSERT INTO sync_runs
# #            (source_type, started_at, finished_at, added, updated, deleted,
# #             status, error_msg)
# #            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
# #         (
# #             source_type,
# #             started_at.isoformat(),
# #             finished_at.isoformat() if finished_at else None,
# #             added,
# #             updated,
# #             deleted,
# #             status,
# #             error_msg,
# #         )
# #     )
# #     return cursor.lastrowid


# # def update_last_full_sync() -> None:
# #     """Mark that a full sync (no delta token) just completed."""
# #     now_iso = datetime.now(timezone.utc).isoformat()
# #     conn = _get_conn(SYNC_DB_PATH)
# #     conn.execute(
# #         """INSERT INTO sync_meta (key, value, updated_at)
# #            VALUES (?, ?, ?)
# #            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
# #                                           updated_at=excluded.updated_at""",
# #         (LAST_FULL_SYNC_KEY, now_iso, now_iso)
# #     )


# # def list_recent_sync_runs(limit: int = 20) -> List[Dict[str, Any]]:
# #     """Return the N most recent sync runs."""
# #     conn = _get_conn(SYNC_DB_PATH)
# #     rows = conn.execute(
# #         "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?",
# #         (limit,)
# #     ).fetchall()
# #     return [dict(r) for r in rows]


# # def sync_state_stats() -> Dict[str, Any]:
# #     """Stats for /health endpoint."""
# #     conn = _get_conn(SYNC_DB_PATH)
# #     total = conn.execute("SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"]
# #     last = conn.execute(
# #         "SELECT started_at, finished_at, status, added, updated, deleted "
# #         "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
# #     ).fetchone()
# #     has_token = bool(get_delta_token())

# #     return {
# #         "total_sync_runs": total,
# #         "has_delta_token": has_token,
# #         "last_sync": dict(last) if last else None,
# #     }


# # def _clear_old_sync_runs() -> int:
# #     """Delete sync_runs older than SYNC_HISTORY_DAYS."""
# #     cutoff = (
# #         datetime.now(timezone.utc) - timedelta(days=SYNC_HISTORY_DAYS)
# #     ).isoformat()
# #     conn = _get_conn(SYNC_DB_PATH)
# #     cursor = conn.execute(
# #         "DELETE FROM sync_runs WHERE started_at < ?",
# #         (cutoff,)
# #     )
# #     return cursor.rowcount


# # # ═══════════════════════════════════════════════════════════
# # #  CLI / quick test
# # # ═══════════════════════════════════════════════════════════

# # if __name__ == "__main__":
# #     """
# #     Quick CLI for inspecting the cache.

# #     Usage:
# #         python -m storage.cache init     # create tables
# #         python -m storage.cache stats    # show stats
# #         python -m storage.cache clear    # clear cache (keeps sync state)
# #     """
# #     import sys

# #     if len(sys.argv) < 2:
# #         print(__doc__)
# #         sys.exit(1)

# #     cmd = sys.argv[1].lower()
# #     if cmd == "init":
# #         init_db()
# #         print(f"✅ Initialized DBs at {DATA_DIR}")
# #     elif cmd == "stats":
# #         init_db()
# #         print("\n── Query Cache Stats ──")
# #         for k, v in cache_stats().items():
# #             print(f"  {k}: {v}")
# #         print("\n── Sync State Stats ──")
# #         for k, v in sync_state_stats().items():
# #             print(f"  {k}: {v}")
# #     elif cmd == "clear":
# #         init_db()
# #         conn = _get_conn(CACHE_DB_PATH)
# #         cursor = conn.execute("DELETE FROM query_cache")
# #         print(f"✅ Cleared {cursor.rowcount} cache entries")
# #     else:
# #         print(f"Unknown command: {cmd}")
# #         sys.exit(1)

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

#         # Migration: add embedding column if it doesn't exist (safe to run repeatedly).
#         # Stores the embedding as a JSON-encoded float list. Used for semantic match.
#         try:
#             existing = {row[1] for row in conn.execute("PRAGMA table_info(query_cache)").fetchall()}
#             if "embedding_json" not in existing:
#                 conn.execute("ALTER TABLE query_cache ADD COLUMN embedding_json TEXT")
#                 log.info("Cache migration: added embedding_json column for semantic matching")
#         except sqlite3.Error as e:
#             log.warning(f"Cache column migration check failed (non-fatal): {e}")

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


# def cache_entry_sources(cached_response: Dict[str, Any]) -> List[str]:
#     """
#     Extract the list of source filenames cited by a cached response.

#     Used by app.py to validate a cache hit: if any cited filename is no
#     longer in the search index, the cache entry is stale and must be dropped.
#     """
#     if not cached_response:
#         return []

#     # The response stores sources as a top-level list of filenames
#     sources = cached_response.get("sources") or []
#     if isinstance(sources, list):
#         return [s for s in sources if isinstance(s, str) and s.strip()]

#     # Fallback: look inside chunks
#     chunks = cached_response.get("chunks") or []
#     fns = set()
#     for c in chunks:
#         if isinstance(c, dict):
#             fn = c.get("filename")
#             if fn and isinstance(fn, str):
#                 fns.add(fn)
#     return list(fns)


# def cache_delete_by_question(question: str) -> bool:
#     """
#     Delete a specific cache entry. Called when validate-on-read detects
#     the entry's source files are no longer in the index.

#     Returns True if an entry was deleted, False otherwise.
#     """
#     if not question:
#         return False
#     qhash = _hash_question(question)
#     conn = _get_conn(CACHE_DB_PATH)
#     try:
#         cursor = conn.execute(
#             "DELETE FROM query_cache WHERE query_hash = ?",
#             (qhash,)
#         )
#         return cursor.rowcount > 0
#     except sqlite3.Error as e:
#         log.warning(f"Cache entry delete failed (non-fatal): {e}")
#         return False


# def cache_store(
#     question: str,
#     response: Dict[str, Any],
#     embedding: Optional[List[float]] = None,
# ) -> None:
#     """
#     Cache a response. Skips empty answers and "couldn't find" responses.

#     Extracts source filenames + file IDs from the response so we can
#     invalidate later when those files change.

#     If `embedding` is provided, it's stored for semantic ("related question")
#     matching by cache_lookup_semantic().
#     """
#     if not question or not question.strip():
#         return

#     answer = response.get("answer", "").strip()
#     if not answer or answer.startswith("I could not find"):
#         return  # Don't cache "couldn't find" answers — they may become findable

#     # Detect Veelead's no-answer phrase (used by new prompt style)
#     if "couldn't find this in our knowledge base" in answer.lower():
#         return

#     sources = response.get("sources") or []
#     docs = response.get("docs") or []
#     file_ids = sorted({
#         d.get("sharepoint_file_id")
#         for d in docs
#         if d.get("sharepoint_file_id")
#     })

#     qhash = _hash_question(question)
#     now_iso = datetime.now(timezone.utc).isoformat()
#     embedding_json = json.dumps(embedding) if embedding else None

#     conn = _get_conn(CACHE_DB_PATH)
#     try:
#         conn.execute(
#             """INSERT OR REPLACE INTO query_cache
#                (query_hash, question, response_json, sources_json, file_ids_json,
#                 embedding_json, hit_count, created_at, last_hit_at)
#                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
#             (
#                 qhash,
#                 question,
#                 json.dumps(response),
#                 json.dumps(sources),
#                 json.dumps(list(file_ids)),
#                 embedding_json,
#                 now_iso,
#                 now_iso,
#             )
#         )
#     except sqlite3.Error as e:
#         log.error(f"Cache write failed: {e}")


# def _cosine_similarity(a: List[float], b: List[float]) -> float:
#     """Cosine similarity between two equal-length float vectors (0.0 to 1.0)."""
#     if not a or not b or len(a) != len(b):
#         return 0.0
#     dot = 0.0
#     norm_a = 0.0
#     norm_b = 0.0
#     for x, y in zip(a, b):
#         dot += x * y
#         norm_a += x * x
#         norm_b += y * y
#     if norm_a == 0.0 or norm_b == 0.0:
#         return 0.0
#     return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


# def cache_lookup_semantic(
#     query_embedding: List[float],
#     threshold: float = 0.92,
# ) -> Optional[Dict[str, Any]]:
#     """
#     Find a cached response whose stored embedding is semantically similar
#     to `query_embedding` above `threshold` (cosine similarity).

#     Returns a dict like:
#         {
#             "response": <cached response>,
#             "similarity": 0.94,
#             "matched_question": "the original cached question text",
#         }
#     or None if no entry above threshold is found.

#     This is called AFTER exact-match cache_lookup() misses. It walks every
#     fresh cached entry, computes similarity, and returns the best match
#     if it crosses the threshold. For < 5000 cached entries this is fast
#     enough (each comparison is ~20 microseconds).
#     """
#     if not query_embedding:
#         return None

#     cutoff = (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)).isoformat()
#     conn = _get_conn(CACHE_DB_PATH)

#     rows = conn.execute(
#         """SELECT query_hash, question, response_json, embedding_json
#            FROM query_cache
#            WHERE created_at >= ?
#            AND embedding_json IS NOT NULL""",
#         (cutoff,)
#     ).fetchall()

#     best_similarity = 0.0
#     best_row = None

#     for row in rows:
#         try:
#             cached_emb = json.loads(row["embedding_json"])
#         except (json.JSONDecodeError, TypeError):
#             continue

#         sim = _cosine_similarity(query_embedding, cached_emb)
#         if sim > best_similarity:
#             best_similarity = sim
#             best_row = row

#     if not best_row or best_similarity < threshold:
#         return None

#     # Hit — update stats (don't fail caller if this errors)
#     try:
#         now_iso = datetime.now(timezone.utc).isoformat()
#         conn.execute(
#             "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? "
#             "WHERE query_hash = ?",
#             (now_iso, best_row["query_hash"])
#         )
#     except sqlite3.Error as e:
#         log.warning(f"Cache hit-count update failed (non-fatal): {e}")

#     return {
#         "response": json.loads(best_row["response_json"]),
#         "similarity": round(float(best_similarity), 4),
#         "matched_question": best_row["question"],
#     }


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

        # LLM usage tracking — every chat/embedding call gets recorded here
        # for cost monitoring via /admin/usage endpoint.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                call_type     TEXT NOT NULL,         -- 'answer', 'spell', 'context', 'classify', 'embed'
                model         TEXT NOT NULL,         -- 'gpt-4o-mini', 'text-embedding-3-small', etc.
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens  INTEGER NOT NULL DEFAULT 0,
                cost_inr      REAL    NOT NULL DEFAULT 0.0,
                request_id    TEXT,                  -- correlates with [req-xxxx] in logs
                question      TEXT,                  -- truncated to 200 chars for privacy
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_usage_created
            ON llm_usage(created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_usage_request_id
            ON llm_usage(request_id)
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


def cache_entry_sources(cached_response: Dict[str, Any]) -> List[str]:
    """
    Extract the list of source filenames cited by a cached response.

    Used by app.py to validate a cache hit: if any cited filename is no
    longer in the search index, the cache entry is stale and must be dropped.
    """
    if not cached_response:
        return []

    # The response stores sources as a top-level list of filenames
    sources = cached_response.get("sources") or []
    if isinstance(sources, list):
        return [s for s in sources if isinstance(s, str) and s.strip()]

    # Fallback: look inside chunks
    chunks = cached_response.get("chunks") or []
    fns = set()
    for c in chunks:
        if isinstance(c, dict):
            fn = c.get("filename")
            if fn and isinstance(fn, str):
                fns.add(fn)
    return list(fns)


def cache_delete_by_question(question: str) -> bool:
    """
    Delete a specific cache entry. Called when validate-on-read detects
    the entry's source files are no longer in the index.

    Returns True if an entry was deleted, False otherwise.
    """
    if not question:
        return False
    qhash = _hash_question(question)
    conn = _get_conn(CACHE_DB_PATH)
    try:
        cursor = conn.execute(
            "DELETE FROM query_cache WHERE query_hash = ?",
            (qhash,)
        )
        return cursor.rowcount > 0
    except sqlite3.Error as e:
        log.warning(f"Cache entry delete failed (non-fatal): {e}")
        return False


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
#  LLM USAGE TRACKING (cost & token metering)
# ═══════════════════════════════════════════════════════════

# Cost table — INR per 1 million tokens. Source: Azure OpenAI pricing,
# India region, as of May 2026. Update if pricing changes.
# Format: { model_name: (input_per_1M, output_per_1M) }
_MODEL_COSTS_INR_PER_1M = {
    # Chat models
    "gpt-4o-mini":             (12.5,  50.0),    # ~$0.15/$0.60 per 1M
    "gpt-4o":                  (208.0, 833.0),   # ~$2.50/$10 per 1M
    "gpt-4-turbo":             (833.0, 2500.0),  # ~$10/$30 per 1M

    # Embedding models — only "input" cost matters (no output tokens)
    "text-embedding-3-small":  (1.67,  0.0),     # ~$0.02 per 1M
    "text-embedding-3-large":  (10.83, 0.0),     # ~$0.13 per 1M
    "text-embedding-ada-002":  (8.33,  0.0),     # ~$0.10 per 1M
}


def _estimate_cost_inr(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in INR for one LLM call. Returns 0.0 for unknown models."""
    if not model:
        return 0.0
    # Match by prefix to handle deployment names that include version suffixes
    for known_model, (input_rate, output_rate) in _MODEL_COSTS_INR_PER_1M.items():
        if known_model in model.lower():
            return (
                (input_tokens / 1_000_000) * input_rate +
                (output_tokens / 1_000_000) * output_rate
            )
    return 0.0


def record_llm_usage(
    call_type: str,
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
    request_id: Optional[str] = None,
    question: Optional[str] = None,
) -> None:
    """
    Record one LLM API call for cost tracking.

    Args:
      call_type:    'answer' | 'spell' | 'context' | 'classify' | 'embed'
      model:        deployment name (e.g. 'gpt-4o-mini')
      input_tokens: prompt token count (from OpenAI response.usage)
      output_tokens: completion token count (0 for embeddings)
      request_id:   correlation ID for the parent HTTP request
      question:     user's question (truncated for privacy)

    Failures are logged but never raised — token tracking should not
    affect the user's request.
    """
    try:
        total = input_tokens + output_tokens
        cost = _estimate_cost_inr(model, input_tokens, output_tokens)
        now_iso = datetime.now(timezone.utc).isoformat()
        # Truncate question for storage (privacy + size)
        q_trunc = (question[:200] + "...") if question and len(question) > 200 else question

        conn = _get_conn(CACHE_DB_PATH)
        conn.execute(
            """INSERT INTO llm_usage
               (call_type, model, input_tokens, output_tokens, total_tokens,
                cost_inr, request_id, question, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (call_type, model, input_tokens, output_tokens, total,
             cost, request_id, q_trunc, now_iso)
        )
    except Exception as e:
        log.warning(f"record_llm_usage failed (non-fatal): {e}")


def llm_usage_stats(days: int = 7) -> Dict[str, Any]:
    """
    Aggregate LLM usage over the last N days.

    Returns counts, token totals, and cost in INR — broken down by
    call_type and model. Used by /admin/usage endpoint.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _get_conn(CACHE_DB_PATH)

    # Overall totals
    row = conn.execute(
        """SELECT COUNT(*) AS calls,
                  SUM(input_tokens) AS in_t,
                  SUM(output_tokens) AS out_t,
                  SUM(total_tokens) AS tot_t,
                  SUM(cost_inr) AS cost
           FROM llm_usage
           WHERE created_at >= ?""",
        (cutoff,)
    ).fetchone()

    totals = {
        "window_days": days,
        "total_calls": row["calls"] or 0,
        "input_tokens": row["in_t"] or 0,
        "output_tokens": row["out_t"] or 0,
        "total_tokens": row["tot_t"] or 0,
        "total_cost_inr": round(row["cost"] or 0.0, 4),
    }

    # Breakdown by call_type
    by_type = []
    for r in conn.execute(
        """SELECT call_type,
                  COUNT(*) AS calls,
                  SUM(total_tokens) AS tokens,
                  SUM(cost_inr) AS cost
           FROM llm_usage
           WHERE created_at >= ?
           GROUP BY call_type
           ORDER BY cost DESC""",
        (cutoff,)
    ).fetchall():
        by_type.append({
            "call_type": r["call_type"],
            "calls": r["calls"],
            "tokens": r["tokens"] or 0,
            "cost_inr": round(r["cost"] or 0.0, 4),
        })

    # Breakdown by model
    by_model = []
    for r in conn.execute(
        """SELECT model,
                  COUNT(*) AS calls,
                  SUM(total_tokens) AS tokens,
                  SUM(cost_inr) AS cost
           FROM llm_usage
           WHERE created_at >= ?
           GROUP BY model
           ORDER BY cost DESC""",
        (cutoff,)
    ).fetchall():
        by_model.append({
            "model": r["model"],
            "calls": r["calls"],
            "tokens": r["tokens"] or 0,
            "cost_inr": round(r["cost"] or 0.0, 4),
        })

    # Daily breakdown
    by_day = []
    for r in conn.execute(
        """SELECT substr(created_at, 1, 10) AS day,
                  COUNT(*) AS calls,
                  SUM(total_tokens) AS tokens,
                  SUM(cost_inr) AS cost
           FROM llm_usage
           WHERE created_at >= ?
           GROUP BY day
           ORDER BY day DESC""",
        (cutoff,)
    ).fetchall():
        by_day.append({
            "day": r["day"],
            "calls": r["calls"],
            "tokens": r["tokens"] or 0,
            "cost_inr": round(r["cost"] or 0.0, 4),
        })

    # Top 10 most expensive individual calls
    expensive_calls = []
    for r in conn.execute(
        """SELECT call_type, model, total_tokens, cost_inr, question, created_at
           FROM llm_usage
           WHERE created_at >= ?
           ORDER BY cost_inr DESC
           LIMIT 10""",
        (cutoff,)
    ).fetchall():
        expensive_calls.append({
            "call_type": r["call_type"],
            "model": r["model"],
            "tokens": r["total_tokens"],
            "cost_inr": round(r["cost_inr"], 4),
            "question_preview": (r["question"] or "")[:100],
            "at": r["created_at"],
        })

    return {
        **totals,
        "by_call_type": by_type,
        "by_model": by_model,
        "by_day": by_day,
        "top_10_expensive_calls": expensive_calls,
    }


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
        print("\n── LLM Usage Stats ──")
        for k, v in llm_usage_stats().items():
            print(f"  {k}: {v}")
    elif cmd == "clear":
        init_db()
        conn = _get_conn(CACHE_DB_PATH)
        cursor = conn.execute("DELETE FROM query_cache")
        print(f"✅ Cleared {cursor.rowcount} cache entries")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)