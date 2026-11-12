"""Azure DevOps helpers for the reporting skill (Step 6 only).

Fully self-contained — no import from ``tools/devops_tools.py``. Two
public functions plus their private HTTP helpers:

* :func:`close_reporting_ticket` — Step 6: find + close the ticket
  (updates ``session.step_6_close_ticket`` state).
* :func:`find_reporting_ticket`  — WIQL lookup only, no state change,
  used by the "Not Acceptable" button in ``app.py`` for the
  ticket-info display.

These same functions used to live in ``tools/devops_tools.py`` and
``agents/reporting_agent.py`` before the reporting skill migration —
moved here so the reporting folder is fully portable and doesn't
depend on external agent / tool modules.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any, Dict, Tuple

import requests

import config
from utils.logger import get_logger

if TYPE_CHECKING:
    from models.session import Session

logger = get_logger("reporting.devops")


# ────────────────────────────────────────────────────────────────────
# Close-ticket configuration
# ────────────────────────────────────────────────────────────────────
# Values used when transitioning the 05-Reporting work item to
# closed at Step 6. The ExperSolve/SSAB-OX Task workflow enforces
# three required "Quality Analysis" custom fields on state
# transition — the PATCH must set them or ADO rejects the whole
# request with TF401320.
#
# To tune (for example, if a run is closed with N/A instead of Yes)
# edit the values below and restart. If the workflow ever adds or
# renames a required field, add / rename the corresponding key here.
CLOSE_STATE   = "Closed"
CLOSE_REASON  = "Completed"
CLOSE_CUSTOM_FIELDS: Dict[str, Any] = {
    "Custom.Alloutputdataorfilesweresynced":     "Yes",
    "Custom.AllAcceptableDifferenceswerelinked": "Yes",
    "Custom.Allissueswerecaughtinvalidation":    "Yes",
}


# ────────────────────────────────────────────────────────────────────
# Private HTTP helpers (Azure DevOps REST API)
# ────────────────────────────────────────────────────────────────────


def _get_auth_header() -> Dict[str, str]:
    """Basic-auth header built from the configured Azure DevOps PAT."""
    pat = config.AZURE_DEVOPS_PAT
    encoded = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }


def _find_reporting_ticket_by_baseline(baseline_name: str) -> Tuple[bool, int, str]:
    """WIQL search for the ``05-Reporting`` work item matching a baseline.

    Returns ``(True, ticket_id, message)`` on hit,
    ``(False, 0, error_message)`` on miss or transport failure.
    """
    logger.info(f"Searching for reporting ticket with baseline: {baseline_name}")

    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT

        url = f"https://dev.azure.com/{org}/{project}/_apis/wit/wiql?api-version=7.0"
        query = {
            "query": (
                f"SELECT [System.Id], [System.Title] FROM WorkItems "
                f"WHERE [System.Title] CONTAINS '{baseline_name}' "
                f"AND [System.Title] CONTAINS '05-Reporting'"
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
            msg = f"No reporting ticket found for baseline: {baseline_name}"
            logger.warning(msg)
            return False, 0, msg

        ticket_id = work_items[0]["id"]
        logger.info(f"Found reporting ticket ID: {ticket_id}")
        return True, ticket_id, f"Found reporting ticket {ticket_id}"

    except Exception as e:  # noqa: BLE001
        msg = f"Error searching reporting ticket: {e}"
        logger.error(msg)
        return False, 0, msg


def _close_ticket(ticket_id: int, reason: str = "Completed") -> Tuple[bool, str]:
    """PATCH the work item state (default ``Closed``) plus any
    project-specific required custom fields.

    Values come from the module-level ``CLOSE_STATE`` /
    ``CLOSE_REASON`` / ``CLOSE_CUSTOM_FIELDS`` constants at the top
    of this file — edit them there to change close behaviour.

    Treats ``already closed`` as a success (idempotent).
    """
    # ``reason`` parameter kept for backwards compatibility with any
    # older caller; the module-level constant is authoritative.
    close_state  = CLOSE_STATE
    close_reason = CLOSE_REASON or reason
    custom_fields = CLOSE_CUSTOM_FIELDS

    logger.info(
        f"Closing ticket {ticket_id}: state='{close_state}', "
        f"reason='{close_reason}', custom_fields={list(custom_fields)}"
    )

    try:
        org = config.AZURE_DEVOPS_ORG
        project = config.AZURE_DEVOPS_PROJECT

        url = (
            f"https://dev.azure.com/{org}/{project}"
            f"/_apis/wit/workitems/{ticket_id}?api-version=7.0"
        )
        headers = _get_auth_header()
        headers["Content-Type"] = "application/json-patch+json"

        payload: list[Dict[str, Any]] = [
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
        elif "already" in response.text.lower():
            msg = f"Ticket {ticket_id} already closed"
            logger.info(msg)
            return True, msg
        else:
            msg = f"Failed to close ticket: {response.text}"
            logger.error(msg)
            return False, msg

    except Exception as e:  # noqa: BLE001
        msg = f"Error closing ticket: {e}"
        logger.error(msg)
        return False, msg


# ────────────────────────────────────────────────────────────────────
# Public API used by the workflow + app.py button callbacks
# ────────────────────────────────────────────────────────────────────


def close_reporting_ticket(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Step 6: find the reporting ticket for ``session.baseline_name``
    and close it. Updates ``session.step_6_close_ticket`` state.

    Returns
    -------
    ``(True,  {"message": <str>, "ticket_id": <int>})``   on success
    ``(False, {"message": <str>, "ticket_id": <int|None>})`` on failure

    The dict shape matches what the previous
    ``ReportingAgent.run_step_6_close_ticket`` returned so
    ``handle_reporting_step_6`` in ``app.py`` renders the Chainlit
    close-ticket card without changes.
    """
    # Lazy import — matches the pattern used by every other migrated
    # skill so ``models.session`` isn't pulled in at facade import time.
    from models.session import StepStatus

    baseline = session.baseline_name
    logger.info(f"Reporting Step 6: Close ticket for {baseline}")
    session.update_step("step_6_close_ticket", StepStatus.RUNNING)

    found, ticket_id, msg = _find_reporting_ticket_by_baseline(baseline)
    if not found:
        session.update_step("step_6_close_ticket", StepStatus.FAILED, error=msg)
        return False, {"message": msg, "ticket_id": None}

    ok, close_msg = _close_ticket(ticket_id)
    if ok:
        session.update_step(
            "step_6_close_ticket",
            StepStatus.COMPLETED,
            data={"ticket_id": ticket_id},
        )
    else:
        session.update_step("step_6_close_ticket", StepStatus.FAILED, error=close_msg)

    return ok, {"message": close_msg, "ticket_id": ticket_id}


def find_reporting_ticket(baseline: str) -> Tuple[bool, int, str]:
    """Public WIQL lookup used by ``on_reporting_not_acceptable`` in ``app.py``.

    Returns the same ``(found, ticket_id, message)`` tuple as
    :func:`_find_reporting_ticket_by_baseline` but is explicitly part
    of the skill's public API. Does not mutate session state and does
    not close the ticket — the "Not Acceptable" flow only needs the
    id for display purposes.
    """
    return _find_reporting_ticket_by_baseline(baseline)


__all__ = ["close_reporting_ticket", "find_reporting_ticket"]
