"""Run sub-agent -- delegates to the shared sub-agent engine.

Responsibility split
--------------------
* :mod:`agents.validation_agent.validation_agent` (the orchestrator)
  decides *which* operation to run by reading the user's request and
  asking the **router LLM**. When the router picks ``run``, the
  orchestrator hands off to this module via Option B convention
  (``sub_agents/<op_id>/<op_id>_agent.py :: run``) and does nothing
  further for this op.

* This module is a thin forwarder. The actual skill-invocation
  logic (read ``SKILL.md``, worker-LLM step, importlib + invoke)
  lives in the shared helper
  :func:`agents.validation_agent.sub_agents.invoke_skill` so every
  sub-agent uses the same engine.

* :mod:`prompts.skills.run` (the skill itself) is **untouched** by
  this forwarder -- same ``SKILL.md``, same ``__init__.py``, same
  ``scripts/run_baseline.py``. The contract is identical:
  ``await run(session) -> Tuple[bool, Dict[str, Any]]``.

Data flow
---------
    validation_agent.py  --->  run_agent.run  --->  invoke_skill('run')
                                                        |
                                                        v
                                                prompts/skills/run/

No reverse dependency: this sub-agent does **not** import the
orchestrator. The shared engine in :mod:`sub_agents.__init__`
reads SKILL.md and calls the LLM service directly, so the only
dependency is the local helper.

Behaviour, log lines, error messages, and the returned dict shape
are byte-identical to the previous ``ScriptRunnerAgent.run_step_3b_run``
path so the Chainlit UI sees no change. The pre-flight
``extract_program_name`` / ``find_program_file`` helpers used by
``handle_run`` in ``app.py`` are called directly against
``prompts.skills.run`` (not through this forwarder) because they
are not LLM-invoked operations.
"""

from __future__ import annotations

from typing import Any

from agents.validation_agent.sub_agents import invoke_skill


_OP_ID = "run"


async def run(*args: Any, **kwargs: Any) -> Any:
    """Execute the ``run`` operation by invoking its skill entrypoint.

    Public entrypoint invoked by
    :meth:`agents.validation_agent.validation_agent.Orchestrator.run_skill`
    once the router LLM has decided the user wants to execute the
    migrated program.

    Forwards ``*args`` / ``**kwargs`` verbatim to
    :func:`invoke_skill`, which performs the worker-LLM step (if
    enabled), resolves the entrypoint declared in
    ``prompts/skills/run/SKILL.md``, and awaits the resolved
    callable inside the run skill folder.
    """
    return await invoke_skill(_OP_ID, *args, **kwargs)


__all__ = ["run"]
