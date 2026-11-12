"""
Auto-apply helper for AI-suggested fixes.

Provides a single pure function :func:`apply_fix_to_file` that safely
replaces an ``original_code`` snippet with a ``suggested_code`` snippet
in a target file, while:

  * detecting and preserving CRLF vs LF line endings,
  * tolerating differences in leading indentation between the AI's
    output and the file on disk, and
  * refusing to apply when the match is ambiguous (multiple hits)
    or missing (zero hits).

Undo relies on the surrounding git repository — no ``.bak`` copies
are written.

This module is intentionally standalone (no side effects at import,
no config coupling) so it does not affect any other part of the
codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


# ── Result type ─────────────────────────────────────────────────────
@dataclass
class ApplyResult:
    """Outcome of an auto-apply attempt.

    Attributes
    ----------
    ok:
        True when the file was successfully modified. False on any
        problem (ambiguous match, missing match, IO error, ...).
    message:
        Human-readable summary suitable for surfacing back to the UI.
    matches_found:
        How many locations in the file matched the original snippet.
        0 → not found; 1 → applied; >1 → refused (ambiguous).
    line_start:
        1-based starting line number of the replaced block in the
        pre-edit file (only populated when ``ok`` is True).
    """

    ok: bool
    message: str
    matches_found: int = 0
    line_start: Optional[int] = None


# ── Internal helpers ────────────────────────────────────────────────
def _detect_newline(raw: str) -> str:
    """Return the dominant newline sequence in *raw*.

    Defaults to the platform default when the string has no newlines.
    """
    if "\r\n" in raw:
        return "\r\n"
    if "\n" in raw:
        return "\n"
    if "\r" in raw:
        return "\r"
    import os
    return os.linesep


def _split_significant_lines(block: str) -> List[str]:
    """Split *block* into lines and drop leading / trailing blank lines.

    Interior blank lines are kept (they are meaningful for matching).
    Each returned line is stripped of leading / trailing whitespace so
    downstream comparison is indentation-agnostic.
    """
    raw_lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stripped = [ln.strip() for ln in raw_lines]
    # Trim outer blanks.
    while stripped and stripped[0] == "":
        stripped.pop(0)
    while stripped and stripped[-1] == "":
        stripped.pop()
    return stripped


def _find_matches(
    file_lines: List[str], needle_lines: List[str],
) -> List[int]:
    """Return every 0-based starting index in *file_lines* where the
    consecutive stripped lines equal *needle_lines*.

    Both sides are compared after ``str.strip()`` on each line so
    indentation differences don't defeat the match.
    """
    if not needle_lines:
        return []
    n = len(needle_lines)
    hits: List[int] = []
    for i in range(len(file_lines) - n + 1):
        candidate = [file_lines[j].strip() for j in range(i, i + n)]
        if candidate == needle_lines:
            hits.append(i)
    return hits


# ── Statement-level fuzzy matcher (line-wrap tolerant) ───────────────
#
# Many LLMs reformat multi-line statements onto a single line when
# reporting the "original" code snippet. When that happens the strict
# per-line matcher above fails even though the semantics are identical.
# The functions below give a fallback matcher that:
#   * splits both the file region and the needle into logical
#     statements (delimited by ';'),
#   * normalizes internal whitespace so line-wrap and extra spacing
#     are irrelevant,
#   * returns the file's line-range covering the matched statement
#     sequence so we can replace exactly those lines in place.

import re as _re  # local alias — keep module-top-level 'shutil' import intact


def _iter_statements(
    file_lines: List[str],
) -> List[Tuple[int, int, str]]:
    """Walk *file_lines* and collect consecutive lines into logical
    statements terminated by ``;``.

    Returns a list of ``(start_line_idx, end_line_idx, normalized_text)``
    tuples where indices are 0-based and inclusive, and the normalized
    text has all whitespace runs collapsed to a single space.

    Lines that don't participate in any ``;``-terminated statement
    (e.g. braces, comments, blank lines) are emitted as their own
    single-line entry with the raw stripped text so the fuzzy matcher
    can still align at those anchors when necessary.
    """
    out: List[Tuple[int, int, str]] = []
    n = len(file_lines)
    i = 0
    while i < n:
        line = file_lines[i]
        stripped = line.strip()
        if stripped == "":
            i += 1
            continue
        if ";" in stripped:
            # Single-line statement (common case).
            norm = _re.sub(r"\s+", " ", stripped).strip()
            out.append((i, i, norm))
            i += 1
            continue
        # Multi-line: consume until we find a ';' or hit end.
        buf = [stripped]
        start = i
        i += 1
        while i < n:
            nxt = file_lines[i].strip()
            if nxt == "":
                i += 1
                continue
            buf.append(nxt)
            if ";" in nxt:
                break
            i += 1
        norm = _re.sub(r"\s+", " ", " ".join(buf)).strip()
        out.append((start, i, norm))
        i += 1
    return out


def _needle_statements(needle_block: str) -> List[str]:
    """Return the AI's snippet split into ``;``-terminated statements
    with whitespace collapsed. Empty entries are dropped.
    """
    raw = needle_block.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = _re.sub(r"\s+", " ", raw).strip()
    parts: List[str] = []
    for chunk in collapsed.split(";"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk + ";")
    return parts


def _find_fuzzy_matches(
    file_statements: List[Tuple[int, int, str]],
    needle: List[str],
) -> List[Tuple[int, int]]:
    """Return every ``(start_line, end_line)`` (0-based inclusive) in
    the file where a consecutive sequence of statements equals
    *needle* (whitespace-normalized). Same-length sequence match.
    """
    if not needle:
        return []
    m = len(needle)
    hits: List[Tuple[int, int]] = []
    for i in range(len(file_statements) - m + 1):
        window_norm = [file_statements[i + k][2] for k in range(m)]
        if window_norm == needle:
            start_line = file_statements[i][0]
            end_line = file_statements[i + m - 1][1]
            hits.append((start_line, end_line))
    return hits


def _leading_indent(line: str) -> str:
    """Return the leading-whitespace prefix of *line* (``""`` if none)."""
    stripped = line.lstrip()
    return line[: len(line) - len(stripped)]


def _reindent_block(block: str, indent: str) -> List[str]:
    """Re-indent every non-blank line of *block* with *indent*.

    The block's own relative indentation (interior structure) is
    preserved by first computing the minimum leading whitespace across
    its non-blank lines and using that as the block's baseline. The
    caller-supplied *indent* is then prepended after that baseline is
    stripped.
    """
    raw_lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Determine the block's own baseline indent.
    non_blank = [ln for ln in raw_lines if ln.strip()]
    if not non_blank:
        return raw_lines
    baseline = min(len(_leading_indent(ln)) for ln in non_blank)
    out: List[str] = []
    for ln in raw_lines:
        if ln.strip():
            out.append(indent + ln[baseline:])
        else:
            # Preserve blank lines but strip any stray whitespace.
            out.append("")
    return out


# ── Public API ──────────────────────────────────────────────────────
def _normalize_file_path(file_path: str) -> str:
    """Strip markdown/quote/brace decorations the AI often wraps paths in.

    e.g. ```` `C:\\...\\Hppl547p.cs` ```` or ``<C:\\...\\Foo.cs>`` → the
    bare filesystem path.
    """
    file_path = (file_path or "").strip()
    for wrapper in ("`", '"', "'"):
        if file_path.startswith(wrapper) and file_path.endswith(wrapper):
            file_path = file_path[len(wrapper) : -len(wrapper)].strip()
    if file_path.startswith("<") and file_path.endswith(">"):
        file_path = file_path[1:-1].strip()
    return file_path


def apply_fix_to_file(
    file_path: str,
    original_code: str,
    suggested_code: str,
) -> ApplyResult:
    """Replace *original_code* with *suggested_code* inside *file_path*.

    Behaviour:

    * The original snippet is matched with **indentation-tolerant**
      line-by-line comparison. Interior whitespace between tokens on
      a line must still match exactly — only leading / trailing
      whitespace per line is ignored.
    * If exactly one match is found, the file is rewritten in place.
      The dominant line-ending style of the file (CRLF or LF) is
      preserved; the block's leading indentation on disk is applied
      to every line of the replacement.
    * If zero or multiple matches are found, the file is left
      untouched and the result explains why.

    Never raises for expected failure modes; unexpected IO errors
    are captured into ``ApplyResult.ok = False``.
    """
    if not file_path:
        return ApplyResult(ok=False, message="No file path provided.")
    if not original_code or not original_code.strip():
        return ApplyResult(ok=False, message="Original code snippet is empty.")
    if suggested_code is None:
        return ApplyResult(ok=False, message="Suggested code snippet is missing.")

    # Normalize the path — the AI often wraps file paths in markdown
    # backticks (``File to Fix: `C:\...\Hppl547p.cs``) or matched
    # quotes, and may include a leading/trailing brace. Strip these
    # decorations so the path is a plain filesystem string.
    file_path = _normalize_file_path(file_path)

    path = Path(file_path)
    if not path.is_file():
        return ApplyResult(
            ok=False,
            message=f"Target file not found on disk: {file_path}",
        )

    try:
        # Read as bytes then decode ourselves so Python's text-mode
        # newline translation doesn't strip the CRLF sequences we
        # need to detect and preserve on Windows source files.
        raw_bytes = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(
            ok=False,
            message=f"Could not read target file: {exc}",
        )

    # Legacy SSAB source files can be UTF-8, UTF-8 with BOM, or
    # Windows-1252 (Swedish characters like å ä ö at 0xE5 0xE4 0xF6).
    # Try each in turn; the encoding that succeeds is remembered so we
    # write the file back in the SAME encoding — never silently
    # convert a Windows-1252 file to UTF-8.
    encoding_used: Optional[str] = None
    raw: Optional[str] = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            raw = raw_bytes.decode(enc)
            encoding_used = enc
            break
        except UnicodeDecodeError:
            continue
    if raw is None or encoding_used is None:
        return ApplyResult(
            ok=False,
            message=(
                "Could not decode target file — tried UTF-8 (with/without BOM), "
                "Windows-1252 and Latin-1. Please apply the fix manually."
            ),
        )

    newline = _detect_newline(raw)
    file_lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    needle = _split_significant_lines(original_code)
    if not needle:
        return ApplyResult(
            ok=False,
            message="Original code snippet had no non-blank lines after trimming.",
        )

    hits = _find_matches(file_lines, needle)
    match_mode = "strict"
    match_start: int = 0
    match_end_exclusive: int = 0

    if hits and len(hits) == 1:
        match_start = hits[0]
        match_end_exclusive = hits[0] + len(needle)
    else:
        # Strict match failed — try the whitespace-normalized,
        # statement-based fallback so line-wrapped or reformatted
        # snippets still align. Ambiguous strict matches (len > 1)
        # also fall through: the fuzzy matcher may still resolve to
        # a single unambiguous statement block.
        statements = _iter_statements(file_lines)
        needle_stmts = _needle_statements(original_code)
        fuzzy_hits = (
            _find_fuzzy_matches(statements, needle_stmts)
            if needle_stmts
            else []
        )
        if not fuzzy_hits:
            if hits and len(hits) > 1:
                return ApplyResult(
                    ok=False,
                    matches_found=len(hits),
                    message=(
                        f"The buggy snippet appears {len(hits)} times "
                        "in the file — refusing to auto-apply an "
                        "ambiguous edit. Please apply manually."
                    ),
                )
            return ApplyResult(
                ok=False,
                matches_found=0,
                message=(
                    "The buggy snippet was not found in the file. It may "
                    "have already been changed, or the AI's snippet does "
                    "not match exactly. Please apply manually."
                ),
            )
        if len(fuzzy_hits) > 1:
            return ApplyResult(
                ok=False,
                matches_found=len(fuzzy_hits),
                message=(
                    f"The buggy snippet matches {len(fuzzy_hits)} "
                    "locations in the file — refusing to auto-apply an "
                    "ambiguous edit. Please apply manually."
                ),
            )
        match_mode = "fuzzy"
        start_line, end_line = fuzzy_hits[0]
        match_start = start_line
        match_end_exclusive = end_line + 1

    start = match_start
    n = match_end_exclusive - match_start

    # Pick the indent of the first non-blank line inside the matched
    # region so the replacement lines up with the surrounding code.
    indent = ""
    for j in range(start, match_end_exclusive):
        if file_lines[j].strip():
            indent = _leading_indent(file_lines[j])
            break

    new_block_lines = _reindent_block(suggested_code, indent)

    new_lines = file_lines[:start] + new_block_lines + file_lines[start + n:]
    new_content = newline.join(new_lines)

    # Re-encode with the SAME encoding the file was read in so we
    # never silently transcode a Windows-1252 file to UTF-8 (which
    # would corrupt Swedish characters). ``utf-8-sig`` is a lenient
    # UTF-8 that also matches plain-ASCII files without a BOM — so
    # don't infer "had a BOM" from that decoder alone. Instead check
    # the raw bytes for a real BOM at the start.
    had_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    write_encoding = "utf-8" if encoding_used == "utf-8-sig" else encoding_used
    try:
        encoded = new_content.encode(write_encoding, errors="strict")
    except UnicodeEncodeError:
        # Fall back to the original encoding with replacement so we
        # never crash mid-apply. This is a last resort; the read side
        # should already have chosen an encoding capable of round-trip.
        encoded = new_content.encode(write_encoding, errors="replace")
    if had_bom and not encoded.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded

    try:
        # Write as bytes so no OS-level newline translation touches
        # the sequences we deliberately joined with the detected style.
        path.write_bytes(encoded)
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(
            ok=False,
            message=f"Failed to write updated file: {exc}",
        )

    return ApplyResult(
        ok=True,
        matches_found=1,
        message=f"Applied fix at line {start + 1}.",
        line_start=start + 1,
    )


def apply_fixes_to_file(
    file_path: str,
    edits: List[Tuple[str, str]],
) -> ApplyResult:
    """Apply several ``(original_code, suggested_code)`` edits to one file.

    Some fixes require several separate, non-adjacent edits in the same
    file (e.g. converting a file-read buffer from ``String`` to
    ``Record`` changes both the declaration and the reset line, and
    neither alone compiles). Each edit is applied with the same
    indentation-tolerant matching as :func:`apply_fix_to_file`.

    The operation is **all-or-nothing**: a byte snapshot is taken first,
    and if any edit fails to match cleanly the file is restored to its
    original content so it is never left half-edited (which would not
    compile).

    A single edit is delegated straight to :func:`apply_fix_to_file` so
    its exact behaviour and messages are preserved.
    """
    pairs = [
        (o, s)
        for (o, s) in (edits or [])
        if o and o.strip() and s is not None
    ]
    if not pairs:
        return ApplyResult(ok=False, message="No edits provided.")
    if len(pairs) == 1:
        return apply_fix_to_file(file_path, pairs[0][0], pairs[0][1])

    norm_path = _normalize_file_path(file_path)
    path = Path(norm_path)
    if not path.is_file():
        return ApplyResult(
            ok=False,
            message=f"Target file not found on disk: {norm_path}",
        )
    try:
        snapshot = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return ApplyResult(ok=False, message=f"Could not read target file: {exc}")

    first_line: Optional[int] = None
    for idx, (orig, sug) in enumerate(pairs, start=1):
        result = apply_fix_to_file(norm_path, orig, sug)
        if not result.ok:
            # Roll back every edit applied so far so the file is never
            # left in a half-edited, non-compiling state.
            try:
                path.write_bytes(snapshot)
            except Exception:  # noqa: BLE001 — best-effort restore
                pass
            return ApplyResult(
                ok=False,
                matches_found=result.matches_found,
                message=(
                    f"Edit {idx} of {len(pairs)} could not be applied "
                    f"({result.message}) — all changes rolled back; file "
                    f"left unchanged."
                ),
            )
        if first_line is None:
            first_line = result.line_start

    return ApplyResult(
        ok=True,
        matches_found=len(pairs),
        message=f"Applied {len(pairs)} edits.",
        line_start=first_line,
    )


__all__ = ["ApplyResult", "apply_fix_to_file", "apply_fixes_to_file"]
