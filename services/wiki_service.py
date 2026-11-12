"""
Wiki Service — Azure DevOps wiki reader for the chat_agent.

Design goals
------------
* **Zero impact when disabled.** If any env var is missing or the
  service fails to initialise, ``is_enabled`` returns ``False`` and the
  chat_agent skips every wiki code path. Existing chat performance and
  behaviour are preserved exactly.
* **Project-generic.** Nothing is hardcoded for any specific project.
  Page names are fetched live via ADO REST; wiki id auto-detected if not
  supplied.
* **Async, non-blocking.** All HTTP calls use ``httpx.AsyncClient`` so
  the Chainlit event loop is never stalled.
* **In-memory cache with TTL.** Repeat questions are cheap.
  ``WIKI_CACHE_TTL_SECONDS`` (default 30 min) controls freshness.
* **Fail-safe.** Any HTTP/parse error is logged and returns ``None`` or
  empty; the chat_agent treats missing wiki data as "no wiki available"
  and falls back to its normal behaviour.

Public surface
--------------
* :func:`get_wiki_service` — singleton accessor
* :meth:`WikiService.is_enabled` — cheap boolean, safe to call every turn
* :meth:`WikiService.get_toc` -> ``str``  (markdown-formatted page list)
* :meth:`WikiService.get_page` -> ``str | None``  (page markdown content)

No prompts, no LLM logic — that all lives in ``agents/chat_agent.py``
and ``prompts/chat_prompts.yaml``.
"""

from __future__ import annotations

import asyncio
import base64
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

import config
from utils.logger import get_logger

logger = get_logger("wiki_service")


# ---------------------------------------------------------------------------
# Internal cache entry
# ---------------------------------------------------------------------------
@dataclass
class _CacheEntry:
    value: str
    expires_at: float  # unix ts

    def is_valid(self) -> bool:
        return time.time() < self.expires_at


