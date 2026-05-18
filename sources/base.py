# # """
# # sources/base.py — Abstract DocumentSource interface.

# # Both LocalFolderSource and SharePointSource implement this contract.
# # The rest of the bot uses these methods without knowing which source it's
# # talking to. Swap source = change one env variable.

# # Data model:
# #     DocumentRef  : lightweight metadata (id, name, modified date, etc.)
# #     DocumentData : the actual file bytes + parsed metadata
# #     ChangeSet    : delta sync result (added/updated/deleted lists)
# # """

# # from abc import ABC, abstractmethod
# # from dataclasses import dataclass, field
# # from datetime import datetime
# # from typing import List, Optional, Dict, Any


# # @dataclass
# # class DocumentRef:
# #     """
# #     Lightweight reference to a document. Just enough info to identify it
# #     and decide whether to (re)download it.
# #     """
# #     file_id: str              # Unique ID — SharePoint file ID OR local file path
# #     filename: str             # Display name with extension
# #     doc_type: str             # 'pdf', 'docx', 'html'
# #     modified_at: datetime     # Last modified timestamp
# #     size_bytes: int = 0       # File size

# #     # ── SharePoint metadata (None for local files) ──
# #     article_title: Optional[str] = None
# #     category: Optional[str] = None
# #     sub_category: Optional[str] = None
# #     tags: List[str] = field(default_factory=list)
# #     summary: Optional[str] = None
# #     status: Optional[str] = None          # 'Published' / 'Draft' / 'Approved'
# #     author: Optional[str] = None
# #     publish_date: Optional[datetime] = None
# #     view_count: int = 0
# #     helpful_count: int = 0
# #     not_helpful_count: int = 0
# #     ai_citation_count: int = 0
# #     source_ticket_id: Optional[str] = None

# #     # ── For HTML files: SharePoint may store body in a column ──
# #     article_content: Optional[str] = None  # If set, skip file download

# #     def to_doc_metadata(self) -> Dict[str, Any]:
# #         """
# #         Return metadata fields to attach to each chunk of this document.
# #         Used by chunk_document().
# #         """
# #         return {
# #             "filename": self.filename,
# #             "doc_type": self.doc_type,
# #             "sharepoint_file_id": self.file_id,
# #             "article_title": self.article_title,
# #             "category": self.category or "Uncategorized",
# #             "sub_category": self.sub_category,
# #             "tags": self.tags,
# #             "summary": self.summary,
# #             "status": self.status,
# #             "author": self.author,
# #             "publish_date": (self.publish_date.isoformat() + "Z"
# #                              if self.publish_date else None),
# #             "view_count": self.view_count,
# #             "helpful_count": self.helpful_count,
# #             "not_helpful_count": self.not_helpful_count,
# #             "ai_citation_count": self.ai_citation_count,
# #             "source_ticket_id": self.source_ticket_id,
# #         }


# # @dataclass
# # class DocumentData:
# #     """A DocumentRef plus the actual file content (bytes) or extracted text."""
# #     ref: DocumentRef
# #     content_bytes: Optional[bytes] = None    # Raw file bytes (for binary parsers)
# #     pre_extracted_text: Optional[str] = None # If source provided text directly (HTML column)


# # @dataclass
# # class ChangeSet:
# #     """
# #     Result of a delta sync. Tells the pipeline what changed since last sync.
# #     """
# #     added: List[DocumentRef] = field(default_factory=list)
# #     updated: List[DocumentRef] = field(default_factory=list)
# #     deleted: List[str] = field(default_factory=list)  # list of file_ids
# #     new_delta_token: Optional[str] = None  # opaque cursor for next sync

# #     def is_empty(self) -> bool:
# #         return not (self.added or self.updated or self.deleted)

# #     def total_changes(self) -> int:
# #         return len(self.added) + len(self.updated) + len(self.deleted)


# # class DocumentSource(ABC):
# #     """
# #     Abstract base class for document sources.

# #     Implementations: LocalFolderSource, SharePointSource.
# #     """

# #     @abstractmethod
# #     def list_documents(self,
# #                        only_published: bool = True) -> List[DocumentRef]:
# #         """
# #         Return all documents the source currently exposes.

# #         Args:
# #             only_published: If True, return only Status=Published/Approved.
# #                             Ignored by sources that don't have a status column.

# #         Used for initial full indexing.
# #         """
# #         ...

# #     @abstractmethod
# #     def download(self, ref: DocumentRef) -> DocumentData:
# #         """
# #         Fetch the full file contents for a document.
# #         For SharePoint HTML with ArticleContent column, may return pre_extracted_text.
# #         """
# #         ...

# #     @abstractmethod
# #     def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
# #         """
# #         Return what changed since the previous sync.

# #         Args:
# #             delta_token: Opaque cursor from previous call. None = first call.

# #         Returns:
# #             ChangeSet with added/updated/deleted refs and a new_delta_token.

# #         For local folders, this compares file modified times.
# #         For SharePoint, uses Graph API delta query.
# #         """
# #         ...

