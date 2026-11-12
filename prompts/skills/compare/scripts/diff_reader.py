"""Mismatch-file reader for the compare auto-fix loop.

Reads the artefacts CompareX.ps1 writes under ``baseline/REPORT/`` for
each failing file:

* ``<name>-delta.txt`` — top-N Left-only (expected / baseline) and
  Right-only (generated / C#) records. Compact, pre-sorted, perfect
  as LLM diff context.
* ``<name>.html`` — Beyond Compare full side-by-side report. Too big
  for the LLM; surfaced to the user as a clickable path.

This module stays inside the compare skill folder (vertical-module
convention) and is lazily imported by callers through
``prompts.skills.compare`` so the main compare path pays zero cost
when the auto-fix loop is not used.
"""

from __future__ import annotations

import csv
import io
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from utils.logger import get_logger

logger = get_logger("compare.diff_reader")

_MAX_RECORDS_PER_SIDE = 25
_MAX_CHARS_PER_RECORD = 400
# Field-level diff caps (keep the LLM prompt lean on wide/large files).
_MAX_ALIGNED_DIFFS = 20
_MAX_FIELD_DIFFS_PER_ROW = 12
# Delimiters tried (in order) when a non-.csv record still looks delimited.
_CANDIDATE_DELIMITERS = (",", ";", "|", "\t")


def _report_dir(baseline_name: str) -> Path:
    return Path(config.get_report_path(baseline_name))


def _stem(file_name: str) -> str:
    return Path(file_name).stem


def html_report_path(baseline_name: str, failed_file: str) -> Optional[str]:
    """Return the on-disk path of the Beyond Compare HTML report, or None."""
    if not failed_file:
        return None
    path = _report_dir(baseline_name) / f"{_stem(failed_file)}.html"
    return str(path) if path.exists() else None


def delta_file_path(baseline_name: str, failed_file: str) -> Optional[str]:
    """Return the on-disk path of the CompareX ``-delta.txt`` snippet, or None."""
    if not failed_file:
        return None
    path = _report_dir(baseline_name) / f"{_stem(failed_file)}-delta.txt"
    return str(path) if path.exists() else None


def _parse_delta(text: str) -> Tuple[List[str], List[str]]:
    """Split a ``-delta.txt`` body into (left_only, right_only) records.

    CompareX writes exactly:
        File: <name>
        ---- Left-only (top N) ----
        <records>
        ---- Right-only (top N) ----
        <records>
    """
    left: List[str] = []
    right: List[str] = []
    section = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        low = line.lower()
        if low.startswith("file:"):
            continue
        if "left-only" in low:
            section = "left"
            continue
        if "right-only" in low:
            section = "right"
            continue
        if section == "left":
            left.append(line)
        elif section == "right":
            right.append(line)
    return left, right


def _truncate(records: List[str]) -> List[str]:
    out: List[str] = []
    for rec in records[:_MAX_RECORDS_PER_SIDE]:
        if len(rec) > _MAX_CHARS_PER_RECORD:
            rec = rec[:_MAX_CHARS_PER_RECORD] + " …(truncated)"
        out.append(rec)
    return out


# ────────────────────────────────────────────────────────────────────
# Beyond Compare HTML side-by-side parser
# ────────────────────────────────────────────────────────────────────
# CompareX writes a ``<table class="fc">`` whose rows are triples
# ``left | center-indicator | right``. The ``td`` class marks row
# status (``TextItemSame`` / ``TextItemSigMod`` / ``TextItemSigOrphan``
# …) and inline ``<span class="TextSeg*Diff">`` marks the exact changed
# characters. That alignment is what pairs a baseline row with its
# generated counterpart — the delta file keeps them in separate lists.


