"""Finalize skill — Step 8 of the validation workflow.

Self-contained vertical module. **This file is the only public
surface of the skill** — external code (``app.py``, the orchestrator,
``handle_finalize``) calls :func:`run` here and nothing else. The
implementation is split across three files inside ``scripts/``:

* ``finalize_baseline.py`` — the orchestrator (git → PR → close ticket).
* ``git_helpers.py``       — subprocess helpers (get_current_branch,
                              is_protected_branch, add, commit, push).
* ``devops.py``            — Azure DevOps REST calls (find validation
                              ticket, create PR with work-item link,
                              close ticket).

Contract
--------
``await run(session) -> Tuple[bool, Dict[str, Any]]``

The orchestrator resolves ``entrypoint: :run`` in ``SKILL.md`` to
this function and awaits it. Delegates to
``scripts/finalize_baseline.run`` (lazy import so ``subprocess`` /
``requests`` only load when the skill actually runs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from models.session import Session


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute the finalize skill (Step 8 of the validation workflow).

    Runs fail-fast in order:
      1. Find validation ticket via WIQL (aborts here if not found)
      2. git add + commit + push (blocked from pushing to protected branches)
      3. Create PR: feature branch → develop, linked to the validation ticket
      4. Close the validation ticket (state=Closed, reason=Completed)

    Returns
    -------
    ``(True,  {branch_name, commit_success, push_success, pr_id, pr_url,
              ticket_id, ticket_closed, message})``   on full success
    ``(False, {..., message})``                       on any failure
    """
    from .scripts.finalize_baseline import run as _run

    return await _run(session)


__all__ = ["run"]
