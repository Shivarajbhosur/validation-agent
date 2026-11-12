"""Parametrized regression entry point.

Discovers every JSON dataset under ``datasets/`` and runs one regression case
per file. For each case it asserts all six required outcomes:

  1. Correct agent sequence executed
  2. Correct exception category detected
  3. Correct root cause identified
  4. Correct knowledge base article retrieved
  5. Expected report generated
  6. Response time under the configured threshold

Results feed the aggregate HTML report written at session end.
"""

from __future__ import annotations

import pytest

from .dataset_loader import discover_cases
from .runner import run_case
from .schema import RegressionCase

_CASES = discover_cases()


def _case_id(case: RegressionCase) -> str:
    return case.case_id


@pytest.mark.regression
@pytest.mark.parametrize("case", _CASES, ids=[c.case_id for c in _CASES])
def test_regression_case(case: RegressionCase, make_session, regression_recorder):
    # RAG checks require chromadb; skip gracefully if absent.
    if case.kb_articles and case.kb_query:
        pytest.importorskip("chromadb", reason="chromadb required for RAG checks")

    session = make_session(case.baseline_name)
    result = run_case(case, session)
    regression_recorder(result)

    failures = result.failed_checks()
    assert not failures, (
        f"{case.case_id} failed checks: "
        + "; ".join(f"{c.name} (expected={c.expected!r}, actual={c.actual!r}; {c.detail})"
                    for c in failures)
    )


def test_datasets_present():
    """Guard: the suite must discover at least one dataset."""
    assert _CASES, "No regression datasets found under tests/regression/datasets/"