class _BCTableParser(HTMLParser):
    """Collect ``<table class="fc">`` rows from a Beyond Compare report."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[Dict[str, Any]]] = []
        self._in_fc = False
        self._row: Optional[List[Dict[str, Any]]] = None
        self._cell: Optional[Dict[str, Any]] = None
        self._in_seg = False
        self._seg_buf: List[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D401
        d = dict(attrs)
        cls = d.get("class", "") or ""
        if tag == "table" and "fc" in cls.split():
            self._in_fc = True
        elif tag == "tr" and self._in_fc:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "cls": cls, "segs": []}
        elif tag == "span" and self._cell is not None and "Diff" in cls:
            self._in_seg = True
            self._seg_buf = []

    def handle_endtag(self, tag):
        if tag == "span" and self._in_seg:
            seg = "".join(self._seg_buf).replace("\xa0", " ")
            if seg:
                self._cell["segs"].append(seg)  # type: ignore[index]
            self._in_seg = False
            self._seg_buf = []
        elif tag == "td" and self._cell is not None:
            self._cell["text"] = (
                "".join(self._cell["text"]).replace("\xa0", " ")
            )
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            # Keep only rows that actually differ (any cell flagged
            # Mod/Orphan/Add/Del). Dropping identical rows here bounds
            # memory + classify cost on large all-matching reports.
            if any(
                any(k in c["cls"] for k in ("Mod", "Orphan", "Add", "Del"))
                for c in self._row
            ):
                self.rows.append(self._row)
            self._row = None
        elif tag == "table" and self._in_fc:
            self._in_fc = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._in_seg:
                self._seg_buf.append(data)


def _classify_row(cells: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Turn a parsed 3-cell row into ``{status,left,right,*_segs}``.

    ``status`` is ``same`` / ``modified`` / ``left_orphan`` /
    ``right_orphan``. Returns ``None`` for spacer rows (<3 cells).
    """
    if len(cells) < 3:
        return None
    left, _center, right = cells[0], cells[1], cells[2]
    lt, rt = left["text"], right["text"]
    lcls, rcls = left["cls"], right["cls"]
    l_has, r_has = lt.strip() != "", rt.strip() != ""
    if "Orphan" in lcls or "Del" in lcls or (l_has and not r_has):
        status = "left_orphan"
    elif "Orphan" in rcls or "Add" in rcls or (r_has and not l_has):
        status = "right_orphan"
    elif lt == rt:
        status = "same"
    else:
        status = "modified"
    return {
        "status": status,
        "left": lt,
        "right": rt,
        "left_segs": left["segs"],
        "right_segs": right["segs"],
    }


def parse_html_report(html_path: str) -> List[Dict[str, Any]]:
    """Return the non-identical aligned rows from a Beyond Compare report.

    Each item: ``{status,left,right,left_segs,right_segs}``. Returns an
    empty list on any read/parse failure — never raises.
    """
    try:
        text = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        logger.warning(f"Could not read HTML report {html_path}: {exc}")
        return []
    parser = _BCTableParser()
    try:
        parser.feed(text)
    except Exception as exc:  # noqa: BLE001 — malformed HTML never breaks analysis
        logger.warning(f"HTML parse failed for {html_path}: {exc}")
        return []
    out: List[Dict[str, Any]] = []
    for cells in parser.rows:
        row = _classify_row(cells)
        if row and row["status"] != "same":
            out.append(row)
    return out


# ────────────────────────────────────────────────────────────────────
# Field-level diff (which column / character range differs)
# ────────────────────────────────────────────────────────────────────


def _split_delimited(s: str, delimiter: str) -> List[str]:
    """Split one record respecting CSV quoting when delimiter is comma."""
    if delimiter == ",":
        try:
            return next(csv.reader(io.StringIO(s)))
        except Exception:  # noqa: BLE001 — fall back to naive split
            return s.split(",")
    return s.split(delimiter)


