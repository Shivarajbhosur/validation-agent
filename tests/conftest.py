"""Shared fixtures and reporting hooks for the Validation Agent test suite.

Ensures the project root is importable, redirects the Session store to a
temporary directory so tests never touch real session data, collects
regression results, and writes the aggregate HTML report at session end.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable (config, models, services, ...).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPORT_DIR = PROJECT_ROOT / "tests" / "regression" / "report"
REPORT_PATH = REPORT_DIR / "regression_report.html"


@pytest.fixture
def temp_sessions_path(tmp_path, monkeypatch):
    """Redirect config.SESSIONS_PATH to a temp dir for the duration of a test."""
    import config

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "SESSIONS_PATH", sessions_dir, raising=False)
    return sessions_dir


@pytest.fixture
def make_session(temp_sessions_path):
    """Factory for isolated Session objects backed by the temp store."""
    from models.session import Session

    def _make(baseline_name: str) -> Session:
        return Session(baseline_name)

    return _make


# --------------------------------------------------------------------------
# Aggregate HTML report collection
# --------------------------------------------------------------------------
def pytest_configure(config):
    config._regression_results = []


def record_regression_result(config, result) -> None:
    """Called by regression tests to register a RegressionResult."""
    getattr(config, "_regression_results", []).append(result)


@pytest.fixture
def regression_recorder(request):
    def _record(result):
        record_regression_result(request.config, result)

    return _record


def pytest_sessionfinish(session, exitstatus):
    results = getattr(session.config, "_regression_results", [])
    if not results:
        return
    from tests.regression.html_report import render_report

    path = render_report(results, REPORT_PATH)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.write_sep("-", f"Regression HTML report: {path}")
