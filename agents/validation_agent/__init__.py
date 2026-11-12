"""Validation root agent package.

This package contains the top-level orchestrator that routes user
requests to the per-operation skill folders under
``prompts/skills/<op>/``.

Historically this code lived at ``services/orchestrator.py``. It was
relocated here so the "main agent" of the system lives alongside the
other agents (``agents/chat_agent.py``, ``agents/script_runner_agent.py``
…). The public API (``get_orchestrator``, ``load_reporting_skill``,
``Orchestrator``) is unchanged — external callers just import from
the new module path:

    from agents.validation_agent.validation_agent import get_orchestrator

Nothing else changed: same singleton, same dispatch behaviour, same
SKILL.md loading, same LLM router/worker split. This file only
re-exports the public surface for convenience.
"""

from __future__ import annotations

from .validation_agent import (  # noqa: F401  (re-export)
    Operation,
    Orchestrator,
    get_orchestrator,
    load_reporting_skill,
    load_validate_skill,
    logger,
)

__all__ = [
    "Operation",
    "Orchestrator",
    "get_orchestrator",
    "load_reporting_skill",
    "load_validate_skill",
    "logger",
]