def _detect_delimiter(name: str, left: str, right: str) -> Optional[str]:
    """Return the field delimiter for a record, or ``None`` (fixed-width)."""
    if name.lower().endswith(".csv"):
        return ","
    for delim in _CANDIDATE_DELIMITERS:
        if delim in left and delim in right:
            return delim
    return None


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix_len(a: str, b: str, floor: int) -> int:
    i = 0
    limit = min(len(a), len(b)) - floor
    while i < limit and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def describe_field_diff(name: str, left: str, right: str) -> Dict[str, Any]:
    """Locate the differing field(s) between an aligned baseline/generated row.

    Returns ``{kind, diffs}`` where ``kind`` is ``"field"`` (delimited)
    or ``"char"`` (fixed-width). For ``field``: ``diffs`` is a list of
    ``(column_1based, expected, generated)``. For ``char``: a single
    ``(start_1based, end_1based, expected_segment, generated_segment)``.
    """
    delim = _detect_delimiter(name, left, right)
    if delim is not None:
        lf = _split_delimited(left, delim)
        rf = _split_delimited(right, delim)
        diffs: List[Tuple[int, str, str]] = []
        for i in range(max(len(lf), len(rf))):
            lv = lf[i] if i < len(lf) else "<missing>"
            rv = rf[i] if i < len(rf) else "<missing>"
            if lv != rv:
                diffs.append((i + 1, lv, rv))
            if len(diffs) >= _MAX_FIELD_DIFFS_PER_ROW:
                break
        return {"kind": "field", "diffs": diffs}
    # Fixed-width: report the single changed character run.
    p = _common_prefix_len(left, right)
    s = _common_suffix_len(left, right, p)
    lseg = left[p: len(left) - s]
    rseg = right[p: len(right) - s]
    return {"kind": "char", "diffs": [(p + 1, len(left) - s, lseg, rseg)]}


