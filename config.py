# """
# config.py — Centralized configuration loader.

# Every other module imports `settings` from here.
# Do NOT scatter os.getenv() calls around the codebase.

# Loads from .env in development, from environment variables in Azure App Service.
# Validates required values at startup so missing config fails loudly, not silently.
# """

# import os
# import sys
# from pathlib import Path
# from dataclasses import dataclass, field
# from typing import Literal, Optional
# from dotenv import load_dotenv

# # Load .env file from project root (works for both local dev and Docker)
# ENV_PATH = Path(__file__).parent / ".env"
# if ENV_PATH.exists():
#     load_dotenv(ENV_PATH)
# else:
#     # In Azure App Service, env vars are set directly — no .env file needed
#     load_dotenv()


# def _get(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
#     """Get an env variable. Raise clear error if required and missing."""
#     val = os.getenv(name, default)
#     if required and (val is None or val.strip() == "" or val.startswith("<")):
#         raise RuntimeError(
#             f"\n❌ Missing required environment variable: {name}\n"
#             f"   Set it in your .env file or Azure App Service settings.\n"
#             f"   See .env.example for guidance.\n"
#         )
#     return val


# def _get_int(name: str, default: int) -> int:
#     val = os.getenv(name)
#     if val is None or val.strip() == "":
#         return default
#     try:
#         return int(val)
#     except ValueError:
#         raise RuntimeError(f"Environment variable {name}={val} must be an integer")


# @dataclass(frozen=True)
# class Settings:
#     # ── Source mode ──
#     source_type: Literal["local", "sharepoint"]
#     local_data_folder: str

#     # ── SharePoint (Microsoft Graph) ──
#     tenant_id: Optional[str]
#     client_id: Optional[str]
#     client_secret: Optional[str]
#     sharepoint_site_url: Optional[str]
#     sharepoint_library: Optional[str]

#     # ── Azure OpenAI: Embedding ──
#     embed_endpoint: str
#     embed_api_key: str
#     embed_deployment: str
#     embed_api_ver: str

#     # ── Azure OpenAI: Chat models ──
#     gpt_endpoint: str
#     gpt_api_key: str
#     gpt_mini_deploy: str
#     gpt_large_deploy: str
#     gpt_api_ver: str

#     # ── Azure AI Search ──
#     search_endpoint: str
#     search_api_key: str
#     search_index: str

#     # ── API auth ──
#     api_key: str

#     # ── RAG tuning ──
#     chunk_size: int
#     chunk_overlap: int
#     top_k_retrieve: int
#     top_k_use: int
#     cache_ttl_hours: int
#     sync_interval_minutes: int

#     # ── Storage paths ──
#     data_dir: str

#     # ── Logging ──
#     log_level: str

#     @property
#     def embed_dim(self) -> int:
#         """Vector dimension. text-embedding-3-small = 1536, -large = 3072."""
#         return 3072 if "large" in self.embed_deployment else 1536

#     @property
#     def is_sharepoint_mode(self) -> bool:
#         return self.source_type == "sharepoint"

#     @property
#     def is_local_mode(self) -> bool:
#         return self.source_type == "local"

#     def validate(self) -> list[str]:
#         """Return list of issues. Empty list = all good."""
#         issues = []

#         if self.is_sharepoint_mode:
#             if not self.tenant_id or self.tenant_id.startswith("<"):
#                 issues.append("SOURCE_TYPE=sharepoint but TENANT_ID is missing")
#             if not self.client_id or self.client_id.startswith("<"):
#                 issues.append("SOURCE_TYPE=sharepoint but CLIENT_ID is missing")
#             if not self.client_secret or self.client_secret.startswith("<"):
#                 issues.append("SOURCE_TYPE=sharepoint but CLIENT_SECRET is missing")
#             if not self.sharepoint_site_url:
#                 issues.append("SOURCE_TYPE=sharepoint but SHAREPOINT_SITE_URL is missing")

#         if self.api_key.startswith("change-me"):
#             issues.append("API_KEY is still the default — generate a strong random string")

#         if self.embed_api_key.startswith("<"):
#             issues.append("EMBED_API_KEY not set — fill in from Azure Portal")
#         if self.gpt_api_key.startswith("<"):
#             issues.append("GPT_API_KEY not set — fill in from Azure Portal")
#         if self.search_api_key.startswith("<"):
#             issues.append("SEARCH_API_KEY not set — create Azure AI Search service first")

#         return issues


# def load_settings() -> Settings:
#     """Load and return validated settings. Call once at app startup."""
#     s = Settings(
#         # Source
#         source_type=_get("SOURCE_TYPE", "local"),  # type: ignore
#         local_data_folder=_get("LOCAL_DATA_FOLDER", "./local_data"),  # type: ignore

