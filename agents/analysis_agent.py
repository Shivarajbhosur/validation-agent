"""
Analysis Agent

AI-powered analysis agent for COBOL-to-C# migration issues.
Analyzes runtime errors and output differences, suggests fixes.
"""

import re
import os
import yaml
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set
from loguru import logger

from models.fix_suggestion import (
    ErrorType,
    ErrorContext,
    CodeContext,
    SimilarFix,
    FixSuggestion,
    CodeEdit,
    FixAttempt,
    FixStatus,
    AnalysisSession,
    AnalysisStatus,
    create_analysis_session,
    create_error_context
)

from services.cobol_service import CobolService
from services.codebase_service import CodebaseService
from services.knowledge_base_service import KnowledgeBaseService
from services.llm_service import LLMService

import config


# ────────────────────────────────────────────────────────────────────
# Stale-KB-path recovery helper
#
# The codebase KB (ChromaDB `codebase` collection) is populated by
# ``scripts/ingest_codebase.py`` — a one-shot batch script. If the
# SSAB.OX repo layout is changed after ingestion (e.g. a ``src/``
# layer is added), every stored ``full_path`` becomes stale and
# ``os.path.exists()`` returns False for all entries. Without this
# helper, ``gather_code_context`` would silently fall back to the
# stale KB content snapshot and the AI would confidently quote code
# that no longer exists on disk.
#
# This helper walks the SSAB.OX repo once (result cached for the
# process lifetime) building a ``{basename_lower: absolute_path}``
# index, so a stale KB path can be recovered to the current on-disk
# location by basename. The user is still warned to re-ingest the KB.
# ────────────────────────────────────────────────────────────────────

_REPO_FILE_INDEX: Optional[Dict[str, List[str]]] = None
_REPO_INDEX_EXCLUDE = {"bin", "obj", ".git", ".vs", "node_modules",
                       "TestResults", "Debug", "Release", "packages"}


def _read_source_best_effort(path: str) -> str:
    """Read a C#/COBOL source file, preserving its real characters.

    Legacy SSAB source files are often Windows-1252 (Swedish ``å ä ö``
    at bytes ``0xE5 0xE4 0xF6`` / ``Ä Å`` at ``0xC4 0xC5``), NOT UTF-8.
    Reading them as UTF-8 with ``errors='ignore'`` silently DROPS those
    bytes, so the LLM sees mangled text (``LSTA`` instead of ``LÄSTA``)
    and quotes an ``original_code`` snippet that can never match the file
    on disk — every auto-apply is then refused. Decode with the same
    encoding ladder :mod:`tools.auto_apply` uses to write, so the content
    the AI quotes is byte-for-byte what the apply step will match.
    """
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort — never raise; latin-1 above practically always
    # succeeds, but keep a defensive fallback.
    return raw.decode("utf-8", errors="ignore")


def _find_file_in_repo(basename: str, hint_path: str = "") -> Optional[str]:
    """Return the current on-disk path of ``basename`` in the SSAB.OX repo.

    Used by :meth:`AnalysisAgent.gather_code_context` to recover from
    stale KB paths (e.g. after a repo restructure). Returns ``None``
    if the file is not found. The full ``{basename: [paths...]}``
    index is cached for the process lifetime — repeat lookups are
    O(candidates).

    When multiple copies of ``basename`` exist in the repo (a common
    case with COBOL-migration codebases that keep ``conversion/``
    scratch folders alongside the real ``src/`` tree), the copy whose
    absolute path shares the longest **suffix** with ``hint_path`` is
    preferred. That way a stale KB path like
    ``C:\\...\\SSAB.OX\\SSAB.OX.PPL\\Programs\\Hppl486p.cs`` still
    finds its live counterpart at
    ``C:\\...\\SSAB.OX\\src\\SSAB.OX\\SSAB.OX.PPL\\Programs\\Hppl486p.cs``
    even though the repo now has a ``src/`` layer.
    """
    global _REPO_FILE_INDEX
    if not basename:
        return None
    if _REPO_FILE_INDEX is None:
        _REPO_FILE_INDEX = {}
        repo_root = str(config.SSAB_OX_REPO_PATH)
        if not os.path.isdir(repo_root):
            logger.warning(
                f"SSAB_OX_REPO_PATH {repo_root!r} is not a directory; "
                "stale-KB-path recovery disabled."
            )
            return None
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d.lower() not in {e.lower() for e in _REPO_INDEX_EXCLUDE}]
            for fname in files:
                key = fname.lower()
                _REPO_FILE_INDEX.setdefault(key, []).append(os.path.join(root, fname))

    candidates = _REPO_FILE_INDEX.get(basename.lower()) or []
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Multiple copies — pick the one whose path shares the longest
    # suffix (by path segments) with the stale KB hint. That biases
    # us toward the real ``src/`` tree over ``conversion/`` scratch
    # folders.
    def _is_conversion(candidate: str) -> bool:
        return "conversion" in {p.lower() for p in Path(candidate).parts}

    hint_parts = [p.lower() for p in Path(hint_path).parts] if hint_path else []
    if not hint_parts:
        # No hint to score against — still prefer a real ``src/`` copy
        # over a ``conversion/`` scratch copy when both exist.
        non_conversion = [c for c in candidates if not _is_conversion(c)]
        return (non_conversion or candidates)[0]

    def _suffix_score(candidate: str) -> int:
        cand_parts = [p.lower() for p in Path(candidate).parts]
        n = 0
        for a, b in zip(reversed(cand_parts), reversed(hint_parts)):
            if a != b:
                break
            n += 1
        return n

    # Rank by suffix overlap first, then break ties by preferring a
    # non-``conversion`` (i.e. real ``src/``) copy.
    best = max(candidates, key=lambda c: (_suffix_score(c), not _is_conversion(c)))
    return best


# ────────────────────────────────────────────────────────────────────
# ExperLib runtime-library source lookup (on-demand, no ingestion)
# ────────────────────────────────────────────────────────────────────
# When the C# code being analyzed calls into ExperLib (DateType,
# SqlQuery, Clock, Record, etc.), the LLM used to guess at the API
# because it couldn't see the runtime source. This helper walks
# ``config.EXPERLIB_REPO_PATH`` once (index cached for process
# lifetime), then reads matching .cs files FRESH from disk on each
# analysis — content is never stale.
#
# Feature is entirely optional:
#   * EXPERLIB_REPO_PATH unset  → helpers return empty, prompt unchanged
#   * Path invalid              → warning logged once, empty results
#   * No PascalCase matches     → empty results
#   * Any file-read error       → skipped silently, next candidate tried
#
# ExperLib naming convention (verified against the repo): one class
# per file, filename == class name (e.g. ``DateType.cs`` defines
# ``class DateType``). So a filename index is enough — no source
# parsing required.
# ────────────────────────────────────────────────────────────────────

_EXPERLIB_INDEX: Optional[Dict[str, str]] = None
_EXPERLIB_INDEX_EXCLUDE = {
    "bin", "obj", ".git", ".vs", "node_modules",
    "TestResults", "Tests", "UnitTests", "IntegrationTests",
    "Debug", "Release",
    # Whole assemblies to skip (naming: "ExperLib.Tests")
    "ExperLib.Tests",
}
_EXPERLIB_MAX_FILES = 5          # cap injected files per analysis
_EXPERLIB_MAX_BYTES_PER_FILE = 8000  # ~2k tokens per file after truncation


def _build_experlib_index() -> Dict[str, str]:
    """Walk ``EXPERLIB_REPO_PATH`` once, return ``{class_name_lower: path}``.

    Result cached at module level for the Chainlit process lifetime.
    Returns an empty dict (feature disabled) if the env var is not set
    or the path is invalid.
    """
    global _EXPERLIB_INDEX
    if _EXPERLIB_INDEX is not None:
        return _EXPERLIB_INDEX

    _EXPERLIB_INDEX = {}
    repo = getattr(config, "EXPERLIB_REPO_PATH", "") or ""
    if not repo:
        return _EXPERLIB_INDEX  # feature disabled — no env var
    if not os.path.isdir(repo):
        logger.warning(
            f"EXPERLIB_REPO_PATH {repo!r} is not a directory; "
            "ExperLib source injection disabled."
        )
        return _EXPERLIB_INDEX

    excludes = {e.lower() for e in _EXPERLIB_INDEX_EXCLUDE}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d.lower() not in excludes]
        for fname in files:
            if not fname.endswith(".cs"):
                continue
            # Filename convention == class name (verified against repo).
            class_name = fname[:-3]
            key = class_name.lower()
            # First one wins if duplicate names exist (defensive).
            _EXPERLIB_INDEX.setdefault(key, os.path.join(root, fname))

    logger.info(
        f"Indexed {len(_EXPERLIB_INDEX)} ExperLib types from {repo}"
    )
    return _EXPERLIB_INDEX


