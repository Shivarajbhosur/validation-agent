"""Finalize sub-agent — delegates to the shared sub-agent engine.

Thin forwarder that calls
:func:`agents.validation_agent.sub_agents.invoke_skill` with the op
id ``finalize``. Same pattern as every other migrated op
(copy/load/run/unload/branch/compare).

The engine reads ``prompts/skills/finalize/SKILL.md``, resolves the
``entrypoint: :run`` field to the facade's :func:`run(session)`, and
awaits it.
"""

from __future__ import annotations

from typing import Any

from agents.validation_agent.sub_agents import invoke_skill

_OP_ID = "finalize"


async def run(*args: Any, **kwargs: Any) -> Any:
    """Execute the ``finalize`` operation via the shared sub-agent engine."""
    return await invoke_skill(_OP_ID, *args, **kwargs)


__all__ = ["run"]
