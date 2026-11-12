"""Copy operation — Step 1 of the validation workflow (single-file).

Merges everything the copy skill needs into one module:

* Filesystem helper:   :func:`baseline_exists_at_source`,
                       :func:`copy_baseline` (``shutil.copytree`` G: → C:).
* PowerShell cmdlets:  :func:`new_sysout`, :func:`new_env`,
                       :func:`copy_input` (Step 1b/1c/1d).
* Orchestration:       :func:`run` — the public entrypoint awaited by
                       :meth:`services.orchestrator.Orchestrator.run_skill`.

This file is **self-contained**: it depends only on the project-wide
``config`` module, the ``utils.logger`` helper, and ``models.session``
for the session state enum. It does NOT import any service
(orchestrator, LLM, knowledge base, …) and reads the PowerShell
cmdlet catalog from the sibling file ``_powershell.yaml`` — kept
inside this folder so the copy skill is not dependent on any catalog
outside its own folder.

The behaviour, log lines, error messages, and the markdown success
summary returned to the UI are byte-identical to the previous split
implementation (``agent/copy_agent.py`` + ``tools/file_tools.py`` +
``tools/powershell_tools.py``) so existing Chainlit progress cards
and follow-up handlers continue to work unchanged.

Contract
--------
``async def run(session) -> Tuple[bool, str]``
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import config
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("copy")


# ────────────────────────────────────────────────────────────────────
# Filesystem helpers (formerly tools/file_tools.py)
# ────────────────────────────────────────────────────────────────────


def baseline_exists_at_source(baseline_name: str) -> bool:
    """Check if baseline exists at source (G:)."""
    source = config.get_source_path(baseline_name)
    return source.exists()


def copy_baseline(baseline_name: str) -> Tuple[bool, str]:
    """Copy baseline folder from source (G:) to destination (C:)."""
    try:
        source = config.get_source_path(baseline_name)
        dest = config.get_baseline_path(baseline_name)

        logger.info(f"Copying baseline: {baseline_name}")
        logger.info(f"Source: {source}")
        logger.info(f"Dest: {dest}")

        # Check if source exists
        if not source.exists():
            msg = f"Source not found: {source}"
            logger.error(msg)
            return False, msg

        # If dest exists, delete it first (overwrite)
        if dest.exists():
            logger.info(f"Destination exists, removing: {dest}")
            shutil.rmtree(dest)

        # Create parent directory if not exists
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Copy folder
        shutil.copytree(source, dest)

        msg = f"Copied successfully: {baseline_name}"
        logger.info(msg)
        return True, msg

    except PermissionError as e:
        msg = f"Permission denied: {e}"
        logger.error(msg)
        return False, msg

    except Exception as e:
        msg = f"Copy failed: {e}"
        logger.error(msg)
        return False, msg


# ────────────────────────────────────────────────────────────────────
# PowerShell cmdlets (formerly tools/powershell_tools.py)
# ────────────────────────────────────────────────────────────────────

# Path to the copy skill's OWN PowerShell cmdlet catalog. Living next
# to this file keeps the skill fully self-contained: no runtime
# dependency on the orchestrator, no read from any shared catalog
# outside the copy folder.
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


def new_sysout(baseline_name: str) -> Tuple[bool, str]:
    """Step 1b: Create SYSOUT file."""
    return _run_powershell_command(_resolve_command("new_sysout", baseline_name))


def new_env(baseline_name: str) -> Tuple[bool, str]:
    """Step 1c: Create ``.env`` file."""
    return _run_powershell_command(_resolve_command("new_env", baseline_name))


def copy_input(baseline_name: str) -> Tuple[bool, str]:
    """Step 1d: Copy input files to working folder."""
    return _run_powershell_command(_resolve_command("copy_input", baseline_name))


# ────────────────────────────────────────────────────────────────────
# Public entrypoint (formerly agent/copy_agent.py :: CopyAgent.run)
# ────────────────────────────────────────────────────────────────────


async def run(session: "Session") -> Tuple[bool, str]:
    """Execute Step 1 — copy baseline from G: to C: and prepare files.

    Sub-steps (identical to the previous ``CopyAgent.run`` implementation):

      1a. Folder copy   — :func:`copy_baseline`   (Python ``shutil``)
      1b. SYSOUT        — :func:`new_sysout`      (PowerShell)
      1c. ``.env``      — :func:`new_env`         (PowerShell)
      1d. Input files   — :func:`copy_input`      (PowerShell)

    On any failure the session step ``step_1_copy`` is marked
    :class:`StepStatus.FAILED` with the underlying error and the
    function returns immediately. On full success the step is marked
    :class:`StepStatus.COMPLETED` and the same multi-line markdown
    summary as before is returned (so the Chainlit UI renders the
    progress card / success message exactly as today).
    """
    # Lazy import keeps the import graph clean — ``run`` is only
    # invoked when the skill actually executes, so the session model
    # is not pulled in just by importing this module.
    from models.session import StepStatus

    baseline_name = session.baseline_name
    logger.info(f"Step 1: Copy baseline - {baseline_name}")

    # Update session status to running
    session.update_step("step_1_copy", StepStatus.RUNNING)

    # Check if source exists
    if not baseline_exists_at_source(baseline_name):
        msg = f"Baseline not found at source: {baseline_name}"
        session.update_step("step_1_copy", StepStatus.FAILED, error=msg)
        return False, msg

    # Step 1a: Copy folder (Python)
    logger.info("Step 1a: Copying folder...")
    success, msg = copy_baseline(baseline_name)
    if not success:
        session.update_step("step_1_copy", StepStatus.FAILED, error=msg)
        return False, f"Copy folder failed: {msg}"

    # Step 1b: New-Sysout (PowerShell)
    logger.info("Step 1b: Creating SYSOUT...")
    success, output = new_sysout(baseline_name)
    if not success:
        session.update_step("step_1_copy", StepStatus.FAILED, error=output)
        return False, f"New-Sysout failed: {output}"

    # Step 1c: New-Env (PowerShell)
    logger.info("Step 1c: Creating .env...")
    success, output = new_env(baseline_name)
    if not success:
        session.update_step("step_1_copy", StepStatus.FAILED, error=output)
        return False, f"New-Env failed: {output}"

    # Step 1d: Copy-Input (PowerShell)
    logger.info("Step 1d: Copying input files...")
    success, output = copy_input(baseline_name)
    if not success:
        session.update_step("step_1_copy", StepStatus.FAILED, error=output)
        return False, f"Copy-Input failed: {output}"

    # All done!
    session.update_step("step_1_copy", StepStatus.COMPLETED)

    # Build detailed success message — kept identical to the previous
    # CopyAgent.run output so the Chainlit UI response is unchanged.
    details = [f"**Step 1 Completed:** `{baseline_name}`"]
    details.append("• ✅ Folder copied")
    details.append("• ✅ SYSOUT created")
    details.append("• ✅ .env file placed")
    details.append("• ✅ Input files copied")

    return True, "\n".join(details)


__all__ = [
    "baseline_exists_at_source",
    "copy_baseline",
    "new_sysout",
    "new_env",
    "copy_input",
    "run",
]