# ---------------------------------------------------------------------------
# Wiki Service
# ---------------------------------------------------------------------------
class WikiService:
    """Reads Azure DevOps wiki pages. Singleton — use :func:`get_wiki_service`."""

    _API_VERSION = "7.1"
    _HTTP_TIMEOUT = 8.0  # seconds — keep small so a slow ADO can't stall chat

    def __init__(self) -> None:
        # Snapshot config once at import time. If env changes, restart.
        # Normalize ORG to a full URL. Accepts either:
        #   AZURE_DEVOPS_ORG=https://dev.azure.com/YourOrg
        #   AZURE_DEVOPS_ORG=YourOrg
        raw_org = (config.AZURE_DEVOPS_ORG or "").strip().rstrip("/")
        if raw_org and not raw_org.lower().startswith(("http://", "https://")):
            raw_org = f"https://dev.azure.com/{raw_org}"
        self._org: str = raw_org
        self._project: str = (config.AZURE_DEVOPS_PROJECT or "").strip()
        self._pat: str = (config.AZURE_DEVOPS_PAT or "").strip()
        self._wiki_id_hint: str = (config.AZURE_DEVOPS_WIKI_ID or "").strip()
        self._ttl: int = max(0, int(config.WIKI_CACHE_TTL_SECONDS or 0))

        # Resolved lazily on first use.
        self._resolved_wiki_id: Optional[str] = None
        self._resolve_lock = asyncio.Lock()

        # {kind:path -> _CacheEntry}   kind ∈ {"toc","page"}
        self._cache: Dict[str, _CacheEntry] = {}
        # {kind:path -> asyncio.Lock}  prevents thundering-herd on cold miss
        self._locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

        # Compute enabled flag ONCE. Cheap to check every chat turn.
        self._enabled: bool = self._compute_enabled()

        # Assistant persona label (auto-derived if not supplied).
        cfg_name = (config.WIKI_ASSISTANT_NAME or "").strip()
        self.assistant_name: str = cfg_name or (self._project or "Project") + " Wiki Assistant"

        logger.info(
            f"WikiService init: enabled={self._enabled} "
            f"project='{self._project}' "
            f"wiki_id_hint='{self._wiki_id_hint or '(auto-detect)'}' "
            f"cache_ttl={self._ttl}s "
            f"assistant='{self.assistant_name}'"
        )

    # ------------------------------------------------------------------
    # Feature flag
    # ------------------------------------------------------------------
    def _compute_enabled(self) -> bool:
        if self._wiki_id_hint.upper() == "DISABLED":
            return False
        if not self._org or not self._project or not self._pat:
            return False
        return True

    @property
    def is_enabled(self) -> bool:
        """Fast, side-effect-free check. Safe to call on every chat turn."""
        return self._enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_toc(self) -> str:
        """Return the wiki table of contents as a compact markdown list.

        Returns empty string if the wiki is disabled or the fetch fails.
        Cached for ``WIKI_CACHE_TTL_SECONDS``.
        """
        if not self._enabled:
            return ""
        return await self._cached_fetch("toc", "__root__", self._fetch_toc_impl)

    async def get_page(self, path: str) -> Optional[str]:
        """Return the markdown content of a wiki page by path (e.g.
        ``"Validation/Workstation Setup"``).

        Returns ``None`` if the wiki is disabled or the fetch fails.
        Cached for ``WIKI_CACHE_TTL_SECONDS``.
        """
        if not self._enabled:
            return None
        path = (path or "").strip().lstrip("/")
        if not path:
            return None
        result = await self._cached_fetch(
            "page", path, lambda: self._fetch_page_impl(path)
        )
        return result or None

    # ------------------------------------------------------------------
    # Cache wrapper (single-flight + TTL)
    # ------------------------------------------------------------------
    async def _cached_fetch(self, kind: str, key: str, fetcher) -> str:
        cache_key = f"{kind}:{key}"

        # Fast path — valid cache hit
        entry = self._cache.get(cache_key)
        if entry and entry.is_valid():
            return entry.value

        # Serialise concurrent misses for the same key
        async with self._locks_guard:
            lock = self._locks.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[cache_key] = lock

        async with lock:
            # Re-check under lock (another coroutine may have populated it)
            entry = self._cache.get(cache_key)
            if entry and entry.is_valid():
                return entry.value

            try:
                value = await fetcher() or ""
            except Exception as exc:
                logger.warning(f"Wiki fetch failed ({cache_key}): {exc}")
                value = ""

            # Only cache non-empty results so a transient failure doesn't
            # poison the cache for 30 minutes.
            if value and self._ttl > 0:
                self._cache[cache_key] = _CacheEntry(
                    value=value, expires_at=time.time() + self._ttl
                )
            return value

    # ------------------------------------------------------------------
    # ADO REST helpers
    # ------------------------------------------------------------------
    def _auth_headers(self) -> Dict[str, str]:
        token = base64.b64encode(f":{self._pat}".encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }

    def _base_url(self) -> str:
        # ADO wiki REST base:  https://dev.azure.com/{org}/{project}/_apis/wiki
        # Full endpoints append '/wikis', '/wikis/{id}/pages', etc.
        return f"{self._org}/{self._project}/_apis/wiki"

    async def _resolve_wiki_id(self, client: httpx.AsyncClient) -> Optional[str]:
        """Resolve the wiki id to use for subsequent calls.

        Uses ``AZURE_DEVOPS_WIKI_ID`` if provided; otherwise fetches
        ``GET /wikis`` and picks the first ProjectWiki (most projects
        have exactly one). Result is memoised for the process lifetime.
        """
        if self._resolved_wiki_id:
            return self._resolved_wiki_id

        async with self._resolve_lock:
            if self._resolved_wiki_id:
                return self._resolved_wiki_id

            if self._wiki_id_hint:
                self._resolved_wiki_id = self._wiki_id_hint
                logger.info(f"Wiki id (from env): {self._resolved_wiki_id}")
                return self._resolved_wiki_id

            url = f"{self._base_url()}/wikis?api-version={self._API_VERSION}"
            r = await client.get(url, headers=self._auth_headers())
            r.raise_for_status()
            wikis: List[Dict] = (r.json() or {}).get("value") or []
            if not wikis:
                logger.warning("No wikis found for project — feature stays enabled but will return empty results")
                return None

            # Prefer ProjectWiki type, fall back to first entry
            chosen = next(
                (w for w in wikis if (w.get("type") or "").lower() == "projectwiki"),
                wikis[0],
            )
            self._resolved_wiki_id = chosen.get("id") or chosen.get("name")
            logger.info(
                f"Wiki id (auto-detected): {self._resolved_wiki_id} "
                f"(name='{chosen.get('name')}', type='{chosen.get('type')}')"
            )
            return self._resolved_wiki_id

    async def _fetch_toc_impl(self) -> str:
        """Fetch the full page tree and format as a compact bullet list."""
        async with httpx.AsyncClient(timeout=self._HTTP_TIMEOUT) as client:
            wiki_id = await self._resolve_wiki_id(client)
            if not wiki_id:
                return ""

            # recursionLevel=full returns the whole tree in one call.
            url = (
                f"{self._base_url()}/wikis/{wiki_id}/pages"
                f"?path=/&recursionLevel=full&api-version={self._API_VERSION}"
            )
            r = await client.get(url, headers=self._auth_headers())
            r.raise_for_status()
            root = r.json() or {}

            lines: List[str] = []
            self._flatten_pages(root, lines, depth=0)
            # Drop the artificial root entry (path "/") if present at the top.
            filtered = [ln for ln in lines if ln.strip() not in ("- /", "-")]
            return "\n".join(filtered).strip()

    def _flatten_pages(self, node: Dict, out: List[str], depth: int) -> None:
        path = (node.get("path") or "").strip()
        if path and path != "/":
            # Show the leaf name with light indentation so the LLM sees
            # the hierarchy but the token cost stays modest.
            indent = "  " * min(depth - 1, 4)
            leaf = path.rsplit("/", 1)[-1]
            # Include the full path in backticks so the LLM has the
            # canonical string to hand back in FETCH:<path>.
            out.append(f"{indent}- {leaf}  `{path}`")

        for child in (node.get("subPages") or []):
            self._flatten_pages(child, out, depth + 1)

    async def _fetch_page_impl(self, path: str) -> str:
        """Fetch a single page's markdown content."""
        async with httpx.AsyncClient(timeout=self._HTTP_TIMEOUT) as client:
            wiki_id = await self._resolve_wiki_id(client)
            if not wiki_id:
                return ""

            # URL-encode the page path so spaces / slashes / unicode survive.
            encoded_path = urllib.parse.quote("/" + path.lstrip("/"), safe="/")
            url = (
                f"{self._base_url()}/wikis/{wiki_id}/pages"
                f"?path={encoded_path}"
                f"&includeContent=true&api-version={self._API_VERSION}"
            )
            r = await client.get(url, headers=self._auth_headers())
            if r.status_code == 404:
                logger.info(f"Wiki page not found: {path}")
                return ""
            r.raise_for_status()
            data = r.json() or {}
            return (data.get("content") or "").strip()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_wiki_service: Optional[WikiService] = None


def get_wiki_service() -> WikiService:
    global _wiki_service
    if _wiki_service is None:
        _wiki_service = WikiService()
    return _wiki_service