# #     @abstractmethod
# #     def source_name(self) -> str:
# #         """Human-readable name, e.g. 'LocalFolder(./local_data)' or 'SharePoint(Helpdesk)'."""
# #         ...


# """
# sources/base.py — Abstract DocumentSource interface.

# Both LocalFolderSource and SharePointSource implement this contract.
# The rest of the bot uses these methods without knowing which source it's
# talking to. Swap source = change one env variable.

# Data model:
#     DocumentRef  : lightweight metadata (id, name, modified date, etc.)
#     DocumentData : the actual file bytes + parsed metadata
#     ChangeSet    : delta sync result (added/updated/deleted lists)
# """

# from abc import ABC, abstractmethod
# from dataclasses import dataclass, field
# from datetime import datetime
# from typing import List, Optional, Dict, Any


# @dataclass
# class DocumentRef:
#     """
#     Lightweight reference to a document. Just enough info to identify it
#     and decide whether to (re)download it.
#     """
#     file_id: str              # Unique ID — SharePoint file ID OR local file path
#     filename: str             # Display name with extension
#     doc_type: str             # 'pdf', 'docx', 'html'
#     modified_at: datetime     # Last modified timestamp
#     size_bytes: int = 0       # File size

#     # ── SharePoint metadata (None for local files) ──
#     article_title: Optional[str] = None
#     category: Optional[str] = None
#     sub_category: Optional[str] = None
#     tags: List[str] = field(default_factory=list)
#     summary: Optional[str] = None
#     status: Optional[str] = None          # 'Published' / 'Draft' / 'Approved'
#     author: Optional[str] = None
#     publish_date: Optional[datetime] = None
#     view_count: int = 0
#     helpful_count: int = 0
#     not_helpful_count: int = 0
#     ai_citation_count: int = 0
#     source_ticket_id: Optional[str] = None

#     # ── For HTML files: SharePoint may store body in a column ──
#     article_content: Optional[str] = None  # If set, skip file download

#     # ── Direct link to the file (for citations in answers) ──
#     pdf_url: Optional[str] = None

#     def to_doc_metadata(self) -> Dict[str, Any]:
#         """
#         Return metadata fields to attach to each chunk of this document.
#         Used by chunk_document().
#         """
#         return {
#             "filename": self.filename,
#             "doc_type": self.doc_type,
#             "sharepoint_file_id": self.file_id,
#             "article_title": self.article_title,
#             "category": self.category or "Uncategorized",
#             "sub_category": self.sub_category,
#             "tags": self.tags,
#             "summary": self.summary,
#             "status": self.status,
#             "author": self.author,
#             "publish_date": (self.publish_date.isoformat() + "Z"
#                              if self.publish_date else None),
#             "view_count": self.view_count,
#             "helpful_count": self.helpful_count,
#             "not_helpful_count": self.not_helpful_count,
#             "ai_citation_count": self.ai_citation_count,
#             "source_ticket_id": self.source_ticket_id,
#             "pdf_url": self.pdf_url,
#         }


# @dataclass
# class DocumentData:
#     """A DocumentRef plus the actual file content (bytes) or extracted text."""
#     ref: DocumentRef
#     content_bytes: Optional[bytes] = None    # Raw file bytes (for binary parsers)
#     pre_extracted_text: Optional[str] = None # If source provided text directly (HTML column)


# @dataclass
# class ChangeSet:
#     """
#     Result of a delta sync. Tells the pipeline what changed since last sync.
#     """
#     added: List[DocumentRef] = field(default_factory=list)
#     updated: List[DocumentRef] = field(default_factory=list)
#     deleted: List[str] = field(default_factory=list)  # list of file_ids
#     new_delta_token: Optional[str] = None  # opaque cursor for next sync

#     def is_empty(self) -> bool:
#         return not (self.added or self.updated or self.deleted)

#     def total_changes(self) -> int:
#         return len(self.added) + len(self.updated) + len(self.deleted)


# class DocumentSource(ABC):
#     """
#     Abstract base class for document sources.

#     Implementations: LocalFolderSource, SharePointSource.
#     """

#     @abstractmethod
#     def list_documents(self,
#                        only_published: bool = True) -> List[DocumentRef]:
#         """
#         Return all documents the source currently exposes.

#         Args:
#             only_published: If True, return only Status=Published/Approved.
#                             Ignored by sources that don't have a status column.

#         Used for initial full indexing.
#         """
#         ...

#     @abstractmethod
#     def download(self, ref: DocumentRef) -> DocumentData:
#         """
#         Fetch the full file contents for a document.
#         For SharePoint HTML with ArticleContent column, may return pre_extracted_text.
#         """
#         ...

#     @abstractmethod
#     def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
#         """
#         Return what changed since the previous sync.

#         Args:
#             delta_token: Opaque cursor from previous call. None = first call.

#         Returns:
#             ChangeSet with added/updated/deleted refs and a new_delta_token.

#         For local folders, this compares file modified times.
#         For SharePoint, uses Graph API delta query.
#         """
#         ...

