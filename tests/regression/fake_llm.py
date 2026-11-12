"""Deterministic fake LLM that mirrors ``services.llm_service.LLMService``.

Returns canned responses from the dataset instead of calling the real EnsoAI
gateway, so regression runs are deterministic, offline and free. The public
surface (``chat`` / ``chat_sync``) matches the real service, so it can be
dropped in via monkeypatch wherever the app uses ``LLMService``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


class FakeLLMService:
    """Drop-in stand-in for the real LLM gateway client.

    The analysis phase expects a structured verdict (category + root cause).
    That verdict is supplied per-case via ``llm_responses['analyze']`` and
    returned as a JSON string, mirroring how the real model returns text that
    the analysis agent parses.
    """

    def __init__(self, llm_responses: Optional[Dict[str, Any]] = None):
        self._responses = llm_responses or {}
        self.model = "fake-regression-model"
        # Record of every call, for assertions on prompt wiring.
        self.calls: List[Dict[str, Any]] = []

    # -- behaviour ---------------------------------------------------------
    def _resolve(self, system_prompt: str, user_prompt: str) -> str:
        analyze = self._responses.get("analyze")
        if analyze is not None:
            return json.dumps(analyze)
        # Fallback: echo marker so callers never receive None.
        return self._responses.get("default", "NO_CANNED_RESPONSE")

    # -- interface parity with LLMService ----------------------------------
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> str:
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, "model": model}
        )
        return self._resolve(system_prompt, user_prompt)

    def chat_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> str:
        self.calls.append(
            {"system": system_prompt, "user": user_prompt, "model": model}
        )
        return self._resolve(system_prompt, user_prompt)

    def set_model(self, model: str) -> None:
        self.model = model

    @property
    def analyze_verdict(self) -> Dict[str, Any]:
        """The structured analysis verdict this fake will return."""
        return dict(self._responses.get("analyze", {}))