# Cheap literal filter for identifiers that would otherwise match the
# PascalCase regex but are almost never the interesting ExperLib class.
# Keeps the "5 files" budget for actually-useful matches.
_EXPERLIB_IGNORE_TOKENS = {
    # C# / .NET generic noise
    "system", "string", "int", "int32", "int64", "double", "decimal",
    "boolean", "bool", "byte", "object", "void", "list", "dictionary",
    "task", "exception", "argumentexception", "nullreferenceexception",
    "invalidoperationexception", "console", "environment", "math",
    "convert", "encoding", "regex", "match", "readonly", "static",
    "public", "private", "internal", "protected", "virtual", "override",
    # Common project noise
    "program", "programservice", "programfactory",  # too many callers
}


def _find_experlib_sources(
    text: str, max_files: int = _EXPERLIB_MAX_FILES,
) -> List[Tuple[str, str]]:
    """Scan ``text`` for PascalCase identifiers; return matching ExperLib
    class sources as ``[(class_name, file_content), ...]``.

    Every file is opened FRESH each call — no content caching — so an
    ExperLib edit on disk is reflected in the next analysis immediately.
    """
    index = _build_experlib_index()
    if not index:
        return []
    if not text:
        return []

    # PascalCase identifiers 3+ chars long. This catches ``DateType``,
    # ``SqlQuery`` but skips single-letter loop variables etc.
    candidates_seen: List[str] = []
    seen_lower: Set[str] = set()
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,})\b", text):
        name = m.group(1)
        low = name.lower()
        if low in seen_lower:
            continue
        if low in _EXPERLIB_IGNORE_TOKENS:
            continue
        seen_lower.add(low)
        candidates_seen.append(name)

    hits: List[Tuple[str, str]] = []
    for name in candidates_seen:
        path = index.get(name.lower())
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError as exc:
            logger.debug(f"ExperLib file read failed for {name}: {exc}")
            continue
        if len(content) > _EXPERLIB_MAX_BYTES_PER_FILE:
            content = (
                content[:_EXPERLIB_MAX_BYTES_PER_FILE]
                + "\n// ... (truncated for context budget)"
            )
        hits.append((name, content))
        if len(hits) >= max_files:
            break

    if hits:
        logger.info(
            "ExperLib hits for this analysis: "
            + ", ".join(name for name, _ in hits)
        )
    return hits


# ────────────────────────────────────────────────────────────────────
# SSAB.OX project-type source lookup (on-demand, no ingestion)
# ────────────────────────────────────────────────────────────────────
# Parallel helper to ``_find_experlib_sources``, but scoped to project
# types defined inside ``config.SSAB_OX_REPO_PATH`` (SqlRows, records,
# helper classes, etc.).
#
# Why we need this:
#   The target program (e.g. Hppl547p.cs) references local classes by
#   VARIABLE name (``pplkpbet``, ``pplcpmfgoperexp``, ...) — never by
#   type name. The AI can't see the FIELD TYPES declared in those
#   record classes unless we hand them over as extra context.
#   Without the field types, the AI can't reason about type-mismatch
#   fixes (e.g. that ``StringField`` accepts ``DateType`` via implicit
#   operator, so an upstream assignment is the real bug).
#
# Feature is entirely optional:
#   * SSAB_OX_REPO_PATH invalid → warning once, empty results
#   * No PascalCase matches   → empty results
#   * Any file-read error     → skipped silently
# ────────────────────────────────────────────────────────────────────

_SSAB_PROJECT_INDEX: Optional[Dict[str, str]] = None
_SSAB_PROJECT_INDEX_EXCLUDE = {
    "bin", "obj", "packages", ".git", ".vs", "node_modules",
    "TestResults", "Tests", "UnitTests", "IntegrationTests",
    "Debug", "Release",
    # Scratch/converter folders (already excluded by codebase_service
    # for KB ingestion — same reason applies here).
    "conversion", "converted", "scratch", "migration_output",
}
_SSAB_PROJECT_MAX_FILES = 6           # cap injected local classes per analysis
_SSAB_PROJECT_MAX_BYTES_PER_FILE = 6000  # ~1.5k tokens per file


def _build_ssab_project_index() -> Dict[str, str]:
    """Walk ``SSAB_OX_REPO_PATH`` once, return ``{class_name_lower: path}``.

    Only ``src/`` files are indexed (thanks to the ``conversion`` etc.
    exclusion). Cached for the process lifetime — same pattern as the
    ExperLib index.
    """
    global _SSAB_PROJECT_INDEX
    if _SSAB_PROJECT_INDEX is not None:
        return _SSAB_PROJECT_INDEX

    _SSAB_PROJECT_INDEX = {}
    repo = getattr(config, "SSAB_OX_REPO_PATH", "") or ""
    if not repo:
        return _SSAB_PROJECT_INDEX
    if not os.path.isdir(repo):
        logger.warning(
            f"SSAB_OX_REPO_PATH {repo!r} is not a directory; "
            "project-type source injection disabled."
        )
        return _SSAB_PROJECT_INDEX

    excludes = {e.lower() for e in _SSAB_PROJECT_INDEX_EXCLUDE}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d.lower() not in excludes]
        for fname in files:
            if not fname.endswith(".cs"):
                continue
            class_name = fname[:-3]
            key = class_name.lower()
            # First one wins if duplicate names exist — the walk visits
            # ``src/`` before other siblings alphabetically, and we've
            # already filtered ``conversion/`` etc.
            _SSAB_PROJECT_INDEX.setdefault(key, os.path.join(root, fname))

    logger.info(
        f"Indexed {len(_SSAB_PROJECT_INDEX)} SSAB.OX project types from {repo}"
    )
    return _SSAB_PROJECT_INDEX


def _find_ssab_project_sources(
    text: str,
    exclude_names: Optional[Set[str]] = None,
    max_files: int = _SSAB_PROJECT_MAX_FILES,
) -> List[Tuple[str, str]]:
    """Scan ``text`` for PascalCase identifiers; return matching SSAB.OX
    project-type sources as ``[(class_name, file_content), ...]``.

    ``exclude_names`` — case-insensitive class names to skip. Used to
    avoid re-injecting the target program's own class (already in the
    prompt as ``csharp_code``) and ExperLib types (handled separately).
    """
    index = _build_ssab_project_index()
    if not index or not text:
        return []

    exclude_lower = {n.lower() for n in (exclude_names or set())}

    # ExperLib types would also match the regex; skip them here so this
    # helper stays focused on LOCAL project types.
    experlib_index = _build_experlib_index() or {}

    candidates_seen: List[str] = []
    seen_lower: Set[str] = set()
    for m in re.finditer(r"\b([A-Z][A-Za-z0-9]{2,})\b", text):
        name = m.group(1)
        low = name.lower()
        if low in seen_lower:
            continue
        if low in _EXPERLIB_IGNORE_TOKENS:
            continue
        if low in exclude_lower:
            continue
        if low in experlib_index:
            continue  # will be handled by ExperLib helper
        seen_lower.add(low)
        candidates_seen.append(name)

    hits: List[Tuple[str, str]] = []
    for name in candidates_seen:
        path = index.get(name.lower())
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError as exc:
            logger.debug(f"SSAB project file read failed for {name}: {exc}")
            continue
        if len(content) > _SSAB_PROJECT_MAX_BYTES_PER_FILE:
            content = (
                content[:_SSAB_PROJECT_MAX_BYTES_PER_FILE]
                + "\n// ... (truncated for context budget)"
            )
        hits.append((name, content))
        if len(hits) >= max_files:
            break

    if hits:
        logger.info(
            "SSAB project-type hits for this analysis: "
            + ", ".join(name for name, _ in hits)
        )
    return hits


# ────────────────────────────────────────────────────────────────────
# Error-signature comparison
#
# When the AnalysisAgent is called for the SECOND (or later) time on
# the same session, we must decide whether the current error is
# *the same* as the previous attempt's error (previous fix truly
# failed → retry with a different approach) or *different* (previous
# fix worked and unmasked a new bug downstream → treat as fresh
# first-attempt analysis).
#
# The comparison is deliberately generic and structural — no
# hardcoded program names, error strings, or fix patterns.
# ────────────────────────────────────────────────────────────────────

_ERROR_SIGNATURE_SIMILARITY_THRESHOLD = 0.85


