"""Validate agent — full 7-step composite workflow.

Peer of :mod:`agents.validation_agent.reporting_agent`. Orchestrates
the six standalone sub-agents (copy / load / branch / run / unload /
compare) followed by a two-stage finalize (git push → PR + close
ticket), yielding events between steps so the caller
(``app.py :: handle_validate``) can drive the Chainlit UI (progress
cards, per-step messages, action buttons, pause dialogs) without
this module importing anything UI-specific.

The step definitions are duplicated with the interactive-path YAML
config at ``prompts/VALIDATE_SKILL.md`` on purpose — the YAML owns
the UI templates (messages, labels, icons); this module owns the
sequence and branching logic. Together they define the workflow.

Public API
----------
* :func:`run` — async generator orchestrating steps 1-6 (copy → load
  → branch → run → unload → compare). Ends at:
    - ``run_fail``       (step 4 failed — user analyses + retries)
    - ``differences``    (step 6 found diffs — user Accept / Reject)
    - ``await_push``     (compare passed OR user accepted — ready to push)

* :func:`resume_after_run_success` — after user clicks "Fix Applied,
  Rerun" AND the retried Run step succeeds, this generator continues
  the composite from step 5 (unload → compare → …). Called by
  ``_retry_run_inside_validate`` in app.py.

* :func:`resume_after_acceptable` — user clicked "Acceptable" on the
  compare-differences dialog → yields ``await_push`` (no further
  steps until user approves push).

* :func:`resume_after_push_approval` — user clicked "Proceed to
  Push" → runs finalize push stage (7a) → ends at ``await_pr``.

* :func:`resume_after_pr_approval` — user clicked "Raise PR & Close
  Ticket" → runs finalize PR stage (7b) → ends with ``finalize_done``.

Event schema
------------
``{"phase":"step", "num":int, "op":str, "title":str, "task":asyncio.Task}``
    A step has started. The caller MUST ``await event["task"]``
    before requesting the next event. If the task raises or returns
    ``(False, ...)`` the generator returns without further events
    (fail-fast for steps 1-3, 5; step 4 yields ``run_fail`` first).

``{"phase":"run_fail", "num":4, "op":"run", "result":dict}``
    Step 4 (run) failed. Generator ENDS here — the caller must
    render the existing Analyse-with-AI / Fix-Applied buttons
    (carrying ``validate_mode=True``). After a successful retry the
    caller resumes via :func:`resume_after_run_success`.

``{"phase":"all_pass", "result":dict}``
    Step 6 (compare) reported ``all_pass=True``. Composite continues
    to ``await_push`` automatically (no user intervention needed for
    the compare stage — but push still needs explicit approval).

``{"phase":"differences", "result":dict}``
    Step 6 reported differences. Generator ENDS — caller must
    render Acceptable / Not-Acceptable buttons and resume via
    :func:`resume_after_acceptable` (or stop the workflow).

``{"phase":"await_push", "result":dict}``
    Ready for finalize push stage. Generator ENDS — caller must
    render the "🚀 Proceed to Push" button and resume via
    :func:`resume_after_push_approval`.

``{"phase":"push_done", "ok":bool, "result":dict}``
    Push stage finished. If ``ok=True``, next event is
    ``await_pr``. If ``ok=False``, generator ends (fail).

``{"phase":"await_pr", "result":dict}``
    Push succeeded → ready to raise PR. Generator ENDS — caller
    must render the "📥 Raise PR & Close Ticket" button and resume
    via :func:`resume_after_pr_approval`.

``{"phase":"finalize_done", "ok":bool, "result":dict}``
    PR stage finished (last event). ``result`` has ``pr_id``,
    ``pr_url``, ``ticket_id`` on success.

Zero-impact guarantees
----------------------
* Sub-agents (copy/load/branch/run/unload/compare) are called via
  ``orchestrator.run_skill`` — same path as reporting composite and
  the standalone ``run <baseline>`` / ``copy <baseline>`` /
  etc. commands.
* Finalize is called via the ADDITIVE ``run_push_stage`` /
  ``run_pr_stage`` helpers in ``finalize_baseline.py`` — the
  original ``run()`` is untouched, so ``finalize <baseline>`` alone
  still runs the full atomic flow as always.
* No changes to the reporting composite, orchestrator, session
  model, pipeline HUD, or wiki-help feature.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, Tuple

from agents.validation_agent import get_orchestrator
from utils.logger import get_logger

logger = get_logger("validate_agent")


# ────────────────────────────────────────────────────────────────────
# Step definitions
# ────────────────────────────────────────────────────────────────────
# ``(step_number, op_id, human_title)``. Op ids match the skill ids
# in ``prompts/skills/<op>/SKILL.md`` so ``run_skill(op_id, session)``
# dispatches the correct sub-agent.
STEPS: Tuple[Tuple[int, str, str], ...] = (
    (1, "copy",    "Copy Baseline"),
    (2, "load",    "Load to DB"),
    (3, "branch",  "Create Branch"),
    (4, "run",     "Run Program"),
    (5, "unload",  "Unload from DB"),
    (6, "compare", "Compare Output"),
)

# Step number for the finalize step (both push and PR stages share
# this number — the pause between them is a UI gate, not a separate
# step in the session model).
FINALIZE_STEP_NUM = 7


# ────────────────────────────────────────────────────────────────────
# Internal — run a single sub-agent step
# ────────────────────────────────────────────────────────────────────

def _make_skill_task(op: str, session: Any):
    """Schedule ``orchestrator.run_skill(op, session)`` on a worker thread.

    The underlying sub-agent may call blocking PowerShell/dotnet
    subprocesses; running the coroutine on the main event loop would
    freeze Chainlit UI. Mirrors the exact pattern the reporting
    composite uses.
    """
    orchestrator = get_orchestrator()
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(
        None,
        lambda o=op: asyncio.run(orchestrator.run_skill(o, session)),
    )


# ────────────────────────────────────────────────────────────────────
# Public API — main flow + 4 resume entry points
# ────────────────────────────────────────────────────────────────────


async def run(session: Any) -> AsyncIterator[Dict[str, Any]]:
    """Orchestrate steps 1-6. See the module docstring for events.

    This generator is the single source of truth for the validate
    sequence; the caller only renders UI in response to yielded
    events.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Validate workflow started for {baseline}")

    result: Any = None
    for num, op, title in STEPS:
        task = _make_skill_task(op, session)
        yield {"phase": "step", "num": num, "op": op, "title": title, "task": task}
        ok, result = await task
        if not ok:
            if op == "run":
                # Step 4 failure gets special treatment: the consumer
                # shows the existing Analyse-with-AI button UI and
                # can resume via :func:`resume_after_run_success`.
                logger.info(
                    f"Validate paused at step 4 (run) for {baseline}: "
                    "awaiting user analysis/fix/retry."
                )
                yield {
                    "phase": "run_fail",
                    "num": num,
                    "op": op,
                    "result": result if isinstance(result, dict) else {},
                }
                return
            # All other step failures are terminal — fail-fast.
            logger.warning(
                f"Validate step {num}/{len(STEPS)} ({op}) failed; "
                "stopping workflow."
            )
            return

    # ── Post-compare branching ────────────────────────────────────
    async for evt in _handle_post_compare(session, result, baseline):
        yield evt


