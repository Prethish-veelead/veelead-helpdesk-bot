# """
# sources/local_folder.py — Read documents from a local folder on disk.

# Used for:
#   - Development and testing before SharePoint credentials are available
#   - Fallback mode if SharePoint is unreachable
#   - Simple deployments that just sync from a folder

# Change detection uses (filename, mtime, size, MD5) — survives moves,
# detects content updates, and tells the difference between renamed files
# and new files.

# Files matching certain test/draft patterns are auto-skipped.
# """

# import hashlib
# import json
# import logging
# import os
# import re
# from datetime import datetime, timezone
# from pathlib import Path
# from typing import List, Optional

# from sources.base import DocumentSource, DocumentRef, DocumentData, ChangeSet

# log = logging.getLogger(__name__)


# # Skip these obvious test/draft files. Adjust as needed for your library.
# # Pattern: short random prefix + timestamp (auto-generated test files),
# # or anything ending in -draft.
# SKIP_PATTERNS = [
#     re.compile(r"^[a-z]{3,5}-\d{10,}\.(docx|pdf|html?)$", re.I),  # dfghj-1778312575121.docx
#     re.compile(r"-draft\.", re.I),
#     re.compile(r"^~\$"),                 # Word lock files (~$Document.docx)
#     re.compile(r"^\."),                  # hidden files (.DS_Store, etc.)
# ]


# SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}


# def _file_md5(path: Path, chunk_size: int = 8192) -> str:
#     """MD5 of file contents — used to detect content changes."""
#     h = hashlib.md5()
#     with open(path, "rb") as f:
#         for chunk in iter(lambda: f.read(chunk_size), b""):
#             h.update(chunk)
#     return h.hexdigest()


# def _is_skippable(filename: str) -> bool:
#     for pat in SKIP_PATTERNS:
#         if pat.search(filename):
#             return True
#     return False


# def _doc_type_from_ext(ext: str) -> str:
#     return {
#         ".pdf": "pdf",
#         ".docx": "docx",
#         ".html": "html",
#         ".htm": "html",
#     }.get(ext.lower(), ext.lstrip(".").lower())


# def _ref_from_path(path: Path) -> DocumentRef:
#     """Build a DocumentRef from a local file path."""
#     stat = path.stat()
#     # Use absolute path as the stable file_id for local mode
#     file_id = str(path.resolve())
#     return DocumentRef(
#         file_id=file_id,
#         filename=path.name,
#         doc_type=_doc_type_from_ext(path.suffix),
#         modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
#         size_bytes=stat.st_size,
#         # No SharePoint metadata for local files — all defaults
#         category="Uncategorized",  # explicit; team may want to organize later
#         status="Published",         # local files are assumed publishable
#     )


# class LocalFolderSource(DocumentSource):
#     """
#     Reads files from a folder on disk.

#     State for delta sync is kept in a small JSON sidecar file
#     (default: {folder}/.local_sync_state.json) — stores per-file MD5
#     + mtime to detect changes on the next call to get_changes().
#     """

#     def __init__(self, folder: str):
#         self.folder = Path(folder).resolve()
#         if not self.folder.exists():
#             self.folder.mkdir(parents=True, exist_ok=True)
#             log.info(f"Created local data folder: {self.folder}")
#         if not self.folder.is_dir():
#             raise NotADirectoryError(f"{self.folder} is not a directory")

#         # State file lives at the project's data dir, not inside the docs folder,
#         # so we never accidentally pick it up as a document.
#         from config import settings
#         state_dir = Path(settings.data_dir)
#         state_dir.mkdir(parents=True, exist_ok=True)
#         self.state_path = state_dir / "local_sync_state.json"

#     # ── Required by DocumentSource ──

#     def source_name(self) -> str:
#         return f"LocalFolder({self.folder})"

#     def list_documents(self, only_published: bool = True) -> List[DocumentRef]:
#         """Return all valid (non-skipped, supported-type) files in the folder."""
#         refs: List[DocumentRef] = []
#         for path in self.folder.iterdir():
#             if not path.is_file():
#                 continue
#             if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
#                 continue
#             if _is_skippable(path.name):
#                 log.debug(f"  ⏭  Skipping test/draft file: {path.name}")
#                 continue
#             refs.append(_ref_from_path(path))

#         log.info(f"LocalFolder: {len(refs)} document(s) found in {self.folder}")
#         return refs

#     def download(self, ref: DocumentRef) -> DocumentData:
#         """Read file bytes from disk."""
#         path = Path(ref.file_id)
#         if not path.exists():
#             raise FileNotFoundError(f"Source file vanished: {path}")
#         return DocumentData(ref=ref, content_bytes=path.read_bytes())

