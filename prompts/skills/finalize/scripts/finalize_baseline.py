"""Finalize workflow — Step 8 orchestration (git → PR → close ticket).

Runs fail-fast in this exact order:

    1. Find validation ticket via WIQL           (abort here if not found)
    2. git add + commit + push                   (blocked from pushing to master/main/develop)
    3. Create PR: feature branch → develop       (linked to validation ticket via workItemRefs)
    4. Close the validation ticket               (state=Closed, reason=Completed)

The blocking work (subprocess + HTTP) runs synchronously inside
``asyncio.to_thread`` because the caller wraps the whole coroutine
in ``loop.run_in_executor`` — but the ``run`` function is declared
``async`` to match the other skills' entrypoint signature so the
shared sub-agent engine can ``await`` it uniformly.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, Tuple

import config
from models.session import StepStatus
from utils.logger import get_logger

from .git_helpers import (
    add_all_changes,
    commit_changes,
    get_current_branch,
    is_protected_branch,
    push_branch,
)
from .devops import (
    close_validation_ticket,
    create_pull_request,
    find_validation_ticket_by_baseline,
)

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("finalize.workflow")


# Commit message format (kept simple per user choice — no ticket_id
# needed pre-commit, so this doesn't create a chicken-and-egg with
# the WIQL lookup order).
COMMIT_MESSAGE_TEMPLATE = "Validation: {baseline}"

# PR title format — matches the observed Azure DevOps convention
# from the example ticket screenshot (e.g. "P02070-HPPL486P
# Validation #82243").
PR_TITLE_TEMPLATE = "{baseline} Validation #{ticket_id}"


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Orchestrate the full Step 8 finalize workflow. See module docstring."""
    baseline = session.baseline_name

    # Off-load the whole synchronous flow to a worker thread so the
    # main event loop keeps ticking (matches the pattern the other
    # blocking-heavy skills — copy/load/unload/branch — use).
    return await asyncio.to_thread(_run_sync, session, baseline)


def _run_sync(session: "Session", baseline: str) -> Tuple[bool, Dict[str, Any]]:
    """Synchronous body of :func:`run`. Runs inside a worker thread."""
    logger.info(f"Finalize workflow started for {baseline}")
    session.update_step("step_8_finalize", StepStatus.RUNNING)

    # Result dict is progressively enriched as sub-steps complete
    # so partial-success failures still surface everything that DID
    # work (useful for post-mortem).
    result: Dict[str, Any] = {
        "baseline": baseline,
        "branch_name": None,
        "commit_success": False,
        "push_success": False,
        "pr_id": None,
        "pr_url": None,
        "ticket_id": None,
        "ticket_closed": False,
        "message": None,
    }

    def _fail(msg: str) -> Tuple[bool, Dict[str, Any]]:
        """Common failure path: log, update session, return."""
        logger.error(f"Finalize failed for {baseline}: {msg}")
        result["message"] = msg
        session.update_step("step_8_finalize", StepStatus.FAILED, error=msg)
        return False, result

    # ── Sub-step 1: Find validation ticket (fail-fast) ────────────
    found, ticket_id, msg = find_validation_ticket_by_baseline(baseline)
    if not found:
        # Fail-fast per user requirement — do NOT touch git if the
        # workflow can't be linked back to a validation ticket.
        return _fail(
            f"No 03-Validation ticket found for {baseline}. "
            "Create one in Azure DevOps first, then re-run finalize."
        )
    result["ticket_id"] = ticket_id

    # ── Sub-step 2: git operations ────────────────────────────────
    repo_path = config.GIT_REPO_PATH

    current_branch = get_current_branch(repo_path)
    if not current_branch:
        return _fail(
            f"Could not determine current git branch in {repo_path}"
        )
    result["branch_name"] = current_branch

    # SAFETY: refuse to finalize from a protected branch. The
    # push_branch() helper re-checks this internally, but bailing
    # here means we don't commit either — cleaner error message.
    if is_protected_branch(current_branch):
        return _fail(
            f"🛑 Cannot finalize from protected branch '{current_branch}'. "
            f"Switch to a feature branch (e.g. run `branch {baseline}` first)."
        )

    add_ok, add_msg = add_all_changes(repo_path)
    if not add_ok:
        return _fail(add_msg)

    commit_msg = COMMIT_MESSAGE_TEMPLATE.format(baseline=baseline)
    commit_ok, commit_result_msg = commit_changes(repo_path, commit_msg)
    if not commit_ok:
        return _fail(commit_result_msg)
    result["commit_success"] = True

    push_ok, push_msg = push_branch(repo_path, current_branch)
    if not push_ok:
        return _fail(push_msg)
    result["push_success"] = True

    # ── Sub-step 3: Create Pull Request ───────────────────────────
    # Description = commit message summary (user requirement).
    pr_title = PR_TITLE_TEMPLATE.format(baseline=baseline, ticket_id=ticket_id)
    pr_ok, pr_result = create_pull_request(
        source_branch=current_branch,
        target_branch=config.GIT_MAIN_BRANCH,
        title=pr_title,
        description=commit_msg,
        ticket_id=ticket_id,
    )
    if not pr_ok:
        return _fail(
            f"Push succeeded but PR creation failed: "
            f"{pr_result.get('message', 'Unknown error')}"
        )
    result["pr_id"] = pr_result.get("pr_id")
    result["pr_url"] = pr_result.get("pr_url")

    # ── Sub-step 4: Close validation ticket ───────────────────────
    close_ok, close_msg = close_validation_ticket(ticket_id)
    if not close_ok:
        # Push + PR succeeded but ticket close failed — surface as
        # a soft failure (workflow overall didn't complete cleanly)
        # but keep the PR info so the user can see the delivery
        # progressed.
        return _fail(
            f"Branch pushed + PR #{result['pr_id']} created, but "
            f"ticket close failed: {close_msg}"
        )
    result["ticket_closed"] = True

    # ── Success ────────────────────────────────────────────────────
    result["message"] = (
        f"Branch {current_branch} pushed, PR #{result['pr_id']} created, "
        f"ticket {ticket_id} closed"
    )
    session.update_step(
        "step_8_finalize",
        StepStatus.COMPLETED,
        data={
            "branch_name": current_branch,
            "commit_message": commit_msg,
            "pr_id": result["pr_id"],
            "pr_url": result["pr_url"],
            "ticket_id": ticket_id,
        },
    )
    logger.info(f"Finalize completed for {baseline}: {result['message']}")
    return True, result