async def resume_after_run_success(
    session: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """Continue after user fixed the Run failure and retried successfully.

    Runs steps 5 (unload) → 6 (compare), then post-compare branching
    identical to :func:`run`.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Validate resuming after run-success for {baseline}")

    result: Any = None
    # Steps 5 and 6 only. Note: STEPS[4] = unload, STEPS[5] = compare
    for num, op, title in STEPS[4:]:
        task = _make_skill_task(op, session)
        yield {"phase": "step", "num": num, "op": op, "title": title, "task": task}
        ok, result = await task
        if not ok:
            logger.warning(
                f"Validate resume step {num} ({op}) failed for {baseline}."
            )
            return

    async for evt in _handle_post_compare(session, result, baseline):
        yield evt


async def resume_after_acceptable(
    session: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """User clicked "Acceptable" on the compare-differences dialog.

    Yields a single ``await_push`` event — the caller then renders
    the "Proceed to Push" button.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Validate resuming after Acceptable for {baseline}")
    yield {"phase": "await_push", "result": {"baseline": baseline}}


async def resume_after_push_approval(
    session: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """User clicked "🚀 Proceed to Push". Run finalize push stage.

    On success yields ``push_done`` + ``await_pr``. On failure yields
    ``push_done`` with ``ok=False`` and ends.
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Validate resuming after push-approval for {baseline}")
    async for evt in _run_finalize_push(session, baseline):
        yield evt


async def resume_after_pr_approval(
    session: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """User clicked "📥 Raise PR & Close Ticket". Run finalize PR stage.

    Yields ``finalize_done`` (last event of the workflow).
    """
    baseline = getattr(session, "baseline_name", "<unknown>")
    logger.info(f"Validate resuming after PR-approval for {baseline}")
    async for evt in _run_finalize_pr(session, baseline):
        yield evt


# ────────────────────────────────────────────────────────────────────
# Internal helpers — post-compare branching + finalize stages
# ────────────────────────────────────────────────────────────────────


async def _handle_post_compare(
    session: Any, cmp_result: Any, baseline: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Decide what to yield after step 6 (compare) completes.

    Shared by :func:`run` and :func:`resume_after_run_success` so
    the same branching logic isn't duplicated.
    """
    cmp: Dict[str, Any] = cmp_result if isinstance(cmp_result, dict) else {}
    if cmp.get("all_pass"):
        # Auto-continue to await_push (still requires user approval
        # for the push itself — compare success just means no diffs
        # to review).
        yield {"phase": "all_pass", "result": cmp}
        yield {"phase": "await_push", "result": {"baseline": baseline, **cmp}}
        return
    # Differences found — pause for Acceptable / Not-Acceptable.
    logger.info(
        f"Validate paused for {baseline}: compare found differences "
        f"({cmp.get('after_csv_fail', 0)} AFTER-CSV + "
        f"{cmp.get('root_mixed_fail', 0)} Root-Mixed) — awaiting user decision."
    )
    yield {"phase": "differences", "result": cmp}


async def _run_finalize_push(
    session: Any, baseline: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Execute step 7a — finalize push stage (git add + commit + push).

    Uses the additive ``run_push_stage`` helper in the finalize
    skill; the existing atomic ``run()`` remains untouched.
    """
    # Lazy import — keeps this module importable without touching
    # git/HTTP for unit tests.
    from prompts.skills.finalize.scripts.finalize_baseline import run_push_stage

    task = asyncio.ensure_future(run_push_stage(session))
    yield {
        "phase": "step",
        "num": FINALIZE_STEP_NUM,
        "op": "finalize_push",
        "title": "Finalize · Push",
        "task": task,
    }
    ok, push_result = await task
    yield {"phase": "push_done", "ok": ok, "result": push_result}
    if ok:
        yield {"phase": "await_pr", "result": push_result}


async def _run_finalize_pr(
    session: Any, baseline: str,
) -> AsyncIterator[Dict[str, Any]]:
    """Execute step 7b — finalize PR stage (create PR + close ticket)."""
    from prompts.skills.finalize.scripts.finalize_baseline import run_pr_stage

    task = asyncio.ensure_future(run_pr_stage(session))
    yield {
        "phase": "step",
        "num": FINALIZE_STEP_NUM,
        "op": "finalize_pr",
        "title": "Finalize · PR",
        "task": task,
    }
    ok, pr_result = await task
    yield {"phase": "finalize_done", "ok": ok, "result": pr_result}


__all__ = [
    "STEPS",
    "FINALIZE_STEP_NUM",
    "run",
    "resume_after_run_success",
    "resume_after_acceptable",
    "resume_after_push_approval",
    "resume_after_pr_approval",
]
