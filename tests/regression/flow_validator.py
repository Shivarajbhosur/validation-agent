"""Validators for a regression run: sequence, category, root cause, KB, report, timing.

Each validator returns a :class:`CheckResult` so the runner can aggregate a
full per-case verdict and the HTML report can show exactly which checks passed
or failed, with the expected vs. actual values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class CheckResult:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


def check_agent_sequence(expected: List[str], actual: List[str]) -> CheckResult:
    passed = expected == actual
    detail = "" if passed else "Executed sequence does not match expected order."
    return CheckResult("agent_sequence", passed, expected, actual, detail)


def check_exception_category(expected: str, actual: Optional[str]) -> CheckResult:
    passed = expected == actual
    detail = "" if passed else f"Detected '{actual}', expected '{expected}'."
    return CheckResult("exception_category", passed, expected, actual, detail)


def check_root_cause(keywords: List[str], root_cause: Optional[str]) -> CheckResult:
    text = (root_cause or "").lower()
    missing = [kw for kw in keywords if kw.lower() not in text]
    passed = not missing
    detail = "" if passed else f"Missing keywords: {missing}"
    return CheckResult("root_cause", passed, keywords, root_cause, detail)


def check_kb_article(expected_id: Optional[str], actual_id: Optional[str]) -> CheckResult:
    # When no KB article is expected (e.g. a clean pass), the check is vacuous.
    if expected_id is None:
        return CheckResult("kb_article", True, None, actual_id, "No KB lookup expected.")
    passed = expected_id == actual_id
    detail = "" if passed else f"Retrieved '{actual_id}', expected '{expected_id}'."
    return CheckResult("kb_article", passed, expected_id, actual_id, detail)


def check_report(expected_status: str, required_fields: List[str],
                 report: Dict[str, Any]) -> CheckResult:
    missing = [f for f in required_fields if f not in report]
    status_ok = report.get("status") == expected_status
    passed = status_ok and not missing
    problems = []
    if not status_ok:
        problems.append(f"status '{report.get('status')}' != '{expected_status}'")
    if missing:
        problems.append(f"missing fields {missing}")
    return CheckResult(
        "report",
        passed,
        {"status": expected_status, "required_fields": required_fields},
        {"status": report.get("status"), "fields": sorted(report.keys())},
        "; ".join(problems),
    )


def check_response_time(threshold_ms: int, actual_ms: float) -> CheckResult:
    passed = actual_ms <= threshold_ms
    detail = "" if passed else f"{actual_ms:.1f}ms exceeds {threshold_ms}ms threshold."
    return CheckResult(
        "response_time",
        passed,
        f"<= {threshold_ms}ms",
        f"{actual_ms:.1f}ms",
        detail,
    )
