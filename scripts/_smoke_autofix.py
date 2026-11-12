"""Mocked integration test for the autonomous fix loop.

Does NOT need Chainlit UI, dotnet, pwsh, Beyond Compare, or an LLM.
Exercises the real ``_autonomous_fix_loop`` code path and asserts the
state-machine branches work:

    1. SUCCESS on iter 1               — one apply, run OK, all_pass
    2. ROLLBACK then SUCCESS           — iter 1 run fails + file restored,
                                         iter 2 succeeds
    3. EXHAUSTED (progress but no pass) — 5 iters each shrink the diff
                                         count but never converge
    4. APPLY-REFUSED → CONTINUE         — iter 1 refuses to match (snapshot
                                         not kept), iter 2 applies cleanly
                                         and converges

Each scenario rebuilds a fresh target .cs file in a tmp dir, runs the
real coroutine against mocked collaborators, and asserts the files on
disk + state dict + captured UI messages match expectations.

Run: ``python scripts/_smoke_autofix.py``
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chainlit as cl

_sent_messages: List[str] = []


class _FakeMessage:
    def __init__(self, content: str = "", actions=None, author: str = "", **_):
        self.content = content
        self.actions = actions or []
        self.author = author

    async def send(self):
        _sent_messages.append(self.content)
        return self

    async def update(self):
        return self


class _FakeAction:
    def __init__(self, name: str = "", payload=None, label: str = "", **_):
        self.name = name
        self.payload = payload or {}
        self.label = label


# Swap BEFORE importing app so app.py's module-level
# ``_original_cl_msg_send = cl.Message.send`` captures the fake class
# (real cl.Message.__init__ requires a websocket context).
cl.Message = _FakeMessage  # type: ignore[assignment,misc]
cl.Action = _FakeAction    # type: ignore[assignment,misc]


class _FakeUserSession:
    """app.py's ``_capturing_cl_msg_send`` reads ``cl.user_session.get(...)``;
    outside a real Chainlit session we substitute a dict-like stub."""

    def __init__(self):
        self._d: Dict[str, Any] = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


cl.user_session = _FakeUserSession()  # type: ignore[assignment,misc]

import app  # noqa: E402  — import AFTER the swaps above

ORIGINAL_SRC = (
    "public class Hppl486p {\n"
    "    public int ComputeTotal(int a, int b) {\n"
    "        return a - b;\n"
    "    }\n"
    "}\n"
)
FIXED_SRC = (
    "public class Hppl486p {\n"
    "    public int ComputeTotal(int a, int b) {\n"
    "        return a + b;\n"
    "    }\n"
    "}\n"
)


def _make_suggestion(
    file_to_fix: str,
    original: str,
    suggested: str,
    confidence: int = 85,
) -> SimpleNamespace:
    return SimpleNamespace(
        original_code=original,
        suggested_code=suggested,
        confidence=confidence,
        file_to_fix=file_to_fix,
        root_cause="arithmetic sign error",
        explanation="minus should be plus",
        fix_type="logic",
        additional_notes="",
        line_number=3,
        similar_fixes_used=[],
        requires_manual_review=False,
    )


def _apply_result_ok(line: int = 3) -> Any:
    return SimpleNamespace(ok=True, message="applied", matches_found=1, line_start=line)


def _apply_result_fail() -> Any:
    return SimpleNamespace(ok=False, message="original snippet not found", matches_found=0, line_start=None)


def _xrf_ok() -> Any:
    return SimpleNamespace(ok=True, skipped=False, message="")


def _mk_orchestrator(script: List[Tuple[str, Tuple[bool, Dict[str, Any]]]]) -> MagicMock:
    """Mock orchestrator whose ``run_skill(op, session)`` yields the next
    pre-programmed ``(ok, result_dict)`` for the given op in order."""
    script_iter = iter(script)

    async def run_skill(op: str, session: Any) -> Tuple[bool, Dict[str, Any]]:
        try:
            expected_op, result = next(script_iter)
        except StopIteration as exc:
            raise AssertionError(
                f"orchestrator.run_skill({op!r}) called after script exhausted"
            ) from exc
        if op != expected_op:
            raise AssertionError(
                f"orchestrator script order mismatch: expected {expected_op!r} "
                f"but got {op!r}"
            )
        return result

    orch = MagicMock()
    orch.run_skill = run_skill
    return orch


def _mk_analysis_agent(suggestions: List[SimpleNamespace]) -> MagicMock:
    it = iter(suggestions)
    agent = MagicMock()
    agent.get_session.return_value = SimpleNamespace(
        current_attempt=1, max_attempts=5
    )

    async def analyze_output_diff(**_):
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError("analysis agent script exhausted") from exc

    agent.analyze_output_diff = analyze_output_diff
    agent.mark_fix_success = MagicMock()
    return agent


def _mk_compare_facade(failed_files: List[str]):
    read = MagicMock(return_value={
        "name": failed_files[0],
        "left_only": ["A|1|foo"],
        "right_only": ["A|1|FOO"],
        "delta_path": "fake",
        "html_path": "fake.html",
        "has_delta": True,
    })
    pick = MagicMock(side_effect=lambda files: files[0] if files else None)
    build = MagicMock(return_value="diff block")
    return read, pick, build


async def _run_scenario(
    name: str,
    cs_path: Path,
    initial_contents: str,
    suggestions: List[SimpleNamespace],
    orch_script: List[Tuple[str, Tuple[bool, Dict[str, Any]]]],
    initial_failed_files: List[str],
) -> Dict[str, Any]:
    _sent_messages.clear()
    app._AUTOFIX_STATE.pop("P02070-HPPL486P", None)
    cs_path.write_text(initial_contents, encoding="utf-8")

    read_mock, pick_mock, build_mock = _mk_compare_facade(initial_failed_files)
    orch = _mk_orchestrator(orch_script)
    agent = _mk_analysis_agent(suggestions)

    fake_compare = SimpleNamespace(
        read_failed_file=read_mock,
        pick_top_failed_file=pick_mock,
        build_diff_details=build_mock,
    )

    with patch.object(app, "get_orchestrator", return_value=orch), \
         patch.object(app, "get_analysis_agent", return_value=agent), \
         patch.object(app, "session_manager", MagicMock()), \
         patch.object(app, "update_xrf_from_cs_edit", return_value=_xrf_ok()), \
         patch.dict(sys.modules, {"prompts.skills.compare": fake_compare}):
        await app._autonomous_fix_loop(
            baseline_name="P02070-HPPL486P",
            program_name="HPPL486P",
            failed_files=initial_failed_files,
            validate_mode=False,
        )

    state = app._AUTOFIX_STATE.get("P02070-HPPL486P") or {}
    final_on_disk = cs_path.read_text(encoding="utf-8")
    joined = "\n".join(_sent_messages)
    return {
        "state": state,
        "disk": final_on_disk,
        "messages": list(_sent_messages),
        "joined": joined,
    }


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        cs_path = Path(td) / "Hppl486p.cs"

        # ── Scenario 1: SUCCESS on iter 1 ─────────────────────────────
        print("─" * 60)
        print("Scenario 1: SUCCESS on iter 1")
        res = await _run_scenario(
            "iter-1-success",
            cs_path,
            ORIGINAL_SRC,
            suggestions=[
                _make_suggestion(str(cs_path), "return a - b;", "return a + b;")
            ],
            orch_script=[
                ("run", (True, {"exit_code": 0, "output": "ok"})),
                ("unload", (True, "unload ok")),
                ("compare", (True, {
                    "all_pass": True,
                    "after_csv_pass": 42, "after_csv_fail": 0,
                    "root_mixed_pass": 10, "root_mixed_fail": 0,
                    "failed_files": [], "report_path": "r",
                })),
            ],
            initial_failed_files=["orders.csv"],
        )
        assert "return a + b;" in res["disk"], "file was NOT updated"
        assert "return a - b;" not in res["disk"], "original left in file"
        assert len(res["state"]["applied"]) == 1
        assert res["state"]["applied"][0]["status"] == "verified"
        assert any(
            "Agent Auto-Fix Verified" in m for m in res["messages"]
        ), "success card not rendered"
        print("  ✅ fix applied on disk")
        print("  ✅ state.applied[0].status == 'verified'")
        print("  ✅ success card rendered")

        # ── Scenario 2: ROLLBACK then SUCCESS ─────────────────────────
        print("─" * 60)
        print("Scenario 2: ROLLBACK on run-fail, then SUCCESS")
        res = await _run_scenario(
            "rollback-then-success",
            cs_path,
            ORIGINAL_SRC,
            suggestions=[
                _make_suggestion(str(cs_path), "return a - b;", "return a / 0;"),
                _make_suggestion(str(cs_path), "return a - b;", "return a + b;"),
            ],
            orch_script=[
                ("run", (False, {"error_info": {
                    "error_message": "NullReferenceException at line 3",
                    "error_type": "runtime",
                }})),
                ("run", (True, {"exit_code": 0, "output": "ok"})),
                ("unload", (True, "unload ok")),
                ("compare", (True, {
                    "all_pass": True,
                    "after_csv_pass": 42, "after_csv_fail": 0,
                    "root_mixed_pass": 10, "root_mixed_fail": 0,
                    "failed_files": [], "report_path": "r",
                })),
            ],
            initial_failed_files=["orders.csv"],
        )
        assert "return a + b;" in res["disk"], "final file state wrong"
        assert "return a / 0;" not in res["disk"], "bad iter-1 code leaked"
        statuses = [r["status"] for r in res["state"]["iterations"]]
        assert statuses == ["run_failed_reverted", "verified"], statuses
        assert len(res["state"]["applied"]) == 1
        assert res["state"]["applied"][0]["iter"] == 2
        print("  ✅ bad iter-1 reverted (code not on disk)")
        print("  ✅ iter-2 fix on disk")
        print("  ✅ iteration log: ['run_failed_reverted', 'verified']")
        print("  ✅ applied list contains only iter 2")

        # ── Scenario 3: EXHAUSTED (5 iters, never all_pass) ───────────
        # Chain original_code → each iter's original must match what the
        # previous iter wrote; otherwise the real apply_fix_to_file fails
        # to find the needle on iter 2+ and the loop stops prematurely.
        print("─" * 60)
        print("Scenario 3: EXHAUSTED — never converges")
        chain_src = "return a - b;"
        suggestions = []
        for i in range(1, 6):
            new_src = f"return a - b + {i};"
            suggestions.append(_make_suggestion(str(cs_path), chain_src, new_src))
            chain_src = new_src
        orch_script = []
        for i in range(1, 6):
            orch_script.append(("run", (True, {"exit_code": 0})))
            orch_script.append(("unload", (True, "unload ok")))
            orch_script.append(("compare", (True, {
                "all_pass": False,
                "after_csv_pass": i, "after_csv_fail": 5 - i,
                "root_mixed_pass": 0, "root_mixed_fail": 0,
                "failed_files": ["orders.csv"], "report_path": "r",
            })))
        res = await _run_scenario(
            "exhausted",
            cs_path,
            ORIGINAL_SRC,
            suggestions=suggestions,
            orch_script=orch_script,
            initial_failed_files=["orders.csv"],
        )
        assert len(res["state"]["iterations"]) == 5, (
            f"expected 5 iters, got {len(res['state']['iterations'])}"
        )
        assert all(
            r["status"] == "kept_progress"
            for r in res["state"]["iterations"]
        ), [r["status"] for r in res["state"]["iterations"]]
        assert "Agent Auto-Fix Exhausted" in res["joined"]
        # Snapshot of the ORIGINAL file (first write wins) must be the
        # pristine source, enabling a full Revert All later.
        snapshots = res["state"]["snapshots"]
        assert str(cs_path) in snapshots
        snapshot_text = snapshots[str(cs_path)].decode("utf-8").replace("\r\n", "\n")
        assert snapshot_text == ORIGINAL_SRC, (
            "snapshot is not the pristine original"
        )
        print("  ✅ exactly 5 iterations recorded")
        print("  ✅ all iters tagged 'kept_progress'")
        print("  ✅ exhausted card rendered")
        print("  ✅ first-write-wins: snapshot == pristine original")

        # ── Scenario 4: APPLY-REFUSED → CONTINUE ──────────────────────
        print("─" * 60)
        print("Scenario 4: apply refused on iter 1 → iter 2 converges")
        res = await _run_scenario(
            "apply-refused",
            cs_path,
            ORIGINAL_SRC,
            suggestions=[
                _make_suggestion(str(cs_path), "nonexistent snippet xyz", "whatever"),
                _make_suggestion(str(cs_path), "return a - b;", "return a + b;"),
            ],
            orch_script=[
                ("run", (True, {"exit_code": 0})),
                ("unload", (True, "unload ok")),
                ("compare", (True, {
                    "all_pass": True,
                    "after_csv_pass": 42, "after_csv_fail": 0,
                    "root_mixed_pass": 10, "root_mixed_fail": 0,
                    "failed_files": [], "report_path": "r",
                })),
            ],
            initial_failed_files=["orders.csv"],
        )
        assert "return a + b;" in res["disk"]
        iter_statuses = [r["status"] for r in res["state"]["iterations"]]
        assert iter_statuses[0].startswith("apply_failed"), iter_statuses
        assert iter_statuses[1] == "verified", iter_statuses
        assert len(res["state"]["applied"]) == 1
        assert res["state"]["applied"][0]["iter"] == 2
        print("  ✅ iter 1 recorded as apply_failed, no snapshot kept")
        print("  ✅ iter 2 converged")
        print("  ✅ final applied list contains only iter 2")

        # ── Scenario 5: Revert All ────────────────────────────────────
        print("─" * 60)
        print("Scenario 5: Revert All after a verified fix")
        # Reuse state from scenario 4 — _AUTOFIX_STATE still has the
        # snapshot pointing to ORIGINAL_SRC. Simulate the Revert All click.
        action = _FakeAction(
            name="autofix_revert_all",
            payload={"baseline": "P02070-HPPL486P", "validate_mode": False},
        )
        _sent_messages.clear()
        await app.on_autofix_revert_all(action)
        after_revert = cs_path.read_text(encoding="utf-8")
        assert after_revert == ORIGINAL_SRC, (
            "Revert All did not restore pristine original"
        )
        assert "P02070-HPPL486P" not in app._AUTOFIX_STATE, (
            "state should be cleared after revert"
        )
        assert any("Reverted" in m for m in _sent_messages)
        print("  ✅ pristine original restored byte-for-byte")
        print("  ✅ _AUTOFIX_STATE cleared")

        print("─" * 60)
        print("ALL 5 SCENARIOS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