#         # SharePoint (optional when SOURCE_TYPE=local)
#         tenant_id=_get("TENANT_ID"),
#         client_id=_get("CLIENT_ID"),
#         client_secret=_get("CLIENT_SECRET"),
#         sharepoint_site_url=_get("SHAREPOINT_SITE_URL"),
#         sharepoint_library=_get("SHAREPOINT_LIBRARY", "HD_KnowledgeDocuments"),

#         # Azure OpenAI: Embedding (always required)
#         embed_endpoint=_get("EMBED_ENDPOINT", required=True),  # type: ignore
#         embed_api_key=_get("EMBED_API_KEY", required=True),  # type: ignore
#         embed_deployment=_get("EMBED_DEPLOYMENT", "text-embedding-3-small"),  # type: ignore
#         embed_api_ver=_get("EMBED_API_VER", "2024-12-01-preview"),  # type: ignore

#         # Azure OpenAI: Chat (always required)
#         gpt_endpoint=_get("GPT_ENDPOINT", required=True),  # type: ignore
#         gpt_api_key=_get("GPT_API_KEY", required=True),  # type: ignore
#         gpt_mini_deploy=_get("GPT_MINI_DEPLOY", "gpt-4o-mini"),  # type: ignore
#         gpt_large_deploy=_get("GPT_LARGE_DEPLOY", "gpt-4o"),  # type: ignore
#         gpt_api_ver=_get("GPT_API_VER", "2024-12-01-preview"),  # type: ignore

#         # Azure AI Search (always required)
#         search_endpoint=_get("SEARCH_ENDPOINT", required=True),  # type: ignore
#         search_api_key=_get("SEARCH_API_KEY", required=True),  # type: ignore
#         search_index=_get("SEARCH_INDEX", "veelead-docs"),  # type: ignore

#         # API auth
#         api_key=_get("API_KEY", "change-me-generate-a-random-string"),  # type: ignore

#         # Tuning
#         chunk_size=_get_int("CHUNK_SIZE", 400),
#         chunk_overlap=_get_int("CHUNK_OVERLAP", 60),
#         top_k_retrieve=_get_int("TOP_K_RETRIEVE", 10),
#         top_k_use=_get_int("TOP_K_USE", 4),
#         cache_ttl_hours=_get_int("CACHE_TTL_HOURS", 168),
#         sync_interval_minutes=_get_int("SYNC_INTERVAL_MINUTES", 60),

#         # Storage
#         data_dir=_get("DATA_DIR", "./data"),  # type: ignore

#         # Logging
#         log_level=_get("LOG_LEVEL", "INFO"),  # type: ignore
#     )
#     return s


# # Singleton — import this everywhere
# settings: Settings = load_settings()


# def print_config_summary():
#     """Print a non-sensitive summary of current config. Useful at startup."""
#     print("\n" + "═" * 60)
#     print("  Veelead Helpdesk RAG Bot — Configuration")
#     print("═" * 60)
#     print(f"  Source mode:        {settings.source_type}")
#     if settings.is_local_mode:
#         print(f"  Local data folder:  {settings.local_data_folder}")
#     else:
#         print(f"  SharePoint site:    {settings.sharepoint_site_url}")
#         print(f"  SharePoint library: {settings.sharepoint_library}")
#         print(f"  Tenant ID:          {settings.tenant_id}")
#         print(f"  Client ID:          {(settings.client_id or '')[:12]}...")
#     print(f"  Embedding model:    {settings.embed_deployment} ({settings.embed_dim} dim)")
#     print(f"  Chat models:        {settings.gpt_mini_deploy} (default), {settings.gpt_large_deploy} (complex)")
#     print(f"  Search service:     {settings.search_endpoint}")
#     print(f"  Search index:       {settings.search_index}")
#     print(f"  Chunk size/overlap: {settings.chunk_size}/{settings.chunk_overlap} words")
#     print(f"  Top-K retrieve/use: {settings.top_k_retrieve}/{settings.top_k_use}")
#     print(f"  Cache TTL:          {settings.cache_ttl_hours} hours")
#     print(f"  Sync interval:      {settings.sync_interval_minutes} minutes")
#     print(f"  Data directory:     {settings.data_dir}")
#     print(f"  Log level:          {settings.log_level}")
#     print("═" * 60)

#     issues = settings.validate()
#     if issues:
#         print("\n  ⚠  Configuration issues:")
#         for issue in issues:
#             print(f"     - {issue}")
#         print()
#     else:
#         print("  ✅ Configuration valid\n")


