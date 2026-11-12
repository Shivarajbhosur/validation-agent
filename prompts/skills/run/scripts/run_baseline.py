"""Run operation — Step 3b of the validation workflow (single-file).

Merges everything the run skill needs into ONE self-contained module.
External dependencies are strictly the project-wide :mod:`utils.logger`
and (lazily inside :func:`run`) :mod:`models.session`. No import from
:mod:`tools.dotnet_tools`, :mod:`agents.script_runner_agent`, or any
orchestrator internal.

What lives here
---------------
* ``extract_program_name``   — ``P02003-HPPL043P → HPPL043P``.
* ``find_program_file``      — walks the SSAB.OX repo for ``<name>.cs``.
* ``_parse_error_output``    — regex parse of ``dotnet run`` stdout/stderr
                               (SQL errors, build errors, runtime
                               exceptions with stack traces, warnings).
* ``_run_program``           — spawn ``dotnet run -- <program>`` in the
                               SSAB.OX project folder and collect the
                               structured result.
* :func:`run`                — the public entrypoint awaited by
                               :meth:`Orchestrator.run_skill`; wraps
                               ``_run_program`` in ``asyncio.to_thread``
                               and manages session state +
                               ``session.run_result``.

Behaviour, log lines, error messages, session state transitions, the
returned ``(bool, dict)`` shape, AND the ``session.run_result``
side-effect are byte-identical to the previous
``ScriptRunnerAgent.run_step_3b_run`` + ``tools.dotnet_tools.*``
implementation so existing Chainlit UI rendering continues to work
unchanged.

Contract
--------
``async def run(session) -> Tuple[bool, Dict[str, Any]]``

Dict keys returned: ``baseline``, ``program``, ``success``,
``exit_code``, ``error_info``, ``output``, ``program_path``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import TYPE_CHECKING, Any, Dict, Tuple

from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("run")


# ────────────────────────────────────────────────────────────────────
# Project paths
#
# NOTE: The SSAB.OX repo was restructured to add a src/ layer; the
# runnable project now lives at <repo>/src/SSAB.OX/SSAB.OX. Keep both
# paths in sync if the repo layout changes again.
#
# Both paths are env-driven so each developer can point at their own
# local checkout. Defaults preserve the previous behaviour so the
# ingesting machine keeps working with no .env entries.
# ────────────────────────────────────────────────────────────────────

_SSAB_REPO_PATH = os.getenv("SSAB_OX_REPO_PATH", "")
_SSAB_PROJECT_PATH = os.getenv(
    "SSAB_OX_PROJECT_PATH",
    os.path.join(_SSAB_REPO_PATH, "src", "SSAB.OX", "SSAB.OX") if _SSAB_REPO_PATH else "",
)

#accepted exit codes for successful program runs
_ACCEPTED_EXIT_CODES = {0, 1, 2, 3, 4}


# ────────────────────────────────────────────────────────────────────
# Program discovery + name extraction (used by handle_run pre-flight)
# ────────────────────────────────────────────────────────────────────


def extract_program_name(baseline_name: str) -> str:
    """Extract program name from baseline.

    ``P02003-HPPL043P → HPPL043P``. If the baseline has no hyphen it
    is returned unchanged.
    """
    if "-" in baseline_name:
        return baseline_name.split("-", 1)[1]
    return baseline_name


def find_program_file(program_name: str) -> Tuple[bool, str]:
    """Find the program ``.cs`` file in the SSAB.OX repo.

    Returns ``(True, absolute_path)`` if found; otherwise
    ``(False, error_message)``. Skips ``bin`` / ``obj`` / ``.git`` /
    ``node_modules`` folders to avoid stale build artefacts.
    """
    logger.info(f"Searching for program: {program_name}.cs")

    for root, dirs, files in os.walk(_SSAB_REPO_PATH):
        # Skip build/vendor folders
        dirs[:] = [d for d in dirs if d not in ("bin", "obj", ".git", "node_modules")]
        for file in files:
            if file.lower() == f"{program_name.lower()}.cs":
                full_path = os.path.join(root, file)
                logger.info(f"Found program: {full_path}")
                return True, full_path

    msg = f"Program {program_name}.cs not found in repository"
    logger.warning(msg)
    return False, msg


# ────────────────────────────────────────────────────────────────────
# Error-output parser (regex) + subprocess runner (local, self-contained)
# ────────────────────────────────────────────────────────────────────


def _parse_error_output(stdout: str, stderr: str, exit_code: int) -> Dict[str, Any]:
    """Parse ``dotnet run`` output to extract structured error info.

    Recognises SQL errors, C# build errors (``error CSxxxx``), runtime
    exceptions (``System.*Exception`` / ``Microsoft.*Exception``) with
    stack traces, and C# warnings.
    """
    error_info: Dict[str, Any] = {
        "has_error": exit_code not in _ACCEPTED_EXIT_CODES,
        "exit_code": exit_code,
        "error_type": None,
        "error_message": None,
        "stack_trace": None,
        "sql_error": None,
        "sql_error_number": None,
        "warnings": [],
    }

    combined_output = (stdout or "") + "\n" + (stderr or "")

    # ── SQL errors ────────────────────────────────────────────────
    sql_error_match = re.search(
        r'\[Error\].*SQL Exception: "(.*?)"', combined_output, re.DOTALL
    )
    if sql_error_match:
        error_info["error_type"] = "sql_error"
        error_info["sql_error"] = sql_error_match.group(1)

    sql_error_num_match = re.search(r"SQL Error Number: (\d+)", combined_output)
    if sql_error_num_match:
        error_info["sql_error_number"] = int(sql_error_num_match.group(1))

    # ── C# build errors ───────────────────────────────────────────
    if "error CS" in combined_output:
        error_info["error_type"] = "build_error"
        build_errors = re.findall(r"error CS\d+: .*", combined_output)
        error_info["error_message"] = "\n".join(build_errors)

    # ── Runtime exceptions (single-line best-effort) ──────────────
    exception_match = re.search(
        r"(System\.\w+Exception|Microsoft\.\w+Exception): (.*?)(?=\n   at|\Z)",
        combined_output,
        re.DOTALL,
    )
    if exception_match:
        error_info["error_type"] = error_info["error_type"] or "runtime_error"
        error_info["error_message"] = (
            f"{exception_match.group(1)}: {exception_match.group(2).strip()}"
        )

    # ── Stack trace — find LAST exception block (actual error,
    #    not warnings). Look for exception followed by stack trace.
    all_exceptions = re.findall(
        r"((?:System\.|Microsoft\.)\w*Exception.*?)\n((?:   at .*\n?)+)",
        combined_output,
        re.DOTALL,
    )

    if all_exceptions:
        last_exception = all_exceptions[-1]
        error_info["error_message"] = last_exception[0].strip()
        stack_lines = last_exception[1].strip().split("\n")[:15]
        error_info["stack_trace"] = "\n".join(stack_lines)
        error_info["error_type"] = "runtime_error"
    else:
        # Fallback: capture whatever stack we can find.
        stack_match = re.search(r"(   at .*)", combined_output, re.DOTALL)
        if stack_match:
            stack_lines = stack_match.group(1).split("\n")[:10]
            error_info["stack_trace"] = "\n".join(stack_lines)

    # ── Warnings ──────────────────────────────────────────────────
    warnings = re.findall(r"warning CS\d+: .*", combined_output)
    error_info["warnings"] = warnings

    # ── Uncategorised error fallback ──────────────────────────────
    if error_info["has_error"] and not error_info["error_type"]:
        error_info["error_type"] = "unknown_error"
        error_lines = [
            line
            for line in combined_output.split("\n")
            if "error" in line.lower() or "exception" in line.lower()
        ]
        if error_lines:
            error_info["error_message"] = error_lines[-1]

    return error_info


def _run_program(baseline_name: str) -> Tuple[bool, Dict[str, Any]]:
    """Invoke ``dotnet run -- <program>`` and return the structured result.

    Blocking; ``run(session)`` above off-loads this to a worker thread
    via ``asyncio.to_thread`` so the async event loop stays responsive.
    """
    program_name = extract_program_name(baseline_name)
    logger.info(f"Running program: {program_name} for baseline: {baseline_name}")

    result: Dict[str, Any] = {
        "baseline": baseline_name,
        "program": program_name,
        "success": False,
        "exit_code": -1,
        "error_info": {},
        "output": "",
        "program_path": None,
    }

    try:
        command = f"dotnet run -- {program_name}"
        logger.info(f"Command: {command}")
        logger.info(f"Working directory: {_SSAB_PROJECT_PATH}")

        process_result = subprocess.run(
            command,
            shell=True,
            cwd=_SSAB_PROJECT_PATH,
            capture_output=True,
            text=True,
            timeout=300,
        )

        result["exit_code"] = process_result.returncode
        result["output"] = process_result.stdout or ""
        result["error_info"] = _parse_error_output(
            process_result.stdout,
            process_result.stderr,
            process_result.returncode,
        )

        if process_result.returncode in _ACCEPTED_EXIT_CODES:
            logger.info(
                f"Program {program_name} completed successfully "
                f"(exit code: {process_result.returncode})"
            )
            result["success"] = True
            return True, result
        else:
            logger.error(
                f"Program {program_name} failed with exit code: "
                f"{process_result.returncode}"
            )
            return False, result

    except subprocess.TimeoutExpired:
        msg = f"Program {program_name} timed out after 5 minutes"
        logger.error(msg)
        result["error_info"] = {"error_type": "timeout", "error_message": msg}
        return False, result

    except Exception as e:  # noqa: BLE001 -- outer safety net
        msg = f"Error running program: {e}"
        logger.error(msg)
        result["error_info"] = {
            "error_type": "execution_error",
            "error_message": str(e),
        }
        return False, result


# ────────────────────────────────────────────────────────────────────
# Public entrypoint (formerly ScriptRunnerAgent.run_step_3b_run)
# ────────────────────────────────────────────────────────────────────


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute Step 3b — run the migrated program via ``dotnet run``.

    Sub-steps (identical to the previous
    ``ScriptRunnerAgent.run_step_3b_run`` implementation):

      1. Extract the program name from ``session.baseline_name``.
      2. Mark ``step_3b_run`` as :class:`StepStatus.RUNNING`.
      3. Off-load the blocking ``dotnet run`` subprocess to a worker
         thread via ``asyncio.to_thread`` (was previously
         ``run_in_executor`` in ``handle_run``).
      4. Write the full result dict onto ``session.run_result`` — the
         downstream AnalysisAgent reads this after a failure.
      5. Mark ``step_3b_run`` COMPLETED (with ``program`` + ``exit_code``
         in the step data) on success, or FAILED (with the parsed error
         message) on failure.
      6. Return ``(success, result_dict)`` for ``handle_run`` to render.

    Returns
    -------
    ``(success, result_dict)`` where ``result_dict`` has the seven keys
    ``baseline``, ``program``, ``success``, ``exit_code``, ``error_info``,
    ``output``, ``program_path`` — same shape ``handle_run`` reads.
    """
    # Lazy import keeps the import graph clean — ``run`` is only
    # invoked when the skill actually executes.
    from models.session import StepStatus

    baseline_name = session.baseline_name
    program_name = extract_program_name(baseline_name)
    logger.info(f"Step 3b: Running program {program_name} for {baseline_name}")

    session.update_step("step_3b_run", StepStatus.RUNNING)

    try:
        # Off-load the blocking dotnet-run subprocess to a worker
        # thread so the async event loop stays responsive.
        success, result = await asyncio.to_thread(_run_program, baseline_name)

        # Store the full result on the session for downstream analysis.
        # This side-effect is preserved verbatim from the old
        # ScriptRunnerAgent path so the AnalysisAgent AI-analysis
        # button (in ``app.py :: on_analyse_with_ai``) still works.
        session.run_result = result

        if success:
            session.update_step(
                "step_3b_run",
                StepStatus.COMPLETED,
                data={
                    "program": program_name,
                    "exit_code": result.get("exit_code", 0),
                },
            )
            logger.info(f"Step 3b completed successfully for {baseline_name}")
        else:
            error_info = result.get("error_info") or {}
            error_msg = error_info.get("error_message", "Unknown error")
            session.update_step("step_3b_run", StepStatus.FAILED, error=error_msg)
            logger.error(f"Step 3b failed for {baseline_name}: {error_msg}")

        return success, result

    except Exception as e:  # noqa: BLE001 -- outer safety net
        error_msg = str(e)
        session.update_step("step_3b_run", StepStatus.FAILED, error=error_msg)
        logger.error(f"Step 3b exception for {baseline_name}: {error_msg}")
        return False, {
            "success": False,
            "error_info": {"error_type": "exception", "error_message": error_msg},
        }


__all__ = ["run", "extract_program_name", "find_program_file"]
