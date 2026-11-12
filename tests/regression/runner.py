"""Per-case regression runner: executes a full case and aggregates all checks."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from models.session import Session

from . import flow_validator as fv
from .analysis import AnalysisVerdict, analyze_exceptions
from .fake_llm import FakeLLMService
from .flow_executor import FlowResult, execute_flow
from .rag_harness import RagHarness
from .report_builder import build_report
from .schema import RegressionCase


@dataclass
class RegressionResult:
    case_id: str
    description: str
    checks: List[fv.CheckResult]
    report: Dict[str, Any]
    flow: FlowResult
    verdict: Optional[AnalysisVerdict]
    kb_article_id: Optional[str]
    wall_ms: float
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def failed_checks(self) -> List[fv.CheckResult]:
        return [c for c in self.checks if not c.passed]


def run_case(case: RegressionCase, session: Session) -> RegressionResult:
    """Run every stage of a regression case and collect all check results."""

    wall_start = time.perf_counter()

    # 1-8. Execute the agent flow on the real Session state machine.
    flow = execute_flow(case, session)

    # 5. Analyze Exceptions — only when a failure actually occurred.
    verdict: Optional[AnalysisVerdict] = None
    if flow.failed_step is not None:
        fake_llm = FakeLLMService(case.llm_responses)
        error_text = (
            f"Baseline {case.baseline_name} failed at {flow.failed_step}: "
            f"{case.input.get('error_message', '')}"
        )
        verdict = analyze_exceptions(fake_llm, error_text)

    # KB retrieval (RAG) — seed an isolated store and query it.
    kb_article_id: Optional[str] = None
    rag_detail: Dict[str, Any] = {}
    if case.kb_articles and case.kb_query:
        try:
            harness = RagHarness(collection_name=f"kb_{case.case_id}")
            harness.seed(case.kb_articles)
            rag = harness.retrieve(case.kb_query)
            kb_article_id = rag.top_id
            rag_detail = {"ranked_ids": rag.ranked_ids, "distances": rag.distances}
        except Exception as exc:  # chromadb unavailable → recorded, check will flag
            rag_detail = {"error": str(exc)}

    # 7. Generate Validation Report.
    report = build_report(
        baseline_name=case.baseline_name,
        program_name=case.program_name,
        flow=flow,
        verdict=verdict,
        kb_article_id=kb_article_id,
    )

    wall_ms = (time.perf_counter() - wall_start) * 1000.0

    # --- Aggregate all required regression checks -------------------------
    exp = case.expected
    actual_category = verdict.exception_category if verdict else "none"
    actual_root_cause = verdict.root_cause if verdict else None

    checks = [
        fv.check_agent_sequence(exp.agent_sequence, flow.executed_sequence),
        fv.check_exception_category(exp.exception_category, actual_category),
        fv.check_root_cause(exp.root_cause_keywords, actual_root_cause),
        fv.check_kb_article(exp.kb_article_id, kb_article_id),
        fv.check_report(exp.report.status, exp.report.required_fields, report),
        fv.check_response_time(exp.max_response_time_ms, wall_ms),
    ]

    return RegressionResult(
        case_id=case.case_id,
        description=case.description,
        checks=checks,
        report=report,
        flow=flow,
        verdict=verdict,
        kb_article_id=kb_article_id,
        wall_ms=wall_ms,
        extra={"rag": rag_detail},
    )