# # Allow `python config.py` to test the config
# if __name__ == "__main__":
#     print_config_summary()
#     sys.exit(1 if settings.validate() else 0)
"""
config.py — Centralized configuration loader.

Every other module imports `settings` from here.
Do NOT scatter os.getenv() calls around the codebase.

Loads from .env in development, from environment variables in Azure App Service.
Validates required values at startup so missing config fails loudly, not silently.
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal, Optional
from dotenv import load_dotenv

# Load .env file from project root (works for both local dev and Docker)
ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    # In Azure App Service, env vars are set directly — no .env file needed
    load_dotenv()


def _get(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Get an env variable. Raise clear error if required and missing."""
    val = os.getenv(name, default)
    if required and (val is None or val.strip() == "" or val.startswith("<")):
        raise RuntimeError(
            f"\n❌ Missing required environment variable: {name}\n"
            f"   Set it in your .env file or Azure App Service settings.\n"
            f"   See .env.example for guidance.\n"
        )
    return val


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        raise RuntimeError(f"Environment variable {name}={val} must be an integer")


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        raise RuntimeError(f"Environment variable {name}={val} must be a number")


@dataclass(frozen=True)
class Settings:
    # ── Source mode ──
    source_type: Literal["local", "sharepoint"]
    local_data_folder: str

    # ── SharePoint (Microsoft Graph) ──
    tenant_id: Optional[str]
    client_id: Optional[str]
    client_secret: Optional[str]
    sharepoint_site_url: Optional[str]
    sharepoint_library: Optional[str]

    # ── Azure OpenAI: Embedding ──
    embed_endpoint: str
    embed_api_key: str
    embed_deployment: str
    embed_api_ver: str

    # ── Azure OpenAI: Chat models ──
    gpt_endpoint: str
    gpt_api_key: str
    gpt_mini_deploy: str
    gpt_large_deploy: str
    gpt_api_ver: str

    # ── Azure AI Search ──
    search_endpoint: str
    search_api_key: str
    search_index: str

    # ── API auth ──
    api_key: str

    # ── RAG tuning ──
    chunk_size: int
    chunk_overlap: int
    top_k_retrieve: int
    top_k_use: int
    cache_ttl_hours: int
    sync_interval_minutes: int

    # Semantic cache threshold — minimum cosine similarity (0.0-1.0) for a
    # cached entry to be considered a match. Higher = stricter. 0.92 is the
    # recommended default; below 0.88 carries real risk of wrong answers.
    cache_semantic_threshold: float

    # ── Storage paths ──
    data_dir: str

    # ── Logging ──
    log_level: str

    @property
    def embed_dim(self) -> int:
        """Vector dimension. text-embedding-3-small = 1536, -large = 3072."""
        return 3072 if "large" in self.embed_deployment else 1536

    @property
    def is_sharepoint_mode(self) -> bool:
        return self.source_type == "sharepoint"

    @property
    def is_local_mode(self) -> bool:
        return self.source_type == "local"

    def validate(self) -> list[str]:
        """Return list of issues. Empty list = all good."""
        issues = []

        if self.is_sharepoint_mode:
            if not self.tenant_id or self.tenant_id.startswith("<"):
                issues.append("SOURCE_TYPE=sharepoint but TENANT_ID is missing")
            if not self.client_id or self.client_id.startswith("<"):
                issues.append("SOURCE_TYPE=sharepoint but CLIENT_ID is missing")
            if not self.client_secret or self.client_secret.startswith("<"):
                issues.append("SOURCE_TYPE=sharepoint but CLIENT_SECRET is missing")
            if not self.sharepoint_site_url:
                issues.append("SOURCE_TYPE=sharepoint but SHAREPOINT_SITE_URL is missing")

        if self.api_key.startswith("change-me"):
            issues.append("API_KEY is still the default — generate a strong random string")

        if self.embed_api_key.startswith("<"):
            issues.append("EMBED_API_KEY not set — fill in from Azure Portal")
        if self.gpt_api_key.startswith("<"):
            issues.append("GPT_API_KEY not set — fill in from Azure Portal")
        if self.search_api_key.startswith("<"):
            issues.append("SEARCH_API_KEY not set — create Azure AI Search service first")

        return issues


