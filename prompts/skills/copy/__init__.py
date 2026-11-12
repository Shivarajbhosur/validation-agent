"""Copy operation package — Step 1 of the validation workflow.

Self-contained vertical module. **This file is the only public
surface of the skill** — external code (``app.py``, the orchestrator,
``script_runner_agent.py``) calls :func:`run` here and nothing else.
The implementation lives in a single file: ``scripts/copy_baseline.py``.

Layout
------
- ``SKILL.md``                  operation contract (YAML front-matter
                                + body) consumed by
                                :mod:`services.orchestrator`.
- ``scripts/copy_baseline.py``  merged implementation: filesystem
                                helpers, PowerShell cmdlets, and the
                                public :func:`run` orchestration.

Contract
--------
``await run(session) -> Tuple[bool, str]``

The orchestrator resolves ``entrypoint: :run`` in ``SKILL.md`` to
this function and awaits it. The actual work is delegated to
``scripts/copy_baseline.py`` so this file stays a thin facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from models.session import Session


async def run(session: "Session") -> Tuple[bool, str]:
    """Execute the copy skill (Step 1 of the validation workflow).

    This is the **only** function external callers should invoke for
    this skill. The orchestrator resolves it via the ``entrypoint:``
    field in ``SKILL.md`` and awaits the returned coroutine.

    Delegates to ``scripts/copy_baseline.run`` (lazy import so
    ``shutil`` / ``subprocess`` only load when the skill actually
    runs — keeping startup fast and the import graph clean).

    Args:
        session: The validation session for the baseline.

    Returns:
        ``(success, detail_message)`` — same shape and content the
        previous :meth:`CopyAgent.run` returned, so callers (and the
        Chainlit UI) see no behaviour change.
    """
    from .scripts.copy_baseline import run as _run

    return await _run(session)


__all__ = ["run"]
