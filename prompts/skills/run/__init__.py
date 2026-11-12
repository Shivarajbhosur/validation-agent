"""Run operation package — Step 3b of the validation workflow.

Self-contained vertical module. This file is the only public
surface of the skill — external code calls one of:

* :func:`run` — the primary entrypoint (invoked by the orchestrator
  via ``run_skill("run", session)``).
* :func:`extract_program_name` — pure string helper. Used by
  ``handle_run`` in ``app.py`` before dispatching so the UI can show
  the program name in log lines and progress cards.
* :func:`find_program_file` — walks the SSAB.OX repo looking for
  ``<program>.cs``. Used by ``handle_run`` as a pre-flight check so
  the UI can short-circuit with a friendly "Program Not Found"
  message instead of the skill failing internally.

The full implementation (``dotnet run`` subprocess, error parser,
session-state transitions) lives inside ``scripts/run_baseline.py``;
this file stays a thin facade so lazy-loading of ``subprocess`` /
``os.walk`` is deferred to invocation.

Contract
--------
``await run(session) -> Tuple[bool, Dict[str, Any]]``
``extract_program_name(baseline) -> str``
``find_program_file(program_name) -> Tuple[bool, str]``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from models.session import Session


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute the run skill (Step 3b of the validation workflow).

    Delegates to ``scripts/run_baseline.run`` (lazy import so
    ``subprocess`` / ``os`` only load when the skill actually runs).

    Args:
        session: The validation session for the baseline.

    Returns:
        ``(success, result_dict)`` — same shape and content the
        previous ``ScriptRunnerAgent.run_step_3b_run`` returned, so
        ``handle_run`` (the Chainlit UI handler) sees no behaviour
        change. Dict keys: ``baseline``, ``program``, ``success``,
        ``exit_code``, ``error_info``, ``output``, ``program_path``.

    Side effects (preserved from the previous implementation):
        - Marks ``step_3b_run`` RUNNING → COMPLETED / FAILED in the
          session with ``program`` + ``exit_code`` in the step data.
        - Writes the full result dict onto ``session.run_result``
          for the downstream AnalysisAgent to consume.
    """
    from .scripts.run_baseline import run as _run

    return await _run(session)


def extract_program_name(baseline: str) -> str:
    """Pure helper: extract program name from a baseline id.

    ``P02003-HPPL043P → HPPL043P``. If the baseline has no hyphen it
    is returned unchanged. Used by ``handle_run`` in ``app.py`` for
    pre-flight display before dispatching to :func:`run`.
    """
    from .scripts.run_baseline import extract_program_name as _impl

    return _impl(baseline)


def find_program_file(program_name: str) -> Tuple[bool, str]:
    """Walk the SSAB.OX repo looking for ``<program>.cs``.

    Returns ``(True, absolute_path)`` if found, else
    ``(False, error_message)``. Used by ``handle_run`` as a
    pre-flight check so the UI shows a friendly "Program Not Found"
    message instead of the skill failing internally when the source
    file is missing.
    """
    from .scripts.run_baseline import find_program_file as _impl

    return _impl(program_name)


__all__ = ["run", "extract_program_name", "find_program_file"]
