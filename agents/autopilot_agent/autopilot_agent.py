"""Autopilot engine — Copilot-style natural-language-driven fix agent.

Separate, additive feature. Reuses existing building blocks READ-ONLY
(orchestrator skills, the compare diff reader, the AnalysisAgent, the
auto-apply helper) and changes NONE of them. In particular it does not
touch the compare auto-fix loop (``app.py :: _autonomous_fix_loop``),
the compare skill, ``diff_reader``, ``analyze_output_diff`` or
``fix_suggestion.yaml``.

What lives here (UI-agnostic helpers only):
  * :func:`interpret_request` — LLM planner that reads the user's free
    text and extracts the target baseline + distilled guidance, or a
    question to ask when the baseline is missing.
  * :func:`compose_guided_diff_details` — wraps the compare skill's
    ``build_diff_details`` output with a HIGH-PRIORITY developer-guidance
    block so the human's hint reaches the analysis WITHOUT modifying the
    shared analysis agent or prompt template.
  * guidance memory (per-baseline hint persisted to ``data/``).
  * small pure predicates (exit command, pause-when-stuck thresholds).

The turn-by-turn Chainlit state machine + message rendering live in
``app.py :: handle_autopilot_turn`` / ``_autopilot_drive`` — kept there
to mirror the existing ``_autonomous_fix_loop`` structure and to keep
this module free of any Chainlit dependency.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
from utils.logger import get_logger

logger = get_logger("autopilot")

# Tunables (Autopilot-owned; independent of the compare auto-fix caps).
MAX_ITERATIONS = 10
LOW_CONFIDENCE_THRESHOLD = 60      # pause + ask the user below this
REPEAT_LIMIT = 2                   # same diff persisting N times → pause

_BASELINE_RE = re.compile(r"\bP\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?\b", re.IGNORECASE)

_HINTS_PATH = Path(config.BASE_DIR) / "data" / "autopilot_hints.json"

_EXIT_WORDS = {
    "/autopilot stop", "/stop", "stop autopilot", "exit autopilot",
    "cancel autopilot", "autopilot off",
}


def is_exit_command(text: str) -> bool:
    """True when the user asks to leave Autopilot mode."""
    low = (text or "").strip().lower()
    return low in _EXIT_WORDS


def extract_baseline(text: str) -> str:
    """Return the first baseline id in *text* (upper-cased) or ''."""
    m = _BASELINE_RE.search(text or "")
    return m.group(0).upper() if m else ""


# ────────────────────────────────────────────────────────────────────
# Guidance memory (per-baseline hint, persisted)
# ────────────────────────────────────────────────────────────────────


def _load_all_hints() -> Dict[str, str]:
    try:
        if _HINTS_PATH.exists():
            return json.loads(_HINTS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — best-effort cache
        logger.warning(f"Could not read autopilot hints: {exc}")
    return {}


def load_hint(baseline: str) -> str:
    """Return the remembered guidance for *baseline* (or '')."""
    if not baseline:
        return ""
    return _load_all_hints().get(baseline.upper(), "")


def save_hint(baseline: str, hint: str) -> None:
    """Persist (merge) the accumulated guidance for *baseline*."""
    if not baseline or not (hint or "").strip():
        return
    try:
        _HINTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = _load_all_hints()
        data[baseline.upper()] = hint.strip()[:4000]
        _HINTS_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — never break the loop
        logger.warning(f"Could not save autopilot hint: {exc}")

_PROGRAM_RE = re.compile(r"\b([A-Z]{1,6}\d{2,8}[A-Z]?)\b", re.IGNORECASE)
 
# The request must mention IMS together with a query/SQL/SELECT verb.
_IMS_QUERY_RE = re.compile(
    r"\bims\b.*\b(quer\w+|sql|select)\b|\b(quer\w+|sql|select)\b.*\bims\b",
    re.IGNORECASE | re.DOTALL,
)
 
# IMS physical segment names referenced by the program, e.g. JCKVS001.
_SEGMENT_RE = re.compile(r"\b([A-Z]{3,5}S\d{3})\b")
 
# Caps so one request can never blow the LLM context window.
_MAX_DBDICT_ROWS = 400
_MAX_RECORDS = 40
_MAX_CONTEXT_CHARS = 300_000

def detect_ims_query_request(text: str) -> Optional[str]:
    """Return the program id when *text* asks to write IMS queries for a
    program (no baseline needed), else ``None``.
 
    A baseline id in the text defers to the normal compare/fix flow.
    """
    s = (text or "").strip()
    if not s or not _IMS_QUERY_RE.search(s):
        return None
    if _BASELINE_RE.search(s):
        return None
    # Ignore the keywords themselves when picking the program token.
    candidates = [
        m.group(0).upper()
        for m in _PROGRAM_RE.finditer(s)
        if m.group(0).lower() not in {"ims", "sql"}
    ]
    return candidates[0] if candidates else None
 
 
def _find_cobol_file(program: str) -> Optional[Path]:
    """Locate ``<program>.COB`` under COBOL_SOURCE_PATH (case-insensitive)."""
    root = (config.COBOL_SOURCE_PATH or "").strip()
    if not root:
        return None
    base = Path(root)
    for ext in ("COB", "cob", "CBL", "cbl"):
        cand = base / f"{program}.{ext}"
        if cand.exists():
            return cand
    return None
 
 
def _dbd_root() -> Optional[Path]:
    """Return ``<SSAB_OX_REPO_PATH>/db/ims/DBD`` when configured and present."""
    root = (config.SSAB_OX_REPO_PATH or "").strip()
    if not root:
        return None
    p = Path(root) / "db" / "ims" / "DBD"
    return p if p.exists() else None


def _resolve_source_map(logical_dbd_texts: List[str]) -> Dict[str, List[str]]:
    """Map each logical `SEGM` name to the physical segment(s) in its
    `SOURCE=((seg,,dbd),...)` clause.

    A logical segment's real data lives in the FIRST physical segment of its
    `SOURCE=`; a two-part source is a logical relationship. These physical
    segments (e.g. `JAHAS012` behind logical `JAHAS201`) are frequently NOT
    named in the COBOL, so their table mapping would otherwise be missing.
    Handles fixed-format DBDs: continuation lines and trailing 6–8 digit
    sequence columns are stripped before parsing.
    """
    result: Dict[str, List[str]] = {}
    for text in logical_dbd_texts:
        cleaned = [
            re.sub(r"\*?\s*\d{6,8}\s*$", "", ln) for ln in text.splitlines()
        ]
        joined = " ".join(cleaned)
        for chunk in re.split(r"\bSEGM\b", joined)[1:]:
            nm = re.search(r"NAME=([A-Z0-9]+)", chunk)
            if not nm:
                continue
            sm = re.search(r"SOURCE=(\(\(.*?\)\))", chunk)
            if not sm:
                continue
            segs = re.findall(r"\(\s*([A-Z0-9]+)\s*,", sm.group(1))
            if segs:
                result[nm.group(1)] = segs
    return result


def gather_ims_artifacts(program: str) -> Dict[str, Any]:
    """Collect the COBOL + IMS DBD metadata needed to author queries.
 
    Returns ``{"ok": bool, "error": str, "program": str,
    "artifacts": {title: text}}``. Read-only, best-effort; a missing
    required artifact yields ``ok=False`` with a user-facing ``error``.
    """
    cob_path = _find_cobol_file(program)
    if not cob_path:
        where = config.COBOL_SOURCE_PATH or "(COBOL_SOURCE_PATH not set)"
        return {
            "ok": False, "program": program, "artifacts": {},
            "error": (
                f"I can't find the COBOL program `{program}.COB` under "
                f"`{where}`. Please confirm the program name or its location."
            ),
        }
 
    dbd_root = _dbd_root()
    if not dbd_root:
        root = config.SSAB_OX_REPO_PATH or "(SSAB_OX_REPO_PATH not set)"
        return {
            "ok": False, "program": program, "artifacts": {},
            "error": (
                f"I found the program but not the IMS DBD folder at "
                f"`{root}\\db\\ims\\DBD`. Please check SSAB_OX_REPO_PATH."
            ),
        }
 
    try:
        cobol = cob_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "ok": False, "program": program, "artifacts": {},
            "error": f"Could not read `{cob_path}`: {exc}",
        }
 
    segments = sorted(set(_SEGMENT_RE.findall(cobol)))

    # Collect DBDs into a local list first (not straight into the output) so
    # we can order the final context by value: the compact, high-value files
    # (DBDICT rows, .record tables) must never be starved by the bulky DBDs.
    dbd_entries: List[tuple] = []
    gathered_dbds: set = set()
    logical_dbd_texts: List[str] = []

    def _gather_dbds_for(prefixes) -> None:
        for pref in sorted(prefixes):
            for dbd in sorted(dbd_root.glob(f"{pref}*.DBD")):
                if dbd.name in gathered_dbds:
                    continue
                try:
                    text = dbd.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                dbd_entries.append((f"DBD {dbd.name}", text))
                gathered_dbds.add(dbd.name)
                if "ACCESS=LOGICAL" in text or "DATASET LOGICAL" in text:
                    logical_dbd_texts.append(text)

    _gather_dbds_for({s[:4] for s in segments})

    # Follow logical-relationship SOURCE= chains: a logical segment's real
    # data lives in the physical segment named in its SOURCE=. Those physical
    # segments (e.g. JAHAS012 behind logical JAHAS201) are often NOT named in
    # the COBOL, so their .record/DBDICT table mapping would be missing and
    # the model would guess the table. Pull them into the metadata set.
    source_map = _resolve_source_map(logical_dbd_texts)
    extra_segments: set = set()
    for seg in segments:
        for src in source_map.get(seg, []):
            if src not in segments:
                extra_segments.add(src)

    if extra_segments:
        # Pull in DBDs for any new database prefixes the sources introduce.
        _gather_dbds_for(
            {s[:4] for s in extra_segments} - {s[:4] for s in segments}
        )

    resolved_segments = sorted(set(segments) | extra_segments)

    # Assemble the context in priority order: COBOL → DBDICT rows → .record
    # tables → DBDs → key summary. The first three are compact and decisive
    # (they carry the FROM table and key columns), so they always survive the
    # context cap even for programs that touch many databases.
    artifacts: "OrderedDict[str, str]" = OrderedDict()
    artifacts[f"PROGRAM {program} (COBOL)"] = cobol

    # DBDICT.CSV — header plus rows for the resolved segments.
    dbdict = dbd_root / "DBDICT.CSV"
    if dbdict.exists():
        try:
            lines = dbdict.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                header = lines[0]
                rows = [
                    ln for ln in lines[1:]
                    if any(seg in ln for seg in resolved_segments)
                ][:_MAX_DBDICT_ROWS]
                artifacts["DBDICT.CSV (referenced segments)"] = (
                    header + "\n" + "\n".join(rows)
                )
        except OSError:
            pass

    # .record files for the resolved segments (Jckvs020.record style). Logical
    # segments often lack a .record (e.g. Jahas201.record), but the resolved
    # physical source (Jahas012.record) carries the real table= mapping.
    rec_count = 0
    for seg in resolved_segments:
        if rec_count >= _MAX_RECORDS:
            break
        rec = dbd_root / f"{seg.capitalize()}.record"
        if rec.exists():
            try:
                artifacts[f"{rec.name}"] = rec.read_text(
                    encoding="utf-8", errors="replace"
                )
                rec_count += 1
            except OSError:
                continue

    # DBDs (logical + physical) — bulkier, needed for hierarchy/SOURCE=.
    for title, text in dbd_entries:
        artifacts[title] = text

    # conversion/DBD.CSV — segment key summary.
    conv = Path(config.SSAB_OX_REPO_PATH) / "conversion" / "DBD.CSV"
    if conv.exists():
        try:
            artifacts["conversion/DBD.CSV"] = conv.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            pass

    # Enforce the overall context cap (keep earliest = most important).
    total = 0
    capped: "OrderedDict[str, str]" = OrderedDict()
    for title, body in artifacts.items():
        if total + len(body) > _MAX_CONTEXT_CHARS:
            body = body[: max(0, _MAX_CONTEXT_CHARS - total)]
        capped[title] = body
        total += len(body)
        if total >= _MAX_CONTEXT_CHARS:
            break

    return {
        "ok": True, "error": "", "program": program,
        "segments": resolved_segments, "artifacts": capped,
    }

# ────────────────────────────────────────────────────────────────────
# Planner — interpret the user's free-text request
# ────────────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = (
    "You are the Autopilot planner for a COBOL→C# validation tool. The "
    "user describes, in natural language, a baseline whose generated "
    "output files do not match the expected baseline, and may add hints "
    "(which field, what format, business rules, pasted data). Extract a "
    "compact plan as STRICT JSON with EXACTLY these keys:\n"
    '  "baseline": the baseline id like "P02070-HPPL486P" if present, else ""\n'
    '  "hint": a concise distillation of any guidance the user gave '
    "(formats, fields, rules, data) — else \"\"\n"
    '  "need_baseline": true if no baseline is known and you must ask\n'
    '  "question": the one question to ask the user if need_baseline, else ""\n'
    '  "explain_only": true if the user asked you to ONLY explain / '
    "diagnose / tell the root cause and NOT make or apply any code "
    'changes (e.g. "just explain", "don\'t do changes", "only root '
    'cause") — else false\n'
    "Return ONLY the JSON object, no prose, no code fences."
)

# Phrases that mean "diagnose only, do NOT change code".
_EXPLAIN_ONLY_RE = re.compile(
    r"(do ?n'?t|do not|dont|no|without)\s+(do|doing|make|making|apply|"
    r"applying|fix|fixing|chang\w*|modify|modifying|edit|editing|touch)"
    r"|just\s+(explain|tell|show|analy[sz]e|check|diagnos\w*)"
    r"|only\s+(explain|tell|analy[sz]e|diagnos\w*|root\s*cause)"
    r"|explain\s+(me\s+)?(the\s+)?root\s*cause"
    r"|what\s+(is|are)\s+the\s+(issue|issues|problem|root\s*cause)"
    r"|analy[sz]e\s+only|explain\s+only|diagnos\w*\s+only",
    re.IGNORECASE,
)


def _is_explain_only(text: str) -> bool:
    """True when the user asked to only explain / not change code."""
    return bool(_EXPLAIN_ONLY_RE.search(text or ""))


# Signals that a sentence carries actual fix guidance (a format, field,
# rule or concrete value) rather than a vague "it's not working" request.
_GUIDANCE_SIGNAL_RE = re.compile(
    r"\d"                                    # any digit (widths, values, dates)
    r"|\b(format|field|column|pad|padd|zero|"
    r"round|rounding|decimal|sign|trim|date|time|timestamp|"
    r"pic|picture|yyyy|mm|dd|hh|ss|d\d|"
    r"should|must|expected|instead|use|convert|map|mapping|"
    r"left|right|justif|uppercase|lowercase|prefix|suffix)\b",
    re.IGNORECASE,
)

# Vague-request words that, alone, never constitute guidance.
_VAGUE_ONLY_RE = re.compile(
    r"^(?:\s*(?:files?|data|output|they|it|this|that|are|is|not|"
    r"match|matching|matched|check|see|look|please|fix|wrong|"
    r"issue|problem|failing|fail|and|the|for|a|an|,|\.|'|\"|-)\s*)+$",
    re.IGNORECASE,
)


def _looks_like_guidance(text: str) -> bool:
    """True when *text* carries concrete fix guidance, not a vague request."""
    s = (text or "").strip()
    if len(s) < 8:
        return False
    if _VAGUE_ONLY_RE.match(s):
        return False
    return bool(_GUIDANCE_SIGNAL_RE.search(s))


async def interpret_request(
    user_text: str, active_baseline: Optional[str] = None
) -> Dict[str, Any]:
    """Parse the user's free-text Autopilot request.

    Returns ``{baseline, hint, need_baseline, question, explain_only}``.
    Falls back to a regex baseline extraction (and the active baseline)
    when the LLM is unavailable or returns unparseable output, so
    Autopilot still starts.
    """
    text = (user_text or "").strip()
    regex_baseline = extract_baseline(text) or (active_baseline or "")

    try:
        from services.llm_service import get_llm_service
        llm = get_llm_service()
        raw = await llm.chat(
            system_prompt=_PLANNER_SYSTEM,
            user_prompt=text or "(no description)",
            max_tokens=400,
            temperature=0.0,
        )
        parsed = _safe_json(raw)
    except Exception as exc:  # noqa: BLE001 — degrade to regex plan
        logger.warning(f"Autopilot planner LLM failed ({exc}); using regex plan")
        parsed = {}

    baseline = (parsed.get("baseline") or "").strip().upper() or regex_baseline
    hint = (parsed.get("hint") or "").strip()
    # Fallback ONLY when the leftover text looks like real guidance
    # (mentions a format/field/rule/value), never a vague request like
    # "files aren't matching, check it" — those are not guidance.
    if not hint:
        stripped = _BASELINE_RE.sub("", text).strip(" .,–-")
        if _looks_like_guidance(stripped):
            hint = stripped

    need_baseline = not baseline
    question = (parsed.get("question") or "").strip() or (
        "Which baseline should Autopilot work on? "
        "(e.g. P02070-HPPL486P)"
    )
    explain_only = bool(parsed.get("explain_only")) or _is_explain_only(text)
    return {
        "baseline": baseline,
        "hint": hint,
        "need_baseline": need_baseline,
        "question": question,
        "explain_only": explain_only,
    }


def _safe_json(raw: str) -> Dict[str, Any]:
    """Best-effort parse of a JSON object possibly wrapped in fences/prose."""
    if not raw:
        return {}
    s = raw.strip()
    # Strip ``` fences if present.
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # Grab the first {...} block.
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ────────────────────────────────────────────────────────────────────
# Guidance injection (zero-touch: embedded into diff_details)
# ────────────────────────────────────────────────────────────────────


def compose_guided_diff_details(diff_details: str, guidance: str) -> str:
    """Prepend a HIGH-PRIORITY developer-guidance block to ``diff_details``.

    ``diff_details`` is produced by the compare skill's
    ``build_diff_details`` (unchanged). Because the AnalysisAgent already
    injects ``diff_details`` into its prompt, embedding the guidance here
    reaches the model WITHOUT editing the analysis agent or the prompt
    template — honouring the zero-touch requirement.
    """
    guidance = (guidance or "").strip()
    if not guidance:
        return diff_details
    block = (
        "## DEVELOPER GUIDANCE (human-provided — HIGH PRIORITY)\n"
        "The developer supplied the following guidance about this "
        "mismatch. Treat it as authoritative INTENT: if it names a field, "
        "format, business rule, acceptable difference, or data, apply it — "
        "but still verify against the COBOL source of truth and do not "
        "invent code.\n\n"
        f"{guidance}\n\n"
        "---\n\n"
    )
    return block + (diff_details or "")


def should_pause_for_help(confidence: int, repeat_count: int) -> Optional[str]:
    """Return a reason string when Autopilot should stop and ask the user.

    Triggers on low confidence or the same diff persisting too long —
    the Copilot-style "ask when uncertain instead of thrashing" rule.
    """
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        return (
            f"low confidence ({confidence}%) — I'm not certain this fix is "
            f"right"
        )
    if repeat_count >= REPEAT_LIMIT:
        return (
            "the same difference persists after my last change — my current "
            "approach isn't converging"
        )
    return None


__all__ = [
    "MAX_ITERATIONS",
    "LOW_CONFIDENCE_THRESHOLD",
    "REPEAT_LIMIT",
    "compose_guided_diff_details",
    "extract_baseline",
    "interpret_request",
    "is_exit_command",
    "load_hint",
    "save_hint",
    "should_pause_for_help",
]