def _build_aligned_diffs(
    name: str, html_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Enrich HTML-aligned rows with field-level diff info (capped)."""
    out: List[Dict[str, Any]] = []
    for row in html_rows:
        if len(out) >= _MAX_ALIGNED_DIFFS:
            break
        if row["status"] == "modified":
            field = describe_field_diff(name, row["left"], row["right"])
            out.append({
                "status": "modified",
                "left": row["left"][:_MAX_CHARS_PER_RECORD],
                "right": row["right"][:_MAX_CHARS_PER_RECORD],
                "field_kind": field["kind"],
                "field_diffs": field["diffs"],
            })
        else:  # orphan rows have no counterpart to field-diff against
            out.append({
                "status": row["status"],
                "left": row["left"][:_MAX_CHARS_PER_RECORD],
                "right": row["right"][:_MAX_CHARS_PER_RECORD],
                "field_kind": None,
                "field_diffs": [],
            })
    return out


def read_failed_file(baseline_name: str, failed_file: str) -> Dict[str, Any]:
    """Load the delta snippet + HTML path for one failing file.

    Returns a dict with:
      * ``name``        — the failing file name as reported by compare
      * ``left_only``   — expected (baseline) records only on the LEFT
      * ``right_only``  — generated (C#) records only on the RIGHT
      * ``delta_path``  — path to ``<name>-delta.txt`` (or None)
      * ``html_path``   — path to Beyond Compare HTML report (or None)
      * ``aligned_diffs`` — HTML-aligned baseline↔generated rows with the
        exact differing column/char range (empty if no HTML report)
      * ``html_parsed`` — bool, True when the HTML report yielded rows
      * ``has_delta``   — bool (True when delta OR HTML produced any diff)
    """
    delta_path = delta_file_path(baseline_name, failed_file)
    html_path = html_report_path(baseline_name, failed_file)

    left: List[str] = []
    right: List[str] = []
    if delta_path:
        try:
            text = Path(delta_path).read_text(encoding="utf-8", errors="ignore")
            left, right = _parse_delta(text)
        except OSError as exc:
            logger.warning(f"Could not read delta file {delta_path}: {exc}")

    aligned_diffs: List[Dict[str, Any]] = []
    html_parsed = False
    if html_path:
        html_rows = parse_html_report(html_path)
        if html_rows:
            html_parsed = True
            aligned_diffs = _build_aligned_diffs(failed_file, html_rows)

    return {
        "name": failed_file,
        "left_only": _truncate(left),
        "right_only": _truncate(right),
        "delta_path": delta_path,
        "html_path": html_path,
        "aligned_diffs": aligned_diffs,
        "html_parsed": html_parsed,
        "has_delta": bool(left or right or aligned_diffs),
    }


def pick_top_failed_file(failed_files: List[str]) -> Optional[str]:
    """Choose which failing file to analyse first.

    Strategy: AFTER-CSV failures first (schema-aligned, cleanest diff),
    then plain .csv, then anything else. Preserves list order within a
    tier so the user sees failures in the same order the compare output
    reported them.
    """
    if not failed_files:
        return None
    csv_files = [f for f in failed_files if f.lower().endswith(".csv")]
    if csv_files:
        return csv_files[0]
    return failed_files[0]


def _format_aligned_section(name: str, aligned: List[Dict[str, Any]]) -> List[str]:
    """Render the FIELD-LEVEL section from HTML-aligned diffs.

    Only emitted for DELIMITED files (CSV): column-number mapping helps
    the LLM there. For fixed-width txt/seq the whole record IS the
    value, so a positional char-range would push the model toward
    hand-rolled PadLeft/PadRight hacks instead of correct COBOL-PIC
    formatting — so those fall back to the whole-record view below.
    """
    lines: List[str] = []
    modified = [
        a for a in aligned
        if a["status"] == "modified" and a.get("field_kind") == "field"
    ]
    if not modified:
        return lines
    lines.append(
        "FIELD-LEVEL DIFFERENCES (aligned baseline↔generated rows — the "
        "EXACT column that differs):"
    )
    for idx, a in enumerate(modified, 1):
        lines.append("")
        lines.append(f"Row {idx}:")
        lines.append(f"  expected (baseline): {a['left']}")
        lines.append(f"  generated (C#)     : {a['right']}")
        for col, lv, rv in a["field_diffs"]:
            lines.append(
                f"  → column {col}: expected {lv!r}, generated {rv!r}"
            )
    return lines


def build_diff_details(record: Dict[str, Any]) -> str:
    """Format a ``read_failed_file`` record into the ``diff_details``
    string the AnalysisAgent expects.

    The output leads with HTML-derived FIELD-LEVEL differences (the
    exact column/char that changed) when available, then the
    whole-record MISSING/EXTRA view from the delta file. Both sources
    are used together so the LLM can pinpoint the offending field and
    still see added/removed records.
    """
    name = record.get("name", "<unknown>")
    left = record.get("left_only") or []
    right = record.get("right_only") or []
    aligned = record.get("aligned_diffs") or []

    lines: List[str] = []
    lines.append(f"File with mismatching records: {name}")
    lines.append("")

    field_section = _format_aligned_section(name, aligned)
    if field_section:
        lines.extend(field_section)
        lines.append("")
        lines.append(
            "NOTE: column numbers are 1-based positions in the record. "
            "Map each to its field using the COBOL/C# record layout in "
            "context, then fix the code that writes THAT field."
        )
        lines.append("")

    lines.append(
        f"Records present ONLY in the EXPECTED (baseline) file "
        f"— generator is MISSING these (top {len(left)}):"
    )
    if left:
        lines.extend(f"  - {rec}" for rec in left)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        f"Records present ONLY in the GENERATED (C#) output "
        f"— generator produced these but baseline does NOT have them "
        f"(top {len(right)}):"
    )
    if right:
        lines.extend(f"  - {rec}" for rec in right)
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(
        "GOAL: adjust the C# program so the generated output matches "
        "the expected baseline for this file — correct the differing "
        "field(s) above and add/remove records as needed."
    )
    return "\n".join(lines)


__all__ = [
    "build_diff_details",
    "delta_file_path",
    "describe_field_diff",
    "html_report_path",
    "parse_html_report",
    "pick_top_failed_file",
    "read_failed_file",
]
