---
id: branch
command: branch
aliases: []
description: "Create Azure DevOps Git branch and pull to local."
handler: branch
entrypoint: ":run"
session_step: step_3a_branch
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "branch <baseline>"
intent_patterns:
  - "branch <baseline>"
  - "create branch for <baseline>"
  - "step 3a for <baseline>"
---

| | |
|---|---|
| **name** | branch |
| **description** | Creates a feature branch in Azure DevOps for the baseline, pulls it to the local working copy, and (if a matching ticket exists) links the branch to the ticket and activates it. This is **Step 3a** of the validation workflow. |

# Branch Skill — Step 3a

## When to use
Call this tool when the user asks to create the Azure DevOps Git branch
for a baseline, or "do step 3a". Examples:
- `branch P02070-HPPL486P`
- "create branch for that baseline"
- "step 3a for HPPL486P"

## What it does
1. Creates a feature branch in Azure DevOps for the baseline.
2. Pulls the branch to the local working copy.
3. Finds a matching ticket by baseline name (best-effort).
4. Links the branch to the ticket and sets the ticket to Active.
5. Marks `step_3a_branch` complete in the session.

## Arguments
| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Implementation

This skill is fully self-contained inside this folder. External
callers (`app.py`, the orchestrator, `handle_branch`) must NOT import
any class, module, or tool from this folder. They invoke the skill
through a single, generic entry point:

```python
ok, result_dict = await orchestrator.run_skill("branch", session)
```

The orchestrator, in turn, does **not** call `run()` directly.
Instead it sends THIS SKILL.md (the markdown you are reading) to the
LLM together with a tool schema exposing the entrypoint declared
above (`run`). The LLM reads this file, understands what the skill
does, and emits a `tool_call("run")`. The orchestrator then invokes
the picked function. So the LLM is the one that decides "call `run`";
the orchestrator only forwards the runtime arguments (e.g. `session`).

If the LLM call fails for any reason, the orchestrator automatically
falls back to invoking the declared entrypoint directly so the
operation never silently stalls. The flag
`agentic.llm_skill_invocation` in
`prompts/skills/orchestration/_config.yaml` controls this (default
`true`).

Inside this folder nothing changes whichever path is taken: `run()`
in `__init__.py` delegates to a single merged module that performs
the Azure DevOps + git work and updates session state.

### Internal layout (single-file implementation)

```
prompts/skills/branch/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface: async run(session)
└── scripts/
    └── branch_baseline.py         ← merged implementation:
                                     • Azure DevOps API + git plumbing
                                       (via the project-wide
                                       ``tools/devops_tools`` module,
                                       kept as shared library since
                                       ``finalize`` (Step 8) also uses it)
                                     • run(session) orchestration
                                     • Result-dict shape for the UI
```

### Call chain

```
orchestrator.run_skill("branch", session)
  └→ prompts/skills/branch/__init__.py :: run(session)
        └→ prompts/skills/branch/scripts/branch_baseline.py :: run(session)
              └→ create_and_pull_branch()   (Azure DevOps + git — Step 3a,
                                             wrapped in asyncio.to_thread so
                                             the async event loop stays
                                             responsive)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, Dict[str, Any]]`
* **Return dict keys:** `baseline`, `branch_name`, `ticket_id`,
  `branch_url`, `ticket_url`, `message`
* **Side effects:** mutates `session.step_3a_branch` status,
  creates/links a branch + ticket in Azure DevOps, checks the branch
  out to the local repo.
