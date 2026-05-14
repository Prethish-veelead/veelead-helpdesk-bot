"""
sources/__init__.py — Factory that returns the configured DocumentSource.

Usage from anywhere in the codebase:
    from sources import get_source
    source = get_source()
    docs = source.list_documents()

Picks LocalFolderSource or SharePointSource based on SOURCE_TYPE env var.
"""

import logging
from typing import Optional

from sources.base import DocumentSource, DocumentRef, DocumentData, ChangeSet
from config import settings

log = logging.getLogger(__name__)

_cached_source: Optional[DocumentSource] = None


def get_source() -> DocumentSource:
    """
    Return the appropriate DocumentSource instance based on config.
    Cached after first call.
    """
    global _cached_source
    if _cached_source is not None:
        return _cached_source

    if settings.is_local_mode:
        from sources.local_folder import LocalFolderSource
        _cached_source = LocalFolderSource(settings.local_data_folder)
    elif settings.is_sharepoint_mode:
        from sources.sharepoint import SharePointSource
        _cached_source = SharePointSource()
    else:
        raise ValueError(f"Unknown SOURCE_TYPE: {settings.source_type}")

    log.info(f"Document source: {_cached_source.source_name()}")
    return _cached_source


def reset_source_cache() -> None:
    """Used by tests to force re-creation."""
    global _cached_source
    _cached_source = None


__all__ = [
    "get_source",
    "reset_source_cache",
    "DocumentSource",
    "DocumentRef",
    "DocumentData",
    "ChangeSet",
]
