"""Azure DevOps HTTP helpers for the finalize skill (Step 8 only).

Fully self-contained — no import from ``tools/devops_tools.py``.
Three public functions plus their private HTTP helpers:

* :func:`find_validation_ticket_by_baseline` — WIQL search for the
  ``03-Validation`` work item matching a baseline. Read-only.
* :func:`create_pull_request` — opens a PR from the current feature
  branch into ``develop`` (or whatever ``config.GIT_MAIN_BRANCH`` is
  set to), with the validation ticket linked via ``workItemRefs`` so
  the PR appears in the ticket's Development section. Tolerates
  "PR already exists" (returns the existing PR).
* :func:`close_validation_ticket` — PATCH work item state to
  ``Closed`` with reason ``Completed``. Idempotent — "already closed"
  is treated as success.

Configuration
-------------
Reads ``config.AZURE_DEVOPS_{ORG,PROJECT,REPO,PAT}`` and
``config.GIT_MAIN_BRANCH`` at call time. No module-level HTTP
sessions — each call builds its own headers (cheap, and keeps the
module import-free of network state).
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional, Tuple

import requests

import config
from utils.logger import get_logger

logger = get_logger("finalize.devops")


# ────────────────────────────────────────────────────────────────────
# Close-ticket configuration
# ────────────────────────────────────────────────────────────────────
# Values used when transitioning the 03-Validation work item to
# closed at the end of the finalize workflow. The ExperSolve/SSAB-OX
# Task workflow enforces THREE required custom fields on state
# transition — the PATCH must set them or ADO rejects the whole
# request with TF401320.
#
# Fields (verified against the ADO ticket UI):
#   Custom.IssuesCreated  — dropdown, must be "Yes" or "No". Set to
#                           "Yes" because the validate flow ALWAYS
#                           surfaces an ai-analysed issue (the run
#                           step failed at least once and an AI fix
#                           was applied). Change to "No" if your
#                           project treats successful validations as
#                           "no issues created".
#   Custom.ExecutionTime  — NUMERIC field (minutes as int). ADO
#                           rejects strings with TF401326
#                           "InvalidType". Placeholder 5 (min) is
#                           used; if you want the real elapsed time
#                           we can compute it from session.
#   Custom.AIAgentUse     — dropdown, values seen in ticket UI:
#                           "Used - Helped", "Used - No Help",
#                           "Not Used". "Used - Helped" is the
#                           correct value for a successful validate
#                           flow that went through the AI fix path.
CLOSE_STATE   = "Closed"
CLOSE_REASON  = "Completed"
CLOSE_CUSTOM_FIELDS: Dict[str, Any] = {
    "Custom.IssuesCreated": "Yes",
    "Custom.ExecutionTime": 5,          # numeric (minutes)
    "Custom.AIAgentUse":    "Used - Helped",
}


# ────────────────────────────────────────────────────────────────────
# Private HTTP helpers
# ────────────────────────────────────────────────────────────────────


def _get_auth_header() -> Dict[str, str]:
    """Basic-auth header built from the configured Azure DevOps PAT."""
    pat = config.AZURE_DEVOPS_PAT
    encoded = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }


def _base_url() -> str:
    """Root REST URL for the configured Azure DevOps org + project."""
    return f"https://dev.azure.com/{config.AZURE_DEVOPS_ORG}/{config.AZURE_DEVOPS_PROJECT}/_apis"


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


def find_validation_ticket_by_baseline(
    baseline_name: str,
) -> Tuple[bool, int, str]:
    """WIQL search for the ``03-Validation`` work item matching a baseline.

    Returns
    -------
    ``(True,  ticket_id, message)`` on hit
    ``(False, 0,         message)`` on miss or transport error
    """
    logger.info(f"Searching for validation ticket with baseline: {baseline_name}")

    try:
        url = f"{_base_url()}/wit/wiql?api-version=7.0"
        query = {
            "query": (
                f"SELECT [System.Id], [System.Title] FROM WorkItems "
                f"WHERE [System.Title] CONTAINS '{baseline_name}' "
                f"AND [System.Title] CONTAINS '03-Validation'"
            )
        }

        response = requests.post(url, headers=_get_auth_header(), json=query)
        if response.status_code != 200:
            msg = f"Failed to search validation tickets: {response.text}"
            logger.error(msg)
            return False, 0, msg

        data = response.json()
        work_items = data.get("workItems", [])
        if not work_items:
            msg = f"No validation ticket found for baseline: {baseline_name}"
            logger.warning(msg)
            return False, 0, msg

        ticket_id = int(work_items[0]["id"])
        logger.info(f"Found validation ticket ID: {ticket_id}")
        return True, ticket_id, f"Found validation ticket {ticket_id}"

    except Exception as exc:  # noqa: BLE001 — HTTP layer must not crash the skill
        msg = f"Error searching validation ticket: {exc}"
        logger.error(msg)
        return False, 0, msg


def create_pull_request(
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    ticket_id: Optional[int] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Open a PR from ``source_branch`` into ``target_branch``.

    When ``ticket_id`` is given, the PR is linked to that work item
    via the ``workItemRefs`` field — this is what populates the
    "Pull Request" entry in the ticket's Development section.

    "PR already exists" (409 / 422 from Azure DevOps) is treated as
    **success** and returns the existing PR info — makes the whole
    finalize flow idempotent on re-run after a partial failure.

    Returns
    -------
    ``(True,  {pr_id, pr_url, message})``  on success (or already-exists)
    ``(False, {                 message})`` on failure
    """
    org = config.AZURE_DEVOPS_ORG
    project = config.AZURE_DEVOPS_PROJECT
    repo = config.AZURE_DEVOPS_REPO
    logger.info(
        f"Creating PR: {source_branch} → {target_branch} "
        f"(repo={repo}, workItem={ticket_id})"
    )

    payload: Dict[str, Any] = {
        "sourceRefName": f"refs/heads/{source_branch}",
        "targetRefName": f"refs/heads/{target_branch}",
        "title": title,
        "description": description,
    }
    if ticket_id:
        payload["workItemRefs"] = [{"id": str(ticket_id)}]

    try:
        url = f"{_base_url()}/git/repositories/{repo}/pullrequests?api-version=7.0"
        response = requests.post(url, headers=_get_auth_header(), json=payload)

        if response.status_code in (200, 201):
            data = response.json()
            pr_id = data.get("pullRequestId")
            pr_url = (
                f"https://dev.azure.com/{org}/{project}/_git/{repo}"
                f"/pullrequest/{pr_id}"
            )
            msg = f"Pull Request {pr_id} created"
            logger.info(msg)
            return True, {"pr_id": pr_id, "pr_url": pr_url, "message": msg}

        # ── Idempotent "already exists" handling ──────────────────
        # Azure DevOps returns 409 (or 422 with a specific message)
        # when a PR from the same source branch already exists.
        # Try to look it up and return it instead of failing.
        body_lower = (response.text or "").lower()
        already_exists = (
            response.status_code == 409
            or "already" in body_lower and "pull request" in body_lower
        )
        if already_exists:
            existing = _find_existing_pr(source_branch, target_branch)
            if existing:
                return True, existing

        msg = f"Failed to create PR ({response.status_code}): {response.text}"
        logger.error(msg)
        return False, {"message": msg}

    except Exception as exc:  # noqa: BLE001
        msg = f"Error creating PR: {exc}"
        logger.error(msg)
        return False, {"message": msg}