#     def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
#         """
#         Compare current folder state vs saved state. Detect added/updated/deleted.

#         The delta_token arg is ignored (we use the JSON state file instead),
#         but kept for interface compatibility with SharePointSource.
#         """
#         previous = self._load_state()
#         current_refs = self.list_documents()
#         current_by_id = {r.file_id: r for r in current_refs}

#         # A forced full sync should re-index every current file even if the
#         # saved local state already thinks the folder is up to date. This is
#         # important when the Azure AI Search index was recreated or cleared.
#         if delta_token is None:
#             current_state = {
#                 ref.file_id: {
#                     "filename": ref.filename,
#                     "mtime": ref.modified_at.isoformat(),
#                     "size": ref.size_bytes,
#                     "md5": _file_md5(Path(ref.file_id)),
#                 }
#                 for ref in current_refs
#             }

#             added = list(current_refs)
#             deleted = [fid for fid in previous.keys() if fid not in current_by_id]

#             self._save_state(current_state)

#             changes = ChangeSet(
#                 added=added,
#                 updated=[],
#                 deleted=deleted,
#                 new_delta_token=None,  # local mode doesn't use tokens
#             )
#             log.info(
#                 f"LocalFolder full sync: +{len(added)} added, "
#                 f"~0 updated, -{len(deleted)} deleted"
#             )
#             return changes

#         # For each current file, compute its content hash for change detection
#         current_state: dict = {}
#         for ref in current_refs:
#             path = Path(ref.file_id)
#             current_state[ref.file_id] = {
#                 "filename": ref.filename,
#                 "mtime": ref.modified_at.isoformat(),
#                 "size": ref.size_bytes,
#                 "md5": _file_md5(path),
#             }

#         added: List[DocumentRef] = []
#         updated: List[DocumentRef] = []
#         for file_id, ref in current_by_id.items():
#             if file_id not in previous:
#                 added.append(ref)
#             else:
#                 # Compare content hash (most reliable change signal)
#                 if current_state[file_id]["md5"] != previous[file_id].get("md5"):
#                     updated.append(ref)

#         # Anything in previous but not in current = deleted
#         deleted: List[str] = [
#             fid for fid in previous.keys() if fid not in current_by_id
#         ]

#         # Save new state for next call
#         self._save_state(current_state)

#         changes = ChangeSet(
#             added=added,
#             updated=updated,
#             deleted=deleted,
#             new_delta_token=None,  # local mode doesn't use tokens
#         )
#         log.info(
#             f"LocalFolder changes: +{len(added)} added, "
#             f"~{len(updated)} updated, -{len(deleted)} deleted"
#         )
#         return changes

#     # ── Internal: state persistence ──

#     def _load_state(self) -> dict:
#         if not self.state_path.exists():
#             return {}
#         try:
#             with open(self.state_path, "r") as f:
#                 return json.load(f)
#         except (json.JSONDecodeError, OSError) as e:
#             log.warning(f"Could not load local sync state: {e}. Starting fresh.")
#             return {}

#     def _save_state(self, state: dict) -> None:
#         try:
#             with open(self.state_path, "w") as f:
#                 json.dump(state, f, indent=2)
#         except OSError as e:
#             log.error(f"Failed to save sync state: {e}")


"""
sources/local_folder.py — Read documents from a local folder on disk.

Used for:
  - Development and testing before SharePoint credentials are available
  - Fallback mode if SharePoint is unreachable
  - Simple deployments that just sync from a folder

Change detection uses (filename, mtime, size, MD5) — survives moves,
detects content updates, and tells the difference between renamed files
and new files.

Files matching certain test/draft patterns are auto-skipped.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from sources.base import DocumentSource, DocumentRef, DocumentData, ChangeSet

log = logging.getLogger(__name__)


# Skip these obvious test/draft files. Adjust as needed for your library.
# Pattern: short random prefix + timestamp (auto-generated test files),
# or anything ending in -draft.
SKIP_PATTERNS = [
    re.compile(r"^[a-z]{3,5}-\d{10,}\.(docx|pdf|html?)$", re.I),  # dfghj-1778312575121.docx
    re.compile(r"-draft\.", re.I),
    re.compile(r"^~\$"),                 # Word lock files (~$Document.docx)
    re.compile(r"^\."),                  # hidden files (.DS_Store, etc.)
]


SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".html", ".htm"}


def _file_md5(path: Path, chunk_size: int = 8192) -> str:
    """MD5 of file contents — used to detect content changes."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_skippable(filename: str) -> bool:
    for pat in SKIP_PATTERNS:
        if pat.search(filename):
            return True
    return False


