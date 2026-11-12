"""Per-operation skill packages.

Each subfolder is a self-contained module for one workflow operation.
The orchestrator (:mod:`services.orchestrator`) discovers each
operation's ``SKILL.md`` spec via ``rglob`` and dispatches commands to
the registered handler in :mod:`app`. Operation-specific code (agent,
tools) lives here so that opening the folder shows everything that
makes the operation work.
"""