def _normalize_error_text(text: Optional[str]) -> str:
    """Normalize an error message for stable structural comparison.

    Strips variable-run data (hex addresses, session IDs, line/column
    numbers, timestamps, paths) so two runs of the same underlying
    error compare equal even when the surface text differs slightly.
    """
    if not text:
        return ""
    s = text.lower()
    # Strip typical variable-run tokens
    s = re.sub(r"0x[0-9a-f]+", " ", s)                       # hex addrs
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}[t ][\d:.]+z?\b", " ", s) # timestamps
    s = re.sub(r"\bline\s*\d+\b", " line ", s)               # line N
    s = re.sub(r"\bcolumn\s*\d+\b", " column ", s)           # column N
    s = re.sub(r":\d+(?::\d+)?\b", " ", s)                   # :line:col
    s = re.sub(r"\d+", " ", s)                               # any digits
    s = re.sub(r"[\\/][^\s\"']+", " ", s)                    # file paths
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_new_error(prev_error: Optional[str], curr_error: Optional[str]) -> bool:
    """Return True when *curr_error* looks like a NEW error, not the
    same one as *prev_error* re-surfacing.

    Uses normalized-string similarity (SequenceMatcher). Similarity ≥
    :data:`_ERROR_SIGNATURE_SIMILARITY_THRESHOLD` means the two are
    considered the same error (retry). Below the threshold means a
    new error has surfaced (fresh analysis).

    Edge cases (all safe defaults):
      * Missing prev or curr → treated as NEW error (no retry bias).
    """
    if not prev_error or not curr_error:
        return True
    prev_norm = _normalize_error_text(prev_error)
    curr_norm = _normalize_error_text(curr_error)
    if not prev_norm or not curr_norm:
        return True
    ratio = SequenceMatcher(None, prev_norm, curr_norm).ratio()
    return ratio < _ERROR_SIGNATURE_SIMILARITY_THRESHOLD


# ────────────────────────────────────────────────────────────────────
# Baseline artefact listing (I/O/S marker + CRLF/NULL sniff)
#
# When a program abends or produces a SQL error, one of the most
# common root causes in this codebase is a WRONG file-type declaration
# in the C# code (``new FlatFile(...)`` for what is actually a text
# file, or vice-versa). To let the AI verify that decision instead of
# guessing, we list the baseline's actual input/output artefacts from
# the shared workspace folder and hand the AI concrete evidence:
#   * marker letter from the filename (``-I-``, ``-O-``, ``-S-``)
#   * CRLF-present sniff  (text-like)
#   * NULL-byte sniff     (binary-like)
#
# The RULE mapping markers/bytes to C# types (``TextFile`` vs
# ``FlatFile``) lives ONLY in the curated Issues KB entry text, NOT
# in this code. This helper just gathers evidence.
#
# Zero impact when the folder is missing — returns [].
# ────────────────────────────────────────────────────────────────────

_BASELINE_FILES_MAX = 20


def _extract_baseline_num(baseline_name: str) -> str:
    """Return the numeric prefix of a baseline name.

    ``P05068-HPPL577P`` -> ``"05068"``.  Empty string if none found.
    """
    if not baseline_name:
        return ""
    m = re.match(r"^[A-Za-z]*(\d+)", baseline_name)
    return m.group(1) if m else ""


def _sniff_text_file(path: str, max_bytes: int = 4096) -> Tuple[bool, bool]:
    """Return ``(has_crlf, has_null)`` from the first *max_bytes* of *path*.

    CRLF suggests a text file; NULL bytes suggest binary/sequential.
    Any read error returns ``(False, False)`` — callers still get the
    filename-marker signal.
    """
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(max_bytes)
    except OSError:
        return (False, False)
    return (b"\r\n" in chunk, b"\x00" in chunk)


def _list_baseline_files(baseline_name: str) -> List[Dict[str, Any]]:
    """Return artefact files matching a baseline in the shared folder.

    Reads ``config.WORKSPACE_PATH / "files"`` — files are pooled across
    baselines, filtered by the baseline number prefix in the filename
    (e.g. ``05068-...`` for ``P05068-HPPL577P``).

    Each returned entry:
        ``{name, marker, has_crlf, has_null, size}``
    where ``marker`` is one of ``"I"``, ``"O"``, ``"S"`` (from the
    filename convention) or ``""`` if the convention isn't followed.
    Empty list on any failure or missing folder — never raises.
    """
    workspace = getattr(config, "WORKSPACE_PATH", "")
    if not workspace:
        return []
    files_root = Path(str(workspace)) / "files"
    if not files_root.is_dir():
        return []
    num = _extract_baseline_num(baseline_name)
    if not num:
        return []

    hits: List[Dict[str, Any]] = []
    try:
        iterator = sorted(files_root.iterdir())
    except OSError:
        return []
    for p in iterator:
        if not p.is_file() or not p.name.startswith(num + "-"):
            continue
        marker = ""
        for seg in p.stem.split("-"):
            if len(seg) == 1 and seg.upper() in ("I", "O", "S"):
                marker = seg.upper()
                break
        has_crlf, has_null = _sniff_text_file(str(p))
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        hits.append({
            "name": p.name,
            "marker": marker,
            "has_crlf": has_crlf,
            "has_null": has_null,
            "size": size,
        })
        if len(hits) >= _BASELINE_FILES_MAX:
            break
    return hits


def _slice_around_error_line(
    csharp_code: str,
    stack_trace: str,
    fallback_line: Optional[int],
    context_lines: int = 30,
    program_name: Optional[str] = None,
) -> str:
    """Return the region of *csharp_code* around the error line.

    Extracts the target program file's line number from *stack_trace*.
    When *program_name* is given, only matches lines from that specific
    ``<Program>.cs`` file — otherwise the first ExperLib frame would win
    and point to a completely unrelated line inside the current file.

    Slices ``±context_lines`` around the target line. If no line number
    can be found — or the code is short — the full code is returned.
    Falls back to *fallback_line* when the stack trace has no match.
    """
    if not csharp_code:
        return ""
    lines = csharp_code.splitlines()
    total = len(lines)
    if total <= context_lines * 2:
        return csharp_code

    line_num: Optional[int] = None
    if stack_trace:
        if program_name:
            pat = rf"{re.escape(program_name)}\.cs\s*:\s*line\s+(\d+)"
            m = re.search(pat, stack_trace, re.IGNORECASE)
            if m:
                candidate = int(m.group(1))
                if 1 <= candidate <= total:
                    line_num = candidate
        if line_num is None:
            for m in re.findall(r"\.cs\s*:\s*line\s+(\d+)", stack_trace):
                n = int(m)
                if 1 <= n <= total:
                    line_num = n
                    break
    if line_num is None and fallback_line is not None:
        line_num = fallback_line if 1 <= fallback_line <= total else None
    if line_num is None:
        # No target-program frame in the stack trace (e.g. SQL errors
        # only show ExperLib/Sql frames). Return the file's first
        # ~200 lines instead of the previous ~10 — big enough to
        # include field/file declarations near the top of the class,
        # small enough to keep embedding queries focused.
        return "\n".join(lines[: min(total, 200)])

    start = max(0, line_num - 1 - context_lines)
    end = min(total, line_num - 1 + context_lines)
    return "\n".join(lines[start:end])


# ────────────────────────────────────────────────────────────────────
# Subroutine fault-site injection (stack-trace-driven)
#
# The AI currently sees the MAIN program's C# and COBOL — but a real
# runtime exception often originates inside a SUBROUTINE class the
# main program calls (Tablasn, Hgsr012p, Abmed, ...). Without the
# subroutine's own source AND its COBOL source of truth in the
# prompt, the AI ends up guessing at a fix in the wrong file, or
# rewriting the main program to work around a bug it can't see.
#
# This helper parses the .NET stack trace for non-framework, non-
# main-program frames, then loads each identified subroutine's C#
# (sliced around its own crash line) plus its COBOL source. The AI
# gets a dedicated section telling it "the fix likely belongs HERE,
# not in the main program".
#
# Zero cost when the stack trace is absent or contains only the main
# program / framework frames — section renders as empty string and
# the prompt is byte-identical to before.
# ────────────────────────────────────────────────────────────────────

_SUBROUTINE_MAX_INJECT = 3
_SUBROUTINE_CS_MAX_BYTES = 6000
_SUBROUTINE_COBOL_MAX_BYTES = 6000

# Root namespaces to treat as framework noise — we never fetch source
# for a frame whose first namespace component is one of these.
_STACK_FRAME_IGNORE_ROOT_NAMESPACES = {
    "system", "microsoft", "mscorlib", "netstandard",
    "experlib", "chainlit", "asyncio",
}


def _extract_subroutine_classes_from_stack(
    stack_trace: str,
    exclude_class: Optional[str] = None,
) -> List[str]:
    """Return PascalCase class names from ``at <ns>.<Class>.<Method>(...)``
    frames, in stack order (deepest first).

    The main program class (``exclude_class``) and framework namespaces
    are filtered out. Duplicates are de-duplicated preserving first
    occurrence (i.e. deepest occurrence wins).
    """
    if not stack_trace:
        return []
    exclude_lower = {(exclude_class or "").lower()}
    seen_lower: Set[str] = set()
    hits: List[str] = []
    for m in re.finditer(r"\bat\s+([A-Za-z0-9_.]+)\s*\(", stack_trace):
        qualified = m.group(1)
        parts = qualified.split(".")
        if len(parts) < 2:
            continue
        class_name = parts[-2]
        root_ns = parts[0].lower() if parts else ""
        if root_ns in _STACK_FRAME_IGNORE_ROOT_NAMESPACES:
            continue
        if not re.match(r"^[A-Z][A-Za-z0-9_]*$", class_name):
            continue
        key = class_name.lower()
        if key in exclude_lower or key in seen_lower:
            continue
        seen_lower.add(key)
        hits.append(class_name)
    return hits


