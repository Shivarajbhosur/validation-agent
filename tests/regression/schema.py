"""Dataset schema for regression cases.

Each regression case is a JSON file under ``datasets/``. These dataclasses
describe its shape and provide the canonical mapping between the eight
logical agent steps and the real ``models.session.Session`` step keys, so
the framework validates against the project's actual state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Logical agent step  ->  real Session.STEPS key.
# The eight business steps the agents perform, mapped onto the concrete
# step identifiers tracked by models.session.Session.
LOGICAL_TO_SESSION_STEP: Dict[str, str] = {
    "copy_baseline": "step_1_copy",
    "load_data": "step_2_load",
    "create_branch": "step_3a_branch",
    "execute_program": "step_3b_run",
    "analyze_exceptions": "step_4_analyze_fix",
    "compare_outputs": "step_6_compare",
    "generate_report": "step_7_report",
    "finalize_validation": "step_8_finalize",
}

# Canonical execution order of the eight logical steps.
LOGICAL_STEP_ORDER: List[str] = list(LOGICAL_TO_SESSION_STEP.keys())

# Human-readable labels for reporting.
LOGICAL_STEP_LABELS: Dict[str, str] = {
    "copy_baseline": "Copy Baseline",
    "load_data": "Load Data",
    "create_branch": "Create Branch",
    "execute_program": "Execute Program",
    "analyze_exceptions": "Analyze Exceptions",
    "compare_outputs": "Compare Outputs",
    "generate_report": "Generate Validation Report",
    "finalize_validation": "Finalize Validation",
}

# Valid exception categories, aligned with models.fix_suggestion.ErrorType
# plus a sentinel for clean (no-exception) runs.
VALID_EXCEPTION_CATEGORIES = {"run_failed", "compare_diff", "none"}


@dataclass
class KbArticle:
    """A knowledge-base article used to seed the isolated RAG store."""

    id: str
    title: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpectedReport:
    status: str
    required_fields: List[str] = field(default_factory=list)


@dataclass
class Expected:
    """Expected outcomes validated on every regression run."""

    agent_sequence: List[str]
    exception_category: str
    root_cause_keywords: List[str]
    kb_article_id: Optional[str]
    report: ExpectedReport
    max_response_time_ms: int


@dataclass
class RegressionCase:
    case_id: str
    description: str
    input: Dict[str, Any]
    step_outcomes: Dict[str, str]
    llm_responses: Dict[str, Any]
    kb_articles: List[KbArticle]
    kb_query: Optional[str]
    expected: Expected
    source_path: Optional[str] = None

    # Convenience accessors -------------------------------------------------
    @property
    def baseline_name(self) -> str:
        return self.input.get("baseline_name", "UNKNOWN-BASELINE")

    @property
    def program_name(self) -> str:
        return self.input.get("program_name", "UNKNOWN")
