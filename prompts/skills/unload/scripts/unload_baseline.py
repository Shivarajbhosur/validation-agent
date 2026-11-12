"""Unload operation — Step 5 of the validation workflow (single-file).

Merges everything the unload skill needs into one module:

* PowerShell cmdlet catalog reader (local ``_powershell.yaml``).
* PowerShell executor:  :func:`unload_baseline_data` (Step 5).
* Orchestration:        :func:`run` — the public entrypoint awaited
                        by :meth:`Orchestrator.run_skill`.

This file is **self-contained**: it depends only on the ``utils.logger``
helper and ``models.session`` for the session state enum. It does
NOT import from ``tools.powershell_tools`` or any orchestrator
internal — the PowerShell cmdlet catalog is read from the sibling
file ``_powershell.yaml`` so the unload skill has no runtime
dependency on any catalog outside its own folder.

The behaviour, log lines, error messages, and the success/failure
strings returned to the UI are byte-identical to the previous
``ScriptRunnerAgent.run_step_5_unload`` implementation so existing
Chainlit messages and follow-up handlers continue to work
unchanged.

Contract
--------
``async def run(session) -> Tuple[bool, str]``
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("unload")


# ────────────────────────────────────────────────────────────────────
# PowerShell cmdlet catalog (local, self-contained)
# ────────────────────────────────────────────────────────────────────

# Path to the unload skill's OWN PowerShell cmdlet catalog. Living
# next to this file keeps the skill fully self-contained: no runtime
# dependency on the orchestrator, no read from any shared catalog
# outside the unload folder.
_PS_CATALOG_PATH = Path(__file__).resolve().parent / "_powershell.yaml"

# Cached cmdlet specs keyed by id. Loaded lazily on first use so the
# YAML file is read once per process lifetime.
_PS_COMMANDS_CACHE: Optional[Dict[str, Dict[str, str]]] = None


def _load_ps_catalog() -> Dict[str, Dict[str, str]]:
    """Load (and cache) the PowerShell cmdlet catalog from the local YAML.

    Returns a ``{id: {cmdlet, args_template}}`` map.
    """
    global _PS_COMMANDS_CACHE
    if _PS_COMMANDS_CACHE is not None:
        return _PS_COMMANDS_CACHE

    import yaml  # local import keeps top-level imports light

    if not _PS_CATALOG_PATH.exists():
        raise FileNotFoundError(
            f"PowerShell catalog not found: {_PS_CATALOG_PATH}"
        )

    with _PS_CATALOG_PATH.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    catalog: Dict[str, Dict[str, str]] = {}
    for entry in doc.get("powershell_commands") or []:
        if not isinstance(entry, dict):
            continue
        cmd_id = entry.get("id")
        if not cmd_id:
            continue
        catalog[str(cmd_id)] = {
            "cmdlet": str(entry.get("cmdlet", "")),
            "args_template": str(entry.get("args_template", "")),
        }

    _PS_COMMANDS_CACHE = catalog
    return catalog


def _resolve_command(cmd_id: str, baseline_name: str) -> str:
    """Build the PowerShell command string from the local cmdlet catalog."""
    catalog = _load_ps_catalog()
    spec = catalog.get(cmd_id)
    if spec is None:
        raise KeyError(
            f"PowerShell command '{cmd_id}' is not defined in "
            f"{_PS_CATALOG_PATH.name}. Known: {sorted(catalog)}"
        )
    cmdlet = spec["cmdlet"]
    args = spec["args_template"].format(baseline=baseline_name)
    return f"{cmdlet} {args}".strip()


def _run_powershell_command(command: str) -> Tuple[bool, str]:
    """Run a PowerShell command and return ``(success, output)``."""
    try:
        logger.info(f"Running PowerShell: {command}")

        result = subprocess.run(
            ["pwsh", "-Command", command],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes timeout
        )

        output = result.stdout + result.stderr

        if result.returncode == 0:
            logger.info("Command successful")
            return True, output
        else:
            logger.error(f"Command failed: {output}")
            return False, output

    except subprocess.TimeoutExpired:
        msg = "Command timed out (5 minutes)"
        logger.error(msg)
        return False, msg

    except Exception as e:
        msg = f"Command error: {e}"
        logger.error(msg)
        return False, msg


def unload_baseline_data(baseline_name: str) -> Tuple[bool, str]:
    """Step 5: Unload data from DB."""
    return _run_powershell_command(_resolve_command("unload_baseline_data", baseline_name))


# ────────────────────────────────────────────────────────────────────
# Public entrypoint (formerly ScriptRunnerAgent.run_step_5_unload)
# ────────────────────────────────────────────────────────────────────


async def run(session: "Session") -> Tuple[bool, str]:
    """Execute Step 5 — unload baseline data from the database.

    Sub-steps (identical to the previous
    ``ScriptRunnerAgent.run_step_5_unload`` implementation):

      1. Mark ``step_5_unload`` as :class:`StepStatus.RUNNING`.
      2. Invoke the ``unload-BaselineData`` PowerShell cmdlet.
      3. Mark ``step_5_unload`` COMPLETED or FAILED based on the
         result.

    On success the same ``"Step 5 completed: <baseline>\\n\\n<output>"``
    string is returned so the Chainlit UI response is unchanged.
    """
    # Lazy import keeps the import graph clean — ``run`` is only
    # invoked when the skill actually executes, so the session model
    # is not pulled in just by importing this module.
    from models.session import StepStatus

    baseline_name = session.baseline_name
    logger.info(f"Step 5: Unload from DB - {baseline_name}")

    # Update session status to running
    session.update_step("step_5_unload", StepStatus.RUNNING)

    success, output = unload_baseline_data(baseline_name)

    if success:
        session.update_step("step_5_unload", StepStatus.COMPLETED)
        return True, f"Step 5 completed: {baseline_name}\n\n{output}"
    else:
        session.update_step("step_5_unload", StepStatus.FAILED, error=output)
        return False, f"Step 5 failed: {output}"


__all__ = [
    "unload_baseline_data",
    "run",
]