def _slice_around_class_frame(
    csharp_code: str,
    stack_trace: str,
    class_name: str,
    context_lines: int = 30,
) -> str:
    """Slice *csharp_code* around the frame for *class_name*.

    Same logic as :func:`_slice_around_error_line` but the file marker
    matched in the stack trace is ``<class_name>.cs`` rather than the
    main program's file. Falls back to the first ~200 lines when no
    frame is found (usually the class's field declarations sit there).
    """
    if not csharp_code:
        return ""
    lines = csharp_code.splitlines()
    total = len(lines)
    if total <= context_lines * 2:
        return csharp_code

    line_num: Optional[int] = None
    if stack_trace and class_name:
        pat = rf"{re.escape(class_name)}\.cs\s*:\s*line\s+(\d+)"
        m = re.search(pat, stack_trace, re.IGNORECASE)
        if m:
            candidate = int(m.group(1))
            if 1 <= candidate <= total:
                line_num = candidate

    if line_num is None:
        return "\n".join(lines[: min(total, 200)])

    start = max(0, line_num - 1 - context_lines)
    end = min(total, line_num - 1 + context_lines)
    return "\n".join(lines[start:end])


def _load_subroutine_cs(class_name: str) -> Tuple[str, str]:
    """Return ``(path, source)`` for the given SSAB project class, or
    ``("", "")`` when not found. Reads FRESH from disk every call, so an
    edit is picked up by the next analysis with no cache invalidation.
    """
    index = _build_ssab_project_index() or {}
    path = index.get(class_name.lower())
    if not path or not os.path.exists(path):
        return ("", "")
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return (path, fh.read())
    except OSError:
        return ("", "")