#     @abstractmethod
#     def source_name(self) -> str:
#         """Human-readable name, e.g. 'LocalFolder(./local_data)' or 'SharePoint(Helpdesk)'."""
#         ...

"""
sources/base.py — Abstract DocumentSource interface.

Both LocalFolderSource and SharePointSource implement this contract.
The rest of the bot uses these methods without knowing which source it's
talking to. Swap source = change one env variable.

Data model:
    DocumentRef  : lightweight metadata (id, name, modified date, etc.)
    DocumentData : the actual file bytes + parsed metadata
    ChangeSet    : delta sync result (added/updated/deleted lists)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any


def _iso_for_search(dt) -> Optional[str]:
    """Format a datetime for Azure AI Search Edm.DateTimeOffset.
    Returns ISO 8601 with timezone (Z if UTC, +HH:MM otherwise). Never both."""
    if dt is None:
        return None
    s = dt.isoformat()
    # If naive (no tz), append Z. If aware, isoformat already has offset.
    if dt.tzinfo is None:
        s += "Z"
    return s


@dataclass
class DocumentRef:
    """
    Lightweight reference to a document. Just enough info to identify it
    and decide whether to (re)download it.
    """
    file_id: str              # Unique ID — SharePoint file ID OR local file path
    filename: str             # Display name with extension
    doc_type: str             # 'pdf', 'docx', 'html'
    modified_at: datetime     # Last modified timestamp
    size_bytes: int = 0       # File size

    # ── SharePoint metadata (None for local files) ──
    article_title: Optional[str] = None
    category: Optional[str] = None
    sub_category: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    summary: Optional[str] = None
    status: Optional[str] = None          # 'Published' / 'Draft' / 'Approved'
    author: Optional[str] = None
    publish_date: Optional[datetime] = None
    view_count: int = 0
    helpful_count: int = 0
    not_helpful_count: int = 0
    ai_citation_count: int = 0
    source_ticket_id: Optional[str] = None

    # ── For HTML files: SharePoint may store body in a column ──
    article_content: Optional[str] = None  # If set, skip file download

    # ── Direct link to the file (for citations in answers) ──
    pdf_url: Optional[str] = None

    def to_doc_metadata(self) -> Dict[str, Any]:
        """
        Return metadata fields to attach to each chunk of this document.
        Used by chunk_document().
        """
        return {
            "filename": self.filename,
            "doc_type": self.doc_type,
            "sharepoint_file_id": self.file_id,
            "article_title": self.article_title,
            "category": self.category or "Uncategorized",
            "sub_category": self.sub_category,
            "tags": self.tags,
            "summary": self.summary,
            "status": self.status,
            "author": self.author,
            "publish_date": _iso_for_search(self.publish_date),
            "view_count": self.view_count,
            "helpful_count": self.helpful_count,
            "not_helpful_count": self.not_helpful_count,
            "ai_citation_count": self.ai_citation_count,
            "source_ticket_id": self.source_ticket_id,
            "pdf_url": self.pdf_url,
        }


@dataclass
class DocumentData:
    """A DocumentRef plus the actual file content (bytes) or extracted text."""
    ref: DocumentRef
    content_bytes: Optional[bytes] = None    # Raw file bytes (for binary parsers)
    pre_extracted_text: Optional[str] = None # If source provided text directly (HTML column)


@dataclass
class ChangeSet:
    """
    Result of a delta sync. Tells the pipeline what changed since last sync.
    """
    added: List[DocumentRef] = field(default_factory=list)
    updated: List[DocumentRef] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)  # list of file_ids
    new_delta_token: Optional[str] = None  # opaque cursor for next sync

    def is_empty(self) -> bool:
        return not (self.added or self.updated or self.deleted)

    def total_changes(self) -> int:
        return len(self.added) + len(self.updated) + len(self.deleted)


class DocumentSource(ABC):
    """
    Abstract base class for document sources.

    Implementations: LocalFolderSource, SharePointSource.
    """

    @abstractmethod
    def list_documents(self,
                       only_published: bool = True) -> List[DocumentRef]:
        """
        Return all documents the source currently exposes.

        Args:
            only_published: If True, return only Status=Published/Approved.
                            Ignored by sources that don't have a status column.

        Used for initial full indexing.
        """
        ...

    @abstractmethod
    def download(self, ref: DocumentRef) -> DocumentData:
        """
        Fetch the full file contents for a document.
        For SharePoint HTML with ArticleContent column, may return pre_extracted_text.
        """
        ...

    @abstractmethod
    def get_changes(self, delta_token: Optional[str] = None) -> ChangeSet:
        """
        Return what changed since the previous sync.

        Args:
            delta_token: Opaque cursor from previous call. None = first call.

        Returns:
            ChangeSet with added/updated/deleted refs and a new_delta_token.

        For local folders, this compares file modified times.
        For SharePoint, uses Graph API delta query.
        """
        ...

    @abstractmethod
    def source_name(self) -> str:
        """Human-readable name, e.g. 'LocalFolder(./local_data)' or 'SharePoint(Helpdesk)'."""
        ...