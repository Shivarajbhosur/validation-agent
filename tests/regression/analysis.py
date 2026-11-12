"""The 'Analyze Exceptions' step, driven by the fake LLM.

Mirrors how the real analysis agent obtains a verdict from the model: it calls
the LLM client and parses a structured result (exception category + root
cause). Here the client is :class:`FakeLLMService`, so the verdict is the
deterministic canned response from the dataset. This isolates and validates the
parsing / response-handling logic independent of the live gateway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .fake_llm import FakeLLMService


@dataclass
class AnalysisVerdict:
    exception_category: Optional[str]
    root_cause: Optional[str]
    confidence: Optional[float]
    raw: str


def analyze_exceptions(llm: FakeLLMService, error_text: str) -> AnalysisVerdict:
    """Call the (fake) LLM and parse its structured verdict."""

    raw = llm.chat_sync(
        system_prompt="You are a COBOL-to-C# validation analysis agent.",
        user_prompt=f"Classify and explain this validation error:\n{error_text}",
    )

    category = root_cause = None
    confidence = None
    try:
        parsed = json.loads(raw)
        category = parsed.get("exception_category")
        root_cause = parsed.get("root_cause")
        confidence = parsed.get("confidence")
    except (json.JSONDecodeError, AttributeError):
        # Non-JSON model output is itself a regression signal.
        root_cause = raw

    return AnalysisVerdict(
        exception_category=category,
        root_cause=root_cause,
        confidence=confidence,
        raw=raw,
    )
