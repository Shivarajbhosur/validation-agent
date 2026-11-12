---
id: unload
command: unload
aliases: []
description: "Unload baseline data from the database."
handler: unload
entrypoint: ":run"
session_step: step_5_unload
args:
  - name: baseline
    required: true
    validation: '^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$'
    usage: "unload <baseline>"
intent_patterns:
  - "unload <baseline>"
  - "unload data for <baseline>"
  - "step 5 for <baseline>"
---

| | |
|---|---|
| **name** | unload |
| **description** | Runs the `unload-BaselineData` PowerShell cmdlet for the baseline, marking **Step 5** of the validation workflow complete. |

# Unload Skill — Step 5

## When to use
Call this tool when the user asks to unload baseline data from the
database, or "do step 5". Examples:
- `unload P02070-HPPL486P`
- "unload data for that baseline"
- "step 5 for HPPL486P"

## What it does
1. Invokes the `unload-BaselineData` PowerShell cmdlet for the baseline.
2. Marks `step_5_unload` complete in the session.

## Arguments
| Name | Required | Pattern | Example |
|------|----------|---------|---------|
| baseline | yes | `^P\d{4,6}-[A-Z]{1,6}\d{2,8}[A-Z]?$` | `P02070-HPPL486P` |

## Implementation

This skill is fully self-contained inside this folder. External
callers (`app.py`, the orchestrator, `script_runner_agent.py`,
`reporting_agent.py`) must NOT import any class, module, or tool
from this folder. They invoke the skill through a single, generic
entry point:

```python
ok, msg = await orchestrator.run_skill("unload", session)
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
the PowerShell call and updates session state.

### Internal layout (single-file implementation)

```
prompts/skills/unload/
├── SKILL.md                       ← this file (the contract)
├── __init__.py                    ← public surface: async run(session)
└── scripts/
    ├── _powershell.yaml           ← local cmdlet catalog (self-contained)
    └── unload_baseline.py         ← merged implementation:
                                     • PowerShell cmdlet catalog reader
                                     • run(session) orchestration
                                     • UI response strings
```

### Call chain

```
orchestrator.run_skill("unload", session)
  └→ prompts/skills/unload/__init__.py :: run(session)
        └→ prompts/skills/unload/scripts/unload_baseline.py :: run(session)
              └→ unload_baseline_data()   (PowerShell — Step 5)
```

### Contract

* **Entrypoint:** `:run` (function `run` in the package's `__init__.py`)
* **Signature:** `async def run(session: Session) -> Tuple[bool, str]`
* **Side effects:** mutates `session.step_5_unload` status, invokes the
  `unload-BaselineData` PowerShell cmdlet.
