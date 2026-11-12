"""Compare operation — Step 6 of the validation workflow (single-file).

Merges everything the compare skill needs into ONE self-contained
module. External dependencies are strictly the project-wide
:mod:`config` (``COMPARE_COMMAND`` template + ``get_report_path`` +
``get_baseline_group``) and :mod:`utils.logger`. No import from
:mod:`tools.compare_tools`, :mod:`agents.compare_agent`, or any
orchestrator internal.

What lives here
---------------
* ``_run_compare``          — spawn ``pwsh -Command compare-x ...`` and
                              collect its stdout/stderr.
* ``_parse_compare_output`` — regex-parse the ``Summary (AFTER CSV):``
                              / ``Summary (Root Mixed):`` counters plus
                              the ``FAIL (data differs...)`` lines.
* :func:`run`               — the public entrypoint awaited by
                              :meth:`Orchestrator.run_skill`.
* :func:`mark_acceptable`   — session-state helper called by the
                              ``compare_acceptable`` Chainlit button.
* :func:`mark_not_acceptable` — session-state helper called by the
                                ``compare_not_acceptable`` Chainlit button.

Behaviour, log lines, error messages, session state transitions, and
the returned ``(bool, dict)`` shape are byte-identical to the previous
``CompareAgent.run_step_6_compare`` + ``mark_acceptable`` /
``mark_not_acceptable`` implementation so existing Chainlit UI
rendering continues to work unchanged.

Contract
--------
``async def run(session) -> Tuple[bool, Dict[str, Any]]``

Dict keys returned: ``exit_code``, ``after_csv_pass``,
``after_csv_fail``, ``root_mixed_pass``, ``root_mixed_fail``,
``all_pass``, ``failed_files``, ``report_path``, ``output``,
``message``.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import TYPE_CHECKING, Any, Dict, Tuple

import config
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("compare")


# ────────────────────────────────────────────────────────────────────
# PowerShell runner + output parser (local, self-contained)
# ────────────────────────────────────────────────────────────────────


def _parse_compare_output(output: str) -> Dict[str, Any]:
    """Parse the compare-x cmdlet console output to extract counters."""
    result: Dict[str, Any] = {
        "after_csv_pass": 0,
        "after_csv_fail": 0,
        "root_mixed_pass": 0,
        "root_mixed_fail": 0,
        "failed_files": [],
    }

    # Parse AFTER CSV summary
    after_csv_match = re.search(
        r"Summary \(AFTER CSV\):\s*(\d+)\s*pass,\s*(\d+)\s*fail", output
    )
    if after_csv_match:
        result["after_csv_pass"] = int(after_csv_match.group(1))
        result["after_csv_fail"] = int(after_csv_match.group(2))

    # Parse Root Mixed summary
    root_mixed_match = re.search(
        r"Summary \(Root Mixed\):\s*(\d+)\s*pass,\s*(\d+)\s*fail", output
    )
    if root_mixed_match:
        result["root_mixed_pass"] = int(root_mixed_match.group(1))
        result["root_mixed_fail"] = int(root_mixed_match.group(2))

    # Parse failed files
    fail_matches = re.findall(r"FAIL \(data differs.*?\):\s*(.+?)(?:\n|$)", output)
    result["failed_files"] = [f.strip() for f in fail_matches]

    return result


def _run_compare(baseline_name: str) -> Tuple[bool, Dict[str, Any]]:
    """Invoke ``compare-x`` (PowerShell cmdlet) and parse the result.

    Returns
    -------
    ``(success, result_dict)`` where ``success`` reflects whether the
    subprocess itself ran without an exception (NOT whether the compare
    passed). ``result_dict["all_pass"]`` is the pass/fail signal for
    the compare itself.
    """
    # Build path: SG02\P02070-HPPL486P (extract group from baseline)
    group = config.get_baseline_group(baseline_name)
    program_rel_path = f"{group}\\{baseline_name}"

    report_path = str(config.get_report_path(baseline_name))

    logger.info(f"Running compare for {baseline_name}")
    logger.info(f"Path: {program_rel_path}")

    try:
        # Run compare-x command from the PowerShell library.
        command = config.COMPARE_COMMAND.format(baseline_name=baseline_name)
        logger.info(f"Command: {command}")

        # Close stdin (DEVNULL) so any interactive Read-Host prompt added
        # inside the compare-x PowerShell module returns immediately on
        # EOF instead of blocking the agent indefinitely.
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            #text=True,
            stdin=subprocess.DEVNULL,
        )

        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        output = stdout + "\n" + stderr
        exit_code = result.returncode

        logger.info(f"Compare exit code: {exit_code}")

        parsed = _parse_compare_output(output)

        # Overall pass = exit code 0 AND both fail counters are 0.
        all_pass = (
            exit_code == 0
            and parsed["after_csv_fail"] == 0
            and parsed["root_mixed_fail"] == 0
        )

        result_dict: Dict[str, Any] = {
            "exit_code": exit_code,
            "after_csv_pass": parsed["after_csv_pass"],
            "after_csv_fail": parsed["after_csv_fail"],
            "root_mixed_pass": parsed["root_mixed_pass"],
            "root_mixed_fail": parsed["root_mixed_fail"],
            "all_pass": all_pass,
            "failed_files": parsed["failed_files"],
            "report_path": report_path,
            "output": output,
            "message": (
                "All files match!"
                if all_pass
                else "Differences found - review needed"
            ),
        }

        return True, result_dict

    except Exception as e:  # noqa: BLE001 -- outer safety net
        msg = f"Error running compare: {e}"
        logger.exception(msg)
        return False, {
            "exit_code": -1,
            "message": msg,
            "output": str(e),
        }


# ────────────────────────────────────────────────────────────────────
# Public entrypoint (formerly CompareAgent.run_step_6_compare)
# ────────────────────────────────────────────────────────────────────


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute Step 6 — compare generated vs expected output.

    Sub-steps (identical to the previous
    ``CompareAgent.run_step_6_compare`` implementation):

      1. Mark ``step_6_compare`` as :class:`StepStatus.RUNNING`.
      2. Invoke the local :func:`_run_compare` which spawns
         ``compare-x`` via PowerShell and parses the output.
      3. If the subprocess itself failed → mark FAILED and return.
      4. If ``all_pass`` → mark COMPLETED with the parsed summary.
      5. Otherwise (differences found) → keep the step RUNNING and
         store the parsed summary; :func:`mark_acceptable` /
         :func:`mark_not_acceptable` will finalise the status once
         the user clicks a button.

    The blocking subprocess is off-loaded to a thread via
    :func:`asyncio.to_thread` so the async event loop stays
    responsive — mirrors what the old ``handle_compare`` did with
    ``loop.run_in_executor(None, agent.run_step_6_compare)``.
    """
    from models.session import StepStatus

    baseline_name = session.baseline_name
    logger.info(f"Step 6: Compare for {baseline_name}")

    # Mark step as running
    session.update_step("step_6_compare", StepStatus.RUNNING)

    # Off-load blocking pwsh subprocess to a worker thread.
    success, result = await asyncio.to_thread(_run_compare, baseline_name)

    if not success:
        logger.error(f"Compare failed: {result.get('message')}")
        session.update_step(
            "step_6_compare",
            StepStatus.FAILED,
            error=result.get("message"),
        )
        return False, result

    # Determine status based on result.
    if result["all_pass"]:
        status = StepStatus.COMPLETED
    else:
        # Differences found - keep as RUNNING until the user decides
        # via the Chainlit action buttons (mark_acceptable /
        # mark_not_acceptable below).
        status = StepStatus.RUNNING

    # Store the parsed summary in the session step data.
    compare_data = {
        "all_pass": result["all_pass"],
        "after_csv_pass": result["after_csv_pass"],
        "after_csv_fail": result["after_csv_fail"],
        "root_mixed_pass": result["root_mixed_pass"],
        "root_mixed_fail": result["root_mixed_fail"],
        "failed_files": result["failed_files"],
        "report_path": result["report_path"],
    }

    session.update_step("step_6_compare", status, data=compare_data)

    logger.info(f"Compare complete. All pass: {result['all_pass']}")
    return True, result


