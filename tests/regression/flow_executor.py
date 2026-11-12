"""Executes the eight-step agent flow on the real Session state machine.

Drives ``models.session.Session`` through the canonical eight logical steps,
recording the exact sequence that ran and honouring per-step outcomes from the
dataset. A failed step halts the flow and the remaining steps are marked
SKIPPED, matching the application's real failure/resume semantics. No external
system (PowerShell, .NET, Beyond Compare, Azure DevOps) is invoked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List

from models.session import Session, StepStatus

from .schema import (
    LOGICAL_STEP_ORDER,
    LOGICAL_TO_SESSION_STEP,
    RegressionCase,
)


@dataclass
class StepExecution:
    logical: str
    session_step: str
    status: str
    duration_ms: float


@dataclass
class FlowResult:
    executed_sequence: List[str]            # logical steps that actually ran
    skipped: List[str]
    executions: List[StepExecution]
    failed_step: str | None
    total_ms: float
    per_step_ms: Dict[str, float] = field(default_factory=dict)


def execute_flow(case: RegressionCase, session: Session) -> FlowResult:
    """Run the flow for ``case`` against ``session`` and record the trace.

    ``case.step_outcomes`` maps a logical step name to ``"failed"`` to simulate
    a failure at that step. Any step not listed is treated as a success.
    """

    executions: List[StepExecution] = []
    executed: List[str] = []
    skipped: List[str] = []
    per_step: Dict[str, float] = {}
    failed_step: str | None = None
    halted = False

    flow_start = time.perf_counter()

    for logical in LOGICAL_STEP_ORDER:
        session_step = LOGICAL_TO_SESSION_STEP[logical]

        if halted:
            session.update_step(session_step, StepStatus.SKIPPED)
            skipped.append(logical)
            executions.append(StepExecution(logical, session_step, "skipped", 0.0))
            continue

        outcome = case.step_outcomes.get(logical, "completed")
        step_start = time.perf_counter()
        session.update_step(session_step, StepStatus.RUNNING)

        # Deterministic, mocked step body — no real external calls.
        _simulate_step_work()

        if outcome == "failed":
            session.update_step(
                session_step,
                StepStatus.FAILED,
                error=f"Simulated failure at {logical}",
            )
            status = "failed"
            failed_step = logical
            halted = True
        else:
            session.update_step(session_step, StepStatus.COMPLETED)
            status = "completed"

        duration_ms = (time.perf_counter() - step_start) * 1000.0
        per_step[logical] = duration_ms
        executed.append(logical)
        executions.append(
            StepExecution(logical, session_step, status, duration_ms)
        )

    total_ms = (time.perf_counter() - flow_start) * 1000.0

    return FlowResult(
        executed_sequence=executed,
        skipped=skipped,
        executions=executions,
        failed_step=failed_step,
        total_ms=total_ms,
        per_step_ms=per_step,
    )


def _simulate_step_work() -> None:
    """Placeholder for a mocked step body.

    Real integration wiring would invoke the corresponding sub-agent here with
    its subprocess / HTTP boundaries mocked. Kept trivial so timing reflects
    framework overhead rather than an artificial sleep.
    """
    return None