def _find_existing_pr(
    source_branch: str, target_branch: str
) -> Optional[Dict[str, Any]]:
    """Look up an ACTIVE PR from ``source_branch`` → ``target_branch``.

    Used by :func:`create_pull_request` when the create call reports
    "already exists". Returns ``{pr_id, pr_url, message}`` on hit,
    ``None`` on miss or transport error.
    """
    org = config.AZURE_DEVOPS_ORG
    project = config.AZURE_DEVOPS_PROJECT
    repo = config.AZURE_DEVOPS_REPO
    try:
        url = f"{_base_url()}/git/repositories/{repo}/pullrequests"
        params = {
            "searchCriteria.status": "active",
            "searchCriteria.sourceRefName": f"refs/heads/{source_branch}",
            "searchCriteria.targetRefName": f"refs/heads/{target_branch}",
            "api-version": "7.0",
        }
        response = requests.get(url, headers=_get_auth_header(), params=params)
        if response.status_code != 200:
            return None
        prs = response.json().get("value") or []
        if not prs:
            return None
        pr = prs[0]
        pr_id = pr.get("pullRequestId")
        pr_url = (
            f"https://dev.azure.com/{org}/{project}/_git/{repo}"
            f"/pullrequest/{pr_id}"
        )
        msg = f"Pull Request {pr_id} already exists (reusing)"
        logger.info(msg)
        return {"pr_id": pr_id, "pr_url": pr_url, "message": msg}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not look up existing PR: {exc}")
        return None


def close_validation_ticket(
    ticket_id: int, reason: str = "Completed"
) -> Tuple[bool, str]:
    """PATCH work item state to ``Closed`` plus any project-specific
    required custom fields.

    Values come from the module-level ``CLOSE_STATE`` /
    ``CLOSE_REASON`` / ``CLOSE_CUSTOM_FIELDS`` constants at the top
    of this file — edit them there to change close behaviour.

    Idempotent — "already closed" is treated as success. Any HTTP
    failure returns ``(False, message)`` so the caller can decide
    whether the (possibly-partial) finalize outcome is acceptable.
    """
    # Module-level constants are authoritative; ``reason`` parameter
    # kept for backwards compatibility with any older caller.
    close_state   = CLOSE_STATE
    close_reason  = CLOSE_REASON or reason
    custom_fields = CLOSE_CUSTOM_FIELDS

    logger.info(
        f"Closing validation ticket {ticket_id}: state='{close_state}', "
        f"reason='{close_reason}', custom_fields={list(custom_fields)}"
    )

    try:
        url = (
            f"{_base_url()}/wit/workitems/{ticket_id}?api-version=7.0"
        )
        headers = _get_auth_header()
        headers["Content-Type"] = "application/json-patch+json"

        payload: list = [
            {"op": "add", "path": "/fields/System.State",  "value": close_state},
            {"op": "add", "path": "/fields/System.Reason", "value": close_reason},
        ]
        # Append each configured custom field to the same PATCH so the
        # state transition + required-field validation succeed atomically.
        for ref_name, value in custom_fields.items():
            payload.append({
                "op": "add",
                "path": f"/fields/{ref_name}",
                "value": value,
            })

        response = requests.patch(url, headers=headers, json=payload)

        if response.status_code in (200, 201):
            msg = f"Ticket {ticket_id} closed successfully"
            logger.info(msg)
            return True, msg

        # Idempotent — the work item is already in the Closed state.
        if "already" in (response.text or "").lower():
            msg = f"Ticket {ticket_id} already closed"
            logger.info(msg)
            return True, msg

        msg = f"Failed to close ticket {ticket_id}: {response.text}"
        logger.error(msg)
        return False, msg

    except Exception as exc:  # noqa: BLE001
        msg = f"Error closing ticket {ticket_id}: {exc}"
        logger.error(msg)
        return False, msg


__all__ = [
    "find_validation_ticket_by_baseline",
    "create_pull_request",
    "close_validation_ticket",
]
