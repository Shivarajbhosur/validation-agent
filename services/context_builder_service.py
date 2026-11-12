"""
Context Builder Service

Gathers code/knowledge context for the chat agent: COBOL main + sub-programs
(via CALL parsing), target-language source / record files, similar past fixes
from the knowledge base, and any user-supplied snippet/diff.

This service is additive — it only consumes existing services
(``CobolService``, ``CodebaseService``, ``KnowledgeBaseService``) and never
mutates them. All failures are downgraded to "no result" so the chat layer
can still respond.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from services.cobol_service import get_cobol_service
from services.codebase_service import get_codebase_service
from services.knowledge_base_service import get_kb_service
from utils.logger import get_logger

import config

logger = get_logger("context_builder_service")


# Match COBOL CALL statements: CALL "HPPL050P" or CALL 'HPPL050P'.
_CALL_PATTERN = re.compile(r"""CALL\s+["']([A-Z][A-Z0-9]{2,9})["']""", re.IGNORECASE)

# Match a subroutine-style field declaration in a translated program:
#     private [static|readonly|new] <Type> <name> = new <Type>(...);
# Captures the type name. Used to enumerate external subroutines a
# main program depends on — the type-name → filesystem check confirms
# whether the type lives under ``SSAB.OX.Batch.Subroutines``.
_SUBROUTINE_FIELD_PATTERN = re.compile(
    r"private\s+(?:static\s+|readonly\s+|new\s+)*"
    r"([A-Z][A-Za-z0-9_]*)\s+"
    r"[A-Za-z_][A-Za-z0-9_]*\s*=\s*"
    r"new\s+\1\s*\(",
    re.MULTILINE,
)

# Convention (from the SSAB.OX repo): translated COBOL subroutines live in
# a folder named ``SSAB.OX.Batch.Subroutines``. Any C# class whose file
# sits under that folder is authoritatively a subroutine dependency.
_SUBROUTINES_FOLDER_MARKER = "ssab.ox.batch.subroutines"

# Target-language file extensions we will surface as "main" / "record" files.
# Kept in sync with the supported extensions in `_fetch_target`.
_TARGET_EXTS = (".cs", ".java", ".py")

# Folders skipped when scanning the SSAB.OX repo for direct lookups.
# Mirrors `CodebaseService.EXCLUDE_FOLDERS` so the fallback behaves the same
# as the ingest pipeline and does not pick up build artefacts.
_REPO_EXCLUDE_FOLDERS = {
    "bin", "obj", "packages", ".git", ".vs",
    "node_modules", "TestResults", "Debug", "Release",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ProgramArtifact:
    """A single COBOL or target-language file resolved from KB or live disk."""

    program_name: str = ""
    file_path: str = ""
    code: str = ""

    def has_code(self) -> bool:
        return bool(self.code and self.code.strip())


@dataclass
class SimilarFixArtifact:
    pr_id: int = 0
    baseline: str = ""
    title: str = ""
    files_changed: List[str] = field(default_factory=list)
    code_diff: str = ""
    score: float = 0.0


@dataclass
class IssueArtifact:
    """A curated bug pattern from the ``issues`` collection."""

    issue_id: str = ""
    title: str = ""
    bug_category: str = ""
    tags: str = ""
    program: str = ""
    root_cause: str = ""
    buggy_code: str = ""
    fixed_code: str = ""
    distance: float = 0.0


@dataclass
class ContextBundle:
    """All context gathered for one chat turn."""

    program_name: Optional[str] = None
    baseline_name: Optional[str] = None
    cobol_main: ProgramArtifact = field(default_factory=ProgramArtifact)
    cobol_subprograms: List[ProgramArtifact] = field(default_factory=list)
    target_main: ProgramArtifact = field(default_factory=ProgramArtifact)
    target_record: ProgramArtifact = field(default_factory=ProgramArtifact)
    similar_fixes: List[SimilarFixArtifact] = field(default_factory=list)
    curated_issues: List[IssueArtifact] = field(default_factory=list)
    # Shallow bundles for external subroutines called by ``target_main``
    # (detected from ``SSAB.OX.Batch.Subroutines`` field declarations) or
    # named explicitly in the user's message. Each has its own COBOL,
    # target C#, and curated-issue matches — the chat prompt renders them
    # alongside the main program so the LLM analyses all of them in one turn.
    subroutine_bundles: List["ContextBundle"] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.cobol_main.has_code()
            or self.target_main.has_code()
            or self.similar_fixes
            or self.curated_issues
            or self.subroutine_bundles
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ContextBuilderService:
    """Lazy, singleton context builder used by the chat agent."""

    def __init__(self) -> None:
        self._cobol = None
        self._codebase = None
        self._kb = None
        # Cached program_id (upper-case, no extension) -> list of repo paths.
        # Built on first miss, refreshed when the repo mtime changes so newly
        # added / renamed files are picked up without restarting the app.
        self._repo_index: Optional[Dict[str, List[str]]] = None
        self._repo_index_mtime: float = 0.0
        # Cached lowercase class names of every ``.cs`` file that lives
        # under an ``SSAB.OX.Batch.Subroutines`` folder in the repo. Used
        # by :meth:`_detect_called_subroutines` to filter field-declared
        # types down to actual subroutine dependencies.
        self._subroutine_class_index: Optional[Set[str]] = None
        self._subroutine_class_index_mtime: float = 0.0

    # Lazy properties so tests / partial deployments don't need every backend.
    @property
    def cobol(self):
        if self._cobol is None:
            self._cobol = get_cobol_service()
        return self._cobol

    @property
    def codebase(self):
        if self._codebase is None:
            self._codebase = get_codebase_service()
        return self._codebase

    @property
    def kb(self):
        if self._kb is None:
            self._kb = get_kb_service()
        return self._kb

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def build(
        self,
        program_name: Optional[str],
        baseline_name: Optional[str],
        query: str,
        limits: Dict[str, int],
        extra_program_names: Optional[List[str]] = None,
    ) -> ContextBundle:
        """Build the full context bundle for one chat turn (best-effort).

        ``extra_program_names`` — programs the user explicitly named
        alongside ``program_name`` (e.g. "any issues in Tablasn and
        Hgsr004p?"). These are treated as sibling subroutines so the
        LLM analyses them in the same turn as the primary program.
        """
        bundle = ContextBundle(
            program_name=program_name,
            baseline_name=baseline_name,
        )

        if program_name:
            try:
                bundle.cobol_main = self._fetch_cobol(
                    program_name,
                    int(limits.get("cobol_main_chars", 6000)),
                )
            except Exception as exc:
                logger.warning(f"Failed to fetch COBOL main for {program_name}: {exc}")

            try:
                bundle.cobol_subprograms = self._fetch_subprograms(
                    bundle.cobol_main,
                    int(limits.get("max_subprograms", 3)),
                    int(limits.get("cobol_subprogram_chars", 1500)),
                )
            except Exception as exc:
                logger.warning(f"Failed to fetch sub-programs for {program_name}: {exc}")

            try:
                bundle.target_main, bundle.target_record = self._fetch_target(
                    program_name,
                    int(limits.get("target_main_chars", 6000)),
                    int(limits.get("target_record_chars", 2000)),
                )
            except Exception as exc:
                logger.warning(f"Failed to fetch target code for {program_name}: {exc}")

        try:
            bundle.similar_fixes = self._fetch_similar_fixes(
                program_name=program_name,
                query=query,
                max_fixes=int(limits.get("max_similar_fixes", 3)),
                diff_chars=int(limits.get("similar_fix_chars", 1500)),
            )
        except Exception as exc:
            logger.warning(f"Failed to fetch similar fixes: {exc}")

        try:
            bundle.curated_issues = self._fetch_curated_issues(
                target_code=bundle.target_main.code,
                target_file_path=bundle.target_main.file_path,
                query=query,
                max_issues=int(limits.get("max_curated_issues", 3)),
                max_distance=float(limits.get("curated_issue_max_distance", 1.3)),
            )
        except Exception as exc:
            logger.warning(f"Failed to fetch curated issues: {exc}")

        try:
            await self._populate_subroutine_bundles(
                bundle=bundle,
                extra_program_names=extra_program_names,
                query=query,
                limits=limits,
            )
        except Exception as exc:
            logger.warning(f"Failed to populate subroutine bundles: {exc}")

        return bundle

    # ------------------------------------------------------------------
    # COBOL
    # ------------------------------------------------------------------

    def _fetch_cobol(self, program_name: str, max_chars: int) -> ProgramArtifact:
        artifact = ProgramArtifact(program_name=program_name)

        # Exact program-name lookup is fastest and most accurate.
        result = self.cobol.get_by_program_name(program_name)
        if not result:
            results = self.cobol.search_hybrid(program_name, n_results=1)
            if results:
                # Hybrid search places content under "content"; normalise it.
                first = results[0]
                if "document" not in first and "content" in first:
                    first = {**first, "document": first.get("content", "")}
                result = first

        if not result:
            return artifact

        meta = result.get("metadata", {}) or {}
        artifact.file_path = (
            meta.get("full_path")
            or meta.get("relative_path")
            or f"{program_name}.COB"
        )
        artifact.code = _truncate(result.get("document", "") or "", max_chars)
        return artifact

    def _fetch_subprograms(
        self,
        main: ProgramArtifact,
        max_count: int,
        max_chars: int,
    ) -> List[ProgramArtifact]:
        if not main.has_code() or max_count <= 0:
            return []

        called: List[str] = []
        seen = {main.program_name.upper()}
        for match in _CALL_PATTERN.finditer(main.code):
            name = match.group(1).upper()
            if name in seen:
                continue
            seen.add(name)
            called.append(name)
            if len(called) >= max_count:
                break

        subs: List[ProgramArtifact] = []
        for name in called:
            try:
                sub = self._fetch_cobol(name, max_chars)
            except Exception as exc:
                logger.debug(f"Could not load sub-program {name}: {exc}")
                continue
            if sub.has_code():
                subs.append(sub)
        return subs

    # ------------------------------------------------------------------
    # Target language (C# / Java / Python)
    # ------------------------------------------------------------------

    def _fetch_target(
        self,
        program_name: str,
        main_chars: int,
        record_chars: int,
    ) -> Tuple[ProgramArtifact, ProgramArtifact]:
        main_art = ProgramArtifact(program_name=program_name)
        record_art = ProgramArtifact(program_name=program_name)

        results = self.codebase.search_hybrid(program_name, n_results=5) or []

        # KB-driven path: walk hybrid hits and pick a main + record file by
        # name convention. ``_read_live_or_kb`` already prefers the live file
        # on disk so user edits to indexed programs are reflected immediately.
        for result in results:
            meta = result.get("metadata", {}) or {}
            file_path = meta.get("full_path") or meta.get("relative_path") or ""
            if not file_path:
                continue
            # Only treat a hybrid hit as the right program when the filename
            # actually matches the requested program ID — otherwise semantic
            # search would happily return e.g. ``J3015123.cs`` for a query of
            # ``OJ011533`` and the caller would never see the on-disk file.
            if not self._filename_matches(file_path, program_name):
                continue

            content = self._read_live_or_kb(file_path, result)
            lower_name = os.path.basename(file_path).lower()
            is_record = "record" in lower_name
            is_supported = lower_name.endswith(_TARGET_EXTS)

            if is_record and not record_art.has_code():
                record_art.file_path = file_path
                record_art.code = _truncate(content, record_chars)
            elif (
                is_supported
                and not is_record
                and not main_art.has_code()
            ):
                main_art.file_path = file_path
                main_art.code = _truncate(content, main_chars)

            if main_art.has_code() and record_art.has_code():
                break

        # Filesystem fallback: covers brand-new programs that have not yet
        # been re-ingested into the codebase KB. We only read files that the
        # KB missed, so behaviour for already-indexed programs is unchanged.
        if not main_art.has_code() or not record_art.has_code():
            for fs_path in self._lookup_program_files(program_name):
                lower_name = os.path.basename(fs_path).lower()
                is_record = "record" in lower_name
                is_supported = lower_name.endswith(_TARGET_EXTS)
                if is_record and not record_art.has_code():
                    record_art.file_path = fs_path
                    record_art.code = _truncate(
                        self._read_disk_file(fs_path), record_chars
                    )
                elif (
                    is_supported
                    and not is_record
                    and not main_art.has_code()
                ):
                    main_art.file_path = fs_path
                    main_art.code = _truncate(
                        self._read_disk_file(fs_path), main_chars
                    )
                if main_art.has_code() and record_art.has_code():
                    break

        return main_art, record_art

    # ------------------------------------------------------------------
    # Filesystem fallback helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _filename_matches(file_path: str, program_name: str) -> bool:
        """True when the file's stem equals or starts with the program ID.

        Matches e.g. ``Oj011533.cs`` and ``Oj011533.record.cs`` for an
        ``OJ011533`` query, case-insensitive.
        """
        stem = os.path.splitext(os.path.basename(file_path))[0].lower()
        program = (program_name or "").lower()
        if not stem or not program:
            return False
        # Strip trailing ``.record`` qualifier when comparing the stem.
        head = stem.split(".", 1)[0]
        return head == program

    @staticmethod
    def _read_disk_file(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return fh.read()
        except OSError as exc:
            logger.debug(f"Direct read failed for {path}: {exc}")
            return ""

    def _lookup_program_files(self, program_name: str) -> List[str]:
        """Return repo files whose stem matches ``program_name``.

        The repo is scanned once and the result cached. The cache is
        rebuilt only when the repo root's mtime changes (cheap stat), so
        adding a new ``.cs`` file is reflected without restarting the app.
        Returns an empty list when the repo path is not configured / does
        not exist.
        """
        repo = getattr(config, "SSAB_OX_REPO_PATH", "") or ""
        if not repo or not os.path.isdir(repo):
            return []

        try:
            current_mtime = os.path.getmtime(repo)
        except OSError:
            current_mtime = 0.0

        if self._repo_index is None or current_mtime != self._repo_index_mtime:
            self._repo_index = self._build_repo_index(repo)
            self._repo_index_mtime = current_mtime

        return self._repo_index.get(program_name.upper(), [])

    @staticmethod
    def _build_repo_index(repo_path: str) -> Dict[str, List[str]]:
        """Walk ``repo_path`` once, return ``{PROGRAM_ID: [file paths]}``.

        Only files with target-language extensions are indexed; build
        folders are skipped. Cost is one walk per repo-mtime change.
        """
        index: Dict[str, List[str]] = {}
        for root, dirs, files in os.walk(repo_path):
            # Skip build / VCS folders in-place for a faster walk.
            dirs[:] = [
                d for d in dirs
                if d.lower() not in {f.lower() for f in _REPO_EXCLUDE_FOLDERS}
            ]
            for name in files:
                lower = name.lower()
                if not lower.endswith(_TARGET_EXTS):
                    continue
                stem = os.path.splitext(name)[0]
                head = stem.split(".", 1)[0].upper()
                index.setdefault(head, []).append(os.path.join(root, name))
        logger.info(
            f"ContextBuilder filesystem index built: "
            f"{len(index)} program IDs across {sum(len(v) for v in index.values())} files"
        )
        return index

    def _read_live_or_kb(self, file_path: str, kb_result: Dict[str, Any]) -> str:
        """Prefer the live filesystem version; fall back to the KB document.

        Resolves ``file_path`` against the developer's local
        ``SSAB_OX_REPO_PATH`` by basename when the stored absolute path
        (from the ingesting machine) does not exist on this machine.
        """
        live = self._resolve_local_path(file_path)
        if live:
            try:
                with open(live, "r", encoding="utf-8", errors="ignore") as fh:
                    return fh.read()
            except OSError as exc:
                logger.debug(f"Live read failed for {live}: {exc}")
        return kb_result.get("content") or kb_result.get("document", "") or ""

    def _resolve_local_path(self, file_path: str) -> str:
        """Return an existing on-disk path for ``file_path`` or ``""``.

        Priority: (1) the stored path if it exists, (2) same basename in
        the local ``SSAB_OX_REPO_PATH`` — matched via the cached
        program-ID index so cross-machine paths are handled without a
        fresh walk.
        """
        if not file_path:
            return ""
        if os.path.exists(file_path):
            return file_path
        basename = os.path.basename(file_path)
        if not basename:
            return ""
        stem = os.path.splitext(basename)[0]
        head = stem.split(".", 1)[0].upper()
        target = basename.lower()
        for candidate in self._lookup_program_files(head):
            if os.path.basename(candidate).lower() == target:
                return candidate
        return ""

    # ------------------------------------------------------------------
    # Knowledge base — similar past fixes
    # ------------------------------------------------------------------

    def _fetch_similar_fixes(
        self,
        program_name: Optional[str],
        query: str,
        max_fixes: int,
        diff_chars: int,
    ) -> List[SimilarFixArtifact]:
        if max_fixes <= 0:
            return []

        kb_query_parts = [p for p in (program_name, (query or "")[:200]) if p]
        if not kb_query_parts:
            return []
        kb_query = " ".join(kb_query_parts)

        # Both keyword and semantic search return ``document`` directly, which
        # is what we want for the diff. ``search_hybrid`` ranks well but does
        # not include documents — so we pick the search variant that gives us
        # the diff content for the given query type.
        if program_name:
            results = self.kb.search_keyword(kb_query, n_results=max_fixes) or []
        else:
            results = self.kb.search_similar(kb_query, n_results=max_fixes) or []

        fixes: List[SimilarFixArtifact] = []
        for result in results:
            meta = result.get("metadata", {}) or {}
            files_changed_str = meta.get("files_changed", "") or ""
            files = [f for f in files_changed_str.split(",") if f]
            try:
                pr_id = int(meta.get("pr_id", 0) or 0)
            except (TypeError, ValueError):
                pr_id = 0
            try:
                score = float(result.get("score", 0) or 0)
            except (TypeError, ValueError):
                score = 0.0
            fixes.append(
                SimilarFixArtifact(
                    pr_id=pr_id,
                    baseline=meta.get("baseline", "") or "",
                    title=meta.get("title", "") or "",
                    files_changed=files,
                    code_diff=_truncate(result.get("document", "") or "", diff_chars),
                    score=score,
                )
            )
        return fixes

    # ------------------------------------------------------------------
    # Subroutine expansion (main program -> all called subroutines)
    # ------------------------------------------------------------------

    async def _populate_subroutine_bundles(
        self,
        bundle: ContextBundle,
        extra_program_names: Optional[List[str]],
        query: str,
        limits: Dict[str, int],
    ) -> None:
        """Detect subroutines referenced by ``bundle`` (auto-detected +
        user-named siblings) and build a shallow bundle for each.

        Recursion: if a subroutine's own C# declares further subroutine
        fields, they are enqueued for later bundle build too — up to
        ``max_subroutine_depth`` levels below the main program (default
        2, so main → subs → nested subs). A shared ``visited`` set
        prevents cycles and duplicates. Total sub-bundles is capped by
        ``max_subroutine_bundles`` (default 25) across all depths.

        Shallow means: COBOL + target C# + curated-issue matches only —
        we skip the chunked embedding phase of the curated-issue scan
        (it adds up quickly across many subroutines and the literal /
        structural phases are already enough for shape-anchored KB
        patterns).
        """
        max_subs = int(limits.get("max_subroutine_bundles", 25))
        max_depth = int(limits.get("max_subroutine_depth", 2))
        if max_subs <= 0 or max_depth <= 0:
            return

        primary_key = (bundle.program_name or "").lower()
        visited: Set[str] = {primary_key} if primary_key else set()

        # Queue holds (program_name, depth). Depth 1 = direct child of
        # the main program (either an explicit sibling or a field
        # declaration in the main C#).
        queue: "deque[Tuple[str, int]]" = deque()

        # Seed 1: explicit siblings from the user's query
        # ("Tablasn AND Hgsr004p AND Kodlasn").
        for name in (extra_program_names or []):
            key = (name or "").lower()
            if not key or key in visited:
                continue
            visited.add(key)
            queue.append((name, 1))

        # Seed 2: subroutines auto-detected from the main program's C#.
        for name in self._detect_called_subroutines(
            bundle.target_main.code, bundle.target_main.file_path,
        ):
            key = name.lower()
            if key in visited:
                continue
            visited.add(key)
            queue.append((name, 1))

        while queue and len(bundle.subroutine_bundles) < max_subs:
            name, depth = queue.popleft()
            try:
                sub = await self._build_subroutine_bundle(
                    name=name, query=query, limits=limits,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Subroutine bundle build failed for {name}: {exc}")
                continue
            if not sub or sub.is_empty():
                continue
            bundle.subroutine_bundles.append(sub)

            # Recurse only when there is still depth budget AND we have
            # this subroutine's C# to scan for further field decls.
            if depth < max_depth and sub.target_main.has_code():
                for nested in self._detect_called_subroutines(
                    sub.target_main.code, sub.target_main.file_path,
                ):
                    key = nested.lower()
                    if key in visited:
                        continue
                    visited.add(key)
                    queue.append((nested, depth + 1))

        if bundle.subroutine_bundles:
            logger.info(
                f"Subroutine expansion for {bundle.program_name}: "
                f"{len(bundle.subroutine_bundles)} sub-bundle(s), "
                f"max_depth={max_depth} "
                f"({', '.join(s.program_name for s in bundle.subroutine_bundles)})"
            )

    async def _build_subroutine_bundle(
        self,
        name: str,
        query: str,
        limits: Dict[str, int],
    ) -> ContextBundle:
        """Shallow bundle for one subroutine (or explicitly-named sibling)."""
        sub = ContextBundle(program_name=name)
        try:
            sub.cobol_main = self._fetch_cobol(
                name, int(limits.get("cobol_main_chars", 6000)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"sub COBOL {name}: {exc}")
        try:
            sub.target_main, sub.target_record = self._fetch_target(
                name,
                int(limits.get("target_main_chars", 6000)),
                int(limits.get("target_record_chars", 2000)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"sub target {name}: {exc}")
        try:
            sub.curated_issues = self._fetch_curated_issues(
                target_code=sub.target_main.code,
                target_file_path=sub.target_main.file_path,
                query=query,
                max_issues=int(limits.get("max_curated_issues", 3)),
                max_distance=float(limits.get("curated_issue_max_distance", 1.3)),
                include_embedding=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"sub issues {name}: {exc}")
        return sub

    def _detect_called_subroutines(
        self, cs_code: str, cs_file_path: str = "",
    ) -> List[str]:
        """Return class names of subroutines referenced by ``cs_code``.

        Uses ``_SUBROUTINE_FIELD_PATTERN`` for the shape, then filters to
        types whose ``.cs`` file lives under an
        ``SSAB.OX.Batch.Subroutines`` folder. That folder-based check is
        the authoritative rule for a subroutine dependency and matches
        the team's static-analysis prompt.

        When ``cs_file_path`` is provided AND readable, the file is
        loaded FRESH from disk so field declarations past the
        prompt-slice truncation window are still seen (main programs
        like J0030043 declare their subroutines below the 6 KB cutoff
        used for the LLM prompt).
        """
        if cs_file_path and os.path.isfile(cs_file_path):
            try:
                with open(cs_file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    cs_code = fh.read()
            except OSError as exc:
                logger.debug(
                    f"Full-file read for subroutine detection failed: {exc}"
                )
        if not cs_code:
            return []
        subroutine_names = self._get_subroutine_class_index()
        if not subroutine_names:
            return []
        seen: Set[str] = set()
        ordered: List[str] = []
        for m in _SUBROUTINE_FIELD_PATTERN.finditer(cs_code):
            type_name = m.group(1)
            key = type_name.lower()
            if key in seen or key not in subroutine_names:
                continue
            seen.add(key)
            ordered.append(type_name)
        return ordered

    def _get_subroutine_class_index(self) -> Set[str]:
        """Return (and cache) lowercase class names found under any
        ``SSAB.OX.Batch.Subroutines`` folder in the repo.

        Cache is rebuilt only when the repo root's mtime changes, so
        newly-added subroutines are picked up without an app restart.
        Returns an empty set when the repo path is unset / missing.
        """
        repo = getattr(config, "SSAB_OX_REPO_PATH", "") or ""
        if not repo or not os.path.isdir(repo):
            return set()
        try:
            current_mtime = os.path.getmtime(repo)
        except OSError:
            current_mtime = 0.0
        if (
            self._subroutine_class_index is None
            or current_mtime != self._subroutine_class_index_mtime
        ):
            self._subroutine_class_index = self._build_subroutine_class_index(repo)
            self._subroutine_class_index_mtime = current_mtime
        return self._subroutine_class_index

    @staticmethod
    def _build_subroutine_class_index(repo_path: str) -> Set[str]:
        """Walk *repo_path*, return lowercase class names for every ``.cs``
        file whose path contains an ``SSAB.OX.Batch.Subroutines`` segment.
        """
        index: Set[str] = set()
        excludes = {f.lower() for f in _REPO_EXCLUDE_FOLDERS}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d.lower() not in excludes]
            if _SUBROUTINES_FOLDER_MARKER not in root.lower():
                continue
            for name in files:
                if not name.lower().endswith(".cs"):
                    continue
                stem = os.path.splitext(name)[0]
                if stem:
                    index.add(stem.lower())
        logger.info(
            f"Subroutine class index built: {len(index)} classes "
            f"under SSAB.OX.Batch.Subroutines/"
        )
        return index

    # ------------------------------------------------------------------
    # Knowledge base — curated issues (bug patterns)
    # ------------------------------------------------------------------

    def _fetch_curated_issues(
        self,
        target_code: str,
        target_file_path: str,
        query: str,
        max_issues: int,
        max_distance: float,
        include_embedding: bool = True,
    ) -> List[IssueArtifact]:
        """Retrieve curated bug patterns relevant to this turn.

        Two-phase retrieval:

        1. **Literal + structural substring match** — scan every curated
           issue's ``buggy_code`` for a line that appears verbatim in
           the target code (or matches structurally when only digit-
           bearing identifiers differ). Uses the FULL on-disk file when
           available so patterns past the truncation window in
           ``target_code`` are still seen.

        2. **Embedding search over chunked target code** — for issues
           the literal / structural scan missed. Uses the (already-
           truncated) prompt slice; that keeps the embedding budget
           bounded. Set ``include_embedding=False`` when scanning many
           files in the same turn (e.g. every subroutine of a big
           program) — the embedding pass adds up quickly there and the
           literal + structural phases are already precise for
           shape-anchored KB patterns.

        Literal hits are ranked first (distance ``0.0``), then embedding
        hits by ascending distance. Overall list capped at ``max_issues``.
        """
        if max_issues <= 0:
            return []
        if not hasattr(self.kb, "search_issues"):
            return []

        # For the literal scan, load the FULL file when we know the
        # path — target_code has already been truncated to fit the LLM
        # prompt budget, so a pattern past the truncation cutoff would
        # be invisible without this.
        full_target = target_code
        if target_file_path and os.path.isfile(target_file_path):
            try:
                with open(target_file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    full_target = fh.read()
            except OSError as exc:
                logger.debug(
                    f"Full-file read for literal issue scan failed: {exc}"
                )

        # issue_id -> (best_distance, metadata_dict)
        best: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        # ── Phase 1: literal substring match ─────────────────────────
        if full_target:
            try:
                all_issues = self.kb.get_all_issues()
            except AttributeError:
                all_issues = self._iter_all_issues_via_collection()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"literal issue scan (get_all) failed: {exc}")
                all_issues = []

            for entry in all_issues:
                meta = entry.get("metadata", {}) or {}
                iid = str(meta.get("issue_id", entry.get("id", "")) or "")
                buggy = str(meta.get("buggy_code", "") or "")
                if not iid or not buggy:
                    continue
                match_kind = self._match_buggy_pattern(full_target, buggy)
                if match_kind:
                    best[iid] = (0.0, meta)
                    logger.info(
                        f"Curated issue {iid}: {match_kind} buggy-pattern "
                        f"match found in target code."
                    )

        # ── Phase 2: chunked embedding search ────────────────────────
        chunks: List[str] = []
        if include_embedding:
            if query:
                chunks.append(query[:300])
            if target_code:
                window = 1200
                overlap = 200
                step = max(1, window - overlap)
                for start in range(0, len(target_code), step):
                    chunks.append(target_code[start:start + window])

        for chunk in chunks:
            try:
                hits = self.kb.search_issues(
                    query=chunk,
                    n_results=max_issues,
                    max_distance=max_distance,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"search_issues (chunk) failed: {exc}")
                continue
            for hit in hits or []:
                meta = hit.get("metadata", {}) or {}
                iid = str(meta.get("issue_id", hit.get("id", "")) or "")
                if not iid:
                    continue
                try:
                    dist = float(hit.get("distance", 0.0) or 0.0)
                except (TypeError, ValueError):
                    dist = 0.0
                prev = best.get(iid)
                if prev is None or dist < prev[0]:
                    best[iid] = (dist, meta)

        if not best:
            return []

        ranked = sorted(best.items(), key=lambda kv: kv[1][0])[:max_issues]
        issues: List[IssueArtifact] = []
        for iid, (dist, meta) in ranked:
            issues.append(
                IssueArtifact(
                    issue_id=iid,
                    title=str(meta.get("title", "") or ""),
                    bug_category=str(meta.get("bug_category", "") or ""),
                    tags=str(meta.get("tags", "") or ""),
                    program=str(meta.get("program", "") or ""),
                    root_cause=str(meta.get("root_cause", "") or ""),
                    buggy_code=str(meta.get("buggy_code", "") or ""),
                    fixed_code=str(meta.get("fixed_code", "") or ""),
                    distance=dist,
                )
            )
        logger.info(
            f"Curated issues fetched: {len(issues)} "
            f"({', '.join(f'{i.issue_id}@{i.distance:.3f}' for i in issues)})"
        )
        return issues

    def _iter_all_issues_via_collection(self) -> List[Dict[str, Any]]:
        """Fallback for when :class:`KnowledgeBaseService` has no public
        ``get_all_issues`` — reach into the collection directly.
        """
        try:
            coll = self.kb._get_issues_collection()  # noqa: SLF001
            got = coll.get()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"issues collection direct fetch failed: {exc}")
            return []
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        return [
            {"id": iid, "metadata": metas[i] if i < len(metas) else {}}
            for i, iid in enumerate(ids)
        ]

    @staticmethod
    def _match_buggy_pattern(target_code: str, buggy_code: str) -> str:
        """Return ``"literal"`` when *target_code* contains any buggy line
        verbatim, ``"structural"`` when it only matches after generalising
        identifier names containing digits, or ``""`` when neither.

        Framework tokens (``Parameters``, ``Record``, ``Link``, ``Wrap``,
        etc.) stay literal in the structural pass so the pattern is
        anchored on shape, not just arbitrary assignment. Matches
        shorter than 15 chars are skipped — they'd false-positive on
        common tokens like ``return true;``.
        """
        if not target_code or not buggy_code:
            return ""
        needles: List[str] = []
        for raw in buggy_code.splitlines():
            needle = raw.strip()
            if len(needle) < 15:
                continue
            needles.append(needle)
        for needle in needles:
            if needle in target_code:
                return "literal"
        for needle in needles:
            pattern = ContextBuilderService._structural_regex_from_line(needle)
            if not pattern:
                continue
            try:
                if re.search(pattern, target_code):
                    return "structural"
            except re.error:
                continue
        return ""

    @staticmethod
    def _target_contains_buggy_pattern(target_code: str, buggy_code: str) -> bool:
        """Backward-compat wrapper returning a bool.

        Use :meth:`_match_buggy_pattern` when the caller needs to know
        whether the hit was literal or structural (e.g. for logging).
        """
        return bool(
            ContextBuilderService._match_buggy_pattern(target_code, buggy_code)
        )

    @staticmethod
    def _structural_regex_from_line(line: str) -> Optional[str]:
        """Convert a code line into a regex that matches structurally.

        Only identifiers containing digits are generalised to ``\\w+``
        (auto-generated program-scoped names). Pure-letter tokens like
        ``Parameters``, ``Record``, ``Link``, ``Wrap`` stay literal so
        the pattern is anchored on framework structure, not just
        arbitrary assignment. Whitespace becomes ``\\s*``. Returns
        ``None`` when no digit-bearing identifier is present — the
        literal pass already covered that line.
        """
        tokens = re.findall(r"\w+|\W", line)
        parts: List[str] = []
        generalised = False
        for tok in tokens:
            if not tok:
                continue
            if tok.isdigit():
                parts.append(r"\d+")
                generalised = True
            elif (
                (tok[0].isalpha() or tok[0] == "_")
                and any(c.isdigit() for c in tok)
            ):
                parts.append(r"\w+")
                generalised = True
            elif tok.isspace():
                parts.append(r"\s*")
            else:
                parts.append(re.escape(tok))
        if not generalised:
            return None
        return "".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


_context_builder: Optional[ContextBuilderService] = None


def get_context_builder() -> ContextBuilderService:
    """Return the process-wide context builder."""
    global _context_builder
    if _context_builder is None:
        _context_builder = ContextBuilderService()
    return _context_builder