# ────────────────────────────────────────────────────────────────────
# Session-state helpers for the post-compare button callbacks
# (formerly CompareAgent.mark_acceptable / mark_not_acceptable)
# ────────────────────────────────────────────────────────────────────


def mark_acceptable(session: "Session") -> Tuple[bool, str]:
    """User accepted the compare differences — mark step COMPLETED.

    Called from the ``compare_acceptable`` Chainlit action callback
    in ``app.py`` when the user clicks "✅ Acceptable" after the
    compare skill reported differences. Preserves the previously
    stored summary and appends ``user_decision="acceptable"``.
    """
    from models.session import StepStatus

    compare_data = session.get_step_data("step_6_compare")
    compare_data["user_decision"] = "acceptable"

    session.update_step(
        "step_6_compare",
        StepStatus.COMPLETED,
        data=compare_data,
    )

    logger.info("Differences marked as acceptable")
    return True, "Differences marked as acceptable. Ready for finalize."


def mark_not_acceptable(session: "Session") -> Tuple[bool, str]:
    """User rejected the compare differences — mark step FAILED.

    Called from the ``compare_not_acceptable`` Chainlit action
    callback in ``app.py`` when the user clicks "❌ Not Acceptable".
    Preserves the previously stored summary and appends
    ``user_decision="not_acceptable"``.
    """
    from models.session import StepStatus

    compare_data = session.get_step_data("step_6_compare")
    compare_data["user_decision"] = "not_acceptable"

    session.update_step(
        "step_6_compare",
        StepStatus.FAILED,
        data=compare_data,
    )

    logger.info("Differences marked as not acceptable")
    return True, "Differences marked as not acceptable. Fix code and re-run."


__all__ = ["run", "mark_acceptable", "mark_not_acceptable"]
