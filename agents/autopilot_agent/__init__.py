"""Autopilot agent package — Copilot-style NL-driven fix feature.

Public surface re-exported for convenience. The turn-by-turn Chainlit
state machine lives in ``app.py``; this package holds the UI-agnostic
engine helpers.
"""

from __future__ import annotations

from .autopilot_agent import (  # noqa: F401
    LOW_CONFIDENCE_THRESHOLD,
    MAX_ITERATIONS,
    REPEAT_LIMIT,
    compose_guided_diff_details,
    detect_ims_query_request,
    extract_baseline,
    gather_ims_artifacts,
    interpret_request,
    is_exit_command,
    load_hint,
    save_hint,
    should_pause_for_help,
)

__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "MAX_ITERATIONS",
    "REPEAT_LIMIT",
    "compose_guided_diff_details",
    "detect_ims_query_request",
    "extract_baseline",
    "gather_ims_artifacts",
    "interpret_request",
    "is_exit_command",
    "load_hint",
    "save_hint",
    "should_pause_for_help",
]
