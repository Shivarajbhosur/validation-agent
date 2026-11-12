"""Reporting agent — composite 6-step workflow.

Peer of :mod:`agents.validation_agent` in the architecture. Unlike
the standalone operations (copy, load, run, unload, compare, branch)
which each own a single unit of work under ``prompts/skills/<op>/``,
reporting is a *composition* of those ops plus a close-ticket step.

Sequences the 5 standalone ops (copy → load → run → unload → compare)
via the validation orchestrator's ``run_skill``, then closes the
Azure DevOps reporting ticket (step 6). Yields events between steps
so the caller (``app.py :: handle_reporting``) can drive the Chainlit
UI (progress cards, per-step messages, action buttons) without this
module importing anything UI-specific.

The step definitions are duplicated with the interactive-path YAML
config at ``prompts/REPORTING_SKILL.md`` on purpose — the YAML owns
the UI templates (messages, labels, icons); this module owns the
sequence and branching logic. Together they define the workflow.

Public API
----------
* :func:`run` — async generator orchestrating steps 1-5 and, when
  compare passes, auto-continuing into step 6.
* :func:`resume_after_acceptable` — async generator yielding only
  the step-6 close-ticket events. Called by the "Acceptable"
  action-button callback after the user confirms a
  compare-differences result is acceptable.

Event schema
------------
``{"phase":"step", "num":int, "op":str, "title":str, "task":asyncio.Task}``
    A step has started. The caller MUST ``await event["task"]``
    before requesting the next event. If the task raises or returns
    ``(False, ...)`` the generator returns without further events.

``{"phase":"all_pass", "result":dict}``
    Step 5 (compare) reported ``all_pass=True``. Step 6 will follow.

``{"phase":"differences", "result":dict}``
    Step 5 reported ``all_pass=False``. Generator ENDS — the caller
    must render Acceptable / Not-Acceptable buttons and resume via
    :func:`resume_after_acceptable` if the user picks Acceptable.

``{"phase":"close_done", "ok":bool, "result":dict}``
    Step 6 finished. ``result`` has ``ticket_id`` and ``message``.

Fail-fast, differences-pause semantics
--------------------------------------
* If any step 1-5 returns ``(False, ...)`` the generator returns
  immediately.
* Step 5 differences → ``differences`` event, no step 6.
* Step 6 is sync (blocking ADO HTTP); wrapped in ``asyncio.to_thread``
  so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Tuple

from agents.validation_agent import get_orchestrator
from utils.logger import get_logger

logger = get_logger("reporting_agent")


# ────────────────────────────────────────────────────────────────────
# Step definitions (sequence + display metadata)
# ────────────────────────────────────────────────────────────────────

# ``(step_number, op_id, human_title)``. Op ids match the skill ids
# in ``prompts/skills/<op>/SKILL.md``, so ``run_skill(op_id, session)``
# dispatches the correct sub-agent.
STEPS: Tuple[Tuple[int, str, str], ...] = (
    (1, "copy",    "Copy Baseline"),
    (2, "load",    "Load to DB"),
    (3, "run",     "Run Program"),
    (4, "unload",  "Unload from DB"),
    (5, "compare", "Compare Output"),
)


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


async def run(session: Any) -> AsyncIterator[Dict[str, Any]]:
    """Orchestrate the full reporting workflow. Yield events.

    See the module docstring for the event schema. This generator
    is the single source of truth for the reporting sequence and
    branching rules — the caller only renders UI in response to
    yielded events.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Reporting workflow started for {baseline}")

    orchestrator = get_orchestrator()

    result: Any = None
    for num, op, title in STEPS:
        # ``orchestrator.run_skill`` is a coroutine but the skills
        # underneath (copy/load/run/unload/compare) call BLOCKING
        # ``subprocess.run`` for PowerShell / dotnet.  Running the
        # coroutine directly on the main event loop would freeze
        # Chainlit (no ProgressCard heartbeats, no other message
        # updates) until the subprocess exits.
        #
        # Off-load the whole coroutine to a worker thread that
        # spins up its own event loop — same pattern
        # ``handle_copy`` etc. in ``app.py`` use for the standalone
        # ops. The consumer just awaits the returned Future.
        loop = asyncio.get_event_loop()
        task = loop.run_in_executor(
            None,
            lambda o=op: asyncio.run(orchestrator.run_skill(o, session)),
        )
        yield {
            "phase": "step",
            "num": num,
            "op": op,
            "title": title,
            "task": task,
        }
        # ``await task`` is idempotent even if the consumer already
        # awaited it — safer than ``task.result()`` in case the
        # consumer broke out of its ProgressCard block early.
        ok, result = await task
        if not ok:
            logger.warning(
                f"Reporting step {num}/{len(STEPS) + 1} ({op}) failed; "
                "stopping workflow"
            )
            return

    # ── Post-compare branching ────────────────────────────────────
    cmp_result: Dict[str, Any] = result if isinstance(result, dict) else {}
    if cmp_result.get("all_pass"):
        yield {"phase": "all_pass", "result": cmp_result}
        # Fall through to step 6 (auto-continue on all_pass).
        async for evt in _run_close(session):
            yield evt
    else:
        # Differences found — pause. Generator ENDS here; the
        # Chainlit button callback resumes via
        # :func:`resume_after_acceptable` if the user consents.
        logger.info(
            f"Reporting workflow paused for {baseline}: compare "
            f"found differences ({cmp_result.get('after_csv_fail', 0)} "
            f"AFTER-CSV + {cmp_result.get('root_mixed_fail', 0)} "
            f"Root-Mixed) — awaiting user decision."
        )
        yield {"phase": "differences", "result": cmp_result}


async def resume_after_acceptable(
    session: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """Continue to Step 6 after the user clicks the Acceptable button.

    Separate entry point because Chainlit's ``@cl.action_callback``
    is a module-level decorator in app.py; when the button fires
    it can't resume a suspended :func:`run` generator, so it calls
    this instead. Yields the same close-related events (``step``
    for the close-ticket step, then ``close_done``) so the caller
    can reuse the same event-consumer that drove :func:`run`.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Reporting workflow resuming (Acceptable) for {baseline}")
    async for evt in _run_close(session):
        yield evt


# ────────────────────────────────────────────────────────────────────
# Internal — step 6 (close ticket)
# ────────────────────────────────────────────────────────────────────


async def _run_close(session: Any) -> AsyncIterator[Dict[str, Any]]:
    """Yield the step-6 events for closing the reporting ticket.

    Wraps the blocking Azure DevOps helper in ``asyncio.to_thread``
    so the event loop keeps ticking while the HTTP requests are in
    flight.
    """
    # Lazy import — keeps ``reporting_agent`` importable in tests
    # that don't touch the network / ``requests``.
    from prompts.skills.reporting.scripts.devops import close_reporting_ticket

    task = asyncio.ensure_future(
        asyncio.to_thread(close_reporting_ticket, session)
    )
    yield {
        "phase": "step",
        "num": len(STEPS) + 1,      # 6
        "op": "close",
        "title": "Close Ticket",
        "task": task,
    }
    ok, close_res = await task
    yield {"phase": "close_done", "ok": ok, "result": close_res}


__all__ = ["STEPS", "run", "resume_after_acceptable"]
