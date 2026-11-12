"""Mocked smoke test for the Autopilot engine helpers + drive loop.

Does NOT need Chainlit UI, dotnet, pwsh, Beyond Compare or an LLM. It
swaps cl.Message/cl.Action before importing app (same approach as
scripts/_smoke_autofix.py), mocks the orchestrator/analysis/compare
collaborators, and drives ``app._autopilot_drive`` through its session
state machine.

Scenarios:
  1. Guided success — one iteration converges using the user's hint.
  2. Low-confidence pause — engine stops and asks the user.
  3. Guidance injection — compose_guided_diff_details embeds the hint.

Run: ``python scripts/_smoke_autopilot.py``
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chainlit as cl

_sent: List[str] = []
_session: Dict[str, Any] = {}


class _FakeMessage:
    def __init__(self, content: str = "", actions=None, author: str = "", **_):
        self.content = content
        self.actions = actions or []

    async def send(self):
        _sent.append(self.content)
        return self

    async def update(self):
        return self


class _FakeAction:
    def __init__(self, name: str = "", payload=None, label: str = "", **_):
        self.name = name
        self.payload = payload or {}
        self.label = label


class _FakeUserSession:
    def get(self, key, default=None):
        return _session.get(key, default)

    def set(self, key, value):
        _session[key] = value


cl.Message = _FakeMessage  # type: ignore[assignment,misc]
cl.Action = _FakeAction    # type: ignore[assignment,misc]
cl.user_session = _FakeUserSession()  # type: ignore[assignment,misc]

import app  # noqa: E402

# Redirect Autopilot guidance memory to a throwaway file so tests never
# write to the real data/autopilot_hints.json.
import agents.autopilot_agent.autopilot_agent as _ap_mod  # noqa: E402
_ap_mod._HINTS_PATH = Path(tempfile.gettempdir()) / "_smoke_autopilot_hints.json"

ORIGINAL = (
    "public class Hppl486p {\n"
    "    void N90() {\n"
    "        Display(\"X\");\n"
    "    }\n"
    "}\n"
)


def _sugg(path: str, orig: str, sug: str, conf: int = 95) -> SimpleNamespace:
    return SimpleNamespace(
        original_code=orig, suggested_code=sug, confidence=conf,
        file_to_fix=path, root_cause="label missing value",
        explanation="x", fix_type="output_format", additional_notes="",
        line_number=3, similar_fixes_used=[], requires_manual_review=False,
    )


def _mk_orch(script: List[Tuple[str, Tuple[bool, Dict[str, Any]]]]) -> MagicMock:
    it = iter(script)

    async def run_skill(op, session):
        exp_op, res = next(it)
        assert op == exp_op, f"expected {exp_op!r}, got {op!r}"
        return res

    m = MagicMock()
    m.run_skill = run_skill
    return m


def _mk_agent(suggestions: List[SimpleNamespace]) -> MagicMock:
    it = iter(suggestions)
    a = MagicMock()

    async def analyze_output_diff(**_):
        return next(it)

    a.analyze_output_diff = analyze_output_diff
    a.mark_fix_success = MagicMock()
    a.get_session.return_value = SimpleNamespace(current_attempt=1, max_attempts=15)
    return a


def _mk_compare_facade(record: Dict[str, Any]):
    return SimpleNamespace(
        read_failed_file=MagicMock(return_value=record),
        pick_top_failed_file=MagicMock(side_effect=lambda fs: fs[0] if fs else None),
        build_diff_details=MagicMock(return_value="DIFF BLOCK"),
    )


def _xrf_ok():
    return SimpleNamespace(ok=True, skipped=False, message="")


async def _drive(cs_path, suggestions, orch_script, record, hint="use D5"):
    _sent.clear()
    _session.clear()
    cs_path.write_text(ORIGINAL, encoding="utf-8")
    _session["autopilot"] = {
        "phase": "running", "baseline": "P50070-HPPL486P", "program": "HPPL486P",
        "hint": hint, "iteration": 0, "applied": [], "snapshots": {},
        "last_sig": "", "repeat": 0, "started_compare": False, "current_failed": [],
    }
    _session["autopilot_active"] = True

    orch = _mk_orch(orch_script)
    agent = _mk_agent(suggestions)
    fake_compare = _mk_compare_facade(record)

    with patch.object(app, "get_orchestrator", return_value=orch), \
         patch.object(app, "get_analysis_agent", return_value=agent), \
         patch.object(app, "session_manager", MagicMock()), \
         patch.object(app, "update_xrf_from_cs_edit", return_value=_xrf_ok()), \
         patch.dict(sys.modules, {"prompts.skills.compare": fake_compare}):
        await app._autopilot_drive()

    return _session.get("autopilot") or {}, "\n".join(_sent), cs_path.read_text(encoding="utf-8")


async def main() -> None:
    from agents.autopilot_agent import compose_guided_diff_details, should_pause_for_help

    with tempfile.TemporaryDirectory() as td:
        cs = Path(td) / "Hppl486p.cs"

        # ── Scenario 1: guided success ────────────────────────────────
        print("─" * 56)
        print("Scenario 1: guided success in one iteration")
        rec = {"name": "SYSOUT.txt", "left_only": ["X 00004"], "right_only": ["X"],
               "aligned_diffs": [], "has_delta": True}
        state, log, disk = await _drive(
            cs,
            suggestions=[_sugg(str(cs), 'Display("X");', 'Display("X" + c.ToString("D5"));', 98)],
            orch_script=[
                ("compare", (True, {"all_pass": False, "failed_files": ["SYSOUT.txt"],
                                     "after_csv_fail": 1, "root_mixed_fail": 0})),
                ("run", (True, {"exit_code": 0})),
                ("unload", (True, "ok")),
                ("compare", (True, {"all_pass": True, "after_csv_pass": 1,
                                     "root_mixed_pass": 2, "failed_files": []})),
            ],
            record=rec,
        )
        assert 'ToString("D5")' in disk, "fix not on disk"
        assert "Verified — All Files Match" in log, "no success card"
        assert state["applied"][-1]["status"] == "verified"
        assert _session.get("autopilot_active") is False
        print("  ✅ fix applied, success rendered, autopilot disabled")

        # ── Scenario 2: low-confidence pause ──────────────────────────
        print("─" * 56)
        print("Scenario 2: low confidence → pause and ask")
        state, log, disk = await _drive(
            cs,
            suggestions=[_sugg(str(cs), 'Display("X");', 'Display("?");', 30)],
            orch_script=[
                ("compare", (True, {"all_pass": False, "failed_files": ["SYSOUT.txt"],
                                     "after_csv_fail": 1, "root_mixed_fail": 0})),
            ],
            record=rec,
        )
        assert "Autopilot needs your input" in log, "did not pause"
        assert state["phase"] == "await_user"
        assert disk == ORIGINAL, "file edited despite low-confidence pause"
        print("  ✅ paused, asked user, file untouched")

        # ── Scenario 3: guidance injection ────────────────────────────
        print("─" * 56)
        print("Scenario 3: compose_guided_diff_details embeds hint")
        out = compose_guided_diff_details("DIFF BLOCK", "counts are PIC 9(5)")
        assert "DEVELOPER GUIDANCE" in out and "PIC 9(5)" in out and "DIFF BLOCK" in out
        assert compose_guided_diff_details("D", "") == "D", "empty hint must pass through"
        assert should_pause_for_help(30, 0) is not None
        assert should_pause_for_help(95, 0) is None
        assert should_pause_for_help(95, 2) is not None
        print("  ✅ guidance embedded; thresholds correct")

        print("─" * 56)
        print("ALL AUTOPILOT SCENARIOS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
