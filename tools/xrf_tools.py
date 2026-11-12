"""
XRF update helper for Auto Apply.

When the user clicks **Auto Apply** on an AI fix suggestion, 
the target ``.cs`` file is written directly by :func:`tools.auto_apply.apply_fix_to_file`.
That bypasses Visual Studio + the ArcEdit extension which normally keeps the
paired ``.xrf`` cross-reference file in sync. To keep the XRF up to date, we
invoke the standalone PowerShell script ``scripts/Update-XrefFromCsEdit.ps1``
here and expose a single :func:`update_xrf_from_cs_edit` entry point.

Design constraints:

* No side effects at import time.
* Blocking ``subprocess.run`` is intended to be off-loaded by the caller
  via ``asyncio.to_thread`` so the Chainlit event loop stays responsive.
* Every failure mode returns a structured :class:`XrfUpdateResult` — never
  raises — so Auto Apply can surface a graceful warning without ever
  breaking the primary code-edit flow.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
from utils.logger import get_logger

logger = get_logger("xrf_tools")


@dataclass
class XrfUpdateResult:
    """Structured outcome of an XRF-update attempt."""

    ok: bool
    message: str
    output: str = ""
    skipped: bool = False


def _resolve_repo_root(cs_file: Path) -> Optional[Path]:
    """Return the SSAB.OX repo root when configured and containing *cs_file*.

    Falls back to ``None`` so the script's own ``git rev-parse`` autodetect
    can take over — that path handles the common developer setup where the
    edited file already lives in the checked-out repo.
    """
    repo = (getattr(config, "SSAB_OX_REPO_PATH", "") or "").strip()
    if not repo:
        return None
    try:
        root = Path(repo).resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    try:
        cs_file.resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    return root


def update_xrf_from_cs_edit(cs_file: str) -> XrfUpdateResult:
    """Run the XRF update script for the just-edited ``.cs`` file.

    Args:
        cs_file: Absolute path to the C# file Auto Apply modified.

    Returns:
        :class:`XrfUpdateResult` — ``skipped=True`` when the feature is
        disabled or the script is not on disk (so Auto Apply still
        reports success with only an informational note).
    """
    if not cs_file:
        return XrfUpdateResult(False, "No C# file path provided.", skipped=True)

    script = (getattr(config, "XRF_UPDATE_SCRIPT_PATH", "") or "").strip()
    if not script:
        return XrfUpdateResult(
            False, "XRF update is disabled (XRF_UPDATE_SCRIPT_PATH is empty).",
            skipped=True,
        )

    script_path = Path(script)
    if not script_path.is_file():
        return XrfUpdateResult(
            False, f"XRF update script not found: {script}", skipped=True,
        )

    cs_path = Path(cs_file)
    if not cs_path.is_file():
        return XrfUpdateResult(False, f"C# file not found: {cs_file}")

    pwsh_exe = (getattr(config, "XRF_UPDATE_POWERSHELL_EXE", "") or "pwsh").strip() or "pwsh"
    base_ref = (getattr(config, "XRF_UPDATE_BASE_REF", "") or "HEAD").strip() or "HEAD"
    timeout = int(getattr(config, "XRF_UPDATE_TIMEOUT_SECONDS", 60) or 60)

    command = [
        pwsh_exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script_path),
        "-CsFile", str(cs_path),
        "-BaseRef", base_ref,
        "-ForceRebuildIfInvalid",
    ]

    repo_root = _resolve_repo_root(cs_path)
    if repo_root is not None:
        command.extend(["-RepoRoot", str(repo_root)])

    logger.info(f"XRF update: {' '.join(command)}")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return XrfUpdateResult(False, f"XRF update script timed out after {timeout}s.")
    except FileNotFoundError:
        return XrfUpdateResult(
            False, f"PowerShell executable not found: {pwsh_exe}", skipped=True,
        )
    except OSError as exc:
        return XrfUpdateResult(False, f"XRF update script failed to start: {exc}")

    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()

    if result.returncode != 0:
        logger.error(f"XRF update failed (exit {result.returncode}): {output}")
        return XrfUpdateResult(
            False,
            f"XRF update script exited with code {result.returncode}.",
            output,
        )

    logger.info(f"XRF update ok: {output}")
    return XrfUpdateResult(True, "XRF file updated.", output)


__all__ = ["XrfUpdateResult", "update_xrf_from_cs_edit"]
