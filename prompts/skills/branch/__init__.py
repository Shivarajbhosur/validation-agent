"""Branch operation package — Step 3a of the validation workflow.

Self-contained vertical module. **This file is the only public
surface of the skill** — external code (``app.py``, the orchestrator,
``handle_branch``) calls :func:`run` here and nothing else. The
implementation lives in a single file: ``scripts/branch_baseline.py``.

Layout
------
- ``SKILL.md``                    operation contract (YAML front-matter
                                  + body) consumed by the orchestrator.
- ``scripts/branch_baseline.py``  merged implementation: Azure DevOps
                                  + git plumbing (via the shared
                                  ``tools/devops_tools`` module) and
                                  the public :func:`run` orchestration.

Contract
--------
``await run(session) -> Tuple[bool, Dict[str, Any]]``

The orchestrator resolves ``entrypoint: :run`` in ``SKILL.md`` to
this function and awaits it. The actual work is delegated to
``scripts/branch_baseline.py`` so this file stays a thin facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from models.session import Session


async def run(session: "Session") -> Tuple[bool, Dict[str, Any]]:
    """Execute the branch skill (Step 3a of the validation workflow).

    This is the **only** function external callers should invoke for
    this skill. The orchestrator resolves it via the ``entrypoint:``
    field in ``SKILL.md`` and awaits the returned coroutine.

    Delegates to ``scripts/branch_baseline.run`` (lazy import so
    ``subprocess`` / Azure DevOps SDK code only loads when the skill
    actually runs — keeping startup fast and the import graph clean).

    Args:
        session: The validation session for the baseline.

    Returns:
        ``(success, result_dict)`` — same shape and content the previous
        ``DevOpsAgent.run_step_3a_branch`` returned, so ``handle_branch``
        (the Chainlit UI handler) sees no behaviour change. Dict keys:
        ``baseline``, ``branch_name``, ``ticket_id``, ``branch_url``,
        ``ticket_url``, ``message``.
    """
    from .scripts.branch_baseline import run as _run

    return await _run(session)


__all__ = ["run"]
