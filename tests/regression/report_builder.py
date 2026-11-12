"""Builds the validation report produced by the 'Generate Validation Report' step."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .analysis import AnalysisVerdict
from .flow_executor import FlowResult


def build_report(
    baseline_name: str,
    program_name: str,
    flow: FlowResult,
    verdict: Optional[AnalysisVerdict],
    kb_article_id: Optional[str],
) -> Dict[str, Any]:
    """Assemble the structured validation report.

    A run is PASSED only when no step failed; otherwise FAILED. The report
    carries the fields downstream consumers (and the regression checks) rely on.
    """

    status = "FAILED" if flow.failed_step else "PASSED"

    steps: List[Dict[str, Any]] = [
        {
            "step": ex.logical,
            "session_step": ex.session_step,
            "status": ex.status,
            "duration_ms": round(ex.duration_ms, 2),
        }
        for ex in flow.executions
    ]

    report: Dict[str, Any] = {
        "baseline_name": baseline_name,
        "program_name": program_name,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exception_category": verdict.exception_category if verdict else "none",
        "root_cause": verdict.root_cause if verdict else None,
        "kb_reference": kb_article_id,
        "failed_step": flow.failed_step,
        "steps": steps,
        "total_ms": round(flow.total_ms, 2),
    }
    return report
