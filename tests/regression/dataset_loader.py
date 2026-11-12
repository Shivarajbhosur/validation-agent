"""Discovery, loading and validation of regression dataset JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .schema import (
    Expected,
    ExpectedReport,
    KbArticle,
    LOGICAL_STEP_ORDER,
    RegressionCase,
    VALID_EXCEPTION_CATEGORIES,
)

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"


class DatasetError(ValueError):
    """Raised when a dataset file is structurally invalid."""


def _require(data: dict, key: str, ctx: str):
    if key not in data:
        raise DatasetError(f"{ctx}: missing required key '{key}'")
    return data[key]


def parse_case(data: dict, source_path: str | None = None) -> RegressionCase:
    """Validate a raw dict and build a :class:`RegressionCase`."""

    ctx = source_path or data.get("case_id", "<case>")

    case_id = _require(data, "case_id", ctx)
    description = data.get("description", "")
    case_input = _require(data, "input", ctx)

    expected_raw = _require(data, "expected", ctx)
    category = _require(expected_raw, "exception_category", ctx)
    if category not in VALID_EXCEPTION_CATEGORIES:
        raise DatasetError(
            f"{ctx}: exception_category '{category}' is not one of "
            f"{sorted(VALID_EXCEPTION_CATEGORIES)}"
        )

    agent_sequence = _require(expected_raw, "agent_sequence", ctx)
    unknown = [s for s in agent_sequence if s not in LOGICAL_STEP_ORDER]
    if unknown:
        raise DatasetError(f"{ctx}: unknown steps in agent_sequence: {unknown}")

    report_raw = expected_raw.get("report", {})
    expected = Expected(
        agent_sequence=list(agent_sequence),
        exception_category=category,
        root_cause_keywords=list(expected_raw.get("root_cause_keywords", [])),
        kb_article_id=expected_raw.get("kb_article_id"),
        report=ExpectedReport(
            status=report_raw.get("status", "UNKNOWN"),
            required_fields=list(report_raw.get("required_fields", [])),
        ),
        max_response_time_ms=int(expected_raw.get("max_response_time_ms", 10_000)),
    )

    kb_articles = [
        KbArticle(
            id=a["id"],
            title=a.get("title", ""),
            content=a.get("content", ""),
            metadata=a.get("metadata", {}),
        )
        for a in data.get("kb_articles", [])
    ]

    return RegressionCase(
        case_id=case_id,
        description=description,
        input=case_input,
        step_outcomes=data.get("step_outcomes", {}),
        llm_responses=data.get("llm_responses", {}),
        kb_articles=kb_articles,
        kb_query=data.get("kb_query"),
        expected=expected,
        source_path=source_path,
    )


def load_case(path: Path) -> RegressionCase:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return parse_case(data, source_path=str(path))


def discover_cases(datasets_dir: Path = DATASETS_DIR) -> List[RegressionCase]:
    """Load every ``*.json`` dataset, sorted by file name."""

    if not datasets_dir.exists():
        return []
    cases: List[RegressionCase] = []
    for path in sorted(datasets_dir.glob("*.json")):
        cases.append(load_case(path))
    return cases