# ────────────────────────────────────────────────────────────────────
# Split entry points — used ONLY by the ``validate`` composite agent
# ────────────────────────────────────────────────────────────────────
# The validate composite pauses TWICE inside finalize:
#     1. "Proceed to Push"        → runs push_stage
#     2. "Raise PR & Close Ticket" → runs pr_stage
#
# The existing ``run()`` above is untouched — the standalone
# ``finalize <baseline>`` command and the analysis / reporting flows
# still call ``run()`` and get the exact same atomic behaviour they
# always had. These new functions are ONLY invoked by the validate
# composite via ``agents.validation_agent.validate_agent``.
# ────────────────────────────────────────────────────────────────────


async def run_push_stage(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Stage 1 of split finalize: find ticket + git add/commit/push.

    Off-loads the blocking subprocess/HTTP work to a worker thread
    (identical pattern to :func:`run`).
    """
    return await asyncio.to_thread(_run_push_stage_sync, session, session.baseline_name)


async def run_pr_stage(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Stage 2 of split finalize: create PR + close validation ticket.

    Assumes :func:`run_push_stage` already succeeded (branch pushed).
    """
    return await asyncio.to_thread(_run_pr_stage_sync, session, session.baseline_name)


def _run_push_stage_sync(session: "Session", baseline: str) -> Tuple[bool, Dict[str, Any]]:
    """Synchronous body of :func:`run_push_stage` — sub-steps 1-2 only.

    Marks ``step_8_finalize`` as RUNNING (not COMPLETED) — the PR
    stage will finalise the state on success. On failure the step
    is marked FAILED like the atomic ``run()`` does.
    """
    logger.info(f"Finalize push-stage started for {baseline}")
    session.update_step("step_8_finalize", StepStatus.RUNNING)

    result: Dict[str, Any] = {
        "baseline": baseline,
        "branch_name": None,
        "commit_success": False,
        "push_success": False,
        "ticket_id": None,
        "commit_message": None,
        "message": None,
    }

    def _fail(msg: str) -> Tuple[bool, Dict[str, Any]]:
        logger.error(f"Finalize push-stage failed for {baseline}: {msg}")
        result["message"] = msg
        session.update_step("step_8_finalize", StepStatus.FAILED, error=msg)
        return False, result

    # Sub-step 1: Find validation ticket (fail-fast, no git touched yet)
    found, ticket_id, msg = find_validation_ticket_by_baseline(baseline)
    if not found:
        return _fail(
            f"No 03-Validation ticket found for {baseline}. "
            "Create one in Azure DevOps first, then re-run validate."
        )
    result["ticket_id"] = ticket_id

    # Sub-step 2: git ops
    repo_path = config.GIT_REPO_PATH

    current_branch = get_current_branch(repo_path)
    if not current_branch:
        return _fail(f"Could not determine current git branch in {repo_path}")
    result["branch_name"] = current_branch

    if is_protected_branch(current_branch):
        return _fail(
            f"🛑 Cannot finalize from protected branch '{current_branch}'. "
            f"Switch to a feature branch (e.g. run `branch {baseline}` first)."
        )

    add_ok, add_msg = add_all_changes(repo_path)
    if not add_ok:
        return _fail(add_msg)

    commit_msg = COMMIT_MESSAGE_TEMPLATE.format(baseline=baseline)
    commit_ok, commit_result_msg = commit_changes(repo_path, commit_msg)
    if not commit_ok:
        return _fail(commit_result_msg)
    result["commit_success"] = True
    result["commit_message"] = commit_msg

    push_ok, push_msg = push_branch(repo_path, current_branch)
    if not push_ok:
        return _fail(push_msg)
    result["push_success"] = True

    result["message"] = (
        f"Branch {current_branch} pushed. "
        f"Ready to raise PR against ticket {ticket_id}."
    )
    logger.info(f"Finalize push-stage completed for {baseline}: {result['message']}")
    # NOTE: session step stays RUNNING — pr_stage will mark COMPLETED.
    return True, result


def _run_pr_stage_sync(session: "Session", baseline: str) -> Tuple[bool, Dict[str, Any]]:
    """Synchronous body of :func:`run_pr_stage` — sub-steps 3-4 only.

    Re-queries the ticket + current branch (both cheap) rather than
    trusting stale session state. On success marks
    ``step_8_finalize`` COMPLETED.
    """
    logger.info(f"Finalize PR-stage started for {baseline}")

    result: Dict[str, Any] = {
        "baseline": baseline,
        "branch_name": None,
        "ticket_id": None,
        "pr_id": None,
        "pr_url": None,
        "ticket_closed": False,
        "message": None,
    }

    def _fail(msg: str) -> Tuple[bool, Dict[str, Any]]:
        logger.error(f"Finalize PR-stage failed for {baseline}: {msg}")
        result["message"] = msg
        session.update_step("step_8_finalize", StepStatus.FAILED, error=msg)
        return False, result

    # Re-fetch ticket + branch (both cheap operations)
    found, ticket_id, msg = find_validation_ticket_by_baseline(baseline)
    if not found:
        return _fail(
            f"Could not re-find validation ticket for {baseline}: {msg}"
        )
    result["ticket_id"] = ticket_id

    repo_path = config.GIT_REPO_PATH
    current_branch = get_current_branch(repo_path)
    if not current_branch:
        return _fail(f"Could not determine current git branch in {repo_path}")
    result["branch_name"] = current_branch

    # Sub-step 3: Create PR
    commit_msg = COMMIT_MESSAGE_TEMPLATE.format(baseline=baseline)
    pr_title = PR_TITLE_TEMPLATE.format(baseline=baseline, ticket_id=ticket_id)
    pr_ok, pr_result = create_pull_request(
        source_branch=current_branch,
        target_branch=config.GIT_MAIN_BRANCH,
        title=pr_title,
        description=commit_msg,
        ticket_id=ticket_id,
    )
    if not pr_ok:
        return _fail(
            f"PR creation failed: {pr_result.get('message', 'Unknown error')}"
        )
    result["pr_id"] = pr_result.get("pr_id")
    result["pr_url"] = pr_result.get("pr_url")

    # Sub-step 4: Close validation ticket
    close_ok, close_msg = close_validation_ticket(ticket_id)
    if not close_ok:
        # Push already done + PR created — soft-fail: preserve PR info
        return _fail(
            f"PR #{result['pr_id']} created, but ticket close failed: {close_msg}"
        )
    result["ticket_closed"] = True

    result["message"] = (
        f"PR #{result['pr_id']} created, ticket {ticket_id} closed"
    )
    session.update_step(
        "step_8_finalize",
        StepStatus.COMPLETED,
        data={
            "branch_name": current_branch,
            "commit_message": commit_msg,
            "pr_id": result["pr_id"],
            "pr_url": result["pr_url"],
            "ticket_id": ticket_id,
        },
    )
    logger.info(f"Finalize PR-stage completed for {baseline}: {result['message']}")
    return True, result


__all__ = ["run", "run_push_stage", "run_pr_stage"]