class AnalysisAgent:
    """AI Analysis Agent for COBOL-to-C# migration issues."""
    
    def __init__(self):
        """Initialize the Analysis Agent."""
        self.base_path = Path(__file__).parent.parent
        self.prompts_config = self._load_prompts_config()
        self.skill_content = self._load_skill_file()
        
        self.cobol_service = CobolService()
        self.codebase_service = CodebaseService()
        self.kb_service = KnowledgeBaseService()
        self.llm_service = LLMService()
        
        self._sessions: Dict[str, AnalysisSession] = {}
        logger.info("AnalysisAgent initialized")
    
    def _load_prompts_config(self) -> Dict[str, Any]:
        """Load prompt templates from YAML."""
        yaml_path = self.base_path / "prompts" / "fix_suggestion.yaml"
        if not yaml_path.exists():
            logger.error(f"Prompts config not found: {yaml_path}")
            raise FileNotFoundError(f"Prompts config not found: {yaml_path}")
        with open(yaml_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        logger.debug(f"Loaded prompts config from {yaml_path}")
        return config
    
    def _load_skill_file(self) -> str:
        """Load SKILL.md as system prompt."""
        skill_path = self.base_path / "prompts" / "COBOL_TO_CSHARP_SKILL.md"
        if not skill_path.exists():
            logger.error(f"Skill file not found: {skill_path}")
            raise FileNotFoundError(f"Skill file not found: {skill_path}")
        with open(skill_path, 'r', encoding='utf-8') as f:
            content = f.read()
        logger.debug(f"Loaded skill file from {skill_path}")

        for rel in self.prompts_config.get("ims_skill_file", []) or []:
            extra_path = self.base_path / rel
            if not extra_path.exists():
                logger.warning(f"Extra skill file not found: {extra_path}")
                continue
            content += "\n\n---\n\n" + extra_path.read_text(encoding='utf-8')
            logger.debug(f"Appended extra skill file from {extra_path}")

        return content

    async def write_ims_queries(self, program_name: str, artifacts: Dict[str, str]) -> str:
        sections = [
            f"=== {title} ===\n{body}"
            for title, body in artifacts.items() if body
        ]
 
        user_prompt = (
            f"Write the correct ExperLib SELECT queries for the IMS DL/I "
            f"reads (GU/GN/GNP/GHU/GHNP) in program {program_name}. Follow "
            f"the select-ims procedure: resolve each SSA via the logical "
            f"DBD, use the physical DBD for hierarchy/cardinality, find each "
            f"target table from its .record, and emit the single-table "
            f"ExperLib Select()/Fetch(). For every DL/I read show a brief "
            f"trace, then the final C# query.\n\n" + "\n\n".join(sections)
        )
    
        logger.debug(
            f"write_ims_queries: program={program_name}, "
            f"artifacts={len(sections)}, chars={len(user_prompt)}"
        )
 
        return await self.llm_service.chat(
            system_prompt=self.skill_content,
            user_prompt=user_prompt,
            max_tokens=self.prompts_config.get(
                "ims_max_tokens", self.prompts_config.get("max_tokens", 4096)
            ),
            temperature=self.prompts_config.get("temperature", 0.2),
    )
    
    def get_session(self, baseline_name: str) -> Optional[AnalysisSession]:
        """Get active session for a baseline."""
        return self._sessions.get(baseline_name)
    
    def create_session(
        self,
        baseline_name: str,
        program_name: str,
        error_context: ErrorContext
    ) -> AnalysisSession:
        """Create a new analysis session."""
        session = create_analysis_session(
            baseline_name=baseline_name,
            program_name=program_name,
            error_context=error_context
        )
        self._sessions[baseline_name] = session
        logger.info(f"Created analysis session: {session.session_id} for {baseline_name}")
        return session
    
    def clear_session(self, baseline_name: str) -> None:
        """Clear/remove a session."""
        if baseline_name in self._sessions:
            del self._sessions[baseline_name]
            logger.info(f"Cleared session for {baseline_name}")
    
    async def gather_code_context(self, program_name: str) -> CodeContext:
        """Gather COBOL and C# code for a program."""
        logger.info(f"Gathering code context for {program_name}")
        context = CodeContext(program_name=program_name)
        
        try:
            cobol_results = self.cobol_service.search_hybrid(
                query=program_name,
                n_results=1
            )
            if cobol_results:
                best_match = cobol_results[0]
                metadata = best_match.get('metadata', {})
                context.cobol_file_path = metadata.get('full_path', '') or metadata.get('relative_path', f"{program_name}.COB")
                context.cobol_code = best_match.get('content', '')
                logger.debug(f"Found COBOL code: {context.cobol_file_path}")
        except Exception as e:
            logger.warning(f"Failed to fetch COBOL code: {e}")
        
        try:
            # ── Deterministic main-program file resolution (preferred) ──
            # The C# file for program HGTR103P is ``Hgtr103p.cs``. Resolve
            # it on disk by name and read its LIVE content FIRST, so the AI
            # always analyses the correct, current file. The fuzzy KB
            # ``search_hybrid`` below can return a NEIGHBOURING program
            # (e.g. Hgmq100p.cs for an HGTR103P query) or a STALE snapshot;
            # either makes the AI quote code that no longer matches the file
            # the auto-apply step edits — so every fix is refused with
            # "buggy snippet was not found". This deterministic lookup is
            # the authoritative source; the KB search is kept only as a
            # fallback and to locate the record/model file.
            try:
                main_hit = _find_file_in_repo(f"{program_name}.cs")
                if main_hit and os.path.isfile(main_hit):
                    context.csharp_code = _read_source_best_effort(main_hit)
                    context.csharp_file_path = main_hit
                    logger.info(
                        f"Resolved main C# file for {program_name} on disk: "
                        f"{main_hit}"
                    )
            except Exception as e:  # noqa: BLE001 — fall back to KB search
                logger.warning(
                    f"Deterministic C# resolution for {program_name} "
                    f"failed ({e}); falling back to KB search."
                )

            csharp_results = self.codebase_service.search_hybrid(
                query=program_name,
                n_results=3
            )
            for result in csharp_results:
                metadata = result.get('metadata', {})
                file_path = metadata.get('full_path', '') or metadata.get('relative_path', '')
                
                # Try to read LIVE file content first — always preferable to
                # the KB snapshot, which reflects the codebase as it was at
                # the last ``scripts/ingest_codebase.py`` run. If the KB
                # path is stale (repo was restructured, file was moved,
                # etc.) fall back to a repo-wide filesystem search by
                # basename BEFORE using the stale KB content — a stale
                # snapshot is the worst possible source because the LLM
                # will confidently quote code that no longer exists.
                content = ''
                resolved_path = file_path
                if file_path and os.path.exists(file_path):
                    try:
                        content = _read_source_best_effort(file_path)
                        logger.debug(f"Read live file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Failed to read live file {file_path}: {e}")
                        content = result.get('content', '')  # Fallback to KB
                else:
                    # Stale KB path — try a repo-wide search by basename
                    # so we still find the file after a repo restructure.
                    # Pass the stale path as a hint so the recovery
                    # prefers the copy whose deeper path segments match
                    # the KB entry (i.e. the real ``src/`` tree, not
                    # a ``conversion/`` scratch folder).
                    live_hit = _find_file_in_repo(
                        os.path.basename(file_path), hint_path=file_path
                    )
                    if live_hit and os.path.exists(live_hit):
                        try:
                            content = _read_source_best_effort(live_hit)
                            resolved_path = live_hit
                            logger.warning(
                                f"KB path '{file_path}' is stale (file not "
                                f"found); recovered via filesystem search "
                                f"at '{live_hit}'. Re-run "
                                f"``scripts/ingest_codebase.py --clear`` "
                                f"followed by ``scripts/ingest_codebase.py`` "
                                f"to refresh the codebase KB."
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to read recovered file {live_hit}: {e}"
                            )
                            content = result.get('content', '')
                    else:
                        # Last resort: use the KB snapshot. Log LOUDLY so
                        # this is not silent — a stale snapshot is the
                        # most misleading source we can hand to the LLM.
                        content = result.get('content', '')
                        logger.warning(
                            f"KB path '{file_path}' does not exist AND "
                            f"filesystem search for "
                            f"'{os.path.basename(file_path)}' found nothing "
                            f"in the SSAB.OX repo. Falling back to the KB "
                            f"snapshot (which may be OUT OF DATE and will "
                            f"cause the AI to quote stale code). "
                            f"Re-run ``scripts/ingest_codebase.py --clear`` "
                            f"followed by ``scripts/ingest_codebase.py`` "
                            f"to refresh the codebase KB."
                        )
                
                if resolved_path.lower().endswith('.cs') and 'record' not in resolved_path.lower():
                    if not context.csharp_code:
                        context.csharp_file_path = resolved_path
                        context.csharp_code = content
                        logger.debug(f"Found C# code: {context.csharp_file_path}")
                elif 'record' in resolved_path.lower():
                    context.csharp_record_path = resolved_path
                    context.csharp_record_code = content
                    logger.debug(f"Found C# record: {context.csharp_record_path}")
        except Exception as e:
            logger.warning(f"Failed to fetch C# code: {e}")
        
        return context
    
    async def search_similar_fixes(
        self,
        program_name: str,
        error_message: str,
        n_results: int = 5
    ) -> List[SimilarFix]:
        """Search KB for similar past fixes."""
        logger.info(f"Searching KB for similar fixes: {program_name}")
        similar_fixes = []
        
        try:
            safe_msg = (error_message or "")[:200]
            query = f"{program_name} {safe_msg}".strip()
            results = self.kb_service.search_hybrid(
                query=query,
                n_results=n_results
            )
            for result in results:
                metadata = result.get('metadata', {})
                files_str = metadata.get('files_changed', '')
                files_list = files_str.split(',') if files_str else []

                fix = SimilarFix(
                    pr_id=metadata.get('pr_id', 0),
                    baseline=metadata.get('baseline', ''),
                    ticket_id=metadata.get('ticket_id'),
                    files_changed=files_list,
                    commit_message=metadata.get('title', ''),
                    fix_description=metadata.get('title', ''),
                    code_diff=result.get('document', ''),  #NEW: Include code diff!
                    relevance_score=result.get('score', 0)
                )
                similar_fixes.append(fix)
            logger.debug(f"Found {len(similar_fixes)} similar fixes")
        except Exception as e:
            logger.warning(f"Failed to search KB: {e}")
        
        return similar_fixes
    
    def _build_prompt(
        self,
        prompt_type: str,
        error_context: ErrorContext,
        code_context: CodeContext,
        similar_fixes: List[SimilarFix],
        session: Optional[AnalysisSession] = None
    ) -> str:
        """Build the user prompt from template."""
        prompt_config = self.prompts_config.get(prompt_type, {})
        template = prompt_config.get('template', '')
        
        if not template:
            logger.error(f"No template found for prompt type: {prompt_type}")
            return ""
        
        stack_trace_section = ""
        if error_context.stack_trace:
            stack_trace_section = f"**Stack Trace:**\n```\n{error_context.stack_trace}\n```"
        
        csharp_record_section = ""
        if code_context.csharp_record_code:
            csharp_record_section = f"## C# Record/Model File\n**File:** {code_context.csharp_record_path}\n```csharp\n{code_context.csharp_record_code}\n```"
        
        similar_fixes_section = ""
        if similar_fixes:
            fixes_list = "\n\n".join([fix.to_prompt_text() for fix in similar_fixes[:3]])
            similar_fixes_section = f"## Similar Past Fixes (From Knowledge Base)\n\n{fixes_list}"

        # ── Baseline artefacts (evidence for file-type decisions) ────
        # Lists files the baseline actually reads/writes with their
        # filename marker (I/O/S) and CRLF/NULL sniff — concrete
        # evidence the AI can point at when deciding TextFile vs
        # FlatFile. Empty when the folder is missing.
        baseline_files_section = ""
        try:
            files_info = _list_baseline_files(error_context.baseline_name)
            if files_info:
                marker_hint = {
                    "I": "INPUT text file",
                    "O": "OUTPUT text file",
                    "S": "SEQ / binary file",
                    "":  "unknown marker",
                }
                bullet_lines = []
                for f in files_info:
                    sniff = []
                    if f["has_crlf"]:
                        sniff.append("CRLF present (text)")
                    if f["has_null"]:
                        sniff.append("NULL bytes (binary)")
                    sniff_str = "  —  " + "; ".join(sniff) if sniff else ""
                    bullet_lines.append(
                        f"- `{f['name']}`  ·  marker `{f['marker'] or '?'}`  "
                        f"·  {marker_hint.get(f['marker'], 'unknown')}"
                        f"{sniff_str}"
                    )
                baseline_files_section = (
                    "## Baseline Artefacts (input / output files this run reads)\n"
                    "Filename convention: `<num>-<seq>-<program>-<TYPE>-<DDNAME>.<ext>` "
                    "where TYPE is `I` (input text), `O` (output text) or `S` "
                    "(sequential/binary). CRLF-present corroborates text; NULL "
                    "bytes corroborate binary.\n\n"
                    "**Use this evidence when deciding runtime file wrappers.** "
                    "Look up the DD name (e.g. `DIN01`) referenced in the C# code "
                    "against the list below: matching file marked `I`/`O` OR with "
                    "CRLF-present indicates a TEXT file. Marker `S` OR NULL bytes "
                    "indicates a SEQUENTIAL/BINARY file. If no evidence is "
                    "available for a DD, do NOT change its declared runtime type.\n\n"
                    + "\n".join(bullet_lines)
                )
                logger.info(
                    f"Baseline artefacts: injected {len(files_info)} file(s)"
                )
        except Exception as exc:  # noqa: BLE001 — never break analysis
            logger.warning(f"Baseline files listing skipped: {exc}")
            baseline_files_section = ""

        # ── Curated Issues KB retrieval ───────────────────────────────
        # Query is a TIGHT slice of the C# code centred on the abend
        # line (±10 lines, program-file-specific) plus the top line of
        # the runtime error. The full stack trace is deliberately not
        # included — it dilutes the embedding away from KB entries whose
        # documents are pure code + short metadata.
        issues_section = ""
        try:
            code_region = _slice_around_error_line(
                code_context.csharp_code or "",
                error_context.stack_trace or "",
                error_context.line_number,
                context_lines=10,
                program_name=error_context.program_name,
            )
            _err_lines = (error_context.error_message or "").splitlines()
            err_line = _err_lines[0][:200] if _err_lines else ""
            issue_query_parts = [code_region[:6000]]
            if err_line:
                issue_query_parts.append(f"ERROR: {err_line}")
            issue_query = "\n".join(issue_query_parts).strip()
            if issue_query and hasattr(self.kb_service, "search_issues"):
                issue_hits = self.kb_service.search_issues(
                    query=issue_query,
                    n_results=5,
                    max_distance=1.3,
                )
                if issue_hits:
                    parts = []
                    for hit in issue_hits:
                        meta = hit.get("metadata", {}) or {}
                        parts.append(
                            "### `" + str(meta.get("issue_id", hit.get("id", "?"))) + "`  " +
                            "*(distance " + f"{hit.get('distance', 0):.3f}" + ")*\n" +
                            "**Title:** " + str(meta.get("title", "")) + "\n" +
                            "**Category:** `" + str(meta.get("bug_category", "")) + "`   " +
                            "**Tags:** " + str(meta.get("tags", "")) + "\n" +
                            (
                                "**Root cause:** " + str(meta.get("root_cause", "")) + "\n"
                                if meta.get("root_cause") else ""
                            ) +
                            "**Buggy pattern:**\n```csharp\n" +
                            str(meta.get("buggy_code", "")) + "\n```\n" +
                            "**Fix that resolved it:**\n```csharp\n" +
                            str(meta.get("fixed_code", "")) + "\n```"
                        )
                    issues_section = (
                        "## Candidate Known Issues (Curated KB) — HIGH PRIORITY\n"
                        "Each entry below is a **confirmed bug pattern from this "
                        "codebase**. The `Fix that resolved it` is proven — it has "
                        "already shipped in a merged PR.\n\n"
                        "**Before proposing any fix, you MUST complete this reasoning "
                        "step for each candidate:**\n\n"
                        "1. **Compare structurally.** Does the candidate's buggy "
                        "pattern show the SAME KIND OF MISTAKE as the current "
                        "failing code? Examples of structural match:\n"
                        "   - Both do the same kind of operation (both open/read a "
                        "file, both assign to a wrapper, both check a status flag).\n"
                        "   - Both make the same kind of error (checking the WRONG "
                        "object's flag, missing a container prefix, wrapping the RHS "
                        "in an unnecessary helper, etc.).\n"
                        "   - **Variable, field, class, method, and file names WILL "
                        "differ across programs. Different names do NOT disqualify "
                        "a match** — the shape of the code is what matters.\n"
                        "   - Even the specific flag name (`IsOk` vs `IsFinns` vs "
                        "`IsEof`), method name (`Read` vs `OpenForRead`), or type "
                        "name may differ. If the underlying pattern is the same, "
                        "it is a match.\n\n"
                        "2. **If it matches structurally:** adapt the fix to the "
                        "identifiers used in the CURRENT code. That adapted fix IS "
                        "your answer. Do NOT propose a different fix. Do NOT invent "
                        "type conversions, format strings, or defensive code that "
                        "isn't in the candidate's fix.\n\n"
                        "3. **If it does NOT match structurally:** state in one "
                        "sentence why not (which structural element differs), then "
                        "fall through to reason from first principles.\n\n"
                        "Do NOT skip step 1. If no reasoning about the candidates "
                        "appears in your analysis, the analysis is incomplete.\n\n"
                        + "\n\n".join(parts)
                    )
                    logger.info(
                        f"Issues KB: injected {len(issue_hits)} candidate issue(s)"
                    )
        except Exception as exc:  # noqa: BLE001 — never break analysis
            logger.warning(f"Issues KB retrieval skipped: {exc}")
            issues_section = ""
        
        # ── Subroutine fault-site injection (stack-trace-driven) ────
        # Additive: when the stack trace names classes OTHER than the
        # main program, pull their C# (sliced around the crash line)
        # and their COBOL source of truth. The AI can then reason about
        # the actual fault site, not just the main program.
        subroutine_sources_section = ""
        subroutine_names_injected: Set[str] = set()
        try:
            if error_context.stack_trace:
                exclude_class = ""
                if error_context.program_name:
                    exclude_class = error_context.program_name.title().replace("_", "")
                sub_classes = _extract_subroutine_classes_from_stack(
                    error_context.stack_trace,
                    exclude_class=exclude_class,
                )
                parts = []
                for cls in sub_classes[:_SUBROUTINE_MAX_INJECT]:
                    cs_path, cs_full = _load_subroutine_cs(cls)
                    if not cs_full:
                        continue
                    cs_slice = _slice_around_class_frame(
                        cs_full, error_context.stack_trace, cls, context_lines=30,
                    )
                    if len(cs_slice) > _SUBROUTINE_CS_MAX_BYTES:
                        cs_slice = cs_slice[:_SUBROUTINE_CS_MAX_BYTES] + "\n// ... (truncated)"

                    cobol_content = ""
                    cobol_path = ""
                    try:
                        # Preferred: exact program_name (== ChromaDB id)
                        # lookup — beats hybrid ranking when other files
                        # merely REFERENCE the subroutine and therefore
                        # outscore the file that IS the subroutine.
                        exact = self.cobol_service.get_by_program_name(cls)
                        if exact:
                            meta = exact.get("metadata", {}) or {}
                            cobol_path = (
                                meta.get("full_path", "")
                                or meta.get("relative_path", "")
                            )
                            cobol_content = exact.get("document", "") or ""
                        else:
                            # Fallback: hybrid search with filename
                            # sanity guard (rejects semantic false
                            # positives whose filename doesn't reference
                            # the class name).
                            cobol_hits = self.cobol_service.search_hybrid(
                                query=cls, n_results=1,
                            )
                            if cobol_hits:
                                top = cobol_hits[0]
                                meta = top.get("metadata", {}) or {}
                                candidate_path = (
                                    meta.get("full_path", "")
                                    or meta.get("relative_path", "")
                                )
                                cand_basename = os.path.basename(
                                    candidate_path or ""
                                ).lower()
                                if cls.lower() in cand_basename:
                                    cobol_path = candidate_path
                                    cobol_content = top.get("content", "") or ""
                                else:
                                    logger.debug(
                                        f"COBOL match for {cls} rejected: "
                                        f"filename {cand_basename!r} does not "
                                        f"reference the class name."
                                    )
                        if len(cobol_content) > _SUBROUTINE_COBOL_MAX_BYTES:
                            cobol_content = (
                                cobol_content[:_SUBROUTINE_COBOL_MAX_BYTES]
                                + "\n*** (truncated) ***"
                            )
                    except Exception as _sub_exc:  # noqa: BLE001
                        logger.debug(
                            f"COBOL lookup for subroutine {cls} skipped: {_sub_exc}"
                        )
                        cobol_content = ""

                    block = (
                        f"### Subroutine `{cls}` "
                        f"(called from main program; stack frame present)\n"
                        f"**C# file:** {cs_path}\n"
                        f"```csharp\n{cs_slice}\n```\n"
                    )
                    if cobol_content:
                        block += (
                            f"\n**COBOL source of truth for `{cls}`:** "
                            f"{cobol_path or 'N/A'}\n"
                            f"```cobol\n{cobol_content}\n```\n"
                        )
                    else:
                        block += (
                            f"\n**COBOL source for `{cls}` not found in KB.** "
                            f"Reason about the C# only; do not invent COBOL logic.\n"
                        )
                    parts.append(block)
                    subroutine_names_injected.add(cls.lower())

                if parts:
                    subroutine_sources_section = (
                        "## Subroutine Sources "
                        "(Called From Main Program — Suspected Fault Site)\n"
                        "The stack trace above points at one or more classes "
                        "called by the main program. Their sources are included "
                        "below. **If the deepest user-code frame is inside one "
                        "of these classes, the fix belongs THERE — name that "
                        "file in `### File to Fix`, not the main program.** "
                        "Compare each subroutine's C# against its COBOL source "
                        "of truth (when provided) exactly the same way you "
                        "compare the main program: the C# must MIRROR the "
                        "COBOL — no extra null checks, no defensive code, no "
                        "invented logic.\n\n"
                        + "\n\n".join(parts)
                    )
                    logger.info(
                        f"Subroutine sources: injected {len(parts)} subroutine(s) "
                        f"({', '.join(sub_classes[:len(parts)])})"
                    )
        except Exception as exc:  # noqa: BLE001 — never break analysis
            logger.warning(f"Subroutine source injection skipped: {exc}")
            subroutine_sources_section = ""

        # ── Live source injection (two-pass, additive, generic) ──────
        # Pass 1: Find LOCAL project types (SqlRows, records, helpers)
        #         referenced by the target C# code. Their contents are
        #         then folded into the scan text for pass 2 so ExperLib
        #         types they reference (e.g. StringField, DateType) are
        #         also injected — the AI gets the full type chain.
        # Pass 2: Find EXPERLIB runtime types referenced by any of the
        #         code above (target file + injected project types).
        # Both are silently no-ops when the corresponding repo path is
        # unset or the scan produces zero matches — the template
        # placeholders collapse cleanly.
        project_types_section = ""
        experlib_section = ""
        try:
            base_scan_text = "\n".join([
                error_context.error_message or "",
                error_context.stack_trace or "",
                error_context.diff_details or "",
                code_context.csharp_code or "",
            ])
            # Never re-inject the target program's own class or the
            # already-included record file (both are already in the
            # prompt above).
            exclude_names: Set[str] = set()
            if error_context.program_name:
                exclude_names.add(error_context.program_name)
                # Also the PascalCase form (e.g. HPPL547P -> Hppl547p).
                exclude_names.add(error_context.program_name.title().replace("_", ""))
            if code_context.csharp_record_path:
                exclude_names.add(
                    os.path.splitext(os.path.basename(code_context.csharp_record_path))[0]
                )
            # Subroutines already have their own dedicated section above
            # (sliced + with COBOL). Don't inject them again as generic
            # project types.
            for _sub in subroutine_names_injected:
                exclude_names.add(_sub)

            project_hits = _find_ssab_project_sources(
                base_scan_text, exclude_names=exclude_names,
            )
            if project_hits:
                parts = []
                for name, src in project_hits:
                    parts.append(
                        f"### `{name}.cs` (from SSAB.OX project — referenced by target program)\n"
                        f"```csharp\n{src}\n```"
                    )
                project_types_section = (
                    "## Referenced Project Types "
                    "(SqlRow / Record / Helper classes used by this program)\n"
                    "Each class below is REFERENCED by the target C# code. "
                    "Their **field type declarations** determine which "
                    "assignments in the target code are type-safe. Consult "
                    "them before proposing any assignment/cast fix — the "
                    "actual field type on the receiver often reveals the "
                    "real bug location.\n\n"
                    + "\n\n".join(parts)
                )
                # Expand scan text so ExperLib pass 2 sees any wrapper
                # type names mentioned in the project-type files
                # (StringField, DateType, IntField, etc.).
                expanded_scan_text = (
                    base_scan_text + "\n" +
                    "\n".join(src for _, src in project_hits)
                )
            else:
                expanded_scan_text = base_scan_text

            experlib_hits = _find_experlib_sources(expanded_scan_text)
            if experlib_hits:
                parts = []
                for name, src in experlib_hits:
                    parts.append(
                        f"### `{name}.cs` (from ExperLib runtime library)\n"
                        f"```csharp\n{src}\n```"
                    )
                experlib_section = (
                    "## ExperLib Runtime Source "
                    "(Dependencies Referenced by This Program)\n"
                    "The following ExperLib types are called by the C# code "
                    "above. Use their real API — do NOT guess parameter types, "
                    "constructor signatures, or method behavior; the source is "
                    "right here.\n\n"
                    + "\n\n".join(parts)
                )
        except Exception as exc:  # noqa: BLE001 — never break analysis
            logger.warning(f"Source injection skipped: {exc}")
            project_types_section = ""
            experlib_section = ""

        previous_attempts_section = ""
        # Only inject the "previous fix attempts (did not work)" section
        # when there is at least one attempt that is actually recorded
        # as FAILED. Attempts marked SUCCESS (i.e. previous fix worked
        # and unmasked a downstream bug) must NOT be shown as failed —
        # doing so would bias the AI toward regressing the earlier
        # correct change.
        if session and session.attempts:
            failed_attempts = [
                a for a in session.attempts
                if getattr(a, "status", None) == FixStatus.FAILED
            ]
            if failed_attempts:
                previous_attempts_section = (
                    "## Previous Fix Attempts (Did Not Work)\n"
                    f"{session.get_failed_fixes_summary()}"
                )
        
        try:
            prompt = template.format(
                program_name=error_context.program_name,
                baseline_name=error_context.baseline_name,
                exit_code=error_context.exit_code or "N/A",
                error_message=error_context.error_message,
                stack_trace_section=stack_trace_section,
                cobol_file_path=code_context.cobol_file_path or "N/A",
                cobol_code=code_context.cobol_code or "COBOL source not available",
                csharp_file_path=code_context.csharp_file_path or "N/A",
                csharp_code=code_context.csharp_code or "C# source not available",
                csharp_record_section=csharp_record_section,
                subroutine_sources_section=subroutine_sources_section,
                project_types_section=project_types_section,
                experlib_section=experlib_section,
                baseline_files_section=baseline_files_section,
                issues_section=issues_section,
                similar_fixes_section=similar_fixes_section,
                previous_attempts_section=previous_attempts_section,
                diff_details=error_context.diff_details or "N/A",
                failed_files=error_context.diff_details or "N/A",
                attempt_number=session.current_attempt + 1 if session else 1,
                max_attempts=session.max_attempts if session else 15,
                previous_attempts_details=session.get_failed_fixes_summary() if session else ""
            )
        except KeyError as e:
            logger.warning(f"Missing template variable: {e}")
            prompt = template
        
        return prompt
    
    def _parse_ai_response(self, response: str) -> FixSuggestion:
        """Parse AI response into FixSuggestion object."""
        logger.debug("Parsing AI response")
        
        root_cause = "Unable to determine root cause"
        explanation = ""
        file_to_fix = ""
        line_number = None
        original_code = ""
        suggested_code = ""
        confidence = 50
        fix_type = "other"
        additional_notes = ""
        
        match = re.search(r'### Root Cause\n(.+?)(?=\n\n|\n###)', response, re.DOTALL)
        if match:
            root_cause = match.group(1).strip()
        
        match = re.search(r'### Analysis\n(.+?)(?=\n### File)', response, re.DOTALL)
        if match:
            explanation = match.group(1).strip()
        
        match = re.search(r'### File to Fix\n(.+?)(?=\n)', response)
        if match:
            file_to_fix = match.group(1).strip()
            # LLMs sometimes wrap file paths in markdown backticks
            # (`` `C:\...\Foo.cs` ``) or matched quotes. Strip these
            # decorations so downstream consumers get a clean path.
            for _w in ("`", '"', "'"):
                if file_to_fix.startswith(_w) and file_to_fix.endswith(_w):
                    file_to_fix = file_to_fix[len(_w):-len(_w)].strip()
                    break
            if file_to_fix.startswith("<") and file_to_fix.endswith(">"):
                file_to_fix = file_to_fix[1:-1].strip()
        
        match = re.search(r'### Line Number\n(\d+|N/A)', response)
        if match:
            line_str = match.group(1).strip()
            if line_str != "N/A":
                try:
                    line_number = int(line_str)
                except ValueError:
                    pass
        
        match = re.search(r'### Original Code\n```(?:csharp)?\n(.+?)```', response, re.DOTALL)
        if match:
            original_code = match.group(1).strip()
        
        match = re.search(r'### Suggested Fix\n```(?:csharp)?\n(.+?)```', response, re.DOTALL)
        if match:
            suggested_code = match.group(1).strip()

        # A single fix may require several separate, non-adjacent edits in
        # the same file (e.g. a declaration change AND a reset change). The
        # AI emits one ``### Original Code`` / ``### Suggested Fix`` pair per
        # edit; collect every pair here, in order.
        orig_blocks = re.findall(
            r'### Original Code\n```(?:csharp)?\n(.+?)```', response, re.DOTALL
        )
        sugg_blocks = re.findall(
            r'### Suggested Fix\n```(?:csharp)?\n(.+?)```', response, re.DOTALL
        )
        edits: List[CodeEdit] = []
        for o, s in zip(orig_blocks, sugg_blocks):
            o, s = o.strip(), s.strip()
            if o and s:
                edits.append(CodeEdit(original_code=o, suggested_code=s))
        if edits:
            original_code = edits[0].original_code
            suggested_code = edits[0].suggested_code
        
        match = re.search(r'### Confidence\n(\d+)%?', response)
        if match:
            try:
                confidence = int(match.group(1))
            except ValueError:
                confidence = 50
        
        match = re.search(r'### Fix Type\n(.+?)(?=\n)', response)
        if match:
            fix_type = match.group(1).strip()
        
        match = re.search(r'### Notes\n(.+?)(?=\n###|$)', response, re.DOTALL)
        if match:
            additional_notes = match.group(1).strip()
        
        suggestion = FixSuggestion(
            root_cause=root_cause,
            explanation=explanation,
            file_to_fix=file_to_fix,
            line_number=line_number,
            original_code=original_code,
            suggested_code=suggested_code,
            edits=edits,
            confidence=confidence,
            fix_type=fix_type,
            additional_notes=additional_notes,
            requires_manual_review=(confidence < 70)
        )
        
        logger.debug(f"Parsed suggestion: confidence={confidence}%, file={file_to_fix}")
        return suggestion

    def _resolve_target_file(
        self,
        suggestion: FixSuggestion,
        code_context: CodeContext,
    ) -> None:
        """Rewrite ``suggestion.file_to_fix`` to a real on-disk path.

        The LLM sometimes emits a plausible-but-wrong directory for the
        target file (e.g. ``...\\SSAB.OX.Batch.Subroutines\\Hgtr103p.cs``
        when the file actually lives at
        ``...\\SSAB.OX.COMMON\\Programs\\Hgtr103p.cs``). Downstream the
        auto-apply/autopilot loop reads this path to snapshot and edit
        the file — a non-existent path makes the whole fix silently fail.

        Resolution order:
          1. If the AI path already exists on disk, keep it.
          2. If its basename matches the program's resolved C# file
             (``code_context.csharp_file_path``) and that exists, use it.
             This is the authoritative target because the AI quoted that
             file's content, so the ``original_code`` snippet only matches
             there.
          3. Otherwise recover by basename via :func:`_find_file_in_repo`.
          4. If nothing resolves, leave the path unchanged (downstream
             surfaces a clear "file not found" message, as before).
        """
        raw = (suggestion.file_to_fix or "").strip()
        if not raw:
            return
        if os.path.isfile(raw):
            return

        base = os.path.basename(raw)
        ctx_path = (getattr(code_context, "csharp_file_path", "") or "").strip()
        if (
            ctx_path
            and os.path.isfile(ctx_path)
            and os.path.basename(ctx_path).lower() == base.lower()
        ):
            logger.warning(
                f"AI file_to_fix '{raw}' not found on disk; using the "
                f"program's resolved C# file '{ctx_path}'."
            )
            suggestion.file_to_fix = ctx_path
            return

        recovered = _find_file_in_repo(base, hint_path=raw)
        if recovered and os.path.isfile(recovered):
            logger.warning(
                f"AI file_to_fix '{raw}' not found on disk; recovered "
                f"'{recovered}' by basename from the SSAB.OX repo."
            )
            suggestion.file_to_fix = recovered
            return

        logger.warning(
            f"AI file_to_fix '{raw}' not found on disk and no on-disk "
            f"copy of '{base}' could be recovered; leaving path unchanged."
        )

    async def analyze_runtime_error(
        self,
        baseline_name: str,
        program_name: str,
        error_message: str,
        exit_code: int = 1,
        stack_trace: Optional[str] = None
    ) -> FixSuggestion:
        """Analyze a runtime error and suggest a fix."""
        logger.info(f"Analyzing runtime error for {program_name}")
        
        error_context = create_error_context(
            program_name=program_name,
            baseline_name=baseline_name,
            error_type=ErrorType.RUN_FAILED,
            error_message=error_message,
            exit_code=exit_code,
            stack_trace=stack_trace
        )
        
        session = self.get_session(baseline_name)
        if not session:
            session = self.create_session(baseline_name, program_name, error_context)
        
        if not session.can_retry:
            session.mark_max_attempts()
            logger.warning(f"Max attempts reached for {baseline_name}")
            return FixSuggestion(
                root_cause="Maximum analysis attempts reached",
                explanation=f"After {session.max_attempts} attempts, the issue could not be automatically resolved.",
                file_to_fix="",
                confidence=0,
                requires_manual_review=True,
                additional_notes="Please review manually or create a bug ticket."
            )
        
        code_context = await self.gather_code_context(program_name)
        session.code_context = code_context
        
        similar_fixes = await self.search_similar_fixes(program_name, error_message)
        session.similar_fixes = similar_fixes
        
        # Decide retry vs fresh analysis by comparing the current error
        # against the previous attempt's error. If the error CHANGED,
        # the previous fix worked and merely unmasked a downstream bug —
        # treat this as a first-attempt analysis (fresh reasoning, no
        # "try a different approach" bias). If the SAME error persists,
        # the previous fix truly failed — use the retry prompt.
        is_retry = False
        if session.current_attempt > 0:
            prev_attempt = session.get_latest_attempt()
            prev_err = prev_attempt.error_context.error_message if prev_attempt else None
            if _is_new_error(prev_err, error_message):
                # Mark the previous attempt as SUCCESS — it worked, the
                # error surface just moved. This also makes the
                # previous_attempts_section in the prompt collapse.
                if prev_attempt is not None:
                    prev_attempt.mark_success(
                        "Previous fix resolved the earlier error; a new "
                        "error surfaced downstream and is now the focus."
                    )
                logger.info(
                    f"New error detected for {baseline_name} — treating "
                    f"as fresh analysis (previous fix marked successful)."
                )
            else:
                is_retry = True
                logger.info(
                    f"Same error persists for {baseline_name} — using "
                    f"retry_analysis prompt."
                )
        prompt_type = "retry_analysis" if is_retry else "analyze_runtime_error"
        
        user_prompt = self._build_prompt(
            prompt_type=prompt_type,
            error_context=error_context,
            code_context=code_context,
            similar_fixes=similar_fixes,
            session=session
        )
        
        logger.debug("Calling LLM for analysis")
        response = await self.llm_service.chat(
            system_prompt=self.skill_content,
            user_prompt=user_prompt,
            max_tokens=self.prompts_config.get('max_tokens', 4096),
            temperature=self.prompts_config.get('temperature', 0.2)
        )
        
        suggestion = self._parse_ai_response(response)
        suggestion.similar_fixes_used = similar_fixes[:3] if similar_fixes else []
        self._resolve_target_file(suggestion, code_context)
        
        session.add_attempt(error_context, suggestion)
        session.status = AnalysisStatus.FIX_SUGGESTED
        
        logger.info(f"Analysis complete: confidence={suggestion.confidence}%, attempt={session.current_attempt}/{session.max_attempts}")
        return suggestion
    
    async def analyze_output_diff(
        self,
        baseline_name: str,
        program_name: str,
        diff_details: str,
        failed_files: Optional[List[str]] = None
    ) -> FixSuggestion:
        """Analyze output differences and suggest a fix."""
        logger.info(f"Analyzing output diff for {program_name}")
        
        error_context = create_error_context(
            program_name=program_name,
            baseline_name=baseline_name,
            error_type=ErrorType.COMPARE_DIFF,
            error_message="Output comparison failed - differences found",
            diff_details=diff_details
        )
        
        session = self.get_session(baseline_name)
        if not session:
            session = self.create_session(baseline_name, program_name, error_context)
        
        if not session.can_retry:
            session.mark_max_attempts()
            logger.warning(f"Max attempts reached for {baseline_name}")
            return FixSuggestion(
                root_cause="Maximum analysis attempts reached",
                explanation=f"After {session.max_attempts} attempts, the issue could not be automatically resolved.",
                file_to_fix="",
                confidence=0,
                requires_manual_review=True,
                additional_notes="Please review manually or create a bug ticket."
            )
        
        code_context = await self.gather_code_context(program_name)
        session.code_context = code_context
        
        similar_fixes = await self.search_similar_fixes(program_name, diff_details[:200])
        session.similar_fixes = similar_fixes
        
        # Same retry-vs-fresh decision as analyze_runtime_error, but
        # comparing on ``diff_details`` (the signal that changes when
        # a previous fix modifies program output).
        is_retry = False
        if session.current_attempt > 0:
            prev_attempt = session.get_latest_attempt()
            prev_diff = prev_attempt.error_context.diff_details if prev_attempt else None
            if _is_new_error(prev_diff, diff_details):
                if prev_attempt is not None:
                    prev_attempt.mark_success(
                        "Previous fix changed the output; a new/remaining "
                        "difference is now the focus."
                    )
                logger.info(
                    f"Output diff changed for {baseline_name} — treating "
                    f"as fresh analysis (previous fix marked successful)."
                )
            else:
                is_retry = True
                logger.info(
                    f"Same output diff persists for {baseline_name} — "
                    f"using retry_analysis prompt."
                )
        prompt_type = "retry_analysis" if is_retry else "analyze_output_diff"
        
        user_prompt = self._build_prompt(
            prompt_type=prompt_type,
            error_context=error_context,
            code_context=code_context,
            similar_fixes=similar_fixes,
            session=session
        )
        
        logger.debug("Calling LLM for analysis")
        response = await self.llm_service.chat(
            system_prompt=self.skill_content,
            user_prompt=user_prompt,
            max_tokens=self.prompts_config.get('max_tokens', 4096),
            temperature=self.prompts_config.get('temperature', 0.2)
        )
        
        suggestion = self._parse_ai_response(response)
        suggestion.similar_fixes_used = similar_fixes[:3] if similar_fixes else []
        self._resolve_target_file(suggestion, code_context)
        
        session.add_attempt(error_context, suggestion)
        session.status = AnalysisStatus.FIX_SUGGESTED
        
        logger.info(f"Analysis complete: confidence={suggestion.confidence}%, attempt={session.current_attempt}/{session.max_attempts}")
        return suggestion
    
    async def retry_analysis(
        self,
        baseline_name: str,
        new_error_message: str,
        stack_trace: Optional[str] = None
    ) -> FixSuggestion:
        """Retry analysis after a failed fix attempt."""
        session = self.get_session(baseline_name)
        
        if not session:
            logger.error(f"No session found for {baseline_name}")
            raise ValueError(f"No active session for {baseline_name}. Start with analyze_runtime_error() first.")
        
        latest_attempt = session.get_latest_attempt()
        if latest_attempt:
            latest_attempt.mark_failed(new_error_message)
        
        return await self.analyze_runtime_error(
            baseline_name=session.baseline_name,
            program_name=session.program_name,
            error_message=new_error_message,
            stack_trace=stack_trace
        )
    
    def mark_fix_success(self, baseline_name: str) -> None:
        """Mark the current fix as successful."""
        session = self.get_session(baseline_name)
        if session:
            latest_attempt = session.get_latest_attempt()
            if latest_attempt:
                latest_attempt.mark_success()
            session.mark_success()
            logger.info(f"Fix successful for {baseline_name}")
    
    def get_session_summary(self, baseline_name: str) -> Optional[Dict[str, Any]]:
        """Get summary of analysis session."""
        session = self.get_session(baseline_name)
        if session:
            return session.to_summary()
        return None


_analysis_agent: Optional[AnalysisAgent] = None


def get_analysis_agent() -> AnalysisAgent:
    """Get singleton instance of AnalysisAgent."""
    global _analysis_agent
    if _analysis_agent is None:
        _analysis_agent = AnalysisAgent()
    return _analysis_agent