def _doc_type_from_ext(ext: str) -> str:
    return {
        ".pdf": "pdf",
        ".docx": "docx",
        ".html": "html",
        ".htm": "html",
    }.get(ext.lower(), ext.lstrip(".").lower())


def _ref_from_path(path: Path) -> DocumentRef:
    """Build a DocumentRef from a local file path."""
    stat = path.stat()
    # Use absolute path as the stable file_id for local mode
    file_id = str(path.resolve())
    return DocumentRef(
        file_id=file_id,
        filename=path.name,
        doc_type=_doc_type_from_ext(path.suffix),
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        size_bytes=stat.st_size,
        # No SharePoint metadata for local files — all defaults
        category="Uncategorized",  # explicit; team may want to organize later
        status="Published",         # local files are assumed publishable
        # For local mode, the "url" is just the file path (useful for debugging)
        pdf_url=f"file://{file_id}",
    )


class LocalFolderSource(DocumentSource):
    """
    Reads files from a folder on disk.

    State for delta sync is kept in a small JSON sidecar file
    (default: {folder}/.local_sync_state.json) — stores per-file MD5
    + mtime to detect changes on the next call to get_changes().
    """

    def __init__(self, folder: str):
        self.folder = Path(folder).resolve()
        if not self.folder.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            log.info(f"Created local data folder: {self.folder}")
        if not self.folder.is_dir():
            raise NotADirectoryError(f"{self.folder} is not a directory")

        # State file lives at the project's data dir, not inside the docs folder,
        # so we never accidentally pick it up as a document.
        from config import settings
        state_dir = Path(settings.data_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / "local_sync_state.json"

    # ── Required by DocumentSource ──

    def source_name(self) -> str:
        return f"LocalFolder({self.folder})"

    def list_documents(self, only_published: bool = True) -> List[DocumentRef]:
        """Return all valid (non-skipped, supported-type) files in the folder."""
        refs: List[DocumentRef] = []
        for path in self.folder.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if _is_skippable(path.name):
                log.debug(f"  ⏭  Skipping test/draft file: {path.name}")
                continue
            refs.append(_ref_from_path(path))

        log.info(f"LocalFolder: {len(refs)} document(s) found in {self.folder}")
        return refs

    def download(self, ref: DocumentRef) -> DocumentData:
        """Read file bytes from disk."""
        path = Path(ref.file_id)
        if not path.exists():
            raise FileNotFoundError(f"Source file vanished: {path}")
        return DocumentData(ref=ref, content_bytes=path.read_bytes())

    def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
        """
        Compare current folder state vs saved state. Detect added/updated/deleted.

        The delta_token arg is ignored (we use the JSON state file instead),
        but kept for interface compatibility with SharePointSource.
        """
        previous = self._load_state()
        current_refs = self.list_documents()
        current_by_id = {r.file_id: r for r in current_refs}

        # For each current file, compute its content hash for change detection
        current_state: dict = {}
        for ref in current_refs:
            path = Path(ref.file_id)
            current_state[ref.file_id] = {
                "filename": ref.filename,
                "mtime": ref.modified_at.isoformat(),
                "size": ref.size_bytes,
                "md5": _file_md5(path),
            }

        added: List[DocumentRef] = []
        updated: List[DocumentRef] = []
        for file_id, ref in current_by_id.items():
            if file_id not in previous:
                added.append(ref)
            else:
                # Compare content hash (most reliable change signal)
                if current_state[file_id]["md5"] != previous[file_id].get("md5"):
                    updated.append(ref)

        # Anything in previous but not in current = deleted
        deleted: List[str] = [
            fid for fid in previous.keys() if fid not in current_by_id
        ]

        # Save new state for next call
        self._save_state(current_state)

        changes = ChangeSet(
            added=added,
            updated=updated,
            deleted=deleted,
            new_delta_token=None,  # local mode doesn't use tokens
        )
        log.info(
            f"LocalFolder changes: +{len(added)} added, "
            f"~{len(updated)} updated, -{len(deleted)} deleted"
        )
        return changes

    # ── Internal: state persistence ──

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            with open(self.state_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not load local sync state: {e}. Starting fresh.")
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            with open(self.state_path, "w") as f:
                json.dump(state, f, indent=2)
        except OSError as e:
            log.error(f"Failed to save sync state: {e}")