def load_settings() -> Settings:
    """Load and return validated settings. Call once at app startup."""
    s = Settings(
        # Source
        source_type=_get("SOURCE_TYPE", "local"),  # type: ignore
        local_data_folder=_get("LOCAL_DATA_FOLDER", "./local_data"),  # type: ignore

        # SharePoint (optional when SOURCE_TYPE=local)
        tenant_id=_get("TENANT_ID"),
        client_id=_get("CLIENT_ID"),
        client_secret=_get("CLIENT_SECRET"),
        sharepoint_site_url=_get("SHAREPOINT_SITE_URL"),
        sharepoint_library=_get("SHAREPOINT_LIBRARY", "HD_KnowledgeDocuments"),

        # Azure OpenAI: Embedding (always required)
        embed_endpoint=_get("EMBED_ENDPOINT", required=True),  # type: ignore
        embed_api_key=_get("EMBED_API_KEY", required=True),  # type: ignore
        embed_deployment=_get("EMBED_DEPLOYMENT", "text-embedding-3-small"),  # type: ignore
        embed_api_ver=_get("EMBED_API_VER", "2024-12-01-preview"),  # type: ignore

        # Azure OpenAI: Chat (always required)
        gpt_endpoint=_get("GPT_ENDPOINT", required=True),  # type: ignore
        gpt_api_key=_get("GPT_API_KEY", required=True),  # type: ignore
        gpt_mini_deploy=_get("GPT_MINI_DEPLOY", "gpt-4o-mini"),  # type: ignore
        gpt_large_deploy=_get("GPT_LARGE_DEPLOY", "gpt-4o"),  # type: ignore
        gpt_api_ver=_get("GPT_API_VER", "2024-12-01-preview"),  # type: ignore

        # Azure AI Search (always required)
        search_endpoint=_get("SEARCH_ENDPOINT", required=True),  # type: ignore
        search_api_key=_get("SEARCH_API_KEY", required=True),  # type: ignore
        search_index=_get("SEARCH_INDEX", "veelead-docs"),  # type: ignore

        # API auth
        api_key=_get("API_KEY", "change-me-generate-a-random-string"),  # type: ignore

        # Tuning
        chunk_size=_get_int("CHUNK_SIZE", 400),
        chunk_overlap=_get_int("CHUNK_OVERLAP", 60),
        top_k_retrieve=_get_int("TOP_K_RETRIEVE", 10),
        top_k_use=_get_int("TOP_K_USE", 4),
        cache_ttl_hours=_get_int("CACHE_TTL_HOURS", 168),
        cache_semantic_threshold=_get_float("CACHE_SEMANTIC_THRESHOLD", 0.92),
        sync_interval_minutes=_get_int("SYNC_INTERVAL_MINUTES", 60),

        # Storage
        data_dir=_get("DATA_DIR", "./data"),  # type: ignore

        # Logging
        log_level=_get("LOG_LEVEL", "INFO"),  # type: ignore
    )
    return s


# Singleton — import this everywhere
settings: Settings = load_settings()


def print_config_summary():
    """Print a non-sensitive summary of current config. Useful at startup."""
    print("\n" + "═" * 60)
    print("  Veelead Helpdesk RAG Bot — Configuration")
    print("═" * 60)
    print(f"  Source mode:        {settings.source_type}")
    if settings.is_local_mode:
        print(f"  Local data folder:  {settings.local_data_folder}")
    else:
        print(f"  SharePoint site:    {settings.sharepoint_site_url}")
        print(f"  SharePoint library: {settings.sharepoint_library}")
        print(f"  Tenant ID:          {settings.tenant_id}")
        print(f"  Client ID:          {(settings.client_id or '')[:12]}...")
    print(f"  Embedding model:    {settings.embed_deployment} ({settings.embed_dim} dim)")
    print(f"  Chat models:        {settings.gpt_mini_deploy} (default), {settings.gpt_large_deploy} (complex)")
    print(f"  Search service:     {settings.search_endpoint}")
    print(f"  Search index:       {settings.search_index}")
    print(f"  Chunk size/overlap: {settings.chunk_size}/{settings.chunk_overlap} words")
    print(f"  Top-K retrieve/use: {settings.top_k_retrieve}/{settings.top_k_use}")
    print(f"  Cache TTL:          {settings.cache_ttl_hours} hours")
    print(f"  Semantic threshold: {settings.cache_semantic_threshold:.2f} (cache match minimum)")
    print(f"  Sync interval:      {settings.sync_interval_minutes} minutes")
    print(f"  Data directory:     {settings.data_dir}")
    print(f"  Log level:          {settings.log_level}")
    print("═" * 60)

    issues = settings.validate()
    if issues:
        print("\n  ⚠  Configuration issues:")
        for issue in issues:
            print(f"     - {issue}")
        print()
    else:
        print("  ✅ Configuration valid\n")


# Allow `python config.py` to test the config
if __name__ == "__main__":
    print_config_summary()
    sys.exit(1 if settings.validate() else 0)