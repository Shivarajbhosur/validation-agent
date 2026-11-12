"""Compare operation package — Step 6 of the validation workflow.

Self-contained vertical module. This file is the only public
surface of the skill — external code calls one of:

* :func:`run` — the primary entrypoint (invoked by the orchestrator
  via ``run_skill("compare", session)``).
* :func:`mark_acceptable` — session-state helper called by the
  ``compare_acceptable`` Chainlit button callback in ``app.py`` once
  the user has reviewed the differences and accepted them.
* :func:`mark_not_acceptable` — session-state helper called by the
  ``compare_not_acceptable`` Chainlit button callback.

The full implementation (PowerShell subprocess runner, output parser,
session-state transitions) lives inside
``scripts/compare_baseline.py``; this file stays a thin facade so
lazy-loading of ``subprocess`` / ``re`` is deferred to invocation.

Contract
--------
``await run(session) -> Tuple[bool, Dict[str, Any]]``
``mark_acceptable(session) -> Tuple[bool, str]``
``mark_not_acceptable(session) -> Tuple[bool, str]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from models.session import Session


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute the compare skill (Step 6 of the validation workflow).

    Delegates to ``scripts/compare_baseline.run`` (lazy import so
    ``subprocess`` / ``re`` only load when the skill actually runs).

    Args:
        session: The validation session for the baseline.

    Returns:
        ``(success, result_dict)`` — same shape and content the
        previous ``CompareAgent.run_step_6_compare`` returned, so
        ``handle_compare`` (the Chainlit UI handler) sees no
        behaviour change. Dict keys: ``exit_code``, ``after_csv_pass``,
        ``after_csv_fail``, ``root_mixed_pass``, ``root_mixed_fail``,
        ``all_pass``, ``failed_files``, ``report_path``, ``output``,
        ``message``.
    """
    from .scripts.compare_baseline import run as _run

    return await _run(session)


def mark_acceptable(session: "Session") -> Tuple[bool, str]:
    """User accepted the compare differences — mark step COMPLETED.

    Called from the ``compare_acceptable`` Chainlit action callback
    in ``app.py`` when the user clicks "✅ Acceptable" after the
    compare skill reported differences.
    """
    from .scripts.compare_baseline import mark_acceptable as _mark

    return _mark(session)


def mark_not_acceptable(session: "Session") -> Tuple[bool, str]:
    """User rejected the compare differences — mark step FAILED.

    Called from the ``compare_not_acceptable`` Chainlit action
    callback in ``app.py`` when the user clicks "❌ Not Acceptable".
    """
    from .scripts.compare_baseline import mark_not_acceptable as _mark

    return _mark(session)


def read_failed_file(baseline_name: str, failed_file: str) -> Dict[str, Any]:
    """Load CompareX's ``-delta.txt`` snippet + HTML path for one failing file.

    Facade for the auto-fix loop; see ``scripts/diff_reader.read_failed_file``.
    """
    from .scripts.diff_reader import read_failed_file as _read
    return _read(baseline_name, failed_file)


def pick_top_failed_file(failed_files):
    """Choose the first failing file to analyse (AFTER-CSV preferred)."""
    from .scripts.diff_reader import pick_top_failed_file as _pick
    return _pick(failed_files)


def build_diff_details(record: Dict[str, Any]) -> str:
    """Format a ``read_failed_file`` record into LLM-ready diff context."""
    from .scripts.diff_reader import build_diff_details as _build
    return _build(record)


__all__ = [
    "run",
    "mark_acceptable",
    "mark_not_acceptable",
    "read_failed_file",
    "pick_top_failed_file",
    "build_diff_details",
]
