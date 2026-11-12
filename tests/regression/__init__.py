"""Regression testing framework for the Validation Agent.

A self-contained, additive suite that validates the agentic validation
workflow without touching the application's runtime code. External systems
(the LLM gateway, PowerShell/.NET steps, Azure DevOps) are mocked, and RAG
retrieval runs against an isolated, offline Chroma store so the suite is
deterministic and runnable on any OS.
"""
