"""Branch operation — Step 3a of the validation workflow (single-file).

Merges everything the branch skill needs into ONE self-contained
module. External dependencies are strictly the project-wide
:mod:`config` (Azure DevOps org / project / repo / PAT + git repo path)
and :mod:`utils.logger` — same status as every other migrated skill.
No import from :mod:`tools.devops_tools` or any orchestrator internal.

What lives here
---------------
* Azure DevOps REST helpers (auth, branch create/pull, ticket
  search / link / activate). Previously in ``tools/devops_tools.py``;
  duplicated here so the branch folder can be lifted into another
  project as a single self-contained unit.
* Local git helpers (fetch/checkout/pull) via ``subprocess``.
* :func:`run` — the public entrypoint awaited by
  :meth:`Orchestrator.run_skill`.

The behaviour, log lines, error messages, session state transitions,
and the returned ``(bool, dict)`` shape are byte-identical to the
previous ``DevOpsAgent.run_step_3a_branch`` implementation so existing
Chainlit UI rendering (``handle_branch`` in ``app.py``) continues to
work unchanged.

Contract
--------
``async def run(session) -> Tuple[bool, Dict[str, Any]]``

Dict keys returned: ``baseline``, ``branch_name``, ``ticket_id``,
``branch_url``, ``ticket_url``, ``message``.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from typing import TYPE_CHECKING, Any, Dict, Tuple

import requests

import config
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("branch")


# Protected branches — the branch skill NEVER creates or pushes to
# these. Local copy so the skill has no dependency on the shared
# devops_tools list.
_PROTECTED_BRANCHES = ["master", "main", "develop"]


# ────────────────────────────────────────────────────────────────────
# Azure DevOps REST helpers (local, self-contained)
# ────────────────────────────────────────────────────────────────────


def _get_auth_header() -> dict:
    """Basic-auth header built from the configured Azure DevOps PAT."""
    pat = config.AZURE_DEVOPS_PAT
    encoded = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }


def _get_api_url() -> str:
    """Base URL for the Azure DevOps Git repositories API."""
    org = config.AZURE_DEVOPS_ORG
    project = config.AZURE_DEVOPS_PROJECT
    repo = config.AZURE_DEVOPS_REPO
    return f"https://dev.azure.com/{org}/{project}/_apis/git/repositories/{repo}"


def _get_branch_name(baseline_name: str) -> str:
    """Return the branch name for a baseline (e.g. ``SG02/P02001-HPPL042P``)."""
    group = config.get_baseline_group(baseline_name)
    return f"{group}/{baseline_name}"


def _is_protected_branch(branch_name: str) -> bool:
    """True iff the branch is in the protected list (never touch)."""
    return branch_name.lower() in _PROTECTED_BRANCHES


def _branch_exists_remote(branch_name: str) -> bool:
    """Check if the branch exists in Azure DevOps."""
    try:
        url = f"{_get_api_url()}/refs?filter=heads/{branch_name}&api-version=7.0"
        response = requests.get(url, headers=_get_auth_header())
        if response.status_code == 200:
            data = response.json()
            return len(data.get("value", [])) > 0
        return False
    except Exception as e:  # noqa: BLE001 -- best-effort HTTP call
        logger.error(f"Error checking branch: {e}")
        return False


def _get_repo_id() -> str:
    """Return the Git repository id (used to build artifact links)."""
    try:
        url = f"{_get_api_url()}?api-version=7.0"
        response = requests.get(url, headers=_get_auth_header())
        if response.status_code == 200:
            return response.json().get("id", "")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _get_project_id() -> str:
    """Return the Azure DevOps project id (used to build artifact links)."""
    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT
        url = f"https://dev.azure.com/{org}/_apis/projects/{project}?api-version=7.0"
        response = requests.get(url, headers=_get_auth_header())
        if response.status_code == 200:
            return response.json().get("id", "")
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _create_branch_remote(baseline_name: str) -> Tuple[bool, str]:
    """Create a new feature branch in Azure DevOps from the main branch.

    SAFETY: only *creates* a new ref; never modifies master/main/develop.
    """
    branch_name = _get_branch_name(baseline_name)
    logger.info(f"Creating branch: {branch_name}")

    if _is_protected_branch(branch_name):
        msg = "SAFETY: Cannot create branch named master/main/develop"
        logger.error(msg)
        return False, msg

    if _branch_exists_remote(branch_name):
        msg = f"Branch {branch_name} already exists"
        logger.info(msg)
        return True, msg

    try:
        main_branch = config.GIT_MAIN_BRANCH
        url = f"{_get_api_url()}/refs?filter=heads/{main_branch}&api-version=7.0"
        response = requests.get(url, headers=_get_auth_header())

        if response.status_code != 200:
            msg = f"Failed to get {main_branch} branch: {response.text}"
            logger.error(msg)
            return False, msg

        data = response.json()
        if not data.get("value"):
            msg = f"Branch {main_branch} not found"
            logger.error(msg)
            return False, msg

        develop_commit = data["value"][0]["objectId"]
        logger.info(f"Develop commit: {develop_commit}")

        url = f"{_get_api_url()}/refs?api-version=7.0"
        payload = [
            {
                "name": f"refs/heads/{branch_name}",
                "oldObjectId": "0000000000000000000000000000000000000000",
                "newObjectId": develop_commit,
            }
        ]

        response = requests.post(url, headers=_get_auth_header(), json=payload)

        if response.status_code in (200, 201):
            msg = f"Branch {branch_name} created successfully"
            logger.info(msg)
            return True, msg
        else:
            msg = f"Failed to create branch: {response.text}"
            logger.error(msg)
            return False, msg

    except Exception as e:  # noqa: BLE001
        msg = f"Error creating branch: {e}"
        logger.error(msg)
        return False, msg


def _pull_branch_local(baseline_name: str) -> Tuple[bool, str]:
    """Fetch, checkout, and pull the branch into the local git repo."""
    branch_name = _get_branch_name(baseline_name)
    repo_path = config.GIT_REPO_PATH
    logger.info(f"Pulling branch {branch_name} to {repo_path}")

    try:
        # Fetch all (read-only, safe)
        result = subprocess.run(
            ["git", "fetch", "--all"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            msg = f"Git fetch failed: {result.stderr}"
            logger.error(msg)
            return False, msg

        # Does the branch exist locally?
        check_local = subprocess.run(
            ["git", "branch", "--list", branch_name],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        branch_exists_locally = bool(check_local.stdout.strip())

        if branch_exists_locally:
            logger.info(f"Branch {branch_name} exists locally, checking out...")
            result = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                msg = f"Git checkout failed: {result.stderr}"
                logger.error(msg)
                return False, msg
        else:
            logger.info(
                f"Branch {branch_name} not found locally, creating tracking branch..."
            )
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name, f"origin/{branch_name}"],
                cwd=repo_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                msg = f"Git checkout failed: {result.stderr}"
                logger.error(msg)
                return False, msg

        # Pull latest
        subprocess.run(
            ["git", "pull"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )

        msg = f"Branch {branch_name} checked out and pulled"
        logger.info(msg)
        return True, msg

    except Exception as e:  # noqa: BLE001
        msg = f"Error pulling branch: {e}"
        logger.error(msg)
        return False, msg


def _find_ticket_by_baseline(baseline_name: str) -> Tuple[bool, int, str]:
    """Find the validation ticket for a baseline (WIQL search).

    Distinct from the reporting-ticket finder used by the reporting
    skill: this one looks for ``03-Validation`` tickets, not
    ``05-Reporting`` ones.
    """
    logger.info(f"Searching for ticket with baseline: {baseline_name}")

    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT
        url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0"

        query = {
            "query": (
                f"SELECT [System.Id], [System.Title] FROM WorkItems "
                f"WHERE [System.Title] CONTAINS '{baseline_name}' "
                f"AND [System.Title] CONTAINS '03-Validation'"
            )
        }

        response = requests.post(url, headers=_get_auth_header(), json=query)
        if response.status_code != 200:
            msg = f"Failed to search tickets: {response.text}"
            logger.error(msg)
            return False, 0, msg

        data = response.json()
        work_items = data.get("workItems", [])
        if not work_items:
            msg = f"No ticket found for baseline: {baseline_name}"
            logger.warning(msg)
            return False, 0, msg

        ticket_id = work_items[0]["id"]
        logger.info(f"Found ticket ID: {ticket_id}")
        return True, ticket_id, f"Found ticket {ticket_id}"

    except Exception as e:  # noqa: BLE001
        msg = f"Error searching ticket: {e}"
        logger.error(msg)
        return False, 0, msg


def _link_branch_to_ticket(baseline_name: str, ticket_id: int) -> Tuple[bool, str]:
    """Add an ArtifactLink from the ticket to the newly created branch."""
    branch_name = _get_branch_name(baseline_name)
    logger.info(f"Linking branch {branch_name} to ticket {ticket_id}")

    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT

        repo_id = _get_repo_id()
        project_id = _get_project_id()
        if not repo_id or not project_id:
            msg = "Failed to get repo/project ID"
            logger.error(msg)
            return False, msg

        artifact_uri = (
            f"vstfs:///Git/Ref/{project_id}%2F{repo_id}"
            f"%2FGB{branch_name.replace('/', '%2F')}"
        )

        url = (
            f"https://dev.azure.com/{org}/{project}"
            f"/_apis/wit/workitems/{ticket_id}?api-version=7.0"
        )
        headers = _get_auth_header()
        headers["Content-Type"] = "application/json-patch+json"

        payload = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "ArtifactLink",
                    "url": artifact_uri,
                    "attributes": {"name": "Branch"},
                },
            }
        ]

        response = requests.patch(url, headers=headers, json=payload)

        if response.status_code in (200, 201):
            msg = f"Branch {branch_name} linked to ticket {ticket_id}"
            logger.info(msg)
            return True, msg
        elif "RelationAlreadyExistsException" in response.text:
            msg = f"Branch {branch_name} already linked to ticket {ticket_id}"
            logger.info(msg)
            return True, msg
        else:
            msg = f"Failed to link branch: {response.text}"
            logger.error(msg)
            return False, msg

    except Exception as e:  # noqa: BLE001
        msg = f"Error linking branch: {e}"
        logger.error(msg)
        return False, msg


def _set_ticket_active(ticket_id: int) -> Tuple[bool, str]:
    """Set the ticket state to 'Active'."""
    logger.info(f"Setting ticket {ticket_id} to Active")

    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT
        url = (
            f"https://dev.azure.com/{org}/{project}"
            f"/_apis/wit/workitems/{ticket_id}?api-version=7.0"
        )
        headers = _get_auth_header()
        headers["Content-Type"] = "application/json-patch+json"

        payload = [
            {
                "op": "add",
                "path": "/fields/System.State",
                "value": "Active",
            }
        ]

        response = requests.patch(url, headers=headers, json=payload)

        if response.status_code in (200, 201):
            msg = f"Ticket {ticket_id} set to Active"
            logger.info(msg)
            return True, msg
        elif "already" in response.text.lower() or response.status_code == 200:
            msg = f"Ticket {ticket_id} already Active"
            logger.info(msg)
            return True, msg
        else:
            msg = f"Failed to update ticket state: {response.text}"
            logger.error(msg)
            return False, msg

    except Exception as e:  # noqa: BLE001
        msg = f"Error updating ticket state: {e}"
        logger.error(msg)
        return False, msg


def _create_and_pull_branch(baseline_name: str) -> Tuple[bool, str]:
    """Full Step 3a: create branch → pull → link → activate ticket.

    Composes the local helpers above. Git operations are CRITICAL;
    ticket operations are OPTIONAL (best-effort) and never fail the
    step — this matches the previous behaviour exactly.
    """
    # 1. Create in Azure DevOps
    success, msg = _create_branch_remote(baseline_name)
    if not success:
        return False, msg

    # 2. Pull to local
    success, msg = _pull_branch_local(baseline_name)
    if not success:
        return False, msg

    branch_name = _get_branch_name(baseline_name)

    # 3. Find + link ticket (optional)
    ticket_success, ticket_id, ticket_msg = _find_ticket_by_baseline(baseline_name)

    if ticket_success:
        link_success, link_msg = _link_branch_to_ticket(baseline_name, ticket_id)
        _set_ticket_active(ticket_id)  # best-effort, never fails the step
        if link_success:
            return True, (
                f"Branch {branch_name} created, pulled, and linked to "
                f"ticket {ticket_id}"
            )
        else:
            logger.warning(f"Branch created but linking failed: {link_msg}")
            return True, (
                f"Branch {branch_name} created and pulled. "
                f"(Linking failed: {link_msg})"
            )
    else:
        logger.warning(f"Branch created but no ticket found: {ticket_msg}")
        return True, (
            f"Branch {branch_name} created and pulled. (No ticket found)"
        )


# ────────────────────────────────────────────────────────────────────
# Public entrypoint (formerly DevOpsAgent.run_step_3a_branch)
# ────────────────────────────────────────────────────────────────────


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute Step 3a — create Azure DevOps branch and pull to local.

    Sub-steps (identical to the previous
    ``DevOpsAgent.run_step_3a_branch`` implementation):

      1. Mark ``step_3a_branch`` as :class:`StepStatus.RUNNING`.
      2. Call the local :func:`_create_and_pull_branch` composer which
         internally: creates the branch in Azure DevOps, pulls it to
         local, finds a matching validation ticket by baseline, links
         the branch to the ticket, and sets the ticket to Active.
      3. Build the branch URL and (best-effort) parse a ticket URL from
         the returned message.
      4. Mark ``step_3a_branch`` COMPLETED (with ``branch_name`` +
         ``ticket_id`` in the session data) or FAILED (with the error
         message) based on the result.

    The blocking Azure DevOps + git subprocess work is off-loaded to a
    thread via :func:`asyncio.to_thread` so the async event loop stays
    responsive — mirrors what the old ``handle_branch`` did with
    ``loop.run_in_executor(None, agent.run_step_3a_branch)``.

    Returns
    -------
    ``(success, result_dict)`` where ``result_dict`` has the six keys
    ``baseline``, ``branch_name``, ``ticket_id``, ``branch_url``,
    ``ticket_url``, ``message`` — same shape ``handle_branch`` renders
    the Chainlit summary card from.
    """
    # Lazy import keeps the import graph clean — ``run`` is only
    # invoked when the skill actually executes.
    from models.session import StepStatus

    baseline_name = session.baseline_name
    logger.info(f"Step 3a: Create branch for {baseline_name}")

    result: Dict[str, Any] = {
        "baseline": baseline_name,
        "branch_name": None,
        "ticket_id": None,
        "branch_url": None,
        "ticket_url": None,
        "message": None,
    }

    session.update_step("step_3a_branch", StepStatus.RUNNING)

    try:
        # Off-load the blocking Azure DevOps + git subprocess calls
        # to a worker thread so the async event loop stays free to
        # handle Chainlit heartbeats / other user messages.
        success, message = await asyncio.to_thread(
            _create_and_pull_branch, baseline_name
        )

        result["message"] = message
        result["branch_name"] = _get_branch_name(baseline_name)

        # Build the Azure DevOps branch URL from project-wide config.
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT
        repo = config.AZURE_DEVOPS_REPO
        branch_name = result["branch_name"]
        result["branch_url"] = (
            f"https://dev.azure.com/{org}/{project}/_git/{repo}"
            f"?version=GB{branch_name.replace('/', '%2F')}"
        )

        # Best-effort ticket ID extraction from the success message.
        if "linked to ticket" in message:
            try:
                ticket_id = int(message.split("ticket ")[-1].split()[0])
                result["ticket_id"] = ticket_id
                result["ticket_url"] = (
                    f"https://dev.azure.com/{org}/{project}"
                    f"/_workitems/edit/{ticket_id}"
                )
            except Exception:  # noqa: BLE001 — best-effort only
                pass

        if success:
            session.update_step(
                "step_3a_branch",
                StepStatus.COMPLETED,
                data={
                    "branch_name": result["branch_name"],
                    "ticket_id": result["ticket_id"],
                },
            )
            logger.info(f"Step 3a completed successfully for {baseline_name}")
        else:
            session.update_step("step_3a_branch", StepStatus.FAILED, error=message)
            logger.error(f"Step 3a failed for {baseline_name}: {message}")

        return success, result

    except Exception as e:  # noqa: BLE001 — outer safety net
        error_msg = str(e)
        session.update_step("step_3a_branch", StepStatus.FAILED, error=error_msg)
        logger.error(f"Step 3a exception for {baseline_name}: {error_msg}")
        result["message"] = error_msg
        return False, result


__all__ = ["run"]
