"""Git subprocess helpers for the finalize skill.

Self-contained — no import from ``tools/devops_tools.py``. All five
public functions are thin wrappers around ``git`` CLI invocations
via ``subprocess.run`` with the correct working directory.

Safety
------
:func:`is_protected_branch` and the explicit re-check inside
:func:`push_branch` are the ONLY guardrail preventing accidental
pushes to ``master`` / ``main`` / ``develop``. Do NOT bypass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

from utils.logger import get_logger

logger = get_logger("finalize.git")

# Branches this skill refuses to push to. Kept as a module-level
# constant so callers can inspect the list without instantiating
# anything. Case-insensitive match.
PROTECTED_BRANCHES = ("master", "main", "develop")


# ────────────────────────────────────────────────────────────────────
# Read helpers
# ────────────────────────────────────────────────────────────────────


def get_current_branch(repo_path: Path) -> str:
    """Return the checked-out branch name in ``repo_path`` (empty on error)."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001 — git absence shouldn't crash us
        logger.error(f"Could not read current branch in {repo_path}: {exc}")
        return ""


def is_protected_branch(branch_name: str) -> bool:
    """True if ``branch_name`` is one of ``master`` / ``main`` / ``develop``."""
    return (branch_name or "").strip().lower() in PROTECTED_BRANCHES


# ────────────────────────────────────────────────────────────────────
# Write helpers
# ────────────────────────────────────────────────────────────────────


# Pathspec patterns EXCLUDED from finalize's ``git add``. Runtime
# logs and build artefacts must never ride along with the developer's
# source-code changes — those would pollute the PR and (worse) get
# pushed to the shared branch.
#
# NOTE: ``*.xrf`` (C# cross-reference files) ARE included on purpose
# — they contain COBOL→C# mapping data that is part of the source
# tree and must land in the PR alongside ``*.cs`` edits.
#
# Tune this list here to match your project's build output layout.
# ``**/pattern`` matches at any depth.
FINALIZE_EXCLUDE_PATHSPECS: Tuple[str, ...] = (
    "*.log",              # loose log files
    "**/logs/**",         # log folder contents (SSAB.OX/logs, src/*/logs, etc.)
    "**/logs",            # empty log folders themselves
    "**/bin/**",          # dotnet build output (defensive — usually gitignored)
    "**/obj/**",          # dotnet intermediate build (defensive — usually gitignored)
    "**/TestResults/**",  # test runner output
    "**/Debug/**",        # Release/Debug build folders (defensive)
    "**/Release/**",
    "**/*.tmp",           # temp files
    "**/*.bak",           # backup files
    ".vs/**",
    "**/.vs/**",          # Visual Studio temporary files and folders ignored while add 
    "**/CopilotSnapshots/**",  # Copilot snapshot files
    "*.mpack",          
    "**/*.mpack",
    "**/*.json",
)


def _get_staged_files(repo_path: Path) -> list:
    """Return the list of files currently staged for the next commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f]
    except Exception:  # noqa: BLE001
        pass
    return []


def add_all_changes(repo_path: Path) -> Tuple[bool, str]:
    """Stage source-code changes for finalize (with pathspec exclusions).

    Uses ``git add . :(exclude)<pattern> …`` so auto-generated files
    (xref, logs, bin/obj) are NEVER staged alongside the developer's
    real changes. Excluded patterns are defined in
    :data:`FINALIZE_EXCLUDE_PATHSPECS` at the top of this module.
    Logs the exact list of staged files so validators can verify
    what will land in the PR.
    """
    try:
        args = ["git", "add", "."]
        for pattern in FINALIZE_EXCLUDE_PATHSPECS:
            args.append(f":(exclude){pattern}")
        result = subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
        )
        if result.returncode != 0:
            msg = (
                f"git add failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            logger.error(msg)
            return False, msg

        staged = _get_staged_files(repo_path)
        if not staged:
            logger.info(
                "No source files staged after exclusions "
                "(all changed paths were build artefacts / logs)"
            )
            return True, "No source changes to stage (only build artefacts)"

        preview = ", ".join(staged[:8])
        if len(staged) > 8:
            preview += f" (+{len(staged) - 8} more)"
        logger.info(f"Staged {len(staged)} source file(s): {preview}")
        return True, f"Staged {len(staged)} file(s)"

    except Exception as exc:  # noqa: BLE001
        msg = f"git add error: {exc}"
        logger.error(msg)
        return False, msg


def commit_changes(repo_path: Path, message: str) -> Tuple[bool, str]:
    """``git commit -m <message>``.

    "nothing to commit" is treated as **success** (idempotent re-runs
    after the first successful finalize should NOT fail the workflow).
    """
    try:
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        combined = (result.stdout + result.stderr).lower()

        # Idempotent case — user re-runs finalize and there are no
        # new changes since the last commit.
        if "nothing to commit" in combined or "no changes added" in combined:
            logger.info("Nothing to commit (idempotent re-run)")
            return True, "Nothing to commit"

        if result.returncode == 0:
            logger.info(f"Committed with message: {message!r}")
            return True, f"Committed: {message}"

        msg = f"git commit failed: {result.stderr.strip() or result.stdout.strip()}"
        logger.error(msg)
        return False, msg
    except Exception as exc:  # noqa: BLE001
        msg = f"git commit error: {exc}"
        logger.error(msg)
        return False, msg


def push_branch(repo_path: Path, branch_name: str) -> Tuple[bool, str]:
    """``git push -u origin <branch_name>`` with a safety re-check.

    Refuses to push if ``branch_name`` is in :data:`PROTECTED_BRANCHES`,
    even if the caller thought they had already validated. Belt-and-
    suspenders — this is the last line of defence before we hit the
    Azure DevOps remote.
    """
    if is_protected_branch(branch_name):
        msg = f"🛑 SAFETY BLOCK: refusing to push to protected branch '{branch_name}'"
        logger.error(msg)
        return False, msg

    try:
        logger.info(f"Pushing branch: {branch_name}")
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            msg = f"Branch {branch_name} pushed successfully"
            logger.info(msg)
            return True, msg
        msg = f"git push failed: {result.stderr.strip() or result.stdout.strip()}"
        logger.error(msg)
        return False, msg
    except Exception as exc:  # noqa: BLE001
        msg = f"git push error: {exc}"
        logger.error(msg)
        return False, msg


__all__ = [
    "PROTECTED_BRANCHES",
    "get_current_branch",
    "is_protected_branch",
    "add_all_changes",
    "commit_changes",
    "push_branch",
